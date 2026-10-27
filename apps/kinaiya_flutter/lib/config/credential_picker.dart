import 'credential_picker_stub.dart'
    if (dart.library.js_interop) 'credential_picker_web.dart'
    as platform;

Future<String?> chooseCredential() => platform.chooseCredential();
