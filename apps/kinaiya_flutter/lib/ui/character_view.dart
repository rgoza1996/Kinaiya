import 'dart:async';

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../audio/speech_audio.dart';
import '../character/pack.dart';
import '../state/app_state.dart';
import 'book_sheet.dart';
import 'widgets.dart';

/// Distinct Character View: ordinary conversation only. Slash-like text is
/// sent verbatim and never parsed, completed or dispatched.
class CharacterView extends StatefulWidget {
  const CharacterView({super.key, required this.state});
  final AppState state;

  @override
  State<CharacterView> createState() => _CharacterViewState();
}

class _CharacterViewState extends State<CharacterView> {
  late final TextEditingController _input;
  late final SpeechAudio _audio;
  Future<CharacterPack>? _pack;
  String _mood = 'idle';
  bool _recording = false;
  bool _speaking = false;
  bool _voiceReady = false;
  bool _speechRun = false; // cancels obsolete fetch loops on barge-in.

  @override
  void initState() {
    super.initState();
    _input = TextEditingController(text: widget.state.converseDraft);
    _audio = speechAudio();
    _pack = loadCharacterPack();
    _refreshVoice();
  }

  @override
  void dispose() {
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

  Future<void> _send({String? overrideText, String inputMode = 'text'}) async {
    final text = (overrideText ?? _input.text).trim();
    final api = _s.client;
    if (text.isEmpty || api == null || _s.busyConverse) return;
    // Barge-in: stop everything audible now; Agent work is untouched.
    if (_speechRun) _speechRun = false;
    await _audio.stopPlayback();
    _s.turnNonce++;
    final nonce = _s.turnNonce;
    _s.busyConverse = true;
    try {
      final seedSession =
          _s.converseSession ??
          await _s.ensureSession('converse', name: 'Kinaiya');
      if (_s.converseCursor == 0) {
        try {
          final seed = await api.recent(sessionId: seedSession.sessionId);
          for (final e in seed) {
            if (e.kind == 'conversation.user') {
              _s.converseHistory.add(ChatMsg(role: 'user', text: e.text));
            } else if (e.kind == 'conversation.converse') {
              _s.converseHistory.add(ChatMsg(role: 'kinaiya', text: e.text));
            }
            if (e.sequence > _s.converseCursor) _s.converseCursor = e.sequence;
          }
          _s.touch();
        } catch (_) {}
      }
    } catch (_) {}
    _s.appendHistory(AppView.character, ChatMsg(role: 'user', text: text));
    if (overrideText == null) {
      _input.clear();
      _s.setDraft(AppView.character, '');
    }
    setState(() {
      _mood = 'thinking';
      _speaking = false;
    });
    try {
      final session =
          _s.converseSession ??
          await _s.ensureSession('converse', name: 'Kinaiya');
      final key = newIdempotencyKey();
      var assistantIndex = -1;
      var playedAcked = <String>{};
      await for (final event in api.streamTurn(
        sessionId: session.sessionId,
        threadId: session.threadId,
        epoch: session.epoch,
        text: text,
        idempotencyKey: key,
        inputMode: inputMode,
      )) {
        if (!mounted) return;
        switch (event.type) {
          case 'accepted':
            _s.speechEpoch =
                (event.raw['generation_epoch'] as num?)?.toInt() ?? 0;
          case 'text':
            final entry = event.entry;
            if (entry == null) break;
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
                text: cur.text + entry.text,
                eventId: entry.eventId,
              );
              _s.touch();
            }
            final replay = event.raw['replay'] == true;
            if (!replay && entry.text.isNotEmpty) {
              unawaited(_speakEntry(api, entry, nonce, playedAcked));
            }
          case 'interrupted':
            _s.appendHistory(
              AppView.character,
              ChatMsg(role: 'system', text: 'Stopped.'),
            );
          case 'error':
            _s.appendHistory(
              AppView.character,
              ChatMsg(
                role: 'system',
                text: 'Kinaiya could not reply. Please try again.',
              ),
            );
        }
      }
    } on ApiException catch (e) {
      if (!mounted) return;
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
      _s.appendHistory(
        AppView.character,
        ChatMsg(
          role: 'system',
          text: "Couldn't reach Kinaiya. Check the connection.",
        ),
      );
    } finally {
      if (mounted) {
        setState(() {
          _mood = 'idle';
          _speaking = false;
        });
        _s.busyConverse = false;
        _s.touch();
      }
    }
  }

  /// Synthesizes one stable entry as it arrives; drops late chunks by nonce
  /// plus server generation epoch. Text completion is never reported as heard
  /// speech: only playing/played/interrupted are acked, conservatively.
  Future<void> _speakEntry(
    KinaiyaClient api,
    JournalEntry entry,
    int nonce,
    Set<String> playedAcked,
  ) async {
    if (!await _audio.availablePlayback) return;
    final epoch =
        (entry.payload['generation_epoch'] as num?)?.toInt() ?? _s.speechEpoch;
    final chunkId = entry.payload['chunk_id'] as String? ?? entry.eventId;
    final run = _speechRun = true;
    var started = false;
    try {
      await for (final frame in api.speech(
        eventId: entry.eventId,
        generationEpoch: epoch,
      )) {
        if (!run || !_s.audioIsLive(nonce, epoch)) break;
        final audio = frame['audio'] as String?;
        if (audio == null || audio.isEmpty) continue;
        await _audio.queuePcm(
          audio,
          (frame['sample_rate'] as num?)?.toInt() ?? 24000,
        );
        if (!started) {
          started = true;
          if (mounted) setState(() => _speaking = true);
          if (_s.audioIsLive(nonce, epoch) && await _audio.isPlaying) {
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
      if (started && run && _s.audioIsLive(nonce, epoch)) {
        playedAcked.add(entry.eventId);
        try {
          await api.reportDelivery(
            eventId: entry.eventId,
            generationEpoch: epoch,
            chunkId: chunkId,
            status: 'played',
          );
        } catch (_) {}
        if (mounted) setState(() => _speaking = false);
      }
    } catch (_) {
      // Stale epochs (409) and transport cuts end synthesis quietly.
      if (mounted) setState(() => _speaking = false);
    }
  }

  Future<void> _stopSpeech() async {
    if (_speechRun) _speechRun = false;
    _s.turnNonce++;
    await _audio.stopPlayback();
    await _audio.cancelRecording();
    final api = _s.client;
    final session = _s.converseSession;
    if (mounted) {
      setState(() {
        _recording = false;
        _speaking = false;
        _mood = 'idle';
      });
    }
    if (api != null && session != null) {
      try {
        await api.interruptSpeech(session.sessionId);
      } catch (_) {}
      _s.appendHistory(
        AppView.character,
        ChatMsg(
          role: 'system',
          text: 'Speech stopped. Agent work is unaffected.',
        ),
      );
    }
  }

  Future<void> _toggleMic() async {
    if (_recording) {
      final rec = await _audio.stopRecording();
      if (!mounted) return;
      setState(() => _recording = false);
      if (rec == null || rec.bytes.isEmpty) return;
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
                                mood: _recording
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
                child: Row(
                  children: [
                    if (_voiceReady)
                      IconButton(
                        tooltip: _recording ? 'Stop recording' : 'Record voice',
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
              ),
            ),
          ],
        );
      },
    );
  }
}
