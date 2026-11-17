import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:kinaiya_flutter/api/live_stream_client.dart';
import 'package:kinaiya_flutter/api/live_streams.dart';

void main() {
  final url = Platform.environment['KINAIYA_LIVE_STREAM_URL'];
  final token = Platform.environment['KINAIYA_LIVE_STREAM_TOKEN'];
  final scope = Platform.environment['KINAIYA_LIVE_STREAM_SCOPE'];
  test(
    'real Dart transports preserve one native mic across decoder rollover',
    () async {
      final previous = HttpOverrides.current;
      HttpOverrides.global = null;
      addTearDown(() => HttpOverrides.global = previous);
      final api = LiveStreamApi(
        baseUrl: url!,
        ownerToken: token!,
        clientFactory: http.Client.new,
      );
      addTearDown(api.close);
      final capability = await api.inspect().result;
      expect(capability.available, isTrue);
      final boundaryCapability = await api.inspectUtteranceBoundary().result;
      expect(boundaryCapability.available, isTrue);
      expect(boundaryCapability.ownerInstance, capability.ownerInstance);
      final binding = <String, Object?>{
        ...jsonDecode(scope!) as Map<String, Object?>,
        'schema_version': '2.0',
        'protocol': liveStreamProtocol,
        'owner_instance': capability.ownerInstance,
        'capture_id': 'd' * 32,
      };
      final begin = LiveStreamOperation.fromJson(LiveStreamPhase.begin, {
        ...binding,
        'segment_index': 0,
      });
      final started = await api.dispatch(begin).result;
      expect(started.confirmed, isTrue);
      expect(started.capture!.captureOpen, isTrue);
      final pcm = LiveStreamOperation.fromJson(LiveStreamPhase.pcm, {
        ...begin.body,
        'stream_id': started.streamId,
        'worker_incarnation': started.workerIncarnation,
        'sequence': 1,
        'sample_offset': 0,
        'user_speaking': true,
        'pcm16_base64': base64Encode(List.filled(320, 0)),
      });
      final observed = await api.dispatch(pcm).result;
      expect(observed.confirmed, isTrue);
      expect(
        (await api.lookup(pcm).result).body['publication'],
        observed.body['publication'],
      );
      final finish = LiveStreamOperation.fromJson(LiveStreamPhase.finish, {
        for (final key in pcm.body.keys)
          if (key != 'pcm16_base64') key: pcm.body[key],
        'sequence': 2,
        'sample_offset': 160,
        'user_speaking': false,
        'capture_ended': false,
      });
      var terminal = await api.dispatch(finish).result;
      final deadline = Stopwatch()..start();
      while (!terminal.segmentRetired &&
          deadline.elapsed < const Duration(seconds: 3)) {
        terminal = await api.reconcile(finish).result;
        await Future<void>.delayed(const Duration(milliseconds: 10));
      }
      expect(terminal.decoderFinished, isTrue);
      expect(terminal.ownershipRetired, isTrue);
      expect(
        terminal.processReleased,
        isFalse,
        reason: 'The same mic still owns its STT lifetime',
      );
      expect(
        terminal.segmentRetired,
        isTrue,
        reason:
            'Decoder cleanup can finish before the logical mic/STT lifetime',
      );
      final next = await api
          .dispatch(
            LiveStreamOperation.fromJson(LiveStreamPhase.begin, {
              ...binding,
              'segment_index': 1,
            }),
          )
          .result;
      expect(next.confirmed, isTrue);
      expect(next.streamId, isNot(started.streamId));
      expect(next.nativeOwner, started.nativeOwner);
      expect(next.segmentSampleOffset, 160);
      expect(next.capture!.body, started.capture!.body);
      final secondPcm = LiveStreamOperation.fromJson(LiveStreamPhase.pcm, {
        ...binding,
        'segment_index': 1,
        'stream_id': next.streamId,
        'worker_incarnation': next.workerIncarnation,
        'sequence': 1,
        'sample_offset': 0,
        'user_speaking': true,
        'pcm16_base64': base64Encode(List.filled(320, 0)),
      });
      expect((await api.dispatch(secondPcm).result).confirmed, isTrue);
      final secondFinish =
          LiveStreamOperation.fromJson(LiveStreamPhase.finish, {
            for (final key in secondPcm.body.keys)
              if (key != 'pcm16_base64') key: secondPcm.body[key],
            'sequence': 2,
            'sample_offset': 160,
            'user_speaking': false,
            'capture_ended': false,
          });
      var secondFinal = await api.dispatch(secondFinish).result;
      final secondDeadline = Stopwatch()..start();
      while (!secondFinal.segmentRetired &&
          secondDeadline.elapsed < const Duration(seconds: 3)) {
        secondFinal = await api.reconcile(secondFinish).result;
        await Future<void>.delayed(const Duration(milliseconds: 10));
      }
      expect(secondFinal.segmentRetired, isTrue);
      final boundary = LiveStreamOperation.advanceUtterance(secondFinal);
      final changed = await api.dispatch(boundary).result;
      expect(changed.confirmed, isTrue);
      expect(changed.capture!.body, started.capture!.body);
      expect(changed.segmentSampleOffset, 320);
      expect(changed.nativeOwner, started.nativeOwner);
      expect(
        ((changed.body['native'] as Map<String, Object?>)['result']
            as Map<String, Object?>)['utterance_id'],
        boundary.body['utterance_id'],
      );
      expect((await api.lookup(boundary).result).confirmed, isTrue);
      final stopped = await api.cancel(begin, 'cancelled').result;
      expect(stopped.capture!.captureOpen, isFalse);
      expect(stopped.cleanupConfirmed, isTrue);
    },
    skip: url == null || token == null || scope == null,
  );
}
