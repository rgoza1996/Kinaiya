import 'storage_stub.dart'
    if (dart.library.js_interop) 'storage_web.dart'
    as platform;

/// A single presentation-only snapshot: preferences and optional artwork pack.
abstract class PresentationStorage {
  bool get persistent;
  Future<String?> read();
  Future<void> write(String value);
}

PresentationStorage createPresentationStorage() =>
    platform.createPresentationStorage();
