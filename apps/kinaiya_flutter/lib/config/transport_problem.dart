/// Classifies transport failures without reflecting raw error text.
///
/// Distinguishes an untrusted HTTPS certificate from an unreachable host so the
/// user gets actionable guidance. There is never a "trust anyway" path.
library;

import 'transport_problem_stub.dart'
    if (dart.library.io) 'transport_problem_io.dart'
    as impl;

enum TransportProblem { certificate, offline, unknown }

TransportProblem classifyTransportError(Object error) => impl.classify(error);

String transportProblemMessage(TransportProblem problem) => switch (problem) {
  TransportProblem.certificate =>
    'This device does not trust the host HTTPS certificate. Check the address and the host certificate; Kinaiya never bypasses certificate checks.',
  TransportProblem.offline =>
    'Host unreachable. Check the network and the host address, then retry.',
  TransportProblem.unknown =>
    'Host unavailable. Check the connection and retry.',
};
