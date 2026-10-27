import 'dart:js_interop';

@JS('kinaiyaChooseCredential')
external JSPromise<JSString> _chooseCredential();

Future<String?> chooseCredential() async {
  final value = (await _chooseCredential().toDart).toDart;
  return value.isEmpty ? null : value;
}
