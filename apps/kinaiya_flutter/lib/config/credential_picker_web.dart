import 'dart:js_interop';

@JS('kinaiyaChooseCredential')
external JSPromise<JSString> _chooseCredential();

@JS('kinaiyaExportCredential')
external void _exportCredential(JSString filename, JSString content);

Future<String?> chooseCredential() async {
  final value = (await _chooseCredential().toDart).toDart;
  return value.isEmpty ? null : value;
}

Future<void> exportCredential(String filename, String content) async {
  _exportCredential(filename.toJS, content.toJS);
}

@JS('kinaiyaDeviceProof')
external JSPromise<JSString> _deviceProof(JSString? value);

Future<String?> readDeviceProof() async {
  try {
    final proof = (await _deviceProof(null).toDart).toDart;
    return proof.isEmpty ? null : proof;
  } catch (_) {
    return null;
  }
}

Future<void> rememberDeviceProof(String proof) async {
  try {
    await _deviceProof(proof.toJS).toDart;
  } catch (_) {
    // A session issued by the host remains usable if pairing storage fails.
  }
}
