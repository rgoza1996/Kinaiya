import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/character/pack.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';

import 'client_test.dart' show mockClient;

void main() {
  test('connection rejects insecure remote http, allows loopback', () {
    expect(
      ConnectionProfile.validateServiceUrl('http://127.0.0.1:8760'),
      isNull,
    );
    expect(
      ConnectionProfile.validateServiceUrl('http://localhost:8760'),
      isNull,
    );
    expect(ConnectionProfile.validateServiceUrl('https://example.com'), isNull);
    expect(
      ConnectionProfile.validateServiceUrl('http://example.com'),
      contains('insecure'),
    );
    expect(ConnectionProfile.validateServiceUrl('not a url'), isNotNull);
  });

  test(
    'drafts, sessions and history survive view change and reconnect',
    () async {
      final state = AppState();
      state.connect(
        ConnectionProfile(
          sessionUrl: 'http://127.0.0.1:8760',
          agentUrl: 'http://127.0.0.1:8761',
          converseUrl: 'http://127.0.0.1:8762',
          ownerToken: 'test-token',
        ),
        injected: mockClient(),
      );
      state.setDraft(AppView.character, 'hello kinaiya');
      state.setDraft(AppView.agent, 'build proof');
      state.appendHistory(AppView.character, ChatMsg(role: 'user', text: 'hi'));
      await state.ensureSession('converse', name: 'Kinaiya');
      final firstId = state.converseSession!.sessionId;

      state.setView(AppView.agent);
      expect(state.converseDraft, 'hello kinaiya');
      state.setView(AppView.character);
      expect(state.agentDraft, 'build proof');

      // Reconnect reuses the named session; history and drafts persist.
      state.disconnect();
      expect(state.converseHistory, isNotEmpty);
      expect(state.converseDraft, 'hello kinaiya');
      state.connect(state.connection!, injected: mockClient());
      await state.ensureSession('converse', name: 'Kinaiya');
      expect(state.converseSession!.sessionId, firstId);
    },
  );

  test('stale audio is dropped by nonce and epoch', () {
    final state = AppState();
    state.beginTurn(3);
    final liveNonce = state.turnNonce;
    expect(state.audioIsLive(liveNonce, 3), isTrue);
    state.beginTurn(4); // barge-in starts a new turn.
    expect(state.audioIsLive(liveNonce, 3), isFalse);
    expect(state.audioIsLive(liveNonce, 4), isFalse);
    expect(state.audioIsLive(state.turnNonce, 4), isTrue);
  });

  test('pack motion is deterministic per state', () {
    expect(
      CharacterPack.motionPhase('speaking'),
      CharacterPack.motionPhase('speaking'),
    );
    expect(
      CharacterPack.motionPhase('speaking'),
      isNot(CharacterPack.motionPhase('idle')),
    );
  });

  test('invalid pack falls back to single frame', () async {
    TestWidgetsFlutterBinding.ensureInitialized();
    final pack = await loadCharacterPack(bounds: const PackBounds(maxPoses: 0));
    expect(pack.fallback, isTrue);
    expect(pack.note, isNotEmpty);
    expect(pack.fallbackAsset, contains('kinaiya-mascot.png'));
  });
}
