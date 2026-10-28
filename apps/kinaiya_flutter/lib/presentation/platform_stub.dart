bool get presentationImportSupported => false;
Future<String?> pickCharacterPackage() async => null;
Future<void> downloadCharacterPackage(String raw) async {
  throw UnsupportedError(
    'Character file export is unavailable on this platform.',
  );
}
