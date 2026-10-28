import 'dart:js_interop';
import 'storage.dart';

@JS('kinaiyaPresentationRead')
external JSPromise<JSString> _read();
@JS('kinaiyaPresentationWrite')
external JSPromise<JSAny?> _write(JSString value);

PresentationStorage createPresentationStorage() =>
    _BrowserPresentationStorage();

class _BrowserPresentationStorage implements PresentationStorage {
  @override
  bool get persistent => true;
  @override
  Future<String?> read() async {
    final value = (await _read().toDart).toDart;
    return value.isEmpty ? null : value;
  }

  @override
  Future<void> write(String value) async {
    await _write(value.toJS).toDart;
  }
}
