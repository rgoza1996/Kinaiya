import 'transport_problem.dart';

/// Browsers do not expose TLS or socket failure details to Dart.
TransportProblem classify(Object error) => TransportProblem.unknown;
