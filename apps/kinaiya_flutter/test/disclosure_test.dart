import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';

void main() {
  test(
    'reported cloud inference requires matching client disclosure',
    () async {
      final state = AppState();
      state.clientFactory = (profile) => KinaiyaClient(
        sessionBase: profile.sessionUrl,
        agentBase: profile.agentUrl,
        converseBase: profile.converseUrl,
        ownerToken: 'synthetic-token',
        httpClient: MockClient((request) async {
          if (request.url.path == '/v1/sessions') {
            return http.Response('[]', 200);
          }
          if (request.url.path == '/v1/agents/capabilities') {
            return http.Response('{}', 200);
          }
          return http.Response(
            jsonEncode({
              'enabled': true,
              'provider': {
                'provider': 'meta',
                'model': 'muse-spark-1.3-contributor',
                'local': false,
              },
            }),
            200,
          );
        }),
      );
      state.connect(ConnectionProfile.defaults());
      await Future<void>.delayed(const Duration(milliseconds: 20));
      expect(state.connected, isFalse);
      expect(state.connectionError, contains('Cloud inference'));
      final defaults = ConnectionProfile.defaults();
      state.connect(
        ConnectionProfile(
          sessionUrl: defaults.sessionUrl,
          agentUrl: defaults.agentUrl,
          converseUrl: defaults.converseUrl,
          ownerToken: 'synthetic-token',
          backendProfile: BackendProfile.metaContributor,
        ),
      );
      await Future<void>.delayed(const Duration(milliseconds: 20));
      expect(state.connected, isTrue);
      expect(state.agentOnline, isTrue);
      state.dispose();
    },
  );
}
