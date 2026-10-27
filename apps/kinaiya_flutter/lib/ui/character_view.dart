import 'dart:async';

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../audio/speech_audio.dart';
import '../audio/turn_audio.dart';
import '../character/pack.dart';
import '../state/app_state.dart';
import 'book_sheet.dart';
import 'widgets.dart';

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
  const CharacterView({super.key, required this.state, this.audio});
  final AppState state;
  final SpeechAudio? audio;

  @override
  State<CharacterView> createState() => _CharacterViewState();
}

const List<int> endSilenceOptionsMs = <int>[800, 1500, 2500];

class _CharacterViewState extends State<CharacterView> {
  late final TextEditingController _input;
  late final SpeechAudio _audio;
  Future<CharacterPack>? _pack;
  KinaiyaClient? _lastClient;
  String _mood = 'idle';
  bool _recording = false;
  bool _speaking = false;
  bool _voiceReady = false;

  /// Serializes speech entries: independently produced sentences from one
  /// turn play strictly in order and never interleave.
  SpeechQueue _speechQueue = SpeechQueue();
  StreamSubscription<JournalEntry>? _noticeSubscription;
  Future<void> _interruptBarrier = Future.value();
  bool _pollingBridge = false;
  int _captureEpoch = 0;
  int _armOwner = 0;
  int _voiceRequest = 0;
  bool _transcribing = false;
  RecordedAudio? _pendingClip;

  /// User-armed continuous listening. Never true unless the user toggled it.
  bool _armed = false;
  int _endSilenceMs = 1500;
  Timer? _pollTimer;
  bool _voiceBusy = false;
  bool _clipOpen = false;

  @override
  void initState() {
    super.initState();
    _input = TextEditingController(text: widget.state.converseDraft);
    _audio = widget.audio ?? speechAudio();
    _lastClient = _s.client;
    _pack = loadCharacterPack();
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
  }

  @override
  void didUpdateWidget(covariant CharacterView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (_lastClient != _s.client) {
      _lastClient = _s.client;
      _s.turnNonce++;
      _s.voiceNonce++;
      _s.speechEpoch++;
      _s.busyConverse = false;
      _armOwner++;
      _voiceRequest++;
      _pendingClip = null;
      _armed = false;
      _recording = false;
      _speaking = false;
      _clipOpen = false;
      _pollTimer?.cancel();
      _speechQueue = SpeechQueue();
      unawaited(_audio.stopArmed());
      unawaited(_audio.cancelRecording());
      unawaited(_audio.stopPlayback());
    }
  }

  @override
  void dispose() {
    _pollTimer?.cancel();
    _noticeSubscription?.cancel();
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
    setState(() => _voiceReady = mic || play);
    final api = widget.state.client;
    if (api != null) {
      try {
        final health = await api.converseHealth();
        widget.state.converseEnabled = (health['enabled'] as bool?) ?? true;
      } catch (_) {
        // Factual status surfaces through the connection indicator.
      }
    }
  }

  AppState get _s => widget.state;

  /// Sends a turn. Interruptible mid-SSE: when a turn is already in flight,
  /// the new utterance invalidates the old nonce first, so the old loop
  /// breaks on its next event and its `finally` (nonce-owned) cannot clear
  /// the new turn's `busyConverse`.
  Future<void> _send({String? overrideText, String inputMode = 'text'}) async {
    final text = (overrideText ?? _input.text).trim();
    final api = _s.client;
    if (text.isEmpty || api == null || !_s.connected) return;
    final nonce = ++_s.turnNonce;
    _s.busyConverse = true;
    _speechQueue = SpeechQueue();
    await _audio.stopPlayback();
    await _interruptBarrier;
    if (!mounted || !turnOwnsCleanup(nonce, _s.turnNonce)) return;
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
    if (!turnOwnsCleanup(nonce, _s.turnNonce)) return;
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
    }
    try {
      final session =
          _s.converseSession ??
          await _s.ensureSession('converse', name: 'Kinaiya');
      if (!turnOwnsCleanup(nonce, _s.turnNonce)) return;
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
    if (!turnOwnsCleanup(nonce, _s.turnNonce)) return;
    if (!await _audio.availablePlayback) return;
    if (!turnOwnsCleanup(nonce, _s.turnNonce)) return;
    final epoch =
        (entry.payload['generation_epoch'] as num?)?.toInt() ?? _s.speechEpoch;
    if (!_s.audioIsLive(nonce, epoch)) return;
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
        scheduled += pcmFrameSeconds(
          base64Length: audio.length,
          sampleRate: rate,
        );
        if (!await _audio.queuePcm(audio, rate)) return;
        if (!turnOwnsCleanup(nonce, _s.turnNonce)) break;
        if (!_s.audioIsLive(nonce, epoch)) break;
        if (!started) {
          started = true;
          startedAt = DateTime.now();
          if (_s.audioIsLive(nonce, epoch) && await _audio.isPlaying) {
            if (mounted && _s.audioIsLive(nonce, epoch)) {
              setState(() => _speaking = true);
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
      }
    } catch (_) {
      // Stale epochs (409) and transport cuts end quietly; only the live
      // turn may reset the speaking indicator.
      if (mounted && _s.audioIsLive(nonce, epoch)) {
        setState(() => _speaking = false);
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

  /// User-armed continuous listening toggle. The mic starts ONLY here.
  Future<void> _setArmed(bool on) async {
    final owner = ++_armOwner;
    if (!on) {
      _armed = false;
      _voiceRequest++;
      _pendingClip = null;
      _s.voiceNonce++;
      _pollTimer?.cancel();
      _pollTimer = null;
      await _audio.stopArmed();
      _clipOpen = false;
      if (mounted && owner == _armOwner) {
        setState(() {
          _mood = _speaking ? 'speaking' : 'idle';
        });
      }
      return;
    }
    if (!_s.connected || _s.client == null) return;
    await _audio.configureVad(endSilenceMs: _endSilenceMs);
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
    await _beginCapture();
    _pollTimer?.cancel();
    _pollTimer = Timer.periodic(
      const Duration(milliseconds: 100),
      (_) => _pollBridge(),
    );
  }

  Future<void> _pollBridge() async {
    if (!_armed || _pollingBridge || _voiceBusy) return;
    _pollingBridge = true;
    try {
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
        _pendingClip = null;
        final request = _voiceRequest;
        final api = _s.client;
        if (api == null) return;
        try {
          final extension = recording.mime.contains('ogg')
              ? 'ogg'
              : recording.mime.contains('mp4')
              ? 'mp4'
              : 'webm';
          final out = await api.transcribe(
            recording.bytes,
            'recording.$extension',
          );
          if (!mounted ||
              !_armed ||
              request != _voiceRequest ||
              owner != _armOwner) {
            continue;
          }
          final text = (out['text'] as String? ?? '').trim();
          if (text.isNotEmpty) {
            unawaited(_send(overrideText: text, inputMode: 'voice'));
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
    final recording = await _audio.chooseRecording();
    final api = _s.client;
    if (!mounted || !_s.connected || recording == null || api == null) return;
    await _onBarge();
    setState(() => _mood = 'listening');
    try {
      final output = await api.transcribe(recording.bytes, recording.filename);
      final text = (output['text'] as String? ?? '').trim();
      if (text.isNotEmpty && mounted) {
        await _send(overrideText: text, inputMode: 'voice');
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
      final rec = await _audio.stopRecording();
      if (!mounted) return;
      setState(() => _recording = false);
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
      final api = _s.client;
      if (api == null) return;
      try {
        final out = await api.transcribe(
          rec.bytes,
          rec.mime.contains('ogg') ? 'recording.ogg' : 'recording.webm',
        );
        final text = (out['text'] as String? ?? '').trim();
        if (text.isNotEmpty) {
          await _send(overrideText: text, inputMode: 'voice');
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
    final status = await _audio.startRecording();
    if (!mounted) return;
    if (status == 'recording') {
      setState(() {
        _recording = true;
        _mood = 'listening';
      });
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

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<CharacterPack>(
      future: _pack,
      builder: (context, snap) {
        final pack = snap.data;
        return Column(
          children: [
            if (!_s.converseEnabled)
              MaterialBanner(
                content: const Text(
                  'Converse is off. Direct Agent Chat remains available.',
                ),
                actions: [
                  TextButton(
                    onPressed: () => _s.setView(AppView.agent),
                    child: const Text('OPEN AGENT CHAT'),
                  ),
                ],
              ),
            Expanded(
              child: ListView(
                padding: const EdgeInsets.symmetric(vertical: 8),
                children: [
                  Center(
                    child: Semantics(
                      button: true,
                      label: 'Open Agent Chat',
                      child: InkWell(
                        onTap: _openAgent,
                        borderRadius: BorderRadius.circular(16),
                        child: pack == null
                            ? const SizedBox(height: 160)
                            : MascotWidget(
                                pack: pack,
                                mood: _recording || _armed
                                    ? 'listening'
                                    : (_speaking ? 'speaking' : _mood),
                              ),
                      ),
                    ),
                  ),
                  if (pack != null && pack.fallback && pack.note.isNotEmpty)
                    Center(
                      child: Padding(
                        padding: const EdgeInsets.all(4),
                        child: Text(
                          pack.note,
                          style: Theme.of(context).textTheme.bodySmall,
                        ),
                      ),
                    ),
                  for (final m in _s.converseHistory)
                    MessageBubble(role: m.role, text: m.text, note: m.note),
                  if (_s.busyConverse && !_speaking)
                    const Padding(
                      padding: EdgeInsets.all(12),
                      child: Center(child: CircularProgressIndicator()),
                    ),
                ],
              ),
            ),
            SafeArea(
              child: Padding(
                padding: const EdgeInsets.all(8),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Row(
                      children: [
                        Tooltip(
                          message: _armed
                              ? 'Stop Live listen'
                              : 'Start Live listen (armed mic)',
                          child: IconButton(
                            icon: Icon(
                              _armed ? Icons.graphic_eq : Icons.hearing,
                              color: _armed
                                  ? Theme.of(context).colorScheme.primary
                                  : null,
                            ),
                            onPressed: _s.connected
                                ? () => _setArmed(!_armed)
                                : null,
                          ),
                        ),
                        const Text('Live'),
                        const SizedBox(width: 8),
                        const Tooltip(
                          message: 'Silence that ends a voice clip',
                          child: Text('Silence'),
                        ),
                        const SizedBox(width: 4),
                        DropdownButton<int>(
                          value: _endSilenceMs,
                          items: [
                            for (final v in endSilenceOptionsMs)
                              DropdownMenuItem(
                                value: v,
                                child: Text(
                                  '${(v / 1000).toStringAsFixed(1)}s',
                                ),
                              ),
                          ],
                          onChanged: (v) async {
                            if (v == null) return;
                            setState(() => _endSilenceMs = v);
                            await _audio.configureVad(endSilenceMs: v);
                          },
                        ),
                        if (_armed)
                          const Padding(
                            padding: EdgeInsets.only(left: 8),
                            child: Text('Listening (live)'),
                          ),
                      ],
                    ),
                    Row(
                      children: [
                        if (_voiceReady && !_armed)
                          IconButton(
                            tooltip: 'Use audio recording',
                            icon: const Icon(Icons.audio_file),
                            onPressed: _s.connected ? _importRecording : null,
                          ),
                        if (_voiceReady && !_armed)
                          IconButton(
                            tooltip: _recording
                                ? 'Stop recording'
                                : 'Record voice',
                            icon: Icon(_recording ? Icons.stop : Icons.mic),
                            onPressed: _s.connected ? _toggleMic : null,
                          ),
                        Expanded(
                          child: TextField(
                            controller: _input,
                            decoration: const InputDecoration(
                              labelText: 'Talk with Kinaiya',
                              border: OutlineInputBorder(),
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
                            icon: const Icon(Icons.stop_circle),
                            onPressed: _stopSpeech,
                          ),
                        FilledButton(
                          onPressed:
                              _s.connected &&
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
          ],
        );
      },
    );
  }
}
