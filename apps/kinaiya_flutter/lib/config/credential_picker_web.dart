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
