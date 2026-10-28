import 'storage.dart';

PresentationStorage createPresentationStorage() =>
    _SessionPresentationStorage();

class _SessionPresentationStorage implements PresentationStorage {
  String? _value;
  @override
  bool get persistent => false;
  @override
  Future<String?> read() async => _value;
  @override
  Future<void> write(String value) async {
    _value = value;
  }
}
