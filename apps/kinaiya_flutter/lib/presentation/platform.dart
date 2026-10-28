import 'platform_stub.dart'
    if (dart.library.js_interop) 'platform_web.dart'
    as platform;

Future<String?> pickCharacterPackage() => platform.pickCharacterPackage();
Future<void> downloadCharacterPackage(String raw) =>
    platform.downloadCharacterPackage(raw);
bool get presentationImportSupported => platform.presentationImportSupported;
