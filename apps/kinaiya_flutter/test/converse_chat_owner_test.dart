import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';
import 'package:kinaiya_flutter/ui/settings_controls.dart';

import 'converse_stage_test.dart' show StageAudio;

void main() {
  testWidgets('actual chat keeps close above keyboard at 320px and 2x text', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(320, 480);
    tester.view.devicePixelRatio = 1;
    tester.platformDispatcher.textScaleFactorTestValue = 2;
    tester.view.viewInsets = const FakeViewPadding(bottom: 320);
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
    addTearDown(tester.view.resetViewInsets);
    final state = AppState();
    final controls = ConverseViewControls();
    addTearDown(() async {
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    });
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: CharacterView(
            state: state,
            controls: controls,
            audio: StageAudio(),
            useDesignScene: true,
          ),
        ),
      ),
    );
    await tester.pump(const Duration(milliseconds: 200));
    final opened = controls.openTextConversation!();
    await tester.pump(const Duration(milliseconds: 300));
    final closeChat = find.byTooltip('Close Converse chat');
    expect(tester.getRect(closeChat).bottom, lessThanOrEqualTo(160));
    expect(closeChat.hitTestable(), findsOneWidget);
    expect(state.converseDraft, isEmpty);
    expect(tester.takeException(), isNull);
    await tester.tap(closeChat);
    await tester.pump(const Duration(milliseconds: 300));
    await opened;
    expect(find.byKey(const ValueKey('converse-input')), findsNothing);
    expect(state.converseDraft, isEmpty);
    expect(tester.takeException(), isNull);
  });

  for (final replacement in ['owner', 'controls']) {
    testWidgets('open chat loses authority after $replacement replacement', (
      tester,
    ) async {
      final first = AppState();
      final second = AppState();
      final firstControls = ConverseViewControls();
      final secondControls = ConverseViewControls();
      var owner = first;
      var controls = firstControls;
      final revision = ValueNotifier(0);
      addTearDown(() async {
        await tester.pumpWidget(const SizedBox.shrink());
        first.dispose();
        second.dispose();
        revision.dispose();
      });
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: ValueListenableBuilder<int>(
              valueListenable: revision,
              builder: (_, _, _) => CharacterView(
                state: owner,
                controls: controls,
                audio: StageAudio(),
                useDesignScene: true,
              ),
            ),
          ),
        ),
      );
      await tester.pump(const Duration(milliseconds: 200));
      final staleOpen = firstControls.openTextConversation!;
      final opened = staleOpen();
      await tester.pump(const Duration(milliseconds: 300));
      final input = find.byKey(const ValueKey('converse-input'));
      await tester.enterText(input, 'Original owner draft');
      // Settle the old owner's notification before replacing the pane. A
      // pending notification would rebuild the route and hide this regression.
      await tester.pump();
      final oldInput = tester.widget<TextField>(input);
      expect(first.converseDraft, 'Original owner draft');
      if (replacement == 'owner') {
        owner = second;
      } else {
        controls = secondControls;
      }
      revision.value++;
      await tester.pump();
      oldInput.onChanged!('Stale route must not edit either owner');
      oldInput.onSubmitted!('Stale route must not send');
      await tester.pump(const Duration(milliseconds: 300));
      await opened;
      expect(first.converseDraft, 'Original owner draft');
      expect(second.converseDraft, isEmpty);
      expect(first.converseHistory, isEmpty);
      expect(second.converseHistory, isEmpty);
      expect(find.text('Converse chat'), findsNothing);
      expect(find.byKey(const ValueKey('converse-input')), findsNothing);
      await staleOpen();
      await tester.pump();
      expect(find.text('Converse chat'), findsNothing);
      expect(tester.takeException(), isNull);
    });
  }
}
