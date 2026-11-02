import 'dart:io';

import 'transport_problem.dart';

TransportProblem classify(Object error) {
  // HandshakeException (certificate verification) is a TlsException.
  if (error is TlsException) return TransportProblem.certificate;
  // package:http's IOClient rethrows socket failures as a ClientException that
  // also implements SocketException.
  if (error is SocketException) return TransportProblem.offline;
  return TransportProblem.unknown;
}
