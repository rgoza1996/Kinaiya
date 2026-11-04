import 'dart:async';

import 'package:crypto/crypto.dart' as crypto;
import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';
import 'package:uuid/uuid.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../audio/speech_audio.dart';
import '../audio/turn_audio.dart';
import '../character/pack.dart';
import '../character/playback.dart';
import '../presentation/controller.dart';
import '../state/app_state.dart';
import '../state/converse_handoff.dart';
import '../state/native_audio_clip.dart';
import 'book_sheet.dart';
import 'converse_companions.dart';
import 'converse_memory.dart';
import 'converse_design.dart';
import 'converse_interaction.dart';
import 'moonlit_home.dart';
import 'model_setup.dart';
import 'motion_transition.dart';
import 'voice_options.dart';
import 'settings_controls.dart';
import 'moonlit_scene.dart';
import 'anchored_moonlit_scene.dart';
import 'design_tokens.dart';
import 'intelligent_converse_stage.dart';
import 'visual_theme.dart';
import 'widgets.dart';

/// Local adapter activity, independent of Agent work and inference readiness.
class VoiceActivity {
  const VoiceActivity({
    this.listening = false,
    this.recording = false,
    this.speaking = false,
    this.stopUnconfirmed = false,
    this.stopPending = false,
    this.stopMicrophone,
    this.stopSpeech,
  });
  final bool listening, recording, speaking, stopUnconfirmed, stopPending;
  final VoidCallback? stopMicrophone, stopSpeech;
  bool get active => listening || recording || speaking || stopUnconfirmed;
}

/// Distinct Character View: ordinary conversation only. Slash-like text is
/// sent verbatim and never parsed, completed or dispatched.
///
/// Audio rules enforced here:
/// - Queued (synthesized) audio is never reported as played; `played` is
///   acked only after true local `onended` via [SpeechAudio.awaitPlayback].
/// - Speech entries are serialized ([SpeechQueue]) so independently
///   produced sentences cannot interleave.
/// - Every async gap re-checks the turn nonce plus server generation epoch;
///   stale callbacks never reset newer speaking/busy state.
/// - Barge-in stops local audio first, then awaits the Converse speech
///   interrupt. Agent work is never touched.
/// - The mic is never enabled automatically: one-shot recording and the
///   Live (armed) mode both start only from explicit user gestures.
enum CharacterSurface { legacy, home, interaction }

class CharacterView extends StatefulWidget {
  const CharacterView({
    super.key,
    required this.state,
    this.audio,
    this.controls,
    this.onActivityChanged,
    this.character,
    this.showCharacter = true,
    this.showCaptions = true,
    this.useDesignScene = false,
    this.voiceOnlyControls = false,
    this.surface = CharacterSurface.legacy,
    this.onModelSettings,
    this.onConnectionSetup,
    this.onOpenConverse,
    this.onOpenModelOptions,
    this.onAddModel,
    this.showConverseView = true,
    this.setupOpen = false,
    this.animateTransitions = true,
    this.speechMouthMotion = true,
    this.richArtifactsEnabled = true,
    this.onRichArtifactsEnabled,
    this.scenePrimary = kinaiyaIndigo,
    this.sceneBackground = kinaiyaWhite,
  });
  final AppState state;
  final SpeechAudio? audio;
  final ConverseViewControls? controls;
  final ValueChanged<VoiceActivity>? onActivityChanged;
  final CharacterSelection? character;
  final bool showCharacter, showCaptions;
  final bool useDesignScene;
  final bool voiceOnlyControls;
  final CharacterSurface surface;
  final VoidCallback? onModelSettings;
  final VoidCallback? onConnectionSetup;
  final VoidCallback? onOpenConverse, onOpenModelOptions, onAddModel;
  final bool showConverseView, setupOpen, animateTransitions, speechMouthMotion;
  final bool richArtifactsEnabled;
  final ValueChanged<bool>? onRichArtifactsEnabled;
  final Color scenePrimary, sceneBackground;

  @override
  State<CharacterView> createState() => _CharacterViewState();
}

const List<int> endSilenceOptionsMs = <int>[800, 1500, 2500];

enum _NativeAdmissionOutcome { notDispatched, accepted, unknown }

class _NativeAdmissionIdentity {
  _NativeAdmissionIdentity(NativeAudioClipOwner owner)
    : client = owner.client,
      connectionGeneration = owner.connectionGeneration,
      sourceGeneration = owner.sourceGeneration,
      sessionId = owner.sessionId,
      threadId = owner.threadId,
      epoch = owner.epoch,
      utteranceId = owner.utteranceId,
      requestId = owner.requestId,
      turnNonce = owner.turnNonce,
      contentScope = owner.contentScope;

  final Object client;
  final int connectionGeneration, sourceGeneration, epoch, turnNonce;
  final String sessionId, threadId, utteranceId, requestId;
  final ContentScope contentScope;
}

/// Settlement of one original upload, independent of permission to play its
/// reply. Spoken interruption must not erase an attempted upload's identity.
class _NativeAdmissionResult {
  const _NativeAdmissionResult({
    required this.outcome,
    required this.callOwner,
    required this.audioSha256,
    required this.endMs,
    this.identity,
    this.receipt,
  });

  final _NativeAdmissionOutcome outcome;
  final int? callOwner;
  final String audioSha256;
  final int endMs;
  final _NativeAdmissionIdentity? identity;
  final ({String eventId, int sequence, int revision})? receipt;
}

class _CharacterViewState extends State<CharacterView> {
  final _modelMenu = MenuController();
  ConverseViewControls? _registeredControls;
  Future<void> Function()? _registeredVoiceOptions;
  Widget? Function()? _registeredVoicePanel;
  Future<void> Function()? _registeredTextConversation;
  bool _textConversationOpen = false;
  Route<void>? _textConversationRoute;

  void _dismissTextConversation() {
    final route = _textConversationRoute;
    _textConversationRoute = null;
    if (route == null) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (route.isActive) route.navigator?.removeRoute(route);
    });
  }

  bool _toolsOwnerCurrent(AppState? owner) =>
      mounted &&
      (owner == null ||
          (identical(widget.state, owner) &&
              _textConversationRoute?.isActive == true));

  VoidCallback? _toolsAction(AppState? owner, VoidCallback? action) =>
      action == null
      ? null
      : () {
          if (_toolsOwnerCurrent(owner)) action();
        };

  void _detachSettingsControls() {
    final controls = _registeredControls;
    if (controls != null &&
        identical(controls.openVoiceOptions, _registeredVoiceOptions)) {
      controls.openVoiceOptions = null;
    }
    if (controls != null &&
        identical(controls.openTextConversation, _registeredTextConversation)) {
      controls.openTextConversation = null;
    }
    if (controls != null &&
        identical(controls.buildVoiceOptions, _registeredVoicePanel)) {
      controls.buildVoiceOptions = null;
    }
    _registeredVoicePanel = null;
    _registeredControls = null;
    _registeredVoiceOptions = null;
    _registeredTextConversation = null;
  }

  void _bindSettingsControls() {
    _detachSettingsControls();
    final controls = widget.controls;
    if (controls == null) return;
    final owner = widget.state;
    late final Future<void> Function() open;
    open = () async {
      if (!mounted ||
          !identical(widget.state, owner) ||
          !identical(widget.controls, controls) ||
          !identical(controls.openVoiceOptions, open)) {
        return;
      }
      await _openVoiceOptions();
    };
    _registeredControls = controls;
    _registeredVoiceOptions = open;
    controls.openVoiceOptions = open;
    late final Widget? Function() panel;
    panel = () {
      if (!mounted ||
          !identical(widget.state, owner) ||
          !identical(widget.controls, controls) ||
          !identical(controls.buildVoiceOptions, panel)) {
        return null;
      }
      return _voiceOptionsPanel(
        embedded: true,
        owner: owner,
        controls: controls,
      );
    };
    _registeredVoicePanel = panel;
    controls.buildVoiceOptions = panel;
    late final Future<void> Function() chat;
    chat = () async {
      if (!mounted ||
          !identical(widget.state, owner) ||
          !identical(widget.controls, controls) ||
          !identical(controls.openTextConversation, chat)) {
        return;
      }
      await _openTextConversation();
    };
    _registeredTextConversation = chat;
    controls.openTextConversation = chat;
  }

  Future<void> _openTextConversation() async {
    if (_textConversationOpen) return;
    _textConversationOpen = true;
    final owner = widget.state;
    try {
      await showDialog<void>(
        context: context,
        builder: (dialogContext) {
          _textConversationRoute = ModalRoute.of<void>(dialogContext);
          return Dialog(
            insetPadding: const EdgeInsets.all(12),
            child: SizedBox(
              width: 640,
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Padding(
                    padding: const EdgeInsets.fromLTRB(16, 4, 4, 4),
                    child: Row(
                      children: [
                        Expanded(
                          child: Text(
                            'Converse chat',
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: Theme.of(
                              dialogContext,
                            ).textTheme.titleMedium,
                          ),
                        ),
                        IconButton(
                          tooltip: 'Close Converse chat',
                          onPressed: () => Navigator.of(dialogContext).pop(),
                          icon: const Icon(Icons.close),
                        ),
                      ],
                    ),
                  ),
                  Flexible(
                    child: SingleChildScrollView(
                      child: ListenableBuilder(
                        listenable: owner,
                        builder: (_, _) =>
                            mounted && identical(widget.state, owner)
                            ? Column(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  if (_s.converseHistory.isNotEmpty)
                                    SizedBox(
                                      height: 260,
                                      child: Column(
                                        children: [
                                          _historyBar(),
                                          Expanded(child: _transcript()),
                                        ],
                                      ),
                                    )
                                  else
                                    const SizedBox.shrink(),
                                  _conversationTools(expectedOwner: owner),
                                ],
                              )
                            : const SizedBox.shrink(),
                      ),
                    ),
                  ),
                ],
              ),
            ),
          );
        },
      );
    } finally {
      _textConversationRoute = null;
      _textConversationOpen = false;
    }
  }

  late final TextEditingController _input;
  late final SpeechAudio _audio;
  Future<CharacterPack>? _pack;
  KinaiyaClient? _lastClient;
  String _mood = 'idle';
  bool _recording = false;
  bool _speaking = false;
  bool _voiceReady = false;
  bool _micReady = false;
  bool _voiceReportScheduled = false;
  bool _micStopUnconfirmed = false;
  bool _micStopPending = false;
  bool _windowSpeechStopUnconfirmed = false;
  final _mouthFrame = ValueNotifier<SpeechMouthFrame>(SpeechMouthFrame.closed);
  Timer? _mouthTimer;
  int _mouthGeneration = 0;
  bool _mouthReadPending = false;
  bool _mouthVisible = false;

  void _setMouthFrame(SpeechMouthFrame frame) {
    // Preferences and connection ownership can change during a parent build.
    // Only the portrait subtree listens; defer notifications in that phase.
    if (SchedulerBinding.instance.schedulerPhase ==
        SchedulerPhase.persistentCallbacks) {
      final generation = _mouthGeneration;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted && generation == _mouthGeneration) {
          _mouthFrame.value = frame;
        }
      });
    } else {
      _mouthFrame.value = frame;
    }
  }

  bool get _canAnimateMouth =>
      _speaking &&
      _mouthVisible &&
      widget.speechMouthMotion &&
      widget.showCharacter &&
      widget.character?.packageJson == null &&
      widget.surface == CharacterSurface.interaction &&
      _audio is SpeechMouthAudio;

  void _syncMouthSampling() {
    if (!_canAnimateMouth) {
      _mouthTimer?.cancel();
      _mouthTimer = null;
      _mouthGeneration++;
      if (_mouthFrame.value.active || _mouthFrame.value.openness != 0) {
        _setMouthFrame(SpeechMouthFrame.closed);
      }
      return;
    }
    _mouthTimer ??= Timer.periodic(
      const Duration(milliseconds: 33),
      (_) => unawaited(_sampleMouth()),
    );
  }

  Future<void> _sampleMouth() async {
    if (!_canAnimateMouth || _mouthReadPending) return;
    final generation = _mouthGeneration;
    final nonce = _s.turnNonce;
    final speechEpoch = _s.speechEpoch;
    _mouthReadPending = true;
    // Future.timeout does not cancel the capability read. Keep its slot until
    // the original future settles so a stalled adapter cannot accumulate reads.
    final read = Future<SpeechMouthFrame>.sync(
      () => (_audio as SpeechMouthAudio).mouthFrame(),
    ).whenComplete(() => _mouthReadPending = false);
    try {
      final frame = await read.timeout(
        const Duration(milliseconds: 100),
        onTimeout: () => SpeechMouthFrame.closed,
      );
      if (!mounted ||
          generation != _mouthGeneration ||
          nonce != _s.turnNonce ||
          speechEpoch != _s.speechEpoch ||
          !_canAnimateMouth) {
        return;
      }
      final current = _mouthFrame.value;
      if (current.openness != frame.openness ||
          current.viseme != frame.viseme ||
          current.active != frame.active) {
        _setMouthFrame(frame);
      }
    } catch (_) {
      if (mounted && generation == _mouthGeneration && _canAnimateMouth) {
        _setMouthFrame(SpeechMouthFrame.closed);
      }
    }
  }

  Future<void> _stopLocalPlayback() {
    _mouthGeneration++;
    _mouthTimer?.cancel();
    _mouthTimer = null;
    _setMouthFrame(SpeechMouthFrame.closed);
    return _audio.stopPlayback();
  }

  void _publishVoiceActivity() {
    _syncMouthSampling();
    if (_voiceReportScheduled || widget.onActivityChanged == null) return;
    _voiceReportScheduled = true;
    // Reconnect can update this widget during its parent's build. Activity is
    // presentation only; publish after the frame without delaying local audio.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _voiceReportScheduled = false;
      if (!mounted) return;
      widget.onActivityChanged?.call(
        VoiceActivity(
          listening: _armed,
          recording: _recording,
          speaking: _speaking,
          stopUnconfirmed: _micStopUnconfirmed,
          stopPending: _micStopPending,
          stopMicrophone: () {
            if (mounted) unawaited(_stopMicrophone());
          },
          stopSpeech: () {
            if (mounted) unawaited(_stopSpeech());
          },
        ),
      );
    });
  }

  /// Serializes speech entries: independently produced sentences from one
  /// turn play strictly in order and never interleave.
  SpeechQueue _speechQueue = SpeechQueue();
  StreamSubscription<JournalEntry>? _noticeSubscription;
  StreamSubscription<JournalEntry>? _windowSubscription;
  Future<void> _windowEvents = Future.value();
  Map<String, Object?>? _pendingWindow;
  bool _windowTranscribing = false;
  int _windowPolls = 0;
  String _windowUtterance = '';
  int _windowIndex = 0;
  final Set<String> _ownedUtterances = {};
  final Set<String> _startedUtterances = {};
  final Set<String> _windowPlayed = {};
  String _liveStatus = 'Listening (live)';
  bool _windowFailureShown = false;
  StreamSubscription<int>? _resourceSubscription;
  Future<void> _interruptBarrier = Future.value();
  bool _pollingBridge = false;
  int _captureEpoch = 0;
  int _armOwner = 0;
  int _voiceRequest = 0;
  NativeAudioClipOwner? _nativeClipOwner;
  int? _nativeClipArmOwner;
  bool _nativeCallActive = false;
  SessionInfo? _nativeCallSession;
  KinaiyaClient? _nativeCallClient;
  int _nativeCallConnection = 0, _nativeCallSource = 0;
  int? _nativeBargedCapture;
  ({List<int> wav, ContentScope scope, int owner})? _pendingNativeClip;
  Future<_NativeAdmissionResult>? _nativeAdmission;
  _NativeAdmissionResult? _nativeAdmissionResult;
  Future<void>? _nativeCaptureStop;
  Future<void>? _callStart;
  bool _nativeDraining = false;
  bool _nativeCallBlocked = false;
  bool _transcribing = false;
  RecordedAudio? _pendingClip;
  ContentScope _pendingClipScope = ContentScope.localOnly;
  ContentScope _captureScope = ContentScope.localOnly;
  ContentScope _recordingScope = ContentScope.localOnly;

  /// User-armed continuous listening. Never true unless the user toggled it.
  bool _armed = false;
  VadSettings _vadSettings = const VadSettings();
  int get _endSilenceMs => _vadSettings.endSilenceMs;
  bool _vadBusy = false;
  String? _vadNotice;
  int _vadChange = 0;
  final _voiceOptionsUpdates = ValueNotifier<int>(0);
  Future<void>? _audioOutputOperation;
  List<AudioOutputDevice>? _audioOutputs = const [];
  String? _audioOutputNotice;
  Timer? _audioOutputTimer;
  Future<void>? _audioInputOperation;
  List<AudioInputDevice>? _audioInputs = const [];
  String? _audioInputNotice;

  NativePcmCaptureAudio? get _nativeCaptureAudio =>
      _audio is NativePcmCaptureAudio &&
          (_audio as NativePcmCaptureAudio).supportsNativePcmCapture
      ? _audio as NativePcmCaptureAudio
      : null;

  AudioInputSelection? get _inputAudio =>
      _audio is AudioInputSelection ? _audio as AudioInputSelection : null;

  Future<void> _changeAudioInput({String? id, bool useDefault = false}) {
    final adapter = _inputAudio;
    if (adapter == null || !mounted || _audioInputOperation != null) {
      return Future.value();
    }
    if (_armed || _recording || _nativeCallActive || _micStopUnconfirmed) {
      _audioInputNotice = 'End microphone capture before changing the input.';
      _voiceOptionsUpdates.value++;
      return Future.value();
    }
    late final Future<void> operation;
    operation = () async {
      await Future<void>.value();
      try {
        final status = useDefault
            ? await adapter.useDefaultAudioInput()
            : id != null
            ? await adapter.selectAudioInput(id)
            : null;
        if (!mounted) return;
        if (status != null) {
          _audioInputNotice = switch (status) {
            'selected' =>
              'Audio input selected for the next Call or recording in this session.',
            'default' =>
              'The next recording will use the browser default input.',
            'busy' =>
              'End microphone capture or the pending input change first.',
            _ =>
              'Audio input selection is unavailable. Refresh inputs and choose again.',
          };
          if (status != 'selected' && status != 'default') return;
        }
        final devices = await adapter.audioInputs();
        if (!mounted) return;
        _audioInputs = devices;
        _audioInputNotice ??=
            'Available audio inputs refreshed. No microphone was activated.';
      } on AudioInputDiscoveryUnavailable catch (error) {
        if (mounted) {
          _audioInputs = null;
          _audioInputNotice = switch (error.reason) {
            AudioInputDiscoveryReason.identifiersUnavailable =>
              'Browser has not provided selectable microphone identifiers. Check this site’s microphone permission, then refresh inputs. No microphone was activated by this refresh.',
          };
        }
      } catch (_) {
        if (mounted) {
          _audioInputs = null;
          _audioInputNotice =
              'Audio input selection is unconfirmed. Refresh inputs and choose again.';
        }
      } finally {
        if (identical(_audioInputOperation, operation)) {
          _audioInputOperation = null;
          if (mounted) _voiceOptionsUpdates.value++;
        }
      }
    }();
    _audioInputOperation = operation;
    _voiceOptionsUpdates.value++;
    return operation;
  }

  AudioOutputSelection? get _outputAudio =>
      _audio is AudioOutputSelection ? _audio as AudioOutputSelection : null;

  Future<void> _changeAudioOutput({
    String? id,
    bool choose = false,
    bool useDefault = false,
  }) {
    final adapter = _outputAudio;
    if (adapter == null || _audioOutputOperation != null || !mounted) {
      return Future.value();
    }
    // Dispatch the genuine browser picker directly in the owner's gesture.
    // No capability query or await may consume activation first.
    Future<String>? selection;
    try {
      if (choose) {
        selection = adapter.chooseAudioOutput();
      } else if (useDefault) {
        selection = adapter.useDefaultAudioOutput();
      } else if (id != null) {
        selection = adapter.selectAudioOutput(id);
      }
    } catch (_) {
      selection = Future.value('unavailable');
    }
    // Attach the error observer before yielding: an immediately rejected
    // adapter future must not escape into the UI's uncaught-error handler.
    final original = selection?.then<String>(
      (status) => status,
      onError: (Object error, StackTrace stack) => 'unavailable',
    );
    late final Future<void> operation;
    operation = () async {
      // This first yield lets the owner retain the operation before settlement.
      await Future<void>.value();
      try {
        final status = original == null ? null : await original;
        if (!mounted) return;
        if (status != null) {
          _audioOutputNotice = switch (status) {
            'selected' => 'Audio output selected for this session.',
            'default' => 'This session uses the system default audio output.',
            'cancelled' => 'Audio output selection cancelled.',
            'busy' => 'Finish playback or the pending output change first.',
            'stopped' => 'Audio output selection was not confirmed.',
            _ =>
              'Audio output selection is unavailable. Refresh devices or choose an output.',
          };
          if (status != 'selected' && status != 'default') {
            if (status != 'busy' && status != 'cancelled') {
              _audioOutputs = const [];
            }
            return;
          }
          if (status == 'selected' || status == 'default') {
            _audioOutputTimer?.cancel();
            _audioOutputTimer = status == 'selected'
                ? Timer.periodic(const Duration(seconds: 1), (_) {
                    if (mounted && _audioOutputOperation == null) {
                      unawaited(_changeAudioOutput());
                    }
                  })
                : null;
          }
        }
        // Keep the same slot through discovery. An observer never times out
        // an original browser future and starts another operation over it.
        final devices = await adapter.audioOutputs();
        if (!mounted) return;
        _audioOutputs = devices;
        if (status == null && _audioOutputNotice == null) {
          _audioOutputNotice = 'Available audio outputs refreshed.';
        }
      } catch (_) {
        if (mounted) {
          _audioOutputs = null;
          _audioOutputNotice =
              'Audio output is unconfirmed. Refresh devices or choose an output.';
          _audioOutputTimer?.cancel();
          _audioOutputTimer = null;
        }
      } finally {
        if (identical(_audioOutputOperation, operation)) {
          _audioOutputOperation = null;
          if (mounted) _voiceOptionsUpdates.value++;
        }
      }
    }();
    _audioOutputOperation = operation;
    _voiceOptionsUpdates.value++;
    return operation;
  }

  AdaptiveVadAudio? get _adaptiveAudio =>
      _audio is AdaptiveVadAudio &&
          (_audio as AdaptiveVadAudio).supportsVadSettings
      ? _audio as AdaptiveVadAudio
      : null;

  Future<VadSettings> _configureVad(VadSettings settings) async {
    final advanced = _adaptiveAudio;
    if (advanced != null) return advanced.configureVadSettings(settings);
    await _audio.configureVad(endSilenceMs: settings.endSilenceMs);
    return settings;
  }

  Future<void> _changeVad({
    VadSettings? settings,
    bool recalibrate = false,
  }) async {
    if (_vadBusy || (settings != null && !settings.valid)) return;
    final owner = widget.state, request = ++_vadChange;
    bool current() =>
        mounted && request == _vadChange && identical(widget.state, owner);
    setState(() {
      _vadBusy = true;
      _vadNotice = null;
    });
    _voiceOptionsUpdates.value++;
    try {
      if (recalibrate) {
        final advanced = _adaptiveAudio;
        if (advanced == null) throw StateError('Unavailable');
        await advanced.recalibrateVad().timeout(const Duration(seconds: 2));
        if (current()) {
          _vadNotice = _armed
              ? 'Calibration queued. Remain quiet for about one second.'
              : 'Calibration will start when you next turn Call on.';
        }
      } else if (settings != null) {
        final applied = await _configureVad(
          settings,
        ).timeout(const Duration(seconds: 2));
        if (current()) {
          _vadSettings = applied;
          _vadNotice = 'Microphone settings applied for this session.';
        }
      }
    } catch (_) {
      if (current()) {
        _vadNotice =
            'Microphone settings could not be confirmed. Apply a choice again.';
      }
    } finally {
      if (current()) {
        setState(() => _vadBusy = false);
        _voiceOptionsUpdates.value++;
      }
    }
  }

  Widget _voiceOptionsPanel({
    required bool embedded,
    required AppState owner,
    required ConverseViewControls? controls,
  }) => ValueListenableBuilder<int>(
    valueListenable: _voiceOptionsUpdates,
    builder: (_, version, child) {
      bool current() =>
          mounted &&
          identical(widget.state, owner) &&
          identical(widget.controls, controls) &&
          (!embedded ||
              identical(controls?.buildVoiceOptions, _registeredVoicePanel));
      return VoiceOptionsSheet(
        embedded: embedded,
        endSilenceMs: _endSilenceMs,
        silenceChoices: endSilenceOptionsMs,
        windowMode: _windowMode,
        nativeAudio: _s.nativeAudio.configured,
        vadSettings: _adaptiveAudio == null ? null : _vadSettings,
        busy: _vadBusy,
        notice: _vadNotice,
        audioInputs: _inputAudio == null ? null : _audioInputs,
        audioInputBusy:
            _audioInputOperation != null ||
            _armed ||
            _recording ||
            _nativeCallActive,
        audioInputNotice: _audioInputNotice,
        onAudioInputSelected: _inputAudio == null
            ? null
            : (id) {
                if (current()) unawaited(_changeAudioInput(id: id));
              },
        onDefaultAudioInput: _inputAudio == null
            ? null
            : () {
                if (current()) unawaited(_changeAudioInput(useDefault: true));
              },
        onRefreshAudioInputs: _inputAudio == null
            ? null
            : () {
                if (current()) unawaited(_changeAudioInput());
              },
        audioOutputs: _outputAudio == null ? null : _audioOutputs,
        audioOutputBusy: _audioOutputOperation != null,
        audioOutputNotice: _audioOutputNotice,
        onChooseAudioOutput: _outputAudio == null
            ? null
            : () {
                if (current()) unawaited(_changeAudioOutput(choose: true));
              },
        onDefaultAudioOutput: _outputAudio == null
            ? null
            : () {
                if (current()) unawaited(_changeAudioOutput(useDefault: true));
              },
        onRefreshAudioOutputs: _outputAudio == null
            ? null
            : () {
                if (current()) unawaited(_changeAudioOutput());
              },
        onAudioOutputSelected: _outputAudio == null
            ? null
            : (id) {
                if (current()) unawaited(_changeAudioOutput(id: id));
              },
        onVadChanged: (settings) {
          if (current()) unawaited(_changeVad(settings: settings));
        },
        onRecalibrate: () {
          if (current()) unawaited(_changeVad(recalibrate: true));
        },
        onSilenceChanged: (value) {
          if (current()) {
            unawaited(
              _changeVad(settings: _vadSettings.copyWith(endSilenceMs: value)),
            );
          }
        },
      );
    },
  );
  Timer? _pollTimer;
  bool _voiceBusy = false;
  bool _clipOpen = false;

  @override
  void initState() {
    super.initState();
    _bindSettingsControls();
    _input = TextEditingController(text: widget.state.converseDraft);
    _audio = widget.audio ?? speechAudio();
    _lastClient = _s.client;
    _pack = widget.character == null
        ? loadCharacterPack()
        : Future.value(widget.character!.pack);
    _refreshVoice();
    _noticeSubscription = _s.outcomeNotices.listen((entry) {
      final api = _s.client;
      if (!mounted ||
          api == null ||
          entry.sessionId != _s.converseSession?.sessionId) {
        return;
      }
      final epoch = (entry.payload['generation_epoch'] as num?)?.toInt() ?? 0;
      if (_s.speechEpoch == 0) _s.speechEpoch = epoch;
      if (epoch == _s.speechEpoch) {
        _enqueueSpeak(api, entry, _s.turnNonce, <String>{});
      }
    });
    _windowSubscription = _s.windowEvents.listen((entry) {
      final owner = _armOwner;
      final generation = _s.connectionGeneration;
      _windowEvents = _windowEvents
          .then((_) async {
            if (owner == _armOwner && generation == _s.connectionGeneration) {
              await _onWindowEvent(entry);
            }
          })
          .catchError((Object _) {});
    });
    _resourceSubscription = _s.resourceStops.listen((_) {
      if (!mounted) return;
      unawaited(_stopMicrophone(stopSpeech: true));
    });
  }

  @override
  void didUpdateWidget(covariant CharacterView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.controls, widget.controls) ||
        !identical(oldWidget.state, widget.state)) {
      _vadChange++;
      _vadBusy = false;
      _vadNotice = null;
      _dismissTextConversation();
      _bindSettingsControls();
    }
    if (!identical(oldWidget.character, widget.character)) {
      // Presentation changes never invalidate audio/session/task ownership.
      _pack = widget.character == null
          ? loadCharacterPack()
          : Future.value(widget.character!.pack);
    }
    if (_lastClient != _s.client) {
      _lastClient = _s.client;
      final owner = _s;
      final client = owner.client;
      // A chat dialog is in a separate route. Updating its controller during
      // this pane's build would notify that route while the tree is building.
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!mounted ||
            !identical(widget.state, owner) ||
            !identical(owner.client, client)) {
          return;
        }
        if (_input.text != owner.converseDraft) {
          _input.text = owner.converseDraft;
        }
      });
      _s.turnNonce++;
      _s.voiceNonce++;
      _s.speechEpoch = 0;
      _s.busyConverse = false;
      _armOwner++;
      _voiceRequest++;
      _nativeClipOwner = null;
      _nativeClipArmOwner = null;
      _nativeCallActive = false;
      _nativeCallSession = null;
      _pendingNativeClip = null;
      _pendingClip = null;
      _clearWindows();
      _armed = false;
      _micStopPending = false;
      _recording = false;
      _speaking = false;
      _clipOpen = false;
      _pollTimer?.cancel();
      _speechQueue = SpeechQueue();
      _publishVoiceActivity();
      unawaited(_audio.stopArmed());
      unawaited(_audio.cancelRecording());
      unawaited(_stopLocalPlayback());
      unawaited(_refreshVoice());
    }
    final nativeOwner = _nativeClipOwner;
    if (nativeOwner != null && !_nativeClipCurrent(nativeOwner)) {
      _nativeClipOwner = null;
      unawaited(_stopLocalPlayback());
    }
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) _syncMouthSampling();
    });
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _mouthVisible =
        TickerMode.valuesOf(context).enabled &&
        !MediaQuery.of(context).disableAnimations;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) _syncMouthSampling();
    });
  }

  @override
  void dispose() {
    _dismissTextConversation();
    _detachSettingsControls();
    _pollTimer?.cancel();
    _audioOutputTimer?.cancel();
    _mouthTimer?.cancel();
    _mouthGeneration++;
    _noticeSubscription?.cancel();
    _windowSubscription?.cancel();
    _resourceSubscription?.cancel();
    _armOwner++;
    _voiceRequest++;
    _nativeClipOwner = null;
    _nativeClipArmOwner = null;
    _nativeCallActive = false;
    _pendingNativeClip = null;
    // Invalidate every in-flight callback: stale completions must not
    // reset newer speaking/busy state or leak the mic.
    _s.turnNonce++;
    _s.voiceNonce++;
    unawaited(_audio.stopArmed());
    unawaited(_audio.cancelRecording());
    unawaited(_audio.stopPlayback());
    unawaited(_audio.dispose());
    _mouthFrame.dispose();
    _voiceOptionsUpdates.dispose();
    _input.dispose();
    super.dispose();
  }

  Future<void> _refreshVoice() async {
    final mic = await _audio.availableMic;
    final play = await _audio.availablePlayback;
    if (!mounted) return;
    // Capture buttons use the actual microphone capability so they never
    // look usable where only playback (or nothing) exists. Import and
    // text paths keep the combined signal and are unaffected.
    setState(() {
      _micReady = mic;
      _voiceReady = mic || play;
    });
    await _s.refreshConverseAvailability();
  }

  AppState get _s => widget.state;
  WindowSpeechAudio? get _windowAudio =>
      _audio is WindowSpeechAudio ? _audio as WindowSpeechAudio : null;
  bool get _windowMode =>
      !_nativeCallActive && _s.streamingWindows && _windowAudio != null;

  bool _nativeCallCurrent(int owner) {
    final api = _nativeCallClient, session = _nativeCallSession;
    return mounted &&
        _nativeCallActive &&
        owner == _armOwner &&
        api != null &&
        session != null &&
        _s.ownsConnection(_nativeCallConnection, api) &&
        _nativeCallSource == _s.nativeAudioSourceGeneration &&
        _s.nativeAudio.configured &&
        _s.nativeAudio.available &&
        _converseAvailable &&
        _s.converseCanDisclose(_captureScope) &&
        _s.converseSession?.sessionId == session.sessionId &&
        _s.converseSession?.threadId == session.threadId &&
        _s.converseSession?.epoch == session.epoch;
  }

  bool _nativeClipCurrent(NativeAudioClipOwner owner) =>
      mounted &&
      identical(_nativeClipOwner, owner) &&
      (_nativeClipArmOwner == null ||
          _nativeCallCurrent(_nativeClipArmOwner!)) &&
      _s.nativeAudio.configured &&
      _s.nativeAudio.available &&
      _converseAvailable &&
      _s.converseCanInfer &&
      _s.converseCanDisclose(owner.contentScope) &&
      owner.current(
        currentClient: _s.client,
        connection: _s.connectionGeneration,
        source: _s.nativeAudioSourceGeneration,
        session: _s.converseSession,
        nonce: _s.turnNonce,
      );

  bool _nativeOutputCurrent(JournalEntry entry, NativeAudioClipOwner? owner) =>
      owner != null && _nativeClipCurrent(owner) && owner.ownsReply(entry);

  void _showNativeNotice(NativeAudioClipOwner owner, NativeAudioNotice notice) {
    if (!_nativeClipCurrent(owner) || owner.hasReply) return;
    if (notice.terminal) {
      // This ends only this local reply wait, never backend work or Agent work.
      setState(() {
        _nativeClipOwner = null;
        if (!_speaking) _mood = 'idle';
      });
      _s.busyConverse = false;
    }
    _s.appendHistory(
      AppView.character,
      ChatMsg(
        role: 'system',
        text: notice.message,
        note: 'Native audio status',
      ),
    );
  }

  void _clearWindows() {
    _pendingWindow = null;
    _ownedUtterances.clear();
    _startedUtterances.clear();
    _windowPlayed.clear();
    _windowUtterance = '';
    _liveStatus = 'Listening (live)';
    _windowFailureShown = false;
  }

  Future<void> _nextWindowUtterance() async {
    _windowUtterance = const Uuid().v4();
    _windowIndex = 0;
    _ownedUtterances.add(_windowUtterance);
    while (_ownedUtterances.length > 16) {
      final old = _ownedUtterances.first;
      _ownedUtterances.remove(old);
      _startedUtterances.remove(old);
    }
    _captureEpoch = await _windowAudio!.nextUtterance();
  }

  Future<void> _onWindowEvent(JournalEntry entry) async {
    final nativeOwner = _nativeClipOwner;
    if (nativeOwner != null &&
        _nativeClipCurrent(nativeOwner) &&
        nativeOwner.statusCandidate(entry)) {
      if (!nativeOwner.admitted) {
        nativeOwner.deferStatus(entry);
        return;
      }
      final notice = nativeOwner.observeStatus(entry);
      if (notice != null) _showNativeNotice(nativeOwner, notice);
      return;
    }
    var nativeOutput =
        nativeOwner != null &&
        _nativeClipCurrent(nativeOwner) &&
        nativeOwner.candidate(entry);
    if (nativeOutput && !nativeOwner.admitted) {
      try {
        nativeOwner.defer(entry);
      } on FormatException {
        _nativeClipOwner = null;
        await _stopLocalPlayback();
      }
      return;
    }
    nativeOutput = nativeOutput && nativeOwner.bindReply(entry);
    if (nativeOutput && _windowPlayed.contains(entry.eventId)) return;
    final liveWindow = _armed && _windowMode;
    if (!mounted ||
        (!nativeOutput && !liveWindow) ||
        entry.sessionId != _s.converseSession?.sessionId ||
        entry.threadId != _s.converseSession?.threadId ||
        entry.epoch != _s.converseSession?.epoch) {
      return;
    }
    if (entry.kind == 'routing.window_status' ||
        entry.kind == 'converse.window_state') {
      final phase = entry.payload['phase'];
      final label = phase == 'error' || phase == 'busy'
          ? 'Response unavailable. Listening continues.'
          : phase == 'preparing'
          ? 'Preparing a reply'
          : 'Listening (live)';
      if (_liveStatus != label) setState(() => _liveStatus = label);
    }
    final epoch =
        ((entry.kind == 'delivery.updated'
                    ? entry.payload['next_generation_epoch']
                    : entry.payload['generation_epoch'])
                as num?)
            ?.toInt();
    // A failed import or service restart can leave the optimistic local
    // interruption counter ahead of the service. Only the exact admitted,
    // current native source may adopt its authenticated reply's epoch in
    // either direction; ordinary window events retain the monotonic gate.
    if (epoch != null &&
        (epoch > _s.speechEpoch || (nativeOutput && epoch != _s.speechEpoch))) {
      _s.turnNonce++;
      if (nativeOutput) nativeOwner.turnNonce = _s.turnNonce;
      _s.speechEpoch = epoch;
      _speechQueue = SpeechQueue();
      await _stopLocalPlayback();
      if (!mounted ||
          (nativeOutput ? !_nativeClipCurrent(nativeOwner) : !_armed)) {
        return;
      }
      setState(() => _speaking = false);
      _publishVoiceActivity();
    }
    final utterance = entry.payload['window_utterance_id'];
    if (entry.kind == 'conversation.converse' &&
        (nativeOutput || _ownedUtterances.contains(utterance)) &&
        epoch == _s.speechEpoch &&
        _windowPlayed.add(entry.eventId)) {
      while (_windowPlayed.length > 128) {
        _windowPlayed.remove(_windowPlayed.first);
      }
      final api = _s.client;
      if (api != null) _enqueueSpeak(api, entry, _s.turnNonce, <String>{});
    }
  }

  bool get _converseAvailable =>
      _s.sessionAuthenticated &&
      _s.converseInferenceAllowed &&
      _s.converseEnabled &&
      !_s.converseControlPending &&
      !_s.converseControlUncertain &&
      !_s.resourceStatusStale;

  /// Sends a turn. Interruptible mid-SSE: when a turn is already in flight,
  /// the new utterance invalidates the old nonce first, so the old loop
  /// breaks on its next event and its `finally` (nonce-owned) cannot clear
  /// the new turn's `busyConverse`.
  Future<void> _send({
    String? overrideText,
    String inputMode = 'text',
    ContentScope? contentScope,
  }) async {
    final text = (overrideText ?? _input.text).trim();
    final scope =
        contentScope ??
        (overrideText == null
            ? _s.draftScope(AppView.character)
            : ContentScope.localOnly);
    final api = _s.client;
    final generation = _s.connectionGeneration;
    if (text.isEmpty || api == null || !_converseAvailable) {
      if (text.isNotEmpty && api != null && mounted) {
        _s.appendHistory(
          AppView.character,
          ChatMsg(
            role: 'system',
            text:
                'Kinaiya is not ready to chat right now. Direct Agent Chat remains available.',
          ),
        );
      }
      return;
    }
    if (!_s.converseCanInfer) return;
    if (!_s.converseCanDisclose(scope)) {
      _s.appendHistory(
        AppView.character,
        ChatMsg(
          role: 'system',
          text: inputMode == 'voice'
              ? 'This recording retains its original disclosure scope and cannot be sent to this provider. Start a new recording under the chosen disclosure setting.'
              : 'This input retains its original disclosure scope and cannot be sent to this provider. Clear and re-enter text to choose a new scope.',
        ),
      );
      return;
    }
    _nativeClipOwner = null;
    final nonce = ++_s.turnNonce;
    bool stillAuthorized() {
      if (!mounted || !turnOwnsCleanup(nonce, _s.turnNonce)) return false;
      if (!_s.ownsConnection(generation, api) ||
          !_converseAvailable ||
          !_s.converseCanDisclose(scope)) {
        _s.busyConverse = false;
        _s.touch();
        return false;
      }
      return true;
    }

    _s.busyConverse = true;
    _speechQueue = SpeechQueue();
    await _stopLocalPlayback();
    await _interruptBarrier;
    if (!stillAuthorized()) return;
    try {
      final seedSession =
          _s.converseSession ??
          await _s.ensureSession('converse', name: 'Kinaiya');
      final historyKey = '${seedSession.sessionId}:${seedSession.epoch}';
      if (_s.converseHistorySeededFor != historyKey) {
        try {
          final seed = await api.recent(sessionId: seedSession.sessionId);
          for (final e in seed) {
            if (!turnOwnsCleanup(nonce, _s.turnNonce)) break;
            if (e.kind == 'conversation.user') {
              _s.appendHistory(
                AppView.character,
                ChatMsg(
                  role: 'user',
                  text: e.text,
                  eventId: e.eventId,
                  windowRevision: (e.payload['revision'] as num?)?.toInt() ?? 1,
                ),
              );
              _s.acceptedConverseSource(
                seedSession,
                e.eventId,
                _s.converseHistory.length - 1,
              );
            } else if (e.kind == 'conversation.converse') {
              _s.appendHistory(
                AppView.character,
                ChatMsg(role: 'kinaiya', text: e.text, eventId: e.eventId),
              );
            }
            if (e.sequence > _s.converseCursor) _s.converseCursor = e.sequence;
          }
          if (turnOwnsCleanup(nonce, _s.turnNonce)) {
            _s.converseHistorySeededFor = historyKey;
          }
          _s.touch();
        } catch (_) {}
      }
    } catch (_) {}
    if (!stillAuthorized()) return;
    final userIndex = _s.converseHistory.length;
    _s.appendHistory(AppView.character, ChatMsg(role: 'user', text: text));
    if (overrideText == null) {
      _input.clear();
      _s.setDraft(AppView.character, '');
    }
    if (mounted && turnOwnsCleanup(nonce, _s.turnNonce)) {
      setState(() {
        _mood = 'thinking';
        _speaking = false;
      });
      _publishVoiceActivity();
    }
    try {
      final session =
          _s.converseSession ??
          await _s.ensureSession('converse', name: 'Kinaiya');
      if (!stillAuthorized()) return;
      final key = newIdempotencyKey();
      var assistantIndex = -1;
      final playedAcked = <String>{};
      await for (final event in api.streamTurn(
        sessionId: session.sessionId,
        threadId: session.threadId,
        epoch: session.epoch,
        text: text,
        idempotencyKey: key,
        inputMode: inputMode,
        contentScope: scope,
      )) {
        if (!mounted) return;
        // Stale turn (interrupted by a newer send): stop consuming.
        if (!turnOwnsCleanup(nonce, _s.turnNonce)) break;
        switch (event.type) {
          case 'accepted':
            _s.acceptedConverseSource(
              session,
              event.raw['source_event_id'],
              userIndex,
            );
            unawaited(_s.refreshNotices());
            _s.speechEpoch =
                (event.raw['generation_epoch'] as num?)?.toInt() ?? 0;
          case 'text':
            final entry = event.entry;
            if (entry == null) break;
            if (entry.sequence > _s.converseCursor) {
              _s.converseCursor = entry.sequence;
            }
            if (assistantIndex < 0) {
              _s.appendHistory(
                AppView.character,
                ChatMsg(
                  role: 'kinaiya',
                  text: entry.text,
                  eventId: entry.eventId,
                ),
              );
              assistantIndex = _s.converseHistory.length - 1;
            } else {
              final cur = _s.converseHistory[assistantIndex];
              _s.converseHistory[assistantIndex] = ChatMsg(
                role: 'kinaiya',
                text: '${cur.text.trimRight()} ${entry.text.trimLeft()}',
                eventId: entry.eventId,
              );
              _s.touch();
            }
            final replay = event.raw['replay'] == true;
            if (!replay && entry.text.isNotEmpty) {
              _enqueueSpeak(api, entry, nonce, playedAcked);
            }
          case 'interrupted':
            if (turnOwnsCleanup(nonce, _s.turnNonce)) {
              _s.appendHistory(
                AppView.character,
                ChatMsg(role: 'system', text: 'Stopped.'),
              );
            }
          case 'error':
            if (turnOwnsCleanup(nonce, _s.turnNonce)) {
              _s.appendHistory(
                AppView.character,
                ChatMsg(
                  role: 'system',
                  text: 'Kinaiya could not reply. Please try again.',
                ),
              );
            }
        }
      }
    } on ApiException catch (e) {
      if (!mounted) return;
      if (!turnOwnsCleanup(nonce, _s.turnNonce)) return;
      if (e.status == 409) {
        _s.converseEnabled = false;
        _s.appendHistory(
          AppView.character,
          ChatMsg(
            role: 'system',
            text: 'Converse is off. Direct Agent Chat is available.',
          ),
        );
      } else {
        _s.appendHistory(
          AppView.character,
          ChatMsg(
            role: 'system',
            text: "Couldn't reach Kinaiya. Check the connection.",
          ),
        );
      }
    } catch (_) {
      if (!mounted) return;
      if (!turnOwnsCleanup(nonce, _s.turnNonce)) return;
      _s.appendHistory(
        AppView.character,
        ChatMsg(
          role: 'system',
          text: "Couldn't reach Kinaiya. Check the connection.",
        ),
      );
    } finally {
      // Cleanup belongs to this nonce only: a stale turn (interrupted by
      // barge-in or a newer send) must not clear the new turn's busy flag
      // or reset its speaking indicator.
      if (turnOwnsCleanup(nonce, _s.turnNonce)) {
        _s.busyConverse = false;
        if (mounted) {
          setState(() {
            _mood = _speaking ? 'speaking' : 'idle';
          });
        }
        _s.touch();
      }
    }
  }

  /// Enqueues one stable entry for serialized playback. Entries from one
  /// turn play strictly in order and never interleave.
  void _enqueueSpeak(
    KinaiyaClient api,
    JournalEntry entry,
    int nonce,
    Set<String> playedAcked,
  ) {
    _speechQueue.enqueue(() => _playOneEntry(api, entry, nonce, playedAcked));
  }

  /// Plays a single entry to true local `onended`, then ack `played`.
  /// Synthesis-complete alone is never reported as played; an interrupt
  /// reports an estimated elapsed fraction (or unknown), never exact words.
  Future<void> _playOneEntry(
    KinaiyaClient api,
    JournalEntry entry,
    int nonce,
    Set<String> playedAcked,
  ) async {
    final nativeOutput = entry.payload['native_audio'] == true;
    final nativeOwner = _nativeClipOwner;
    bool nativeCurrent() =>
        !nativeOutput || _nativeOutputCurrent(entry, nativeOwner);
    if (!nativeCurrent()) return;
    if (!_s.converseCanInfer || !_converseAvailable) return;
    if (!turnOwnsCleanup(nonce, _s.turnNonce)) return;
    if (!await _audio.availablePlayback) return;
    if (!nativeCurrent()) return;
    if (!_s.converseCanInfer || !_converseAvailable) return;
    if (!turnOwnsCleanup(nonce, _s.turnNonce)) return;
    final epoch =
        (entry.payload['generation_epoch'] as num?)?.toInt() ?? _s.speechEpoch;
    if (!_s.audioIsLive(nonce, epoch)) return;
    final windowUtterance = entry.payload['window_utterance_id'] as String?;
    if (windowUtterance != null &&
        !nativeOutput &&
        (!_armed || !_ownedUtterances.contains(windowUtterance))) {
      return;
    }
    final chunkId = entry.payload['chunk_id'] as String? ?? entry.eventId;
    DateTime? startedAt;
    var scheduled = 0.0;
    var started = false;
    try {
      await for (final frame in api.speech(
        eventId: entry.eventId,
        generationEpoch: epoch,
      )) {
        if (!nativeCurrent()) break;
        if (!turnOwnsCleanup(nonce, _s.turnNonce)) break;
        if (!_s.audioIsLive(nonce, epoch)) break;
        final audio = frame['audio'] as String?;
        if (audio == null || audio.isEmpty) continue;
        final rate = (frame['sample_rate'] as num?)?.toInt() ?? 24000;
        if (!started &&
            !nativeOutput &&
            windowUtterance != null &&
            entry.payload['window_behavior'] ==
                'RESPOND_WITHOUT_INTERRUPTING_USER' &&
            !_startedUtterances.contains(windowUtterance)) {
          while (mounted &&
              _armed &&
              _s.audioIsLive(nonce, epoch) &&
              await _windowAudio!.userSpeaking) {
            await Future<void>.delayed(const Duration(milliseconds: 40));
          }
          if (!mounted || !_armed || !_s.audioIsLive(nonce, epoch)) return;
        }
        scheduled += pcmFrameSeconds(
          base64Length: audio.length,
          sampleRate: rate,
        );
        final mouthAudio = _audio;
        final queued = mouthAudio is SpeechMouthAudio
            ? await (mouthAudio as SpeechMouthAudio).queuePcmWithVisemes(
                audio,
                rate,
                frame['visemes'],
              )
            : await _audio.queuePcm(audio, rate);
        if (!queued) return;
        if (!nativeCurrent()) break;
        if (!turnOwnsCleanup(nonce, _s.turnNonce)) break;
        if (!_s.audioIsLive(nonce, epoch)) break;
        if (!started) {
          started = true;
          if (windowUtterance != null && !nativeOutput) {
            _startedUtterances.add(windowUtterance);
            if (_windowUtterance == windowUtterance) {
              await _nextWindowUtterance();
            }
          }
          startedAt = DateTime.now();
          if (_s.audioIsLive(nonce, epoch) && await _audio.isPlaying) {
            if (mounted && _s.audioIsLive(nonce, epoch)) {
              setState(() => _speaking = true);
              _publishVoiceActivity();
            }
            try {
              await api.reportDelivery(
                eventId: entry.eventId,
                generationEpoch: epoch,
                chunkId: chunkId,
                status: 'playing',
              );
            } catch (_) {}
          }
        }
      }
      if (!started) return;
      if (!turnOwnsCleanup(nonce, _s.turnNonce) ||
          !_s.audioIsLive(nonce, epoch) ||
          !nativeCurrent()) {
        await _reportInterrupted(
          api,
          entry,
          epoch,
          chunkId,
          startedAt!,
          scheduled,
        );
        return;
      }
      // True local playback gate: resolves only on source onended.
      await _audio.awaitPlayback();
      if (!turnOwnsCleanup(nonce, _s.turnNonce) ||
          !_s.audioIsLive(nonce, epoch) ||
          !nativeCurrent()) {
        await _reportInterrupted(
          api,
          entry,
          epoch,
          chunkId,
          startedAt!,
          scheduled,
        );
        return;
      }
      playedAcked.add(entry.eventId);
      try {
        await api.reportDelivery(
          eventId: entry.eventId,
          generationEpoch: epoch,
          chunkId: chunkId,
          status: 'played',
          playedSeconds: scheduled,
        );
      } catch (_) {}
      if (nativeOutput && identical(_nativeClipOwner, nativeOwner)) {
        _nativeClipOwner = null;
      }
      if (mounted && _s.audioIsLive(nonce, epoch)) {
        setState(() => _speaking = false);
        _publishVoiceActivity();
      }
    } catch (_) {
      // Stale epochs (409) and transport cuts end quietly; only the live
      // turn may reset the speaking indicator.
      if (mounted && _s.audioIsLive(nonce, epoch)) {
        setState(() => _speaking = false);
        _publishVoiceActivity();
      }
    }
  }

  Future<void> _reportInterrupted(
    KinaiyaClient api,
    JournalEntry entry,
    int epoch,
    String chunkId,
    DateTime startedAt,
    double scheduled,
  ) async {
    final est = estimatePlayedSeconds(
      start: startedAt,
      now: DateTime.now(),
      scheduledSeconds: scheduled,
    );
    try {
      await api.reportDelivery(
        eventId: entry.eventId,
        generationEpoch: epoch,
        chunkId: chunkId,
        status: 'interrupted',
        playedSeconds: est ?? 0,
      );
    } catch (_) {}
  }

  /// User stop: invalidates speech (epoch bump keeps any text SSE alive),
  /// stops local audio immediately, then tells the Converse service to stop
  /// speech. Never interrupts Agent work.
  Future<void> _stopSpeech() async {
    await _onBarge();
    if (mounted) {
      _s.appendHistory(
        AppView.character,
        ChatMsg(
          role: 'system',
          text: 'Speech stopped. Agent work is unaffected.',
        ),
      );
    }
  }

  Future<void> _onBarge({bool nativeCapture = false}) {
    final expected = _s.speechEpoch;
    final owner = ++_s.turnNonce;
    _voiceRequest++;
    _nativeClipOwner = null;
    _nativeClipArmOwner = null;
    if (!nativeCapture) _pendingNativeClip = null;
    _pendingClip = null;
    _s.speechEpoch++;
    _s.busyConverse = false;
    _speechQueue = SpeechQueue();
    final stopped = _stopLocalPlayback();
    if (mounted) {
      setState(() {
        _speaking = false;
        _mood = _armed ? 'listening' : 'idle';
      });
      _publishVoiceActivity();
    }
    _s.touch();
    final previous = _interruptBarrier;
    final api = _s.client, session = _s.converseSession;
    _interruptBarrier = () async {
      await stopped;
      await previous;
      if (api != null && session != null) {
        try {
          await api.interruptSpeech(
            session.sessionId,
            expectedGeneration: expected > 0 ? expected : null,
          );
        } catch (_) {
          /* Local audio is stopped even when the service is unavailable. */
        }
      }
      if (mounted && owner == _s.turnNonce) _s.touch();
    }();
    return _interruptBarrier;
  }

  Future<void> _beginCapture() async {
    final owner = _armOwner;
    if (!_armed || !mounted) return;
    final status = await _audio.beginClip();
    final epoch = await _audio.captureEpoch;
    if (!mounted || !_armed || owner != _armOwner) return;
    _captureEpoch = epoch;
    _clipOpen = status == 'clip-recording' || status == 'busy';
  }

  /// Cancel capture locally without submitting the unfinished message or
  /// cancelling Agent work. Invalidate ownership before issuing adapter stops.
  Future<void> _stopMicrophone({bool stopSpeech = false}) async {
    // Clearing _armed does not acknowledge a failed playback stop. Retain
    // that obligation so a retry must stop the same window speech as well.
    final stopWindowSpeech =
        stopSpeech ||
        _nativeCallActive ||
        _nativeClipOwner != null ||
        _windowSpeechStopUnconfirmed ||
        (_armed && _windowMode);
    final nativeInterrupt = _nativeCallCurrent(_armOwner)
        ? _onBarge(nativeCapture: true)
        : null;
    _windowSpeechStopUnconfirmed = stopWindowSpeech;
    final owner = ++_armOwner;
    _voiceRequest++;
    _nativeClipOwner = null;
    _nativeClipArmOwner = null;
    _nativeCallActive = false;
    _nativeCallSession = null;
    _pendingNativeClip = null;
    _pendingClip = null;
    _s.voiceNonce++;
    _pollTimer?.cancel();
    _pollTimer = null;
    _clearWindows();
    if (stopWindowSpeech) {
      _s.turnNonce++;
      _speechQueue = SpeechQueue();
    }
    setState(() {
      _armed = _recording = _clipOpen = false;
      _voiceBusy = _transcribing = false;
      _micStopUnconfirmed = _micStopPending = true;
      if (stopWindowSpeech) _speaking = false;
      _mood = _speaking ? 'speaking' : 'idle';
    });
    _publishVoiceActivity();
    try {
      await Future.wait([
        _audio.stopArmed(),
        _audio.cancelRecording(),
        if (_nativeCaptureStop != null) _nativeCaptureStop!,
        if (nativeInterrupt != null) nativeInterrupt,
        if (stopWindowSpeech) _stopLocalPlayback(),
      ]).timeout(const Duration(seconds: 3));
      if (mounted && owner == _armOwner) {
        setState(() {
          _micStopUnconfirmed = _micStopPending = false;
          _windowSpeechStopUnconfirmed = false;
        });
        _publishVoiceActivity();
      }
    } catch (_) {
      if (mounted && owner == _armOwner) {
        setState(() {
          _micStopUnconfirmed = true;
          _micStopPending = false;
        });
        _publishVoiceActivity();
      }
    }
  }

  /// User-armed continuous listening toggle. The mic starts ONLY here.
  Future<void> _startCall() {
    if (_callStart != null) return _callStart!;
    final original = _setArmed(true);
    _callStart = original;
    unawaited(
      original.then<void>(
        (_) {
          if (identical(_callStart, original)) _callStart = null;
          if (mounted) _voiceOptionsUpdates.value++;
        },
        onError: (Object error, StackTrace stack) {
          if (identical(_callStart, original)) _callStart = null;
          if (mounted) {
            _nativeCallBlocked = true;
            if (_nativeCallActive) {
              _pauseNativeCapture(
                'Microphone activation was not confirmed. End the Call and review Voice options.',
              );
            } else {
              _nativeCallMessage(
                'Microphone activation was not confirmed. Review Voice options.',
              );
              unawaited(_stopMicrophone());
            }
          }
        },
      ),
    );
    return original;
  }

  Future<void> _setArmed(bool on) async {
    if (!on) return _stopMicrophone();
    if (_nativeCallActive) {
      if (_nativeCallBlocked) {
        _nativeCallMessage(
          'End the current Call before starting another native audio submission.',
        );
      }
      return;
    }
    if (_micStopUnconfirmed || _vadBusy) return;
    if (_nativeAdmission != null || _nativeDraining) {
      _nativeCallMessage(
        'The previous native audio admission is still pending. Wait for its original result before calling again.',
      );
      return;
    }
    if (_nativeClipOwner != null) await _onBarge();
    if (!mounted) return;
    final owner = ++_armOwner;
    if (!_converseAvailable || !_s.converseCanInfer || _s.client == null) {
      return;
    }
    final api = _s.client!;
    final connection = _s.connectionGeneration;
    final initialSession = _s.converseSession;
    if (!_s.nativeAudio.known) await _s.refreshConverseAvailability();
    if (!mounted || owner != _armOwner || !_s.ownsConnection(connection, api)) {
      return;
    }
    if (!_s.nativeAudio.known) {
      _nativeCallMessage(
        'Audio input configuration is unavailable. Refresh the connection before calling.',
      );
      return;
    }
    final native = _s.nativeAudio.configured;
    if (native &&
        initialSession != null &&
        (_s.converseSession?.sessionId != initialSession.sessionId ||
            _s.converseSession?.threadId != initialSession.threadId ||
            _s.converseSession?.epoch != initialSession.epoch)) {
      return;
    }
    if (native && (!_s.nativeAudio.available || _nativeCaptureAudio == null)) {
      _nativeCallMessage(
        'Native microphone capture is unavailable. Review the host and browser connection.',
      );
      return;
    }
    _nativeCallActive = native;
    _nativeCallBlocked = false;
    _nativeAdmissionResult = null;
    _nativeCallSession = null;
    _nativeCallClient = api;
    _nativeCallConnection = connection;
    _nativeCallSource = _s.nativeAudioSourceGeneration;
    _nativeBargedCapture = null;
    _captureScope = _s.selectedContentScope;
    try {
      if (native) {
        final session =
            initialSession ??
            await _s.ensureSession('converse', name: 'Converse');
        if (!mounted ||
            owner != _armOwner ||
            !_s.ownsConnection(connection, api)) {
          return;
        }
        _s.converseSession ??= session;
        _nativeCallSession = session;
        if (!_nativeCallCurrent(owner)) {
          _nativeCallActive = false;
          return;
        }
      }
      await _nativeCaptureAudio?.configureNativePcmCapture(native);
      await _windowAudio?.configureWindows(native ? false : _windowMode);
      if (native) {
        await _configureVad(_vadSettings);
      } else {
        await _configureVad(_vadSettings).timeout(const Duration(seconds: 2));
      }
    } catch (_) {
      if (mounted && owner == _armOwner) {
        _nativeCallActive = false;
        setState(
          () => _vadNotice =
              'Microphone configuration is unavailable. Review Voice options.',
        );
        _voiceOptionsUpdates.value++;
      }
      return;
    }
    if (!mounted ||
        owner != _armOwner ||
        !_converseAvailable ||
        !_s.ownsConnection(connection, api) ||
        (native && _nativeCallSource != _s.nativeAudioSourceGeneration)) {
      return;
    }
    final result = await _audio.startArmed();
    if (!mounted || owner != _armOwner) return;
    if (native && !_nativeCallCurrent(owner)) {
      _nativeCallActive = false;
      await _audio.stopArmed();
      return;
    }
    if (result != 'armed') {
      _nativeCallActive = false;
      _s.appendHistory(
        AppView.character,
        ChatMsg(
          role: 'system',
          text: 'Live listening is unavailable here; text works.',
        ),
      );
      return;
    }
    _voiceBusy = false;
    _s.beginVoiceTurn(_s.voiceEpoch + 1);
    setState(() {
      _armed = true;
      _mood = 'listening';
    });
    _publishVoiceActivity();
    if (_windowMode || native) {
      try {
        final api = _s.client;
        final generation = _s.connectionGeneration;
        _s.converseSession ??= await _s.ensureSession(
          'converse',
          name: 'Converse',
        );
        if (!_s.ownsConnection(generation, api)) return;
        if (!mounted || !_armed || owner != _armOwner) return;
        if (native) {
          _nativeCallSession = _s.converseSession;
          await _beginCapture();
        } else {
          await _nextWindowUtterance();
        }
      } catch (_) {
        if (mounted && owner == _armOwner) await _setArmed(false);
        return;
      }
    } else {
      await _beginCapture();
    }
    _pollTimer?.cancel();
    _pollTimer = Timer.periodic(
      const Duration(milliseconds: 100),
      (_) => _pollBridge(),
    );
  }

  Future<void> _pollBridge() async {
    if (!_armed ||
        _pollingBridge ||
        (!_nativeCallActive && !_windowMode && _voiceBusy)) {
      return;
    }
    _pollingBridge = true;
    try {
      if (_windowMode) {
        if (++_windowPolls >= 10) {
          _windowPolls = 0;
          await _captureWindow();
        }
        await _audio.pollBridgeEvents();
        return;
      }
      final events = await _audio.pollBridgeEvents();
      for (final event in events) {
        if (!_armed || !mounted) return;
        if (event['epoch'] != null && event['epoch'] != _captureEpoch) continue;
        if (_nativeCallActive) {
          if (event['type'] == 'native-clip-overflow') {
            _pauseNativeCapture(
              'Native speech exceeded 30 seconds. This utterance was not submitted; any earlier pending clip is retained.',
            );
            return;
          }
          if (event['type'] == 'native-input-unavailable' ||
              event['type'] == 'native-capture-unavailable') {
            _nativeCallBlocked = true;
            _pauseNativeCapture(
              'Native microphone capture is unavailable. The pending clip is retained; end the Call and review the selected input.',
            );
            return;
          }
          if (event['type'] == 'barge' || event['type'] == 'speech-start') {
            if (event['type'] == 'barge' ||
                _nativeBargedCapture != _captureEpoch) {
              _nativeBargedCapture = _captureEpoch;
              unawaited(_onBarge(nativeCapture: true));
            }
            if (_pendingNativeClip != null) {
              _pauseNativeCapture(
                'Native audio is busy. Microphone capture stopped; the earlier pending clip is retained and this additional utterance was not submitted.',
              );
              return;
            }
          }
        }
        if (event['type'] == 'speech-end' || event['type'] == 'clip-cap') {
          if (_nativeCallActive) {
            unawaited(_finishNativeCallClip());
          } else {
            await _finishVoiceClip();
          }
        } else if (event['type'] == 'barge') {
          if (!_nativeCallActive) unawaited(_onBarge());
        } else if (event['type'] == 'speech-start') {
          setState(() => _mood = 'listening');
        }
      }
    } catch (_) {
      /* The next bounded poll can recover. */
    } finally {
      _pollingBridge = false;
    }
  }

  Future<void> _captureWindow() async {
    final owner = _armOwner;
    final utterance = _windowUtterance;
    final snapshot = await _windowAudio!.snapshotWindow();
    final session = _s.converseSession;
    if (!mounted ||
        !_armed ||
        owner != _armOwner ||
        snapshot == null ||
        session == null ||
        utterance != _windowUtterance ||
        snapshot['capture_epoch'] != _captureEpoch) {
      return;
    }
    _pendingWindow = <String, Object?>{
      'schema_version': '1.0',
      'session_id': session.sessionId,
      'thread_id': session.threadId,
      'epoch': session.epoch,
      'utterance_id': utterance,
      'window_index': _windowIndex++,
      'revision': 1,
      'start_ms': snapshot['start_ms'],
      'end_ms': snapshot['end_ms'],
      'user_speaking': snapshot['user_speaking'],
      'capture_ended': false,
      'content_scope': _captureScope.wireValue,
      'audio_base64': snapshot['base64'],
    };
    unawaited(_drainWindows(owner));
  }

  Future<void> _drainWindows(int owner) async {
    if (_windowTranscribing) return;
    _windowTranscribing = true;
    final api = _s.client;
    final generation = _s.connectionGeneration;
    try {
      while (mounted &&
          _armed &&
          owner == _armOwner &&
          _pendingWindow != null &&
          api != null &&
          _s.converseCanInfer &&
          _s.ownsConnection(generation, api)) {
        final window = _pendingWindow!;
        _pendingWindow = null;
        try {
          await api.transcribeWindow(window);
          _windowFailureShown = false;
          if (!mounted ||
              owner != _armOwner ||
              !_s.ownsConnection(generation, api)) {
            return;
          }
          unawaited(_s.refreshNotices());
        } catch (_) {
          if (mounted && _armed && owner == _armOwner && !_windowFailureShown) {
            _windowFailureShown = true;
            _s.appendHistory(
              AppView.character,
              ChatMsg(
                role: 'system',
                text:
                    'A live transcript window could not be received. Listening continues.',
              ),
            );
          }
        }
      }
    } finally {
      _windowTranscribing = false;
      if (mounted && _armed && _pendingWindow != null) {
        unawaited(_drainWindows(_armOwner));
      }
    }
  }

  Future<void> _finishVoiceClip() async {
    if (_nativeCallActive) {
      await _finishNativeCallClip();
      return;
    }
    if (_voiceBusy || !_armed || !_clipOpen) return;
    _voiceBusy = true;
    final owner = _armOwner;
    try {
      final recording = await _audio.finishClip();
      if (!mounted || !_armed || owner != _armOwner) return;
      _clipOpen = false;
      // Capture resumes before transcription or response generation starts.
      await _beginCapture();
      if (recording == null || recording.bytes.isEmpty) return;
      _pendingClip =
          recording; // One latest pending clip, never an unbounded queue.
      _pendingClipScope = _captureScope;
      _voiceRequest++;
      unawaited(_drainVoiceClips(owner));
    } finally {
      if (owner == _armOwner) _voiceBusy = false;
    }
  }

  void _nativeCallMessage(String text) {
    if (!mounted) return;
    if (_s.converseHistory.isNotEmpty && _s.converseHistory.last.text == text) {
      return;
    }
    _s.appendHistory(AppView.character, ChatMsg(role: 'system', text: text));
  }

  void _pauseNativeCapture(String notice) {
    if (!mounted || !_nativeCallActive) return;
    final owner = _armOwner;
    _pollTimer?.cancel();
    _pollTimer = null;
    setState(() {
      _armed = false;
      _clipOpen = false;
    });
    _publishVoiceActivity();
    _nativeCallMessage(notice);
    if (_nativeCaptureStop != null) return;
    final original = Future.wait([
      _audio.stopArmed(),
      _audio.cancelRecording(),
    ]).then<void>((_) {});
    _nativeCaptureStop = original;
    unawaited(
      original.then<void>(
        (_) {
          if (identical(_nativeCaptureStop, original)) {
            _nativeCaptureStop = null;
          }
        },
        onError: (Object error, StackTrace stack) {
          if (mounted && owner == _armOwner) {
            setState(() => _micStopUnconfirmed = true);
            _publishVoiceActivity();
          }
          // The original failed stop remains retained and awaited by owner Stop.
        },
      ),
    );
  }

  Future<void> _finishNativeCallClip() async {
    final owner = _armOwner;
    if (_voiceBusy || !_armed || !_clipOpen || !_nativeCallCurrent(owner)) {
      return;
    }
    if (_pendingNativeClip != null) {
      _pauseNativeCapture(
        'Native audio is busy. Microphone capture stopped; the earlier pending clip is retained and this additional utterance was not submitted.',
      );
      return;
    }
    _voiceBusy = true;
    try {
      final recording = await _audio.finishClip();
      if (!_nativeCallCurrent(owner) || !_armed) return;
      _clipOpen = false;
      if (recording != null && recording.bytes.isNotEmpty) {
        final wav = canonicalNativeAudioWav(recording.bytes);
        _pendingNativeClip = (wav: wav, scope: _captureScope, owner: owner);
      }
      // Keep genuine capture and VAD available for spoken interruption.
      await _beginCapture();
      if (_pendingNativeClip != null) unawaited(_drainNativeClips(owner));
    } on FormatException {
      if (_nativeCallCurrent(owner)) {
        _pauseNativeCapture(
          'Native microphone audio must be complete PCM16 mono16k WAV, at most 30 seconds. Nothing from this utterance was submitted.',
        );
      }
    } catch (_) {
      if (_nativeCallCurrent(owner)) {
        _nativeCallBlocked = true;
        _pauseNativeCapture(
          'Native microphone capture could not be confirmed. The original operation is retained; end the Call before recording again.',
        );
      }
    } finally {
      if (owner == _armOwner) _voiceBusy = false;
    }
  }

  Future<void> _drainNativeClips(int owner) async {
    if (_nativeDraining) return;
    _nativeDraining = true;
    try {
      while (_nativeCallCurrent(owner) &&
          !_nativeCallBlocked &&
          _pendingNativeClip?.owner == owner) {
        // This is the same original admission and interrupt cleanup, not a
        // timed-out observer or permission to replay a request.
        final previous = _nativeAdmission;
        if (previous != null) {
          final result = await previous;
          _nativeAdmissionResult = result;
        }
        if (_nativeAdmissionResult?.outcome ==
            _NativeAdmissionOutcome.unknown) {
          if (_nativeCallCurrent(owner)) {
            _nativeCallBlocked = true;
            _pauseNativeCapture(
              'Native audio admission is unconfirmed. Microphone capture stopped; the pending clip is retained. Check the original result before another submission.',
            );
          }
          return;
        }
        await _interruptBarrier;
        if (!_nativeCallCurrent(owner) || _nativeCallBlocked) return;
        final clip = _pendingNativeClip;
        if (clip == null || clip.owner != owner) return;
        _pendingNativeClip = null;
        final original = _submitNativeRecording(
          clip.wav,
          clip.scope,
          callOwner: owner,
        );
        _nativeAdmission = original;
        late final _NativeAdmissionResult result;
        try {
          result = await original;
          _nativeAdmissionResult = result;
        } finally {
          if (identical(_nativeAdmission, original)) _nativeAdmission = null;
        }
        if (_nativeCallCurrent(owner) &&
            result.outcome == _NativeAdmissionOutcome.unknown) {
          _nativeCallBlocked = true;
          _pauseNativeCapture(
            'Native audio admission is unconfirmed. Microphone capture stopped; the pending clip is retained. Check the original result before another submission.',
          );
          return;
        }
      }
    } catch (_) {
      if (_nativeCallCurrent(owner)) {
        _nativeCallBlocked = true;
        _pauseNativeCapture(
          'Native audio could not be confirmed. Microphone capture stopped; check the original result before another submission.',
        );
      }
    } finally {
      _nativeDraining = false;
    }
  }

  Future<void> _drainVoiceClips(int owner) async {
    if (_transcribing) return;
    _transcribing = true;
    try {
      while (_armed && mounted && _pendingClip != null && owner == _armOwner) {
        final recording = _pendingClip!;
        final scope = _pendingClipScope;
        _pendingClip = null;
        final request = _voiceRequest;
        final api = _s.client;
        final generation = _s.connectionGeneration;
        if (api == null || !_s.converseCanInfer) return;
        try {
          final extension = recording.mime.contains('ogg')
              ? 'ogg'
              : recording.mime.contains('mp4')
              ? 'mp4'
              : 'webm';
          final out = await api.transcribe(
            recording.bytes,
            'recording.$extension',
            contentScope: scope,
          );
          if (!mounted ||
              !_armed ||
              !_s.ownsConnection(generation, api) ||
              request != _voiceRequest ||
              owner != _armOwner) {
            continue;
          }
          final text = (out['text'] as String? ?? '').trim();
          if (text.isNotEmpty) {
            unawaited(
              _send(
                overrideText: text,
                inputMode: 'voice',
                contentScope: _transcriptScope(scope, out),
              ),
            );
          }
        } catch (_) {
          if (mounted && _armed && owner == _armOwner) {
            _s.appendHistory(
              AppView.character,
              ChatMsg(
                role: 'system',
                text: 'Could not transcribe that recording.',
              ),
            );
          }
        }
      }
    } finally {
      _transcribing = false;
      if (_armed && mounted && _pendingClip != null) {
        unawaited(_drainVoiceClips(_armOwner));
      }
    }
  }

  Future<void> _importRecording() async {
    if (_nativeCallActive) {
      _nativeCallMessage('End the current Call before importing a recording.');
      return;
    }
    if (!_converseAvailable || !_s.converseCanInfer) return;
    final api = _s.client;
    final generation = _s.connectionGeneration;
    final scope = _s.selectedContentScope;
    if (api == null) return;
    final request = ++_voiceRequest;
    if (!_s.nativeAudio.known) {
      await _s.refreshConverseAvailability();
      if (!mounted ||
          !_s.ownsConnection(generation, api) ||
          request != _voiceRequest) {
        return;
      }
      if (!_s.nativeAudio.known) {
        _s.appendHistory(
          AppView.character,
          ChatMsg(
            role: 'system',
            text:
                'Audio input configuration is unavailable. Refresh the connection before importing.',
          ),
        );
        return;
      }
    }
    final native = _s.nativeAudio.configured;
    final nativeSource = _s.nativeAudioSourceGeneration;
    if (native && !_s.nativeAudio.available) {
      _s.appendHistory(
        AppView.character,
        ChatMsg(
          role: 'system',
          text:
              'Native audio is configured but unavailable. Review the host connection.',
        ),
      );
      return;
    }
    final recording = await _audio.chooseRecording();
    if (!mounted ||
        !_converseAvailable ||
        !_s.ownsConnection(generation, api) ||
        request != _voiceRequest ||
        recording == null) {
      return;
    }
    if (nativeSource != _s.nativeAudioSourceGeneration) {
      _s.appendHistory(
        AppView.character,
        ChatMsg(
          role: 'system',
          text:
              'Audio input configuration changed. Choose the recording again.',
        ),
      );
      return;
    }
    if (native) {
      try {
        final wav = canonicalNativeAudioWav(recording.bytes);
        await _submitNativeRecording(wav, scope);
      } on FormatException {
        if (mounted && _s.ownsConnection(generation, api)) {
          _s.appendHistory(
            AppView.character,
            ChatMsg(role: 'system', text: nativeAudioImportHint),
          );
        }
      }
      return;
    }
    await _onBarge();
    if (!mounted ||
        !_s.ownsConnection(generation, api) ||
        !_converseAvailable) {
      return;
    }
    setState(() => _mood = 'listening');
    try {
      final output = await api.transcribe(
        recording.bytes,
        recording.filename,
        contentScope: scope,
      );
      if (!mounted ||
          !_s.ownsConnection(generation, api) ||
          !_converseAvailable) {
        return;
      }
      final text = (output['text'] as String? ?? '').trim();
      if (text.isNotEmpty && mounted) {
        await _send(
          overrideText: text,
          inputMode: 'voice',
          contentScope: _transcriptScope(scope, output),
        );
      }
    } catch (_) {
      if (mounted) {
        _s.appendHistory(
          AppView.character,
          ChatMsg(role: 'system', text: 'Could not transcribe that recording.'),
        );
      }
    }
  }

  Future<_NativeAdmissionResult> _submitNativeRecording(
    List<int> wav,
    ContentScope scope, {
    int? callOwner,
  }) async {
    final api = _s.client;
    final connection = _s.connectionGeneration;
    final source = _s.nativeAudioSourceGeneration;
    final audioSha256 = crypto.sha256.convert(wav).toString();
    final endMs = ((wav.length - 44) / 32).ceil();
    NativeAudioClipOwner? owner;
    _NativeAdmissionIdentity? identity;
    ({String eventId, int sequence, int revision})? receipt;
    _NativeAdmissionResult result(_NativeAdmissionOutcome outcome) =>
        _NativeAdmissionResult(
          outcome: outcome,
          callOwner: callOwner,
          audioSha256: audioSha256,
          endMs: endMs,
          identity: identity,
          receipt: receipt,
        );
    if (api == null ||
        !_s.nativeAudio.available ||
        !_s.converseCanDisclose(scope)) {
      return result(_NativeAdmissionOutcome.notDispatched);
    }
    if (callOwner != null && !_nativeCallCurrent(callOwner)) {
      return result(_NativeAdmissionOutcome.notDispatched);
    }
    final interrupted = _onBarge(nativeCapture: callOwner != null);
    // Capture this submission's nonce before awaiting its interrupt barrier;
    // newer speech may supersede it before any POST has been dispatched.
    final nonce = _s.turnNonce;
    bool current() =>
        mounted &&
        (callOwner == null || _nativeCallCurrent(callOwner)) &&
        _s.ownsConnection(connection, api) &&
        source == _s.nativeAudioSourceGeneration &&
        _s.nativeAudio.configured &&
        _s.nativeAudio.available &&
        _converseAvailable &&
        _s.converseCanDisclose(scope) &&
        turnOwnsCleanup(nonce, _s.turnNonce);
    await interrupted;
    if (!current()) return result(_NativeAdmissionOutcome.notDispatched);
    var attempted = false;
    _s.busyConverse = true;
    setState(() => _mood = 'listening');
    _s.touch();
    try {
      final session =
          _s.converseSession ??
          await _s.ensureSession('converse', name: 'Converse');
      if (!current()) return result(_NativeAdmissionOutcome.notDispatched);
      owner = NativeAudioClipOwner(
        client: api,
        connectionGeneration: connection,
        sourceGeneration: source,
        session: session,
        utteranceId: const Uuid().v4(),
        requestId: const Uuid().v4(),
        turnNonce: nonce,
        contentScope: scope,
      );
      identity = _NativeAdmissionIdentity(owner);
      // Register before dispatch: polling may observe the response before POST returns.
      _nativeClipOwner = owner;
      _nativeClipArmOwner = callOwner;
      attempted = true;
      final admission = await api.submitNativeAudioClip(
        session: session,
        utteranceId: owner.utteranceId,
        requestId: owner.requestId,
        wav: wav,
        contentScope: scope,
      );
      if (admission.entry.payload['audio_sha256'] != audioSha256 ||
          admission.entry.payload['content_scope'] != scope.wireValue ||
          admission.entry.payload['start_ms'] != 0 ||
          admission.entry.payload['end_ms'] != endMs) {
        throw const FormatException(
          'Native audio clip admission did not match the submitted recording.',
        );
      }
      owner.confirm(admission.entry);
      receipt = (
        eventId: admission.entry.eventId,
        sequence: admission.entry.sequence,
        revision: 1,
      );
      // Even an obsolete playback owner must settle its original exact ACK.
      // Only publication/playback remains behind the current turn's fences.
      if (!_nativeClipCurrent(owner)) {
        return result(_NativeAdmissionOutcome.accepted);
      }
      if (!_s.converseHistory.any(
        (message) => message.eventId == admission.entry.eventId,
      )) {
        _s.appendHistory(
          AppView.character,
          ChatMsg(
            role: 'user',
            text: admission.entry.text,
            eventId: admission.entry.eventId,
            utteranceId: owner.utteranceId,
            windowIndex: 0,
            windowRevision: 1,
            note: 'Native audio transcript',
          ),
        );
      }
      final pending = owner.takePending();
      // A newer exact reply wins over an earlier deferred terminal observation.
      // Binding changes no playback state and remains behind exact admission.
      for (final entry in pending) {
        if (!_nativeClipCurrent(owner)) break;
        if (owner.accepts(entry)) owner.bindReply(entry);
      }
      if (_nativeClipCurrent(owner) &&
          owner.statusOverflowed &&
          owner.markNotice(NativeAudioNotice.unconfirmed)) {
        _showNativeNotice(owner, NativeAudioNotice.unconfirmed);
      }
      for (final entry in pending) {
        if (!_nativeClipCurrent(owner)) break;
        await _onWindowEvent(entry);
      }
      unawaited(_s.refreshNotices());
      return result(_NativeAdmissionOutcome.accepted);
    } catch (_) {
      if (mounted && (owner == null ? current() : _nativeClipCurrent(owner))) {
        if (owner != null && !owner.admitted) {
          // A failed/lost ACK cannot prove that the original window was absent.
          // Keep its non-playable quarantine until explicit local owner Stop.
          if (owner.markNotice(NativeAudioNotice.admissionUnknown)) {
            _showNativeNotice(owner, NativeAudioNotice.admissionUnknown);
          }
          return result(_NativeAdmissionOutcome.unknown);
        }
        _nativeClipOwner = null;
        await _stopLocalPlayback();
        if (mounted && _s.ownsConnection(connection, api)) {
          _s.appendHistory(
            AppView.character,
            ChatMsg(
              role: 'system',
              text:
                  'Native audio clip admission was not confirmed. Check conversation history before submitting again.',
            ),
          );
        }
      }
      return result(
        owner?.admitted == true
            ? _NativeAdmissionOutcome.accepted
            : attempted
            ? _NativeAdmissionOutcome.unknown
            : _NativeAdmissionOutcome.notDispatched,
      );
    } finally {
      if (mounted &&
          _s.ownsConnection(connection, api) &&
          (turnOwnsCleanup(nonce, _s.turnNonce) ||
              (owner != null && identical(_nativeClipOwner, owner)))) {
        _s.busyConverse = false;
        _s.touch();
      }
    }
  }

  Future<void> _toggleMic() async {
    final api = _s.client;
    final generation = _s.connectionGeneration;
    if (_armed || _nativeCallActive) {
      _s.appendHistory(
        AppView.character,
        ChatMsg(
          role: 'system',
          text: 'Stop Live listen before one-shot recording.',
        ),
      );
      return;
    }
    if (_recording) {
      final scope = _recordingScope;
      final request = ++_voiceRequest;
      final rec = await _audio.stopRecording();
      if (!mounted || request != _voiceRequest) return;
      // The stop itself always lands locally so capture can end even when
      // readiness changed mid-recording. Transcription and sending still
      // require a live, permitted connection below.
      setState(() => _recording = false);
      _publishVoiceActivity();
      if (api == null ||
          !_s.ownsConnection(generation, api) ||
          !_converseAvailable) {
        return;
      }
      if (rec == null || rec.bytes.isEmpty) {
        _s.appendHistory(
          AppView.character,
          ChatMsg(
            role: 'system',
            text:
                'Recording unfinished (too short or undecodable); nothing sent.',
          ),
        );
        return;
      }
      try {
        final out = await api.transcribe(
          rec.bytes,
          rec.mime.contains('ogg') ? 'recording.ogg' : 'recording.webm',
          contentScope: scope,
        );
        if (!mounted ||
            request != _voiceRequest ||
            !_s.ownsConnection(generation, api) ||
            !_converseAvailable) {
          return;
        }
        final text = (out['text'] as String? ?? '').trim();
        if (text.isNotEmpty) {
          await _send(
            overrideText: text,
            inputMode: 'voice',
            contentScope: _transcriptScope(scope, out),
          );
        } else {
          _s.appendHistory(
            AppView.character,
            ChatMsg(
              role: 'system',
              text: 'Transcription unfinished; nothing sent.',
            ),
          );
        }
      } catch (_) {
        _s.appendHistory(
          AppView.character,
          ChatMsg(role: 'system', text: 'Could not transcribe that recording.'),
        );
      }
      return;
    }
    if (api == null || !_converseAvailable || !_s.converseCanInfer) return;
    if (_nativeClipOwner != null) await _onBarge();
    if (!mounted ||
        !_s.ownsConnection(generation, api) ||
        !_converseAvailable) {
      return;
    }
    final request = ++_voiceRequest;
    _recordingScope = _s.selectedContentScope;
    final status = await _audio.startRecording();
    // Pause/disposal already cancel the adapter's pending mic generation.
    // An older completion must never cancel a newer user-owned recording.
    if (!mounted || request != _voiceRequest) return;
    if (!_converseAvailable || !_s.ownsConnection(generation, api)) {
      await _audio.cancelRecording();
      return;
    }
    if (status == 'recording') {
      setState(() {
        _recording = true;
        _mood = 'listening';
      });
      _publishVoiceActivity();
    } else {
      _s.appendHistory(
        AppView.character,
        ChatMsg(
          role: 'system',
          text: 'Voice recording is unavailable here; text works.',
        ),
      );
    }
  }

  ContentScope _transcriptScope(
    ContentScope captured,
    Map<String, Object?> output,
  ) => output.containsKey('content_scope')
      ? captured.combine(ContentScope.fromWire(output['content_scope']))
      : captured;

  void _openAgent() {
    showDialog<void>(
      context: context,
      builder: (_) => BookSheet(
        onOpenAgent: () {
          Navigator.of(context).pop();
          _s.setView(AppView.agent);
        },
      ),
    );
  }

  String get _status {
    if (_micStopPending) return 'Stopping microphone…';
    if (_micStopUnconfirmed) return 'Microphone stop unconfirmed';
    if (_s.resourceStatusStale) return 'Voice availability unconfirmed';
    if (_s.resourceConverseDesired == true && _s.resourcePhase == 'restoring') {
      return 'Restoring voice…';
    }
    if (_s.resourceConverseDesired == true &&
        _s.resourcePhase == 'initializing') {
      return 'Starting voice services…';
    }
    if (_s.resourcesConfigured == true && _s.resourcePhase == 'failed') {
      return 'Voice restoration failed';
    }
    if (!_s.converseEnabled) {
      return _s.resourcePhase == 'local_work'
          ? 'Paused for local work'
          : 'Converse is off';
    }
    if (!_s.sessionAuthenticated) return 'Connect to start a conversation';
    if (!_s.converseInferenceAllowed) return 'Review inference settings';
    if (_speaking) return 'Speaking';
    if (_recording) return 'Recording a voice message';
    if (_armed) return 'Live microphone on';
    if (_s.busyConverse) return 'Preparing a reply';
    return 'Ready to talk';
  }

  ConverseStageState get _converseStageState {
    if (_micStopPending ||
        _micStopUnconfirmed ||
        _s.resourceStatusStale ||
        (_s.resourceConverseDesired == true &&
            (_s.resourcePhase == 'restoring' ||
                _s.resourcePhase == 'initializing')) ||
        (_s.resourcesConfigured == true && _s.resourcePhase == 'failed') ||
        !_s.converseEnabled ||
        !_s.sessionAuthenticated ||
        !_s.converseInferenceAllowed) {
      return ConverseStageState.unavailable;
    }
    if (_speaking) return ConverseStageState.speaking;
    if (_s.busyConverse || _liveStatus == 'Preparing a reply') {
      return ConverseStageState.thinking;
    }
    if (_recording) return ConverseStageState.recording;
    if (_armed) return ConverseStageState.listening;
    return ConverseStageState.muted;
  }

  CharacterSignals get _characterSignals {
    final work = _s.characterWorkActivity;
    return CharacterSignals(
      speaking: _speaking,
      listening: _recording || _armed,
      warming:
          _s.sessionAuthenticated &&
          !_s.resourceStatusStale &&
          _s.resourcesConfigured == true &&
          _s.resourceConverseDesired == true &&
          const {'initializing', 'restoring'}.contains(_s.resourcePhase),
      blocked: work == 'blocked',
      working: work == 'working.generic',
      activity: _s.busyConverse && _mood == 'thinking' ? 'thinking' : work,
    );
  }

  static const _mascotSuggestions = <String>[
    'What can we explore today?',
    'Help me think something through.',
    'Tell me something interesting.',
  ];

  void _selectMascotSuggestion(String choice) {
    if (choice == 'setup') {
      (widget.onModelSettings ??
          widget.onConnectionSetup ??
          _openVoiceOptions)();
      return;
    }
    final previous = _s.converseDraft;
    final next = previous.trim().isEmpty ? choice : '$previous\n$choice';
    if (next.length > 16000) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('There is not enough room in the current draft.'),
        ),
      );
      return;
    }
    _s.setDraft(AppView.character, next);
    _input.value = TextEditingValue(
      text: next,
      selection: TextSelection.collapsed(offset: next.length),
    );
    if (widget.surface == CharacterSurface.home) {
      if (widget.onOpenConverse != null) {
        widget.onOpenConverse!();
      } else {
        unawaited(_openTextConversation());
      }
    }
  }

  Widget _mascotPrompts(Widget child) => MenuAnchor(
    key: const ValueKey('mascot-prompt-trigger'),
    style: const MenuStyle(
      alignment: AlignmentDirectional.topEnd,
      shape: WidgetStatePropertyAll(
        RoundedRectangleBorder(
          borderRadius: BorderRadius.all(Radius.circular(18)),
        ),
      ),
    ),
    alignmentOffset: const Offset(8, 0),
    menuChildren: [
      for (final suggestion in _mascotSuggestions)
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 3),
          child: MenuItemButton(
            autofocus: suggestion == _mascotSuggestions.first,
            onPressed: () => _selectMascotSuggestion(suggestion),
            style: ButtonStyle(
              shape: const WidgetStatePropertyAll(
                RoundedRectangleBorder(
                  borderRadius: BorderRadius.all(Radius.circular(16)),
                ),
              ),
              backgroundColor: WidgetStatePropertyAll(
                Theme.of(context).colorScheme.surfaceContainerHigh,
              ),
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(Icons.chat_bubble_outline_rounded, size: 18),
                const SizedBox(width: 10),
                Flexible(child: Text(suggestion)),
              ],
            ),
          ),
        ),
      const Divider(),
      MenuItemButton(
        onPressed: () => _selectMascotSuggestion('setup'),
        child: const Text('Open Kinaiya setup'),
      ),
    ],
    builder: (context, controller, _) => Tooltip(
      message: 'Show conversation suggestions',
      child: Semantics(
        label: 'Show conversation suggestions',
        button: true,
        child: InkWell(
          onTap: () =>
              controller.isOpen ? controller.close() : controller.open(),
          child: child,
        ),
      ),
    ),
  );

  Widget _mascot(CharacterPack? pack, double size) {
    if (!widget.showCharacter) {
      return Semantics(
        label: 'Conversation without character animation',
        child: Icon(
          Icons.chat_bubble_outline_rounded,
          size: size.clamp(48, 72),
          color: Theme.of(context).colorScheme.primary,
        ),
      );
    }
    final artwork = pack == null
        ? SizedBox(width: size, height: size)
        : MascotWidget(
            pack: pack,
            bundle: widget.character?.bundle,
            size: size,
            mood: _characterSignals.semantic,
            signals: _characterSignals,
          );
    if (widget.surface != CharacterSurface.legacy) {
      return _mascotPrompts(artwork);
    }
    return Semantics(
      button: true,
      label: widget.onModelSettings == null && widget.onConnectionSetup == null
          ? 'Open Agent Chat'
          : 'Open Kinaiya setup',
      child: InkWell(
        onTap: widget.onModelSettings ?? widget.onConnectionSetup ?? _openAgent,
        borderRadius: BorderRadius.circular(24),
        child: artwork,
      ),
    );
  }

  /// Readable status caption shared by the wide and narrow stages.
  Widget _statusPill() => DecoratedBox(
    decoration: BoxDecoration(
      color: kinaiyaNavy.withValues(alpha: .8),
      borderRadius: BorderRadius.circular(24),
      border: Border.all(color: kinaiyaWhite.withValues(alpha: .24)),
    ),
    child: Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      child: Text(
        _status,
        textAlign: TextAlign.center,
        style: const TextStyle(color: kinaiyaWhite),
      ),
    ),
  );

  // Captions and fallback notes can cross the moon's white pixels. An opaque
  // theme surface keeps their ink readable independently of image/crop size.
  Widget _sceneCopy(Widget child) =>
      widget.useDesignScene && widget.showCharacter
      ? Container(
          key: const ValueKey('converse-scene-copy'),
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          decoration: BoxDecoration(
            color: Theme.of(context).colorScheme.surface,
            borderRadius: BorderRadius.circular(12),
            border: Border.all(
              color: Theme.of(context).colorScheme.outlineVariant,
            ),
          ),
          child: child,
        )
      : child;

  /// Pack fallback note; drawn on the moonlit scenery when the character is
  /// shown, otherwise on the surrounding theme surface.
  Widget _packNote(CharacterPack? pack) {
    if (pack == null || !pack.fallback || pack.note.isEmpty) {
      return const SizedBox.shrink();
    }
    return Padding(
      padding: const EdgeInsets.all(8),
      child: _sceneCopy(
        Text(
          pack.note,
          style: TextStyle(
            color: widget.showCharacter && !widget.useDesignScene
                ? kinaiyaWhite
                : Theme.of(context).colorScheme.onSurface,
          ),
        ),
      ),
    );
  }

  /// Readable caption of the latest message. The inline transcript and the
  /// full-history dialog always carry the complete text; this stays short
  /// with an ellipsis so narrow and enlarged-text layouts cannot overflow.
  Widget _currentCaption() {
    if (!widget.showCaptions || _s.converseHistory.isEmpty) {
      return const SizedBox.shrink();
    }
    final latest = _s.converseHistory.last;
    final speaker = switch (latest.role) {
      'user' => 'You',
      'kinaiya' => 'Kinaiya',
      _ => 'Status',
    };
    return Padding(
      key: const ValueKey('current-caption'),
      padding: const EdgeInsets.only(top: 8),
      child: _sceneCopy(
        Text(
          '$speaker: ${latest.text}',
          key: const ValueKey('current-caption-text'),
          maxLines: 3,
          overflow: TextOverflow.ellipsis,
          textAlign: TextAlign.center,
          style: TextStyle(
            color: widget.showCharacter && !widget.useDesignScene
                ? kinaiyaWhite
                : Theme.of(context).colorScheme.onSurface,
          ),
        ),
      ),
    );
  }

  Widget _stage(CharacterPack? pack, BoxConstraints box) {
    if (widget.useDesignScene) {
      return LayoutBuilder(builder: (_, actual) => _designStage(pack, actual));
    }
    final size = (box.maxHeight - 76).clamp(100.0, 320.0);
    return Center(
      child: SingleChildScrollView(
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              _mascot(pack, size),
              const SizedBox(height: 12),
              _statusPill(),
              _currentCaption(),
              _packNote(pack),
            ],
          ),
        ),
      ),
    );
  }

  /// Narrow stage: keeps the real animated character prominent after history
  /// arrives. Height-bounded by the caller; internally scrollable so short
  /// landscape heights and 2x text cannot overflow.
  Widget _narrowStage(CharacterPack? pack) => LayoutBuilder(
    builder: (context, box) {
      if (widget.useDesignScene) return _designStage(pack, box);
      final size = (box.maxHeight - 116).clamp(96.0, 240.0);
      return Center(
        child: SingleChildScrollView(
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                _mascot(pack, size),
                const SizedBox(height: 8),
                _statusPill(),
                _currentCaption(),
                _packNote(pack),
              ],
            ),
          ),
        ),
      );
    },
  );

  /// The sprite remains the existing semantic, reduced-motion-aware owner.
  /// Only its allotted hero area changes; scenery never supplies a character.
  Widget _designStage(CharacterPack? pack, BoxConstraints box) {
    final heroScale = (box.maxWidth / 580).clamp(.72, 1.0);
    final size = (box.maxHeight - 108)
        .clamp(140.0, 440.0 * heroScale)
        .clamp(0.0, (box.maxWidth - 32).clamp(0.0, double.infinity));
    final sceneHeight = (box.maxHeight - 108).clamp(size, double.infinity);
    return Align(
      alignment: Alignment.topCenter,
      child: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            SizedBox(
              width: box.maxWidth,
              height: sceneHeight,
              child: AnchoredMoonlitScene(
                primary: widget.scenePrimary,
                background: widget.sceneBackground,
                artwork: box.maxWidth < sceneHeight
                    ? KinaiyaDesign.moonlitPhone
                    : KinaiyaDesign.moonlitDesktop,
                surfaceY: sceneHeight - size * .05,
                child: Align(
                  alignment: Alignment.bottomCenter,
                  child: SizedBox(
                    key: const ValueKey('converse-design-hero'),
                    width: size,
                    height: size,
                    child: _mascot(pack, size),
                  ),
                ),
              ),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [_statusPill(), _currentCaption(), _packNote(pack)],
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _designHome(CharacterPack? pack, BoxConstraints box, Widget banner) {
    final scale = MediaQuery.textScalerOf(context).scale(1);
    final heroHeight = (box.maxHeight - 272 * scale).clamp(
      (scale < 1.5 ? 180.0 : 320.0) * scale.clamp(1.0, 2.0),
      double.infinity,
    );
    final size = (heroHeight - 28)
        .clamp(140.0, 560.0)
        .clamp(0.0, (box.maxWidth - 32).clamp(0.0, double.infinity));
    return SingleChildScrollView(
      child: AnchoredMoonlitScene(
        artwork: box.maxWidth < box.maxHeight
            ? KinaiyaDesign.moonlitPhone
            : KinaiyaDesign.moonlitDesktop,
        surfaceY: heroHeight - size * .05,
        primary: widget.scenePrimary,
        background: widget.sceneBackground,
        child: ConstrainedBox(
          constraints: BoxConstraints(minHeight: box.maxHeight),
          child: Stack(
            children: [
              Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  SizedBox(
                    height: heroHeight,
                    width: box.maxWidth,
                    child: Align(
                      alignment: Alignment.bottomCenter,
                      child: SizedBox(
                        key: const ValueKey('converse-design-hero'),
                        width: size,
                        height: size,
                        child: _mascot(pack, size),
                      ),
                    ),
                  ),
                  Padding(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 16,
                      vertical: 8,
                    ),
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [_statusPill(), _packNote(pack)],
                    ),
                  ),
                  _composer(),
                ],
              ),
              Positioned(top: 0, left: 0, right: 0, child: banner),
            ],
          ),
        ),
      ),
    );
  }

  Widget _transcript() => ListenableBuilder(
    listenable: _s,
    builder: (context, _) {
      final attachedSources = <String>{
        for (final m in _s.converseHistory)
          if (m.role == 'user')
            if (_s.handoffForMessage(m) case final notice?)
              notice.sourceEventId,
      };
      return ListView(
        padding: const EdgeInsets.symmetric(vertical: 12),
        children: [
          for (var i = 0; i < _s.converseHistory.length; i++)
            switch (_s.converseHistory[i]) {
              final m
                  when m.role == 'system' &&
                      m.note == 'Agent handoff' &&
                      m.eventId != null &&
                      m.eventId!.startsWith('handoff:') =>
                attachedSources.contains(
                      m.eventId!.substring('handoff:'.length),
                    )
                    ? const SizedBox.shrink()
                    : switch (_s.handoffNotice(
                        m.eventId!.substring('handoff:'.length),
                      )) {
                        final notice? => ConverseHandoffStatusBanner(
                          key: ValueKey(
                            'converse-handoff-${notice.sourceEventId}',
                          ),
                          notice: notice,
                          attachedToMessage: false,
                          animateTransitions: widget.animateTransitions,
                          destinationStatus: _s.handoffTaskDestinationStatus(
                            notice,
                          ),
                          taskProgress: _s.handoffTaskProgress(notice),
                          onOpenTask: () => _s.openHandoffTask(notice),
                        ),
                        null => MessageBubble(
                          role: m.role,
                          text: m.text,
                          note: m.note,
                        ),
                      },
              final m when m.role == 'user' => Column(
                key: ValueKey(
                  'converse-user-turn-${m.eventId ?? m.utteranceId ?? i}',
                ),
                crossAxisAlignment: CrossAxisAlignment.end,
                mainAxisSize: MainAxisSize.min,
                children: [
                  MessageBubble(role: m.role, text: m.text, note: m.note),
                  if (_s.handoffForMessage(m) case final notice?)
                    ConverseHandoffStatusBanner(
                      key: ValueKey('converse-handoff-${notice.sourceEventId}'),
                      notice: notice,
                      animateTransitions: widget.animateTransitions,
                      destinationStatus: _s.handoffTaskDestinationStatus(
                        notice,
                      ),
                      taskProgress: _s.handoffTaskProgress(notice),
                      onOpenTask: () => _s.openHandoffTask(notice),
                    ),
                ],
              ),
              final m => MessageBubble(
                role: m.role,
                text: m.text,
                note: m.note,
              ),
            },
          if (_s.busyConverse && !_speaking)
            const Padding(
              padding: EdgeInsets.all(12),
              child: Center(child: CircularProgressIndicator()),
            ),
        ],
      );
    },
  );

  /// Explicit, accessible entry to the complete transcript. The inline
  /// transcript never drops earlier messages or delivery notes; this
  /// affordance keeps full history one tap away on narrow layouts and where
  /// captions are hidden.
  Widget _historyBar() => Padding(
    padding: const EdgeInsets.fromLTRB(12, 4, 12, 4),
    child: Material(
      color: Theme.of(context).colorScheme.surface,
      borderRadius: BorderRadius.circular(12),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              '${_s.converseHistory.length} message${_s.converseHistory.length == 1 ? '' : 's'}',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                fontSize: 12,
                color: Theme.of(context).colorScheme.onSurface,
              ),
            ),
            Align(
              alignment: Alignment.centerLeft,
              child: Tooltip(
                message: 'Open full conversation history',
                child: TextButton.icon(
                  key: const ValueKey('conversation-history'),
                  icon: const Icon(Icons.history_rounded),
                  label: const Text('Conversation history', maxLines: 2),
                  onPressed: _openHistory,
                ),
              ),
            ),
          ],
        ),
      ),
    ),
  );

  /// Full transcript dialog: every history entry with its delivery note, in
  /// the same order as the inline transcript. Presentation only; opening or
  /// closing it never touches capture, drafts, or sessions.
  void _openHistory() {
    final count = _s.converseHistory.length;
    showDialog<void>(
      context: context,
      builder: (dialogContext) {
        final size = MediaQuery.sizeOf(dialogContext);
        return AlertDialog(
          title: Text('Conversation history ($count)'),
          content: SizedBox(
            width: (size.width - 80).clamp(200.0, 560.0),
            height: (size.height * 0.6).clamp(200.0, 520.0),
            child: _transcript(),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(dialogContext).pop(),
              child: const Text('CLOSE'),
            ),
          ],
        );
      },
    );
  }

  /// Narrow conversation: a flexible, height-bounded animated stage stays
  /// prominent, a readable current caption sits beneath it, and the complete
  /// transcript remains available inline plus behind [_historyBar].
  Widget _narrowConversation(CharacterPack? pack, BoxConstraints box) {
    return Column(
      children: [
        Flexible(flex: 5, fit: FlexFit.tight, child: _narrowStage(pack)),
        _historyBar(),
        Expanded(flex: 6, child: _transcript()),
      ],
    );
  }

  Widget _conversation(CharacterPack? pack) => LayoutBuilder(
    builder: (context, box) {
      if (!widget.showCharacter) {
        return Column(
          children: [
            _historyBar(),
            Expanded(child: _transcript()),
          ],
        );
      }
      if (_s.converseHistory.isEmpty ||
          (!widget.showCaptions && widget.showCharacter)) {
        if (_s.converseHistory.isEmpty) return _stage(pack, box);
        // Captions hidden by choice: the stage keeps its presence while the
        // history affordance preserves access to the full text.
        return Column(
          children: [
            Expanded(child: _stage(pack, box)),
            _historyBar(),
          ],
        );
      }
      final scale = MediaQuery.textScalerOf(context).scale(1).clamp(1, 2);
      if (box.maxWidth / scale >= 1040) {
        return Row(
          children: [
            Expanded(
              child: _stage(
                pack,
                BoxConstraints.tightFor(
                  width: box.maxWidth * .55,
                  height: box.maxHeight,
                ),
              ),
            ),
            SizedBox(
              width: (box.maxWidth * .4).clamp(360.0, 560.0),
              child: Card(
                margin: const EdgeInsets.fromLTRB(0, 24, 24, 24),
                child: Column(
                  children: [
                    _historyBar(),
                    Expanded(child: _transcript()),
                  ],
                ),
              ),
            ),
          ],
        );
      }
      return _narrowConversation(pack, box);
    },
  );

  VoidCallback? get _callAction => _micStopPending
      ? null
      : _micStopUnconfirmed
      ? _stopMicrophone
      : _armed || _nativeCallActive
      ? () => _setArmed(false)
      : _callStart != null
      ? null
      : _micReady && _converseAvailable && _audioInputOperation == null
      ? _startCall
      : null;

  VoidCallback? get _tellAction => _recording
      ? _toggleMic
      : _micReady &&
            _converseAvailable &&
            !_armed &&
            !_nativeCallActive &&
            !_micStopUnconfirmed
      ? _toggleMic
      : null;

  bool get _callHasStopState =>
      _armed || _nativeCallActive || _micStopPending || _micStopUnconfirmed;

  String get _callLabel => _micStopPending
      ? 'Stopping microphone…'
      : _armed || _nativeCallActive
      ? 'End conversation'
      : _micStopUnconfirmed
      ? 'Stop microphone'
      : 'Call Kinaiya';

  Widget _voiceActions() => widget.useDesignScene
      ? ConverseVoiceActions(
          callLabel: _callLabel,
          tellLabel: _recording ? 'Send voice message' : 'Tell Kinaiya',
          callTooltip: _callHasStopState
              ? 'Stop Live listen'
              : _micReady
              ? 'Start Live listen (armed mic)'
              : 'Microphone unavailable here',
          tellTooltip: _recording
              ? 'Stop recording'
              : _micReady
              ? 'Record voice'
              : 'Microphone unavailable here',
          callIcon: _callHasStopState
              ? Icons.call_end_rounded
              : Icons.mic_none_rounded,
          tellIcon: _recording ? Icons.send_rounded : Icons.graphic_eq_rounded,
          onCall: _callAction,
          onTell: _tellAction,
        )
      : Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(
              child: Tooltip(
                message: _callHasStopState
                    ? 'Stop Live listen'
                    : _micReady
                    ? 'Start Live listen (armed mic)'
                    : 'Microphone unavailable here',
                child: FilledButton.icon(
                  key: const ValueKey('call-kinaiya'),
                  onPressed: _callAction,
                  icon: Icon(
                    _callHasStopState
                        ? Icons.call_end_rounded
                        : Icons.mic_none_rounded,
                  ),
                  label: Padding(
                    padding: const EdgeInsets.symmetric(vertical: 10),
                    child: Text(_callLabel, textAlign: TextAlign.center),
                  ),
                ),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Tooltip(
                message: _recording
                    ? 'Stop recording'
                    : _micReady
                    ? 'Record voice'
                    : 'Microphone unavailable here',
                child: OutlinedButton.icon(
                  key: const ValueKey('tell-kinaiya'),
                  onPressed: _tellAction,
                  icon: Icon(
                    _recording ? Icons.send_rounded : Icons.graphic_eq_rounded,
                  ),
                  label: Padding(
                    padding: const EdgeInsets.symmetric(vertical: 10),
                    child: Text(
                      _recording ? 'Send voice message' : 'Tell Kinaiya',
                      textAlign: TextAlign.center,
                    ),
                  ),
                ),
              ),
            ),
          ],
        );

  /// Opens the Voice options surface. Presentation only: opening, changing
  /// the silence value, or dismissing it never starts, stops, or rearms
  /// capture. Ownership of the value, handler, draft, session, and audio
  /// stays in this state object.
  Future<void> _openVoiceOptions() {
    final owner = widget.state;
    final controls = widget.controls;
    return showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      isScrollControlled: true,
      builder: (sheetContext) => Padding(
        padding: EdgeInsets.only(
          bottom: MediaQuery.viewInsetsOf(sheetContext).bottom,
        ),
        child: _voiceOptionsPanel(
          embedded: false,
          owner: owner,
          controls: controls,
        ),
      ),
    );
  }

  Widget _composer() => widget.useDesignScene || widget.voiceOnlyControls
      ? SafeArea(
          top: false,
          child: Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 520),
              child: Padding(
                padding: const EdgeInsets.fromLTRB(16, 8, 16, 20),
                child: _voiceActions(),
              ),
            ),
          ),
        )
      : _conversationTools();

  Widget _conversationTools({AppState? expectedOwner}) => SafeArea(
    top: false,
    child: Center(
      child: ConstrainedBox(
        constraints: BoxConstraints(
          maxWidth: widget.useDesignScene ? 640 : 1120,
        ),
        child: Container(
          margin: const EdgeInsets.fromLTRB(16, 0, 16, 12),
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: Theme.of(context).colorScheme.surface,
            borderRadius: BorderRadius.circular(
              widget.useDesignScene ? 16 : 24,
            ),
            border: Border.all(
              color: Theme.of(context).colorScheme.outlineVariant,
            ),
          ),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (!widget.useDesignScene && !widget.voiceOnlyControls)
                _voiceActions(),
              // Voice options keep their key, handler, and stop state; the
              // Memory entry beside them opens an owner-only read dialog and
              // never touches capture, drafts, sessions, or audio. A Wrap
              // keeps both reachable at 320px and 2x text.
              Wrap(
                spacing: 8,
                runSpacing: 4,
                crossAxisAlignment: WrapCrossAlignment.center,
                children: [
                  Tooltip(
                    message: 'Voice options (capture and conversation timing)',
                    child: TextButton.icon(
                      key: const ValueKey('voice-options'),
                      style: TextButton.styleFrom(
                        alignment: Alignment.centerLeft,
                      ),
                      icon: const Icon(Icons.tune_rounded),
                      label: const Text(
                        'Voice options',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                      ),
                      onPressed: _toolsAction(expectedOwner, _openVoiceOptions),
                    ),
                  ),
                  ConverseMemoryButton(state: expectedOwner ?? _s),
                  ConverseCompanionsButton(state: expectedOwner ?? _s),
                  if (widget.surface != CharacterSurface.legacy)
                    Tooltip(
                      message: _recording
                          ? 'Stop recording'
                          : _micReady
                          ? 'Record voice'
                          : 'Microphone unavailable here',
                      child: TextButton.icon(
                        key: const ValueKey('tell-kinaiya'),
                        onPressed: _toolsAction(expectedOwner, _tellAction),
                        icon: Icon(
                          _recording
                              ? Icons.send_rounded
                              : Icons.mic_none_rounded,
                        ),
                        label: Text(
                          _recording ? 'Send voice message' : 'Tell Kinaiya',
                        ),
                      ),
                    ),
                  if (_armed)
                    Text(
                      _windowMode ? _liveStatus : 'Listening (live)',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                    ),
                ],
              ),
              Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  TextField(
                    controller: _input,
                    key: const ValueKey('converse-input'),
                    decoration: const InputDecoration(
                      labelText: 'Talk with Kinaiya',
                    ),
                    minLines: 1,
                    maxLines: 4,
                    textInputAction: TextInputAction.send,
                    onChanged: (v) {
                      if (_toolsOwnerCurrent(expectedOwner)) {
                        _s.setDraft(AppView.character, v);
                      }
                    },
                    onSubmitted: (_) =>
                        _toolsAction(expectedOwner, () => _send())?.call(),
                  ),
                  const SizedBox(height: 8),
                  Wrap(
                    alignment: WrapAlignment.end,
                    spacing: 8,
                    runSpacing: 8,
                    crossAxisAlignment: WrapCrossAlignment.center,
                    children: [
                      if (_voiceReady && !_armed)
                        IconButton(
                          tooltip: _s.nativeAudio.configured
                              ? nativeAudioImportHint
                              : 'Use audio recording',
                          icon: const Icon(Icons.audio_file_outlined),
                          onPressed: _toolsAction(
                            expectedOwner,
                            _converseAvailable ? _importRecording : null,
                          ),
                        ),
                      if (_speaking ||
                          _s.busyConverse ||
                          _nativeClipOwner != null)
                        IconButton(
                          tooltip: 'Stop speech',
                          icon: const Icon(Icons.stop_circle_outlined),
                          onPressed: _toolsAction(expectedOwner, _stopSpeech),
                        ),
                      FilledButton(
                        onPressed: _toolsAction(
                          expectedOwner,
                          _converseAvailable &&
                                  !_s.busyConverse &&
                                  _s.converseDraft.trim().isNotEmpty
                              ? () => _send()
                              : null,
                        ),
                        child: const Text('Send'),
                      ),
                    ],
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    ),
  );

  Widget _inlineComposer() => Column(
    mainAxisSize: MainAxisSize.min,
    children: [
      TextField(
        key: const ValueKey('converse-input'),
        controller: _input,
        decoration: const InputDecoration(
          hintText: 'Message Kinaiya…',
          labelText: 'Talk with Kinaiya',
        ),
        minLines: 1,
        maxLines: 4,
        textInputAction: TextInputAction.send,
        onChanged: (value) => _s.setDraft(AppView.character, value),
        onSubmitted: (_) => _send(),
      ),
      const SizedBox(height: 8),
      Wrap(
        alignment: WrapAlignment.end,
        spacing: 8,
        runSpacing: 8,
        children: [
          IconButton(
            key: const ValueKey('converse-tell-inline'),
            tooltip: _recording ? 'Send voice message' : 'Tell Kinaiya',
            onPressed: _tellAction,
            icon: Icon(
              _recording ? Icons.send_rounded : Icons.mic_none_rounded,
            ),
          ),
          if (_voiceReady && !_armed)
            IconButton(
              tooltip: _s.nativeAudio.configured
                  ? nativeAudioImportHint
                  : 'Use audio recording',
              onPressed: _converseAvailable ? _importRecording : null,
              icon: const Icon(Icons.audio_file_outlined),
            ),
          if (_speaking || _s.busyConverse || _nativeClipOwner != null)
            IconButton(
              tooltip: 'Stop speech',
              onPressed: _stopSpeech,
              icon: const Icon(Icons.stop_circle_outlined),
            ),
          FilledButton.icon(
            key: const ValueKey('converse-send'),
            onPressed:
                _converseAvailable &&
                    !_s.busyConverse &&
                    _s.converseDraft.trim().isNotEmpty
                ? () => _send()
                : null,
            icon: const Icon(Icons.arrow_upward),
            label: const Text('Send'),
          ),
        ],
      ),
      Text(
        _s.nativeAudio.configured
            ? nativeAudioImportHint
            : 'Type or speak — your choice',
      ),
    ],
  );

  bool get _homeSleeping =>
      _s.converseSleeping &&
      !_micStopPending &&
      !_micStopUnconfirmed &&
      !_speaking;

  void _changeConverseOwner(bool enabled) {
    final owner = _s;
    unawaited(
      owner.setConverseEnabled(
        enabled,
        beforeSleep: () {
          if (mounted && identical(widget.state, owner)) {
            // The existing cancellation owner invalidates late capture and TTS
            // completions before its first await, retaining uncertain-stop state.
            unawaited(_stopMicrophone(stopSpeech: true));
          }
        },
      ),
    );
  }

  Widget _ownerSleepControl() {
    final sleeping =
        _s.converseOwnerEnabled == false && !_s.converseOwnerStatusStale;
    final pending = _s.converseControlPending;
    final label = pending
        ? (_s.converseControlTarget == true
              ? 'Waking Kinaiya…'
              : 'Putting Kinaiya to sleep…')
        : sleeping
        ? 'Wake Kinaiya'
        : 'Sleep';
    final action = _s.converseOwnerControlAvailable
        ? () => _changeConverseOwner(sleeping)
        : null;
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        if (sleeping)
          FilledButton.icon(
            key: const ValueKey('home-owner-control'),
            onPressed: action,
            icon: const Icon(Icons.wb_sunny_outlined),
            label: Text(label),
          )
        else
          OutlinedButton.icon(
            key: const ValueKey('home-owner-control'),
            onPressed: action,
            icon: const Icon(Icons.bedtime_outlined),
            label: Text(label),
          ),
        if (_s.converseControlError != null)
          Text(_s.converseControlError!, textAlign: TextAlign.center),
        if (!pending &&
            (_s.converseOwnerStatusStale || _s.converseControlUncertain))
          TextButton(
            key: const ValueKey('home-owner-refresh'),
            onPressed: _s.sessionAuthenticated && _s.client != null
                ? () => unawaited(_s.refreshConverseAvailability())
                : null,
            child: const Text('Check host status'),
          ),
      ],
    );
  }

  String get _homeSetupLabel => !_s.connected ? 'Connect / Setup' : 'Setup';

  VoidCallback get _homeSetupAction =>
      (!_s.connected ? widget.onConnectionSetup : null) ??
      widget.onModelSettings ??
      widget.onConnectionSetup ??
      _openVoiceOptions;

  String? get _homeHelperCopy {
    if (_homeSleeping) {
      return 'Wake Kinaiya below to resume voice conversation, or open Setup.';
    }
    if (_s.converseControlPending) {
      return _s.converseControlTarget == true
          ? 'Waiting for the host to confirm Converse is awake.'
          : 'Waiting for the host to confirm Converse is sleeping.';
    }
    if (!_s.connected) {
      final error = _s.connectionError?.trim();
      if (error != null && error.isNotEmpty) {
        return 'Connection failed: $error. Open Connect / Setup to check your host URLs and owner token, then retry.';
      }
      return 'Not connected to a Kinaiya host. Open Connect / Setup to enter your host URLs and owner token.';
    }
    if (!_s.sessionAuthenticated) {
      return 'Host connected, but owner session is not authenticated. Open Setup to verify your owner token.';
    }
    if (_s.converseControlUncertain || _s.converseOwnerStatusStale) {
      return 'Converse status is unconfirmed. Use Check host status below or open Setup.';
    }
    if (_s.resourceStatusStale) {
      return 'Resource status is unconfirmed; voice readiness is unknown until the host responds.';
    }
    if (_s.resourceConverseDesired == true &&
        const {'initializing', 'restoring'}.contains(_s.resourcePhase)) {
      return 'Voice services are starting on the host. Wait for readiness or open Setup.';
    }
    if (_s.resourcesConfigured == true && _s.resourcePhase == 'failed') {
      return 'Voice restoration failed on the host. Open Setup to review model and voice settings.';
    }
    if (!_s.converseEnabled) {
      return _s.resourcePhase == 'local_work'
          ? 'Voice is paused for local work. Direct Agent Chat remains available.'
          : 'Converse is off on the host. Wake Kinaiya or open Setup to configure Converse.';
    }
    if (!_s.converseOnline) {
      return 'Converse service is unreachable. Open Setup to verify the Converse service URL.';
    }
    if (!_s.converseInferenceAllowed) {
      return 'Prerequisite needed: select a qualified Converse model and confirm privacy disclosure in Setup.';
    }
    if (!_micReady) {
      return 'Microphone is unavailable here. Open Converse chat to type, or check Voice settings in Setup.';
    }
    return null;
  }

  Widget _homeSurface(CharacterPack? pack, Widget banner) => MoonlitHome(
    character: LayoutBuilder(
      builder: (_, bounds) => _mascot(pack, bounds.maxWidth),
    ),
    sleeping: _homeSleeping,
    animateCharacter: widget.animateTransitions,
    // This is a visual availability cue, not a claim about owner intent.
    offlineResting:
        !_s.converseOnline &&
        !_micStopPending &&
        !_micStopUnconfirmed &&
        !_speaking,
    ownerControl: _ownerSleepControl(),
    helperText: _homeHelperCopy,
    status: _homeSleeping
        ? 'Offline · Sleeping'
        : _s.converseControlPending
        ? (_s.converseControlTarget == true
              ? 'Waking Kinaiya…'
              : 'Putting Kinaiya to sleep…')
        : !_s.connected
        ? 'Connect to talk with Kinaiya'
        : !_s.converseOnline
        ? 'Converse unavailable'
        : _armed || _recording
        ? _status
        : _s.busyConverse || _speaking
        ? _status
        : _converseAvailable
        ? 'Ready when you are'
        : _status,
    callLabel: _callHasStopState ? _callLabel : 'Talk with Kinaiya',
    onCall: _callAction,
    callTooltip: _callHasStopState
        ? 'Stop Live listen'
        : _micReady
        ? 'Start Live listen (armed mic)'
        : 'Microphone unavailable here',
    setupLabel: _homeSetupLabel,
    onSetup: _homeSetupAction,
    onConverseSetup: widget.onModelSettings ?? _homeSetupAction,
    sleepingCharacterWrapper: _mascotPrompts,
    agentStatus: !_s.agentOnline
        ? 'Not connected'
        : !_s.sessionAuthenticated || _s.tasksStatusStale
        ? 'Status unavailable'
        : _s.tasks.any(
            (task) => const {
              'running',
              'queued',
              'verifying',
              'accepted',
            }.contains(task.state),
          )
        ? 'Working'
        : _s.agentInferenceAllowed
        ? 'Available'
        : 'Controls available',
    sessionName: _s.agentSession?.name,
    onAgent: () => _s.setView(AppView.agent),
    showConverseView: widget.showConverseView,
    setupOpen: widget.setupOpen,
    caption: _s.converseHistory.isEmpty ? null : _currentCaption(),
    banner: !_homeSleeping && (_s.resourceStatusStale || !_s.converseEnabled)
        ? banner
        : null,
    useDesignScene: widget.useDesignScene && widget.showCharacter,
  );

  Widget _interactionPortrait(CharacterPack? pack) =>
      widget.showCharacter &&
          widget.character?.packageJson == null &&
          pack?.fallback != true
      ? _mascotPrompts(
          ValueListenableBuilder<SpeechMouthFrame>(
            valueListenable: _mouthFrame,
            builder: (context, frame, _) => ConversePortrait(
              speaking: _speaking,
              thinking: _s.busyConverse && !_speaking,
              mouthOpenness: _canAnimateMouth ? frame.openness : 0,
              viseme: _canAnimateMouth ? frame.viseme : null,
            ),
          ),
        )
      : LayoutBuilder(
          builder: (_, bounds) => Center(
            child: _mascot(pack, bounds.maxHeight.clamp(48.0, bounds.maxWidth)),
          ),
        );

  void _prefillArtifactQuestion(String message) {
    if (_s.prefillArtifactQuestion(message)) {
      _input.value = TextEditingValue(
        text: _s.converseDraft,
        selection: TextSelection.collapsed(offset: _s.converseDraft.length),
      );
    } else {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text(
            'This question could not be added to the current draft.',
          ),
        ),
      );
    }
  }

  Widget _interactionSurface(CharacterPack? pack, Widget banner) => Column(
    children: [
      banner,
      Expanded(
        child: IntelligentConverseStage(
          state: _s,
          character: _interactionPortrait(pack),
          transcript: _transcript(),
          composer: _inlineComposer(),
          controls: _voiceActions(),
          onHistory: _openHistory,
          onAsk: _prefillArtifactQuestion,
          richEnabled: widget.richArtifactsEnabled,
          onRichEnabled: widget.onRichArtifactsEnabled,
          animate: widget.animateTransitions,
          normalBuilder: (character, transcript, composer) =>
              ConverseInteraction(
                character: character,
                transcript: transcript,
                composer: composer,
                modelSummary: ConverseModelSummary(
                  state: _s,
                  onModelSettings: widget.onModelSettings ?? _openVoiceOptions,
                  onConnectionSetup: widget.onConnectionSetup,
                  onOpenModelOptions: widget.onOpenModelOptions,
                  onAddModel: widget.onAddModel,
                  menuController: _modelMenu,
                ),
                status: _status,
                stageState: _converseStageState,
                callLabel: _callHasStopState ? _callLabel : 'Talk with Kinaiya',
                onCall: _callAction,
                onMicrophone: _micStopPending
                    ? null
                    : _armed || _recording || _micStopUnconfirmed
                    ? _stopMicrophone
                    : _nativeCallActive
                    ? null
                    : _callAction,
                microphoneLabel: _micStopPending
                    ? 'Stopping microphone…'
                    : _armed || _recording || _micStopUnconfirmed
                    ? 'Mute'
                    : _nativeCallActive
                    ? 'Microphone paused'
                    : 'Unmute',
                onModelSettings: _modelMenu.open,
                onHistory: _openHistory,
                onVoiceOptions: _openVoiceOptions,
                onMemory: () =>
                    showConverseMemoryDialog(context: context, state: _s),
                onCompanions: () =>
                    showConverseCompanionsDialog(context: context, state: _s),
                activeVoice: _armed || _recording || _speaking,
              ),
        ),
      ),
    ],
  );

  Widget _fadeSurface(Widget child) => UiFadeTransition(
    enabled: widget.animateTransitions,
    transitionKey: widget.surface,
    child: child,
  );

  @override
  Widget build(BuildContext context) => FutureBuilder<CharacterPack>(
    future: _pack,
    builder: (context, snap) => LayoutBuilder(
      builder: (context, box) {
        final resourceNote = _s.resourceStatusStale
            ? 'Resource status is unavailable. Voice availability is unconfirmed; Agent Chat remains available.'
            : _s.resourceConverseDesired == true &&
                  const {'initializing', 'restoring'}.contains(_s.resourcePhase)
            ? 'Voice services are starting or restoring. Agent Chat and approvals remain available.'
            : _s.resourcePhase == 'failed'
            ? 'Voice restoration failed. Agent Chat and approvals remain available.'
            : _s.resourcePhase == 'local_work'
            ? 'Voice is paused for local work. Agent Chat and approvals remain available.'
            : 'Converse is off. Direct Agent Chat remains available.';
        final banner = _s.resourceStatusStale || !_s.converseEnabled
            ? MaterialBanner(
                content: Text(resourceNote),
                actions: [
                  TextButton(
                    onPressed: () => _s.setView(AppView.agent),
                    child: const Text('OPEN AGENT CHAT'),
                  ),
                ],
              )
            : const SizedBox.shrink();
        final pack =
            widget.character?.pack ??
            (snap.connectionState == ConnectionState.done ? snap.data : null);
        if (widget.surface == CharacterSurface.home) {
          if (!widget.showCharacter) {
            return _fadeSurface(
              MoonlitScene(
                primary: widget.scenePrimary,
                background: widget.sceneBackground,
                enabled: false,
                child: Column(
                  children: [
                    banner,
                    _historyBar(),
                    Expanded(child: _transcript()),
                    SafeArea(
                      top: false,
                      child: Padding(
                        padding: const EdgeInsets.all(16),
                        child: Wrap(
                          alignment: WrapAlignment.center,
                          spacing: 12,
                          runSpacing: 12,
                          children: [
                            if (_callAction != null) ...[
                              FilledButton.icon(
                                key: const ValueKey('call-kinaiya'),
                                onPressed: _callAction,
                                icon: Icon(
                                  _callHasStopState
                                      ? Icons.call_end_rounded
                                      : Icons.mic_none,
                                ),
                                label: Text(
                                  _callHasStopState
                                      ? _callLabel
                                      : 'Talk with Kinaiya',
                                ),
                              ),
                              OutlinedButton(
                                key: const ValueKey('home-setup'),
                                onPressed: _homeSetupAction,
                                child: Text(_homeSetupLabel),
                              ),
                            ] else ...[
                              FilledButton.icon(
                                key: const ValueKey('home-setup'),
                                onPressed: _homeSetupAction,
                                icon: const Icon(Icons.tune_rounded),
                                label: Text(_homeSetupLabel),
                              ),
                              OutlinedButton.icon(
                                key: const ValueKey('call-kinaiya'),
                                onPressed: null,
                                icon: Icon(
                                  _callHasStopState
                                      ? Icons.call_end_rounded
                                      : Icons.mic_none,
                                ),
                                label: Text(
                                  _callHasStopState
                                      ? _callLabel
                                      : 'Talk with Kinaiya',
                                ),
                              ),
                            ],
                            _ownerSleepControl(),
                            if (!widget.showConverseView)
                              TextButton.icon(
                                key: const ValueKey(
                                  'home-converse-hidden-setup',
                                ),
                                onPressed:
                                    widget.onModelSettings ?? _homeSetupAction,
                                icon: const Icon(
                                  Icons.visibility_outlined,
                                  size: 18,
                                ),
                                label: const Text(
                                  'Converse view is hidden · Enable it in Setup',
                                  textAlign: TextAlign.center,
                                ),
                              ),
                          ],
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            );
          }
          final home = _homeSurface(pack, banner);
          return _fadeSurface(
            widget.useDesignScene && widget.showCharacter
                ? home
                : MoonlitScene(
                    primary: widget.scenePrimary,
                    background: widget.sceneBackground,
                    enabled: widget.showCharacter,
                    child: home,
                  ),
          );
        }
        if (widget.surface == CharacterSurface.interaction) {
          return _fadeSurface(_interactionSurface(pack, banner));
        }
        if (widget.useDesignScene &&
            widget.showCharacter &&
            _s.converseHistory.isEmpty) {
          return _designHome(pack, box, banner);
        }
        final scale = MediaQuery.textScalerOf(context).scale(1);
        final stageHeight = (box.maxHeight - 240 * scale).clamp(
          (widget.showCharacter
                  ? (widget.useDesignScene &&
                            _s.converseHistory.isEmpty &&
                            scale < 1.5
                        ? 140.0
                        : 320.0)
                  : 200.0) *
              scale.clamp(1.0, 2.0),
          widget.useDesignScene ? double.infinity : 700.0,
        );
        // Natural composer height grows with text or the keyboard. Keep it
        // scrollable without replacing the focused input. Illustrated scenery
        // belongs to its actual mascot substage, including layouts with history.
        final content = SingleChildScrollView(
          child: ConstrainedBox(
            constraints: BoxConstraints(minHeight: box.maxHeight),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                banner,
                SizedBox(
                  // The outer surface scrolls when a keyboard/large text
                  // leaves little height. Preserve room for the stage and
                  // wrapped history button instead of overfilling a tiny flex.
                  height: stageHeight,
                  child: _conversation(
                    widget.character?.pack ??
                        (snap.connectionState == ConnectionState.done
                            ? snap.data
                            : null),
                  ),
                ),
                _composer(),
              ],
            ),
          ),
        );
        return widget.useDesignScene && widget.showCharacter
            ? ColoredBox(color: Colors.white, child: content)
            : MoonlitScene(
                primary: widget.scenePrimary,
                background: widget.sceneBackground,
                enabled: widget.showCharacter,
                child: content,
              );
      },
    ),
  );
}

class ConverseHandoffStatusBanner extends StatefulWidget {
  const ConverseHandoffStatusBanner({
    super.key,
    required this.notice,
    this.attachedToMessage = true,
    this.animateTransitions = true,
    this.destinationStatus = HandoffTaskDestinationStatus.none,
    this.taskProgress,
    this.onOpenTask,
  });

  final HandoffNotice notice;
  final bool attachedToMessage;
  final bool animateTransitions;
  final HandoffTaskDestinationStatus destinationStatus;
  final HandoffTaskProgressSummary? taskProgress;
  final VoidCallback? onOpenTask;

  static final Map<(String, int), HandoffStatus> _observedStatuses = {};
  static final Set<(String, int, HandoffStatus)> _animatedTransitions = {};

  @visibleForTesting
  static void resetMicrofeedbackForTesting() {
    _observedStatuses.clear();
    _animatedTransitions.clear();
  }

  @override
  State<ConverseHandoffStatusBanner> createState() =>
      _ConverseHandoffStatusBannerState();
}

class _ConverseHandoffStatusBannerState
    extends State<ConverseHandoffStatusBanner>
    with SingleTickerProviderStateMixin {
  static const Duration _microfeedbackDuration = Duration(milliseconds: 220);

  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: _microfeedbackDuration,
    value: 1.0,
  );
  bool _pendingFreshTransition = false;

  HandoffNotice get notice => widget.notice;
  bool get attachedToMessage => widget.attachedToMessage;
  HandoffTaskDestinationStatus get destinationStatus =>
      widget.destinationStatus;
  HandoffTaskProgressSummary? get taskProgress => widget.taskProgress;
  VoidCallback? get onOpenTask => widget.onOpenTask;

  bool _canAnimate(BuildContext context) =>
      widget.animateTransitions &&
      MediaQuery.maybeDisableAnimationsOf(context) != true;

  void _recordAndEvaluateTransition({required bool allowStart}) {
    final key = (notice.sourceEventId, notice.sourceRevision);
    final previous = ConverseHandoffStatusBanner._observedStatuses[key];
    ConverseHandoffStatusBanner._observedStatuses[key] = notice.status;
    while (ConverseHandoffStatusBanner._observedStatuses.length > 256) {
      ConverseHandoffStatusBanner._observedStatuses.remove(
        ConverseHandoffStatusBanner._observedStatuses.keys.first,
      );
    }
    final isFreshTransition =
        previous != null &&
        previous != notice.status &&
        notice.status != HandoffStatus.queued &&
        ConverseHandoffStatusBanner._animatedTransitions.add((
          notice.sourceEventId,
          notice.sourceRevision,
          notice.status,
        ));
    while (ConverseHandoffStatusBanner._animatedTransitions.length > 256) {
      ConverseHandoffStatusBanner._animatedTransitions.remove(
        ConverseHandoffStatusBanner._animatedTransitions.first,
      );
    }
    if (!isFreshTransition) return;
    if (!allowStart) {
      _pendingFreshTransition = true;
      return;
    }
    if (_canAnimate(context)) {
      _controller.forward(from: 0);
    } else {
      _controller.stop();
      _controller.value = 1.0;
    }
  }

  @override
  void initState() {
    super.initState();
    _recordAndEvaluateTransition(allowStart: false);
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (!_canAnimate(context)) {
      _pendingFreshTransition = false;
      if (_controller.isAnimating) {
        _controller.stop();
      }
      _controller.value = 1.0;
      return;
    }
    if (_pendingFreshTransition) {
      _pendingFreshTransition = false;
      _controller.forward(from: 0);
    }
  }

  @override
  void didUpdateWidget(covariant ConverseHandoffStatusBanner oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!widget.animateTransitions && _controller.isAnimating) {
      _controller.stop();
      _controller.value = 1.0;
    }
    _recordAndEvaluateTransition(allowStart: true);
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  String? get _destinationMessage => switch (destinationStatus) {
    HandoffTaskDestinationStatus.none ||
    HandoffTaskDestinationStatus.known => null,
    HandoffTaskDestinationStatus.missing =>
      'Task ${notice.nativeTaskId} is unavailable or retired in current Agent tasks.',
    HandoffTaskDestinationStatus.stale =>
      'Task ${notice.nativeTaskId} belongs to an earlier session or epoch.',
    HandoffTaskDestinationStatus.unauthorized =>
      'Reconnect to inspect task ${notice.nativeTaskId} in Agent.',
  };

  IconData get _icon => switch (notice.status) {
    HandoffStatus.queued => Icons.schedule_rounded,
    HandoffStatus.accepted => Icons.task_alt_rounded,
    HandoffStatus.applied => Icons.published_with_changes_rounded,
    HandoffStatus.rejected => Icons.block_rounded,
    HandoffStatus.notApplied => Icons.do_not_disturb_on_outlined,
    HandoffStatus.unknown => Icons.help_outline_rounded,
    HandoffStatus.unavailable => Icons.cloud_off_rounded,
  };

  IconData _progressIcon(HandoffTaskLifecycleKind kind) => switch (kind) {
    HandoffTaskLifecycleKind.queued => Icons.hourglass_top_rounded,
    HandoffTaskLifecycleKind.running => Icons.sync_rounded,
    HandoffTaskLifecycleKind.awaitingApproval => Icons.gavel_rounded,
    HandoffTaskLifecycleKind.blocked => Icons.pause_circle_outline_rounded,
    HandoffTaskLifecycleKind.failed => Icons.error_outline_rounded,
    HandoffTaskLifecycleKind.succeededWithEvidence => Icons.verified_rounded,
    HandoffTaskLifecycleKind.succeededUnverified => Icons.info_outline_rounded,
    HandoffTaskLifecycleKind.unknown => Icons.help_outline_rounded,
  };

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final (Color bg, Color fg, Color border) = switch (notice.status) {
      HandoffStatus.accepted || HandoffStatus.applied => (
        scheme.primaryContainer,
        scheme.onPrimaryContainer,
        scheme.primary,
      ),
      HandoffStatus.queued => (
        scheme.secondaryContainer,
        scheme.onSecondaryContainer,
        scheme.secondary,
      ),
      HandoffStatus.rejected => (
        scheme.errorContainer,
        scheme.onErrorContainer,
        scheme.error,
      ),
      HandoffStatus.notApplied ||
      HandoffStatus.unknown ||
      HandoffStatus.unavailable => (
        scheme.surfaceContainerHighest,
        scheme.onSurface,
        scheme.outline,
      ),
    };
    final semanticLabel = taskProgress == null
        ? notice.semanticSummary
        : '${notice.semanticSummary}. ${taskProgress!.stateLabel}: ${taskProgress!.detailText}';
    final canAnimate = _canAnimate(context);
    return Semantics(
      container: true,
      liveRegion: true,
      label: semanticLabel,
      child: Align(
        alignment: attachedToMessage
            ? Alignment.centerRight
            : Alignment.centerLeft,
        child: AnimatedBuilder(
          animation: _controller,
          builder: (context, child) {
            final highlighting = canAnimate && _controller.isAnimating;
            final t = highlighting
                ? Curves.easeOutCubic.transform(_controller.value)
                : 1.0;
            final highlightAlpha = highlighting ? (1.0 - t) * 0.35 : 0.0;
            return Container(
              margin: attachedToMessage
                  ? const EdgeInsets.fromLTRB(24, 2, 8, 6)
                  : const EdgeInsets.symmetric(vertical: 4, horizontal: 8),
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
              decoration: BoxDecoration(
                color: Color.lerp(bg, border, highlightAlpha) ?? bg,
                borderRadius: BorderRadius.circular(12),
                border: Border.all(
                  color: border,
                  width: highlighting ? 1.5 : 1.0,
                ),
              ),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Wrap(
                    spacing: 8,
                    runSpacing: 4,
                    crossAxisAlignment: WrapCrossAlignment.center,
                    children: [
                      Container(
                        key: ValueKey(
                          'converse-handoff-status-chip-${notice.sourceEventId}',
                        ),
                        padding: highlighting
                            ? const EdgeInsets.symmetric(
                                horizontal: 6,
                                vertical: 2,
                              )
                            : EdgeInsets.zero,
                        decoration: highlighting
                            ? BoxDecoration(
                                color: border.withValues(alpha: 0.14),
                                borderRadius: BorderRadius.circular(8),
                              )
                            : null,
                        child: Row(
                          key: highlighting
                              ? ValueKey(
                                  'converse-handoff-microfeedback-active-${notice.sourceEventId}',
                                )
                              : null,
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Icon(_icon, size: 16, color: fg),
                            const SizedBox(width: 4),
                            Text(
                              notice.statusLabel,
                              key: ValueKey(
                                'converse-handoff-status-${notice.sourceEventId}',
                              ),
                              style: TextStyle(
                                fontSize: 12,
                                fontWeight: FontWeight.w700,
                                color: fg,
                              ),
                            ),
                          ],
                        ),
                      ),
                      Text(
                        notice.text,
                        key: ValueKey(
                          'converse-handoff-text-${notice.sourceEventId}',
                        ),
                        style: TextStyle(fontSize: 12, color: fg, height: 1.35),
                      ),
                      if (notice.hasConfirmedTaskIdentifier &&
                          destinationStatus ==
                              HandoffTaskDestinationStatus.known &&
                          onOpenTask != null)
                        TextButton.icon(
                          key: ValueKey(
                            'converse-handoff-open-task-${notice.sourceEventId}',
                          ),
                          style: TextButton.styleFrom(
                            foregroundColor: fg,
                            visualDensity: VisualDensity.compact,
                            padding: const EdgeInsets.symmetric(
                              horizontal: 8,
                              vertical: 4,
                            ),
                          ),
                          onPressed: onOpenTask,
                          icon: const Icon(Icons.open_in_new_rounded, size: 15),
                          label: const Text(
                            'Open task',
                            style: TextStyle(
                              fontSize: 12,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                        )
                      else if (_destinationMessage case final destText?)
                        Text(
                          destText,
                          key: ValueKey(
                            'converse-handoff-destination-status-${notice.sourceEventId}',
                          ),
                          style: TextStyle(
                            fontSize: 11,
                            fontStyle: FontStyle.italic,
                            color: fg,
                          ),
                        ),
                    ],
                  ),
                  if (taskProgress case final progress?) ...[
                    const SizedBox(height: 6),
                    Wrap(
                      key: ValueKey(
                        'converse-handoff-task-progress-${notice.sourceEventId}',
                      ),
                      spacing: 8,
                      runSpacing: 4,
                      crossAxisAlignment: WrapCrossAlignment.center,
                      children: [
                        Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Icon(
                              _progressIcon(progress.kind),
                              size: 14,
                              color: fg,
                            ),
                            const SizedBox(width: 4),
                            Text(
                              progress.stateLabel,
                              key: ValueKey(
                                'converse-handoff-task-state-${notice.sourceEventId}',
                              ),
                              style: TextStyle(
                                fontSize: 11,
                                fontWeight: FontWeight.w700,
                                color: fg,
                              ),
                            ),
                          ],
                        ),
                        Text(
                          progress.detailText,
                          key: ValueKey(
                            'converse-handoff-task-detail-${notice.sourceEventId}',
                          ),
                          style: TextStyle(
                            fontSize: 11,
                            color: fg,
                            height: 1.3,
                          ),
                        ),
                        if (progress.actionLabel != null &&
                            progress.actionLabel != 'Open task' &&
                            destinationStatus ==
                                HandoffTaskDestinationStatus.known &&
                            onOpenTask != null)
                          TextButton.icon(
                            key: ValueKey(
                              'converse-handoff-task-action-${notice.sourceEventId}',
                            ),
                            style: TextButton.styleFrom(
                              foregroundColor: fg,
                              visualDensity: VisualDensity.compact,
                              padding: const EdgeInsets.symmetric(
                                horizontal: 8,
                                vertical: 4,
                              ),
                            ),
                            onPressed: onOpenTask,
                            icon: Icon(
                              progress.kind ==
                                      HandoffTaskLifecycleKind.awaitingApproval
                                  ? Icons.fact_check_outlined
                                  : Icons.receipt_long_outlined,
                              size: 14,
                            ),
                            label: Text(
                              progress.actionLabel!,
                              style: const TextStyle(
                                fontSize: 11,
                                fontWeight: FontWeight.w700,
                              ),
                            ),
                          ),
                      ],
                    ),
                  ],
                ],
              ),
            );
          },
        ),
      ),
    );
  }
}
