import 'dart:async';

import 'package:flutter/material.dart';
import 'package:uuid/uuid.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../audio/speech_audio.dart';
import '../audio/turn_audio.dart';
import '../character/pack.dart';
import '../character/playback.dart';
import '../presentation/controller.dart';
import '../state/app_state.dart';
import 'book_sheet.dart';
import 'converse_memory.dart';
import 'converse_design.dart';
import 'voice_options.dart';
import 'settings_controls.dart';
import 'moonlit_scene.dart';
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
  final Color scenePrimary, sceneBackground;

  @override
  State<CharacterView> createState() => _CharacterViewState();
}

const List<int> endSilenceOptionsMs = <int>[800, 1500, 2500];

class _CharacterViewState extends State<CharacterView> {
  ConverseViewControls? _registeredControls;
  Future<void> Function()? _registeredVoiceOptions;

  void _detachSettingsControls() {
    final controls = _registeredControls;
    if (controls != null &&
        identical(controls.openVoiceOptions, _registeredVoiceOptions)) {
      controls.openVoiceOptions = null;
    }
    _registeredControls = null;
    _registeredVoiceOptions = null;
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

  void _publishVoiceActivity() {
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
  bool _transcribing = false;
  RecordedAudio? _pendingClip;
  ContentScope _pendingClipScope = ContentScope.localOnly;
  ContentScope _captureScope = ContentScope.localOnly;
  ContentScope _recordingScope = ContentScope.localOnly;

  /// User-armed continuous listening. Never true unless the user toggled it.
  bool _armed = false;
  int _endSilenceMs = 1500;
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
      _armOwner++;
      _voiceRequest++;
      _pendingClip = null;
      _clearWindows();
      _pollTimer?.cancel();
      _speechQueue = SpeechQueue();
      setState(() {
        _armed = _recording = _speaking = _voiceBusy = _transcribing =
            _clipOpen = false;
        _micStopPending = false;
      });
      _publishVoiceActivity();
      unawaited(_audio.stopArmed());
      unawaited(_audio.cancelRecording());
      unawaited(_audio.stopPlayback());
    });
  }

  @override
  void didUpdateWidget(covariant CharacterView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.controls, widget.controls) ||
        !identical(oldWidget.state, widget.state)) {
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
      if (_input.text != _s.converseDraft) _input.text = _s.converseDraft;
      _s.turnNonce++;
      _s.voiceNonce++;
      _s.speechEpoch = 0;
      _s.busyConverse = false;
      _armOwner++;
      _voiceRequest++;
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
      unawaited(_audio.stopPlayback());
      unawaited(_refreshVoice());
    }
  }

  @override
  void dispose() {
    _detachSettingsControls();
    _pollTimer?.cancel();
    _noticeSubscription?.cancel();
    _windowSubscription?.cancel();
    _resourceSubscription?.cancel();
    _armOwner++;
    _voiceRequest++;
    // Invalidate every in-flight callback: stale completions must not
    // reset newer speaking/busy state or leak the mic.
    _s.turnNonce++;
    _s.voiceNonce++;
    unawaited(_audio.stopArmed());
    unawaited(_audio.cancelRecording());
    unawaited(_audio.stopPlayback());
    unawaited(_audio.dispose());
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
    final api = widget.state.client;
    final connectionGeneration = _s.connectionGeneration;
    if (api != null) {
      try {
        final health = await api.converseHealth();
        if (!mounted || !_s.ownsConnection(connectionGeneration, api)) return;
        widget.state.converseEnabled =
            ((health['enabled'] as bool?) ?? true) &&
            !_s.resourceStatusStale &&
            (_s.resourcesConfigured != true || _s.resourcePhase == 'ready');
        _s.touch();
      } catch (_) {
        // Factual status surfaces through the connection indicator.
      }
    }
  }

  AppState get _s => widget.state;
  WindowSpeechAudio? get _windowAudio =>
      _audio is WindowSpeechAudio ? _audio as WindowSpeechAudio : null;
  bool get _windowMode => _s.streamingWindows && _windowAudio != null;

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
    if (!mounted ||
        !_armed ||
        !_windowMode ||
        entry.sessionId != _s.converseSession?.sessionId ||
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
    if (epoch != null && epoch > _s.speechEpoch) {
      _s.turnNonce++;
      _s.speechEpoch = epoch;
      _speechQueue = SpeechQueue();
      await _audio.stopPlayback();
      if (!mounted || !_armed) return;
      setState(() => _speaking = false);
      _publishVoiceActivity();
    }
    final utterance = entry.payload['window_utterance_id'];
    if (entry.kind == 'conversation.converse' &&
        _ownedUtterances.contains(utterance) &&
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
    await _audio.stopPlayback();
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
                ChatMsg(role: 'user', text: e.text),
              );
            } else if (e.kind == 'conversation.converse') {
              _s.appendHistory(
                AppView.character,
                ChatMsg(role: 'kinaiya', text: e.text),
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
    if (!_s.converseCanInfer || !_converseAvailable) return;
    if (!turnOwnsCleanup(nonce, _s.turnNonce)) return;
    if (!await _audio.availablePlayback) return;
    if (!_s.converseCanInfer || !_converseAvailable) return;
    if (!turnOwnsCleanup(nonce, _s.turnNonce)) return;
    final epoch =
        (entry.payload['generation_epoch'] as num?)?.toInt() ?? _s.speechEpoch;
    if (!_s.audioIsLive(nonce, epoch)) return;
    final windowUtterance = entry.payload['window_utterance_id'] as String?;
    if (windowUtterance != null &&
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
        if (!turnOwnsCleanup(nonce, _s.turnNonce)) break;
        if (!_s.audioIsLive(nonce, epoch)) break;
        final audio = frame['audio'] as String?;
        if (audio == null || audio.isEmpty) continue;
        final rate = (frame['sample_rate'] as num?)?.toInt() ?? 24000;
        if (!started &&
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
        if (!await _audio.queuePcm(audio, rate)) return;
        if (!turnOwnsCleanup(nonce, _s.turnNonce)) break;
        if (!_s.audioIsLive(nonce, epoch)) break;
        if (!started) {
          started = true;
          if (windowUtterance != null) {
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
          !_s.audioIsLive(nonce, epoch)) {
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
          !_s.audioIsLive(nonce, epoch)) {
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

  Future<void> _onBarge() {
    final expected = _s.speechEpoch;
    final owner = ++_s.turnNonce;
    _voiceRequest++;
    _pendingClip = null;
    _s.speechEpoch++;
    _s.busyConverse = false;
    _speechQueue = SpeechQueue();
    final stopped = _audio.stopPlayback();
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
  Future<void> _stopMicrophone() async {
    // Clearing _armed does not acknowledge a failed playback stop. Retain
    // that obligation so a retry must stop the same window speech as well.
    final stopWindowSpeech =
        _windowSpeechStopUnconfirmed || (_armed && _windowMode);
    _windowSpeechStopUnconfirmed = stopWindowSpeech;
    final owner = ++_armOwner;
    _voiceRequest++;
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
      _micStopUnconfirmed = _micStopPending = true;
      if (stopWindowSpeech) _speaking = false;
      _mood = _speaking ? 'speaking' : 'idle';
    });
    _publishVoiceActivity();
    try {
      await Future.wait([
        _audio.stopArmed(),
        _audio.cancelRecording(),
        if (stopWindowSpeech) _audio.stopPlayback(),
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
  Future<void> _setArmed(bool on) async {
    if (!on) return _stopMicrophone();
    if (_micStopUnconfirmed) return;
    final owner = ++_armOwner;
    if (!_converseAvailable || !_s.converseCanInfer || _s.client == null) {
      return;
    }
    _captureScope = _s.selectedContentScope;
    await _windowAudio?.configureWindows(_windowMode);
    await _audio.configureVad(endSilenceMs: _endSilenceMs);
    if (!mounted || owner != _armOwner || !_converseAvailable) return;
    final result = await _audio.startArmed();
    if (!mounted || owner != _armOwner) return;
    if (result != 'armed') {
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
    if (_windowMode) {
      try {
        await _s.ensureSession('converse', name: 'Converse');
        if (!mounted || !_armed || owner != _armOwner) return;
        await _nextWindowUtterance();
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
    if (!_armed || _pollingBridge || (!_windowMode && _voiceBusy)) return;
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
        if (event['type'] == 'speech-end' || event['type'] == 'clip-cap') {
          await _finishVoiceClip();
        } else if (event['type'] == 'barge') {
          unawaited(_onBarge());
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
    if (!_converseAvailable || !_s.converseCanInfer) return;
    final api = _s.client;
    final generation = _s.connectionGeneration;
    final scope = _s.selectedContentScope;
    if (api == null) return;
    final request = ++_voiceRequest;
    final recording = await _audio.chooseRecording();
    if (!mounted ||
        !_converseAvailable ||
        !_s.ownsConnection(generation, api) ||
        request != _voiceRequest ||
        recording == null) {
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

  Future<void> _toggleMic() async {
    final api = _s.client;
    final generation = _s.connectionGeneration;
    if (_armed) {
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
    return Semantics(
      button: true,
      label: 'Open Agent Chat',
      child: InkWell(
        onTap: _openAgent,
        borderRadius: BorderRadius.circular(24),
        child: pack == null
            ? SizedBox(width: size, height: size)
            : MascotWidget(
                pack: pack,
                bundle: widget.character?.bundle,
                size: size,
                mood: _characterSignals.semantic,
                signals: _characterSignals,
              ),
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
      return _designStage(pack, box);
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
    return Center(
      child: SingleChildScrollView(
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              SizedBox(
                key: const ValueKey('converse-design-hero'),
                width: size,
                height: size,
                child: _mascot(pack, size),
              ),
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

  Widget _transcript() => ListView(
    padding: const EdgeInsets.symmetric(vertical: 12),
    children: [
      for (final m in _s.converseHistory)
        MessageBubble(role: m.role, text: m.text, note: m.note),
      if (_s.busyConverse && !_speaking)
        const Padding(
          padding: EdgeInsets.all(12),
          child: Center(child: CircularProgressIndicator()),
        ),
    ],
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
      : _armed
      ? () => _setArmed(false)
      : _micReady && _converseAvailable
      ? () => _setArmed(true)
      : null;

  VoidCallback? get _tellAction => _recording
      ? _toggleMic
      : _micReady && _converseAvailable && !_armed && !_micStopUnconfirmed
      ? _toggleMic
      : null;

  String get _callLabel => _micStopPending
      ? 'Stopping microphone…'
      : _armed
      ? 'End conversation'
      : _micStopUnconfirmed
      ? 'Stop microphone'
      : 'Call Kinaiya';

  Widget _voiceActions() => widget.useDesignScene
      ? ConverseVoiceActions(
          callLabel: _callLabel,
          tellLabel: _recording ? 'Send voice message' : 'Tell Kinaiya',
          callTooltip: _armed || _micStopUnconfirmed
              ? 'Stop Live listen'
              : _micReady
              ? 'Start Live listen (armed mic)'
              : 'Microphone unavailable here',
          tellTooltip: _recording
              ? 'Stop recording'
              : _micReady
              ? 'Record voice'
              : 'Microphone unavailable here',
          callIcon: _armed ? Icons.call_end_rounded : Icons.mic_none_rounded,
          tellIcon: _recording ? Icons.send_rounded : Icons.graphic_eq_rounded,
          onCall: _callAction,
          onTell: _tellAction,
        )
      : Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(
              child: Tooltip(
                message: _armed || _micStopUnconfirmed
                    ? 'Stop Live listen'
                    : _micReady
                    ? 'Start Live listen (armed mic)'
                    : 'Microphone unavailable here',
                child: FilledButton.icon(
                  key: const ValueKey('call-kinaiya'),
                  onPressed: _callAction,
                  icon: Icon(
                    _armed ? Icons.call_end_rounded : Icons.mic_none_rounded,
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
    return showVoiceOptions(
      context: context,
      endSilenceMs: _endSilenceMs,
      silenceChoices: endSilenceOptionsMs,
      windowMode: _windowMode,
      onSilenceChanged: (v) async {
        if (!mounted ||
            !identical(widget.state, owner) ||
            !identical(widget.controls, controls)) {
          return;
        }
        setState(() => _endSilenceMs = v);
        await _audio.configureVad(endSilenceMs: v);
      },
    );
  }

  Widget _composer() => SafeArea(
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
                    message: 'Voice options (silence and timing)',
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
                      onPressed: _openVoiceOptions,
                    ),
                  ),
                  ConverseMemoryButton(state: _s),
                  if (_armed)
                    Text(
                      _windowMode ? _liveStatus : 'Listening (live)',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                    ),
                ],
              ),
              Row(
                children: [
                  if (_voiceReady && !_armed)
                    IconButton(
                      tooltip: 'Use audio recording',
                      icon: const Icon(Icons.audio_file_outlined),
                      onPressed: _converseAvailable ? _importRecording : null,
                    ),
                  Expanded(
                    child: TextField(
                      controller: _input,
                      key: const ValueKey('converse-input'),
                      decoration: const InputDecoration(
                        labelText: 'Talk with Kinaiya',
                      ),
                      minLines: 1,
                      maxLines: 4,
                      textInputAction: TextInputAction.send,
                      onChanged: (v) => _s.setDraft(AppView.character, v),
                      onSubmitted: (_) => _send(),
                    ),
                  ),
                  const SizedBox(width: 8),
                  if (_speaking || _s.busyConverse)
                    IconButton(
                      tooltip: 'Stop speech',
                      icon: const Icon(Icons.stop_circle_outlined),
                      onPressed: _stopSpeech,
                    ),
                  FilledButton(
                    onPressed:
                        _converseAvailable &&
                            !_s.busyConverse &&
                            _s.converseDraft.trim().isNotEmpty
                        ? () => _send()
                        : null,
                    child: const Text('Send'),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    ),
  );

  @override
  Widget build(BuildContext context) => FutureBuilder<CharacterPack>(
    future: _pack,
    builder: (context, snap) => MoonlitScene(
      primary: widget.scenePrimary,
      background: widget.sceneBackground,
      enabled: widget.showCharacter,
      useIllustration: widget.useDesignScene,
      child: LayoutBuilder(
        builder: (context, box) {
          final resourceNote = _s.resourceStatusStale
              ? 'Resource status is unavailable. Voice availability is unconfirmed; Agent Chat remains available.'
              : _s.resourceConverseDesired == true &&
                    const {
                      'initializing',
                      'restoring',
                    }.contains(_s.resourcePhase)
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
          final scale = MediaQuery.textScalerOf(context).scale(1);
          // Natural composer height can grow with accessibility text or the
          // keyboard. Keep it scrollable without replacing the focused input.
          return SingleChildScrollView(
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
                    height: (box.maxHeight - 240 * scale).clamp(
                      (widget.showCharacter ? 320.0 : 200.0) *
                          scale.clamp(1.0, 2.0),
                      700.0,
                    ),
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
        },
      ),
    ),
  );
}
