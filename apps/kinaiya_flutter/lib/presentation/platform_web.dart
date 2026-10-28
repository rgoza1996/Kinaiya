import 'dart:js_interop';

@JS('kinaiyaPickCharacterPackage')
external JSPromise<JSString> _pick();
@JS('kinaiyaDownloadCharacterPackage')
external JSPromise<JSAny?> _download(JSString value);

bool get presentationImportSupported => true;
Future<String?> pickCharacterPackage() async {
  final value = (await _pick().toDart).toDart;
  return value.isEmpty ? null : value;
}

Future<void> downloadCharacterPackage(String raw) async {
  await _download(raw.toJS).toDart;
}
