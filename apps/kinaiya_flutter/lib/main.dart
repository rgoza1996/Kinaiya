import 'package:flutter/material.dart';

import 'api/client.dart';
import 'app.dart';
import 'config/connection.dart';
import 'state/app_state.dart';

void main() {
  final state = AppState();
  // Real HTTP transport. Tests inject a labeled mock via connect(injected:).
  state.clientFactory = (ConnectionProfile profile) => KinaiyaClient(
    sessionBase: profile.sessionUrl,
    agentBase: profile.agentUrl,
    converseBase: profile.converseUrl,
    decisionsBase: profile.decisionsUrl,
    ownerToken: profile.ownerToken,
  );
  runApp(KinaiyaApp(state: state));
}
