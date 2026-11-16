import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

int _fixtureBackend = 0;

/// Explicit synthetic settings transport for existing capture-owner fixtures.
/// The opaque backend is unique per owner; these tests never contact a host.
http.Client Function() captureModeFixtureFactory(String mode) {
  final native =
      jsonDecode(
            File('test/fixtures/capture_modes_native.json').readAsStringSync(),
          )
          as Map<String, Object?>;
  final row = (native['modes'] as List).cast<Map<String, Object?>>().firstWhere(
    (row) => (row['after'] as Map)['selected_mode'] == mode,
  );
  final snapshot = <String, Object?>{
    ...row['after'] as Map<String, Object?>,
    'backend_id': (++_fixtureBackend).toRadixString(16).padLeft(32, '0'),
  };
  return () => MockClient((request) async {
    if (request.method != 'GET' ||
        request.url.path != '/v1/converse/options/capture-mode') {
      throw StateError(
        'Synthetic capture fixture only supports explicit settings inspection',
      );
    }
    return http.Response(
      jsonEncode(snapshot),
      200,
      headers: {'content-type': 'application/json'},
    );
  });
}
