import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/audio/speech_audio.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';
import 'package:kinaiya_flutter/ui/voice_options.dart';

import 'converse_stage_test.dart' show StageAudio;

// Public synthetic devices only. Futures belong to the adapter, so a widget
// timeout, dismissal, or timer cannot turn a pending operation into permission
// to dispatch another one. There is no service client or browser microphone.
const _privateMarker = 'DO_NOT_DISPLAY_RAW_OUTPUT_EXCEPTION_57';
const _deviceId = 'opaque:device/with?reserved=%20#characters';
const _devices = [
  AudioOutputDevice(opaqueId: _deviceId, label: 'Synthetic monitor'),
  AudioOutputDevice(opaqueId: 'opaque-second', label: 'Synthetic headset'),
];

class _OutputAudio extends StageAudio implements AudioOutputSelection {
  int reads = 0, chooses = 0, defaults = 0, queues = 0, imports = 0;
  final selectedIds = <String>[];
  final calls = <String>[];
  List<AudioOutputDevice> devices = _devices;
  Completer<List<AudioOutputDevice>>? heldDiscovery;
  Completer<String>? heldSelection;
  String chooseResult = 'selected', defaultResult = 'default';
  bool discoveryThrows = false, selectionThrows = false, playing = false;

  @override
  Future<List<AudioOutputDevice>> audioOutputs() {
    reads++;
    calls.add('discover');
    if (discoveryThrows) return Future.error(StateError(_privateMarker));
    return heldDiscovery?.future ?? Future.value(devices);
  }

  Future<String> _selection(String result) {
    if (playing) return Future.value('busy');
    if (selectionThrows) return Future.error(StateError(_privateMarker));
    return heldSelection?.future ?? Future.value(result);
  }

  @override
  Future<String> chooseAudioOutput() {
    chooses++;
    calls.add('choose');
    return _selection(chooseResult);
  }

  @override
  Future<String> selectAudioOutput(String opaqueId) {
    selectedIds.add(opaqueId);
    calls.add('select');
    return _selection('selected');
  }

  @override
  Future<String> useDefaultAudioOutput() {
    defaults++;
    calls.add('default');
    return _selection(defaultResult);
  }

  @override
  Future<bool> get isPlaying async => playing;

  @override
  Future<bool> queuePcm(String value, int rate) async {
    queues++;
    return true;
  }

  @override
  Future<RecordedAudio?> chooseRecording() async {
    imports++;
    return null;
  }
}

Finder _control(String name) => find.byKey(ValueKey('audio-output-$name'));

Future<void> _tap(WidgetTester tester, String name) async {
  await tester.ensureVisible(_control(name));
  await tester.tap(_control(name));
  await tester.pump();
}

Future<AppState> _open(WidgetTester tester, SpeechAudio audio) async {
  tester.view.physicalSize = const Size(1280, 900);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final state = AppState();
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: CharacterView(state: state, audio: audio),
      ),
    ),
  );
  await tester.pump(const Duration(milliseconds: 100));
  final options = find.byKey(const ValueKey('voice-options'));
  await tester.ensureVisible(options);
  await tester.tap(options);
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 350));
  expect(find.byType(VoiceOptionsSheet), findsOneWidget);
  return state;
}

VoiceOptionsSheet _sheet(WidgetTester tester) =>
    tester.widget<VoiceOptionsSheet>(find.byType(VoiceOptionsSheet));

DropdownButton<String> _dropdown(WidgetTester tester) =>
    tester.widget<DropdownButton<String>>(
      find.descendant(
        of: _control('device'),
        matching: find.byType(DropdownButton<String>),
      ),
    );

void _localOnly(_OutputAudio audio, AppState state) {
  expect(audio.arms, 0);
  expect(audio.clips, 0);
  expect(audio.recordStarts, 0);
  expect(audio.imports, 0);
  expect(audio.queues, 0);
  expect(state.client, isNull);
  expect(state.converseSession, isNull);
  expect(state.converseHistory, isEmpty);
}

String _fixedNotice(WidgetTester tester) {
  expect(_control('notice'), findsOneWidget);
  final notice = _sheet(tester).audioOutputNotice;
  expect(notice, isNotNull);
  expect(notice!.trim(), isNotEmpty);
  expect(notice, isNot(contains(_privateMarker)));
  expect(find.textContaining(_privateMarker), findsNothing);
  return notice;
}

Future<void> _remove(WidgetTester tester, AppState state) async {
  await tester.pumpWidget(const SizedBox.shrink());
  await tester.pump();
  state.dispose();
}

void main() {
  testWidgets('optional output controls are absent on a legacy audio owner', (
    tester,
  ) async {
    final audio = StageAudio();
    final state = await _open(tester, audio);
    expect(find.byKey(const ValueKey('voice-group-output')), findsNothing);
    expect(audio.arms, 0);
    expect(audio.clips, 0);
    expect(audio.recordStarts, 0);
    await tester.tap(find.byTooltip('Close voice options'));
    await tester.pump(const Duration(milliseconds: 350));
    expect(audio.arms, 0);
    expect(audio.recordStarts, 0);
    expect(state.converseHistory, isEmpty);
    await _remove(tester, state);
  });

  testWidgets(
    'opening and dismissing output options has no adapter operation',
    (tester) async {
      final audio = _OutputAudio();
      final state = await _open(tester, audio);
      expect(find.byKey(const ValueKey('voice-group-output')), findsOneWidget);
      expect(audio.calls, isEmpty);
      _localOnly(audio, state);
      await tester.tap(find.byTooltip('Close voice options'));
      await tester.pump(const Duration(milliseconds: 350));
      expect(audio.calls, isEmpty);
      _localOnly(audio, state);
      await _remove(tester, state);
    },
  );

  testWidgets('one held discovery retains its slot beyond elapsed UI time', (
    tester,
  ) async {
    final original = Completer<List<AudioOutputDevice>>();
    final audio = _OutputAudio()..heldDiscovery = original;
    final state = await _open(tester, audio);
    await _tap(tester, 'refresh');
    expect(audio.reads, 1);
    expect(original.isCompleted, isFalse);
    expect(_sheet(tester).audioOutputBusy, isTrue);
    await tester.pump(const Duration(seconds: 20));
    await _tap(tester, 'refresh');
    await _tap(tester, 'choose');
    await _tap(tester, 'default');
    expect(audio.reads, 1);
    expect(audio.chooses, 0);
    expect(audio.defaults, 0);
    expect(original.isCompleted, isFalse);
    audio.heldDiscovery = null;
    original.complete(_devices);
    await tester.pump();
    expect(_sheet(tester).audioOutputBusy, isFalse);
    expect(_dropdown(tester).items!.map((item) => item.value), [
      _deviceId,
      'opaque-second',
    ]);
    _localOnly(audio, state);
    await _remove(tester, state);
  });

  testWidgets(
    'picker starts in the gesture and original choice is not replaced',
    (tester) async {
      final original = Completer<String>();
      final audio = _OutputAudio()..heldSelection = original;
      final state = await _open(tester, audio);
      await tester.ensureVisible(_control('choose'));
      await tester.tap(_control('choose'));
      // No extra pump/awaited discovery is needed to dispatch the picker.
      expect(audio.calls, ['choose']);
      await tester.pump();
      expect(_sheet(tester).audioOutputBusy, isTrue);
      await tester.pump(const Duration(seconds: 20));
      await _tap(tester, 'choose');
      await _tap(tester, 'refresh');
      await _tap(tester, 'default');
      expect(audio.calls, ['choose']);
      expect(original.isCompleted, isFalse);
      audio.devices = const [
        AudioOutputDevice(
          opaqueId: _deviceId,
          label: 'Synthetic monitor',
          selected: true,
        ),
      ];
      audio.heldSelection = null;
      original.complete('selected');
      await tester.pump();
      expect(audio.calls, ['choose', 'discover']);
      expect(_dropdown(tester).value, _deviceId);
      expect(_sheet(tester).audioOutputBusy, isFalse);
      _localOnly(audio, state);
      await _remove(tester, state);
    },
  );

  testWidgets(
    'fresh device choice sends the opaque ID and refreshes after ACK',
    (tester) async {
      final audio = _OutputAudio();
      final state = await _open(tester, audio);
      await _tap(tester, 'refresh');
      expect(audio.reads, 1);
      await tester.ensureVisible(_control('device'));
      await tester.tap(_control('device'));
      await tester.pump(const Duration(milliseconds: 300));
      final selection = Completer<String>();
      audio.heldSelection = selection;
      await tester.tap(find.text('Synthetic monitor').last);
      await tester.pump();
      expect(audio.selectedIds, [_deviceId]);
      expect(
        audio.reads,
        1,
        reason: 'No optimistic refresh before original ACK',
      );
      expect(_dropdown(tester).value, isNull);
      audio.devices = const [
        AudioOutputDevice(
          opaqueId: _deviceId,
          label: 'Synthetic monitor',
          selected: true,
        ),
      ];
      audio.heldSelection = null;
      selection.complete('selected');
      await tester.pump();
      expect(audio.reads, 2);
      expect(_dropdown(tester).value, _deviceId);
      _localOnly(audio, state);
      await _remove(tester, state);
    },
  );

  testWidgets(
    'explicit default awaits its own ACK then refreshes local devices',
    (tester) async {
      final original = Completer<String>();
      final audio = _OutputAudio()..heldSelection = original;
      final state = await _open(tester, audio);
      await _tap(tester, 'default');
      expect(audio.calls, ['default']);
      expect(_sheet(tester).audioOutputBusy, isTrue);
      audio.heldSelection = null;
      original.complete('default');
      await tester.pump();
      expect(audio.calls, ['default', 'discover']);
      expect(_dropdown(tester).value, isNull);
      expect(_sheet(tester).audioOutputBusy, isFalse);
      _localOnly(audio, state);
      await _remove(tester, state);
    },
  );

  testWidgets('selected route monitoring never overlaps a held discovery', (
    tester,
  ) async {
    final audio = _OutputAudio()
      ..devices = const [
        AudioOutputDevice(
          opaqueId: _deviceId,
          label: 'Synthetic monitor',
          selected: true,
        ),
      ];
    final state = await _open(tester, audio);
    await _tap(tester, 'choose');
    expect(audio.reads, 1);
    final original = Completer<List<AudioOutputDevice>>();
    audio.heldDiscovery = original;
    await tester.pump(const Duration(seconds: 1));
    expect(audio.reads, 2);
    await tester.pump(const Duration(seconds: 8));
    expect(audio.reads, 2);
    expect(original.isCompleted, isFalse);
    await _tap(tester, 'refresh');
    expect(audio.reads, 2);
    await _remove(tester, state);
    audio.heldDiscovery = null;
    original.complete(audio.devices);
    await tester.pump(const Duration(seconds: 3));
    expect(audio.reads, 2);
    expect(tester.takeException(), isNull);
  });

  for (final result in ['busy', 'unavailable', 'failed', 'cancelled']) {
    testWidgets(
      '$result choice is visible and never redirects to another sink',
      (tester) async {
        final audio = _OutputAudio()..chooseResult = result;
        final state = await _open(tester, audio);
        await _tap(tester, 'choose');
        _fixedNotice(tester);
        expect(audio.calls, ['choose']);
        expect(audio.defaults, 0);
        expect(audio.selectedIds, isEmpty);
        expect(_sheet(tester).audioOutputBusy, isFalse);
        _localOnly(audio, state);
        await _remove(tester, state);
      },
    );
  }

  testWidgets(
    'active playback refusal preserves original audio and shows notice',
    (tester) async {
      final audio = _OutputAudio()..playing = true;
      final state = await _open(tester, audio);
      final stops = audio.stops;
      await _tap(tester, 'choose');
      _fixedNotice(tester);
      expect(audio.chooses, 1);
      expect(audio.stops, stops);
      expect(audio.defaults, 0);
      expect(audio.reads, 0);
      expect(audio.playing, isTrue);
      _localOnly(audio, state);
      await _remove(tester, state);
    },
  );

  for (final discovery in [false, true]) {
    testWidgets(
      'raw adapter ${discovery ? 'discovery' : 'selection'} error is redacted',
      (tester) async {
        final audio = _OutputAudio()
          ..discoveryThrows = discovery
          ..selectionThrows = !discovery;
        final state = await _open(tester, audio);
        await _tap(tester, discovery ? 'refresh' : 'choose');
        final first = _fixedNotice(tester);
        expect(_sheet(tester).audioOutputBusy, isFalse);
        await _tap(tester, discovery ? 'refresh' : 'choose');
        expect(_fixedNotice(tester), first);
        expect(audio.defaults, 0);
        expect(audio.selectedIds, isEmpty);
        _localOnly(audio, state);
        await _remove(tester, state);
      },
    );
  }

  for (final discovery in [false, true]) {
    testWidgets(
      'unmount fences late original ${discovery ? 'discovery' : 'choice'}',
      (tester) async {
        final heldChoice = Completer<String>();
        final heldRead = Completer<List<AudioOutputDevice>>();
        final audio = _OutputAudio()
          ..heldSelection = discovery ? null : heldChoice
          ..heldDiscovery = discovery ? heldRead : null;
        final state = await _open(tester, audio);
        await _tap(tester, discovery ? 'refresh' : 'choose');
        final calls = List<String>.of(audio.calls);
        await _remove(tester, state);
        if (discovery) {
          heldRead.complete(_devices);
        } else {
          heldChoice.complete('selected');
        }
        await tester.pump(const Duration(seconds: 3));
        expect(
          audio.calls,
          calls,
          reason: 'No follow-up refresh by a disposed owner',
        );
        expect(tester.takeException(), isNull);
        final replacement = _OutputAudio();
        final replacementState = await _open(tester, replacement);
        expect(replacement.calls, isEmpty);
        expect(_sheet(tester).audioOutputBusy, isFalse);
        expect(_sheet(tester).audioOutputs, isEmpty);
        _localOnly(replacement, replacementState);
        await _remove(tester, replacementState);
      },
    );
  }
}
