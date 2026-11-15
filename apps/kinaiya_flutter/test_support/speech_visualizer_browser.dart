/// Isolated browser rehearsal: actual CharacterView, Dart/JS adapter and Web
/// Audio, with public PCM and an in-memory service fixture. No model or mic.
library;

import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/audio/speech_audio.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';
import 'package:kinaiya_flutter/ui/visual_theme.dart';

void main() => runApp(const _Rehearsal());

class _Rehearsal extends StatefulWidget {
  const _Rehearsal();
  @override
  State<_Rehearsal> createState() => _RehearsalState();
}

class _RehearsalState extends State<_Rehearsal> {
  static const rate = 24000;
  final audio = speechAudio();
  late final AppState state;
  Uint8List? pcm;
  String status = 'Loading public PCM', receipts = '', meter = '0.0000';
  bool started = false, reading = false;
  Timer? timer;

  @override
  void initState() {
    super.initState();
    final api = KinaiyaClient(
      sessionBase: 'http://fixture',
      agentBase: 'http://fixture',
      converseBase: 'http://fixture',
      ownerToken: 'public-synthetic-owner',
      artifactClientFactory: () =>
          throw StateError('No artifacts in this rehearsal'),
      httpClient: MockClient((request) async {
        final path = request.url.path;
        if (path == '/health') return _json({'enabled': true});
        if (path == '/v1/transcript') {
          return _json(
            !started
                ? []
                : [
                    {
                      'schema_version': '1.0',
                      'event_id': 'public-visualizer-entry',
                      'sequence': 1,
                      'session_id': 'converse',
                      'thread_id': 'thread-converse',
                      'epoch': 1,
                      'kind': 'conversation.converse',
                      'origin': 'converse',
                      'idempotency_key': 'public-visualizer-notice',
                      'payload': {
                        'text':
                            'Public PCM playback control. No Agent or model runs in this rehearsal.',
                        'chunk_id': 'public-visualizer-chunk',
                        'generation_epoch': 17,
                        'outcome_event_id': 'controlled-fixture-only',
                        'delivery': 'generated',
                      },
                    },
                  ],
          );
        }
        if (path == '/v1/converse/speech') {
          final lines = <String>[];
          for (var offset = 0; offset < pcm!.length; offset += rate ~/ 2) {
            final end = math.min(pcm!.length, offset + rate ~/ 2);
            lines.add(
              jsonEncode({
                'type': 'audio',
                'audio': base64Encode(Uint8List.sublistView(pcm!, offset, end)),
                'sample_rate': rate,
                'samples': (end - offset) ~/ 2,
                'chunk_index': lines.length,
                'event_id': 'public-visualizer-entry',
                'generation_epoch': 17,
                'chunk_id': 'public-visualizer-chunk',
              }),
            );
          }
          return http.Response(
            '${lines.join('\n')}\n',
            200,
            headers: {'content-type': 'application/x-ndjson'},
          );
        }
        if (path == '/v1/converse/delivery') {
          receipts += '${(jsonDecode(request.body) as Map)['status']} ';
          if (mounted) setState(() {});
          return _json({});
        }
        if (path == '/v1/converse/interrupt') return _json({});
        return _json({
          'detail': 'Unavailable in controlled fixture',
        }, code: 404);
      }),
    );
    state = AppState()
      ..connect(ConnectionProfile.defaults(), injected: api)
      ..converseSession = SessionInfo(
        sessionId: 'converse',
        threadId: 'thread-converse',
        channel: 'converse',
        epoch: 1,
      );
    unawaited(_load());
  }

  http.Response _json(Object value, {int code = 200}) => http.Response(
    jsonEncode(value),
    code,
    headers: {'content-type': 'application/json'},
  );

  Future<void> _load() async {
    if (Uri.base.queryParameters['sample'] == 'native') {
      final response = await http.get(Uri.base.resolve('native-speech.wav'));
      final bytes = response.bodyBytes;
      final header = ByteData.sublistView(bytes);
      if (response.statusCode != 200 ||
          bytes.length < 44 ||
          header.getUint32(24, Endian.little) != rate ||
          header.getUint16(22, Endian.little) != 1 ||
          header.getUint16(34, Endian.little) != 16 ||
          header.getUint32(40, Endian.little) != bytes.length - 44) {
        throw StateError('Expected canonical public mono24k PCM16 WAV');
      }
      pcm = Uint8List.sublistView(bytes, 44);
      status = 'Retained public native-model PCM · controlled delivery';
    } else {
      pcm = Uint8List(rate * 2 * 16);
      final samples = ByteData.sublistView(pcm!);
      for (var i = 0; i < pcm!.length ~/ 2; i++) {
        final phase = (i ~/ (rate * 2)) % 4;
        final amplitude = [0.0, .04, .2, 0.0][phase];
        samples.setInt16(
          i * 2,
          (32767 * amplitude * math.sin(2 * math.pi * 220 * i / rate)).round(),
          Endian.little,
        );
      }
      status = '16-second control: silence → soft → loud → silence, twice';
    }
    if (mounted) setState(() {});
  }

  Future<void> _probe() async {
    if (reading || audio is! SpeechPlaybackAudio) return;
    reading = true;
    try {
      final frame = await (audio as SpeechPlaybackAudio).playbackFrame();
      if (mounted) setState(() => meter = frame.rms.toStringAsFixed(4));
    } finally {
      reading = false;
    }
  }

  @override
  void dispose() {
    timer?.cancel();
    state.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => MaterialApp(
    theme: kinaiyaTheme(brightness: Brightness.dark, designSystem: true),
    home: Scaffold(
      body: SafeArea(
        child: Column(
          children: [
            Padding(
              padding: const EdgeInsets.all(8),
              child: Wrap(
                spacing: 12,
                runSpacing: 6,
                crossAxisAlignment: WrapCrossAlignment.center,
                children: [
                  FilledButton(
                    onPressed: pcm == null || started
                        ? null
                        : () async {
                            setState(() => started = true);
                            timer = Timer.periodic(
                              const Duration(milliseconds: 100),
                              (_) => unawaited(_probe()),
                            );
                            await state.refreshNotices();
                          },
                    child: const Text('Play public PCM'),
                  ),
                  Text(status),
                  Text('PCM RMS $meter'),
                  Text('Fixture receipts: $receipts'),
                  const Text('Mouth disabled · no microphone or provider call'),
                ],
              ),
            ),
            Expanded(
              child: AnimatedBuilder(
                animation: state,
                builder: (_, _) => CharacterView(
                  state: state,
                  audio: audio,
                  surface: CharacterSurface.interaction,
                  speechMouthMotion: false,
                ),
              ),
            ),
          ],
        ),
      ),
    ),
  );
}
