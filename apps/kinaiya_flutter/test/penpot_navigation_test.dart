import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:kinaiya_flutter/ui/character_view.dart';
import 'package:kinaiya_flutter/ui/agent_chat.dart';
import 'package:kinaiya_flutter/ui/presentation_settings.dart';
import 'package:kinaiya_flutter/ui/model_setup.dart';
import 'package:kinaiya_flutter/ui/motion_transition.dart';

import 'settings_navigation_test.dart' show NavigationFixture, routePump;
import 'model_setup_test.dart' show setupFixture;

class PenpotNavigationFixture extends NavigationFixture {
  @override
  Future<http.Response> respond(http.Request request) async {
    if (request.url.path == '/v1/converse/setup') {
      requests.add('${request.method} ${request.url.path}');
      return http.Response(jsonEncode(setupFixture()), 200);
    }
    return super.respond(request);
  }
}

void main() {
  for (final mode in ['off', 'reduced', 'animated']) {
    testWidgets(
      'navigation $mode fades preserve the mounted voice owner and both drafts',
      (tester) async {
        await tester.binding.setSurfaceSize(const Size(1440, 1000));
        addTearDown(() => tester.binding.setSurfaceSize(null));
        final f = PenpotNavigationFixture();
        await f.mount(tester);
        await f.presentation.setPreferences(
          f.presentation.preferences.copyWith(
            animateTransitions: mode != 'off',
            reducedMotion: mode == 'reduced',
          ),
        );
        await routePump(tester);
        final owner = tester.state(find.byType(CharacterView));
        await tester.tap(find.byKey(const ValueKey('home-talk')));
        await routePump(tester);
        final session = f.state.converseSession;
        await tester.tap(find.byKey(const ValueKey('navigation-converse')));
        await tester.pump();
        final transition = find.descendant(
          of: find.byType(CharacterView),
          matching: find.byType(UiFadeTransition),
        );
        final switcher = find
            .descendant(of: transition, matching: find.byType(AnimatedSwitcher))
            .first;
        expect(
          tester.widget<AnimatedSwitcher>(switcher).duration,
          mode == 'animated'
              ? const Duration(milliseconds: 220)
              : Duration.zero,
        );
        await routePump(tester);
        expect(
          identical(tester.state(find.byType(CharacterView)), owner),
          isTrue,
        );
        expect(identical(f.state.converseSession, session), isTrue);
        expect(f.state.converseDraft, 'Retained conversation draft');
        expect(f.state.agentDraft, 'Retained Agent draft');
        expect(f.audio.arms, 1);
        expect(f.audio.armStops, 0);
        expect(f.audio.disposals, 0);
        expect(f.dispatchedEffect, isFalse);
        expect(tester.takeException(), isNull);
        await f.dispose(tester);
      },
    );
  }

  testWidgets(
    'Home and optional Converse share voice owner and drafts across navigation and hide',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(1440, 1000));
      addTearDown(() => tester.binding.setSurfaceSize(null));
      final f = PenpotNavigationFixture();
      await f.mount(tester);
      expect(find.byKey(const ValueKey('home-scene')), findsOneWidget);
      final owner = tester.state(find.byType(CharacterView));
      final agent = tester.state(find.byType(AgentChat, skipOffstage: false));
      await tester.ensureVisible(find.byKey(const ValueKey('home-talk')));
      await tester.tap(find.byKey(const ValueKey('home-talk')));
      await routePump(tester);
      expect(f.audio.arms, 1);
      final session = f.state.converseSession;
      await tester.tap(find.byKey(const ValueKey('navigation-converse')));
      await routePump(tester);
      expect(
        identical(tester.state(find.byType(CharacterView)), owner),
        isTrue,
      );
      expect(
        find.byKey(const ValueKey('converse-character-canvas')),
        findsOneWidget,
      );
      expect(
        tester
            .widget<TextField>(find.byKey(const ValueKey('converse-input')))
            .controller!
            .text,
        'Retained conversation draft',
      );
      await tester.enterText(
        find.byKey(const ValueKey('converse-input')),
        'Keep this new Converse draft',
      );
      await tester.tap(find.byKey(const ValueKey('navigation-agent')));
      await routePump(tester);
      expect(identical(tester.state(find.byType(AgentChat)), agent), isTrue);
      await tester.tap(find.byKey(const ValueKey('navigation-home')));
      await routePump(tester);
      expect(
        identical(tester.state(find.byType(CharacterView)), owner),
        isTrue,
      );
      await tester.ensureVisible(find.byKey(const ValueKey('home-setup')));
      await tester.tap(find.byKey(const ValueKey('home-setup')));
      await routePump(tester);
      expect(
        find.descendant(
          of: find.byType(ModelSetupDialog),
          matching: find.text('Stop microphone'),
        ),
        findsOneWidget,
      );
      final viewBubble = find.byKey(const ValueKey('setup-bubble-view'));
      await tester.ensureVisible(viewBubble);
      await routePump(tester);
      await tester.tap(viewBubble);
      await routePump(tester);
      final toggle = find.byKey(const ValueKey('show-converse-view'));
      await tester.ensureVisible(toggle);
      await tester.pump();
      expect(toggle.hitTestable(), findsOneWidget);
      await tester.tap(toggle);
      await tester.pump();
      await tester.ensureVisible(
        find.byKey(const ValueKey('save-model-setup')),
      );
      await tester.pump();
      await tester.tap(find.byKey(const ValueKey('save-model-setup')));
      await routePump(tester);
      expect(find.byKey(const ValueKey('navigation-converse')), findsNothing);
      expect(find.byKey(const ValueKey('home-scene')), findsOneWidget);
      expect(
        find.text('Converse view is hidden · Enable it in Setup'),
        findsOneWidget,
      );
      expect(f.state.converseEnabled, isTrue);
      expect(f.audio.arms, 1);
      expect(f.audio.armStops, 0);
      expect(f.audio.disposals, 0);
      expect(identical(f.state.converseSession, session), isTrue);
      expect(f.state.converseDraft, 'Keep this new Converse draft');
      expect(f.state.agentDraft, 'Retained Agent draft');
      expect(f.dispatchedEffect, isFalse);
      await tester.tap(find.byTooltip('Appearance'));
      await routePump(tester);
      expect(find.byType(PresentationSettings), findsOneWidget);
      await tester.tap(find.byTooltip('Close options'));
      await routePump(tester);
      expect(f.audio.arms, 1);
      expect(f.audio.armStops, 0);
      expect(tester.takeException(), isNull);
      await f.dispose(tester);
    },
  );
}
