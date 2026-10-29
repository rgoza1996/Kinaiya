/// Presentation state has no access to service configuration or runtime state.
library;

import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import '../character/package.dart';
import 'preferences.dart';
import 'storage.dart';

export '../character/package.dart'
    show CharacterSelection, CharacterPackageException;
export 'preferences.dart';
export 'storage.dart' show PresentationStorage;

class PresentationController extends ChangeNotifier {
  PresentationController({
    PresentationStorage? storage,
    AssetBundle? defaultBundle,
  }) : _storage = storage ?? createPresentationStorage(),
       _defaultBundle = defaultBundle ?? rootBundle;

  final PresentationStorage _storage;
  final AssetBundle _defaultBundle;
  PresentationPreferences _preferences = const PresentationPreferences();
  CharacterSelection? _selected;
  bool _loading = true;
  bool _disposed = false;
  int _revision = 0;
  String? _notice;
  Future<void>? _initialization;
  Future<CharacterSelection>? _defaultSelection;
  Future<void> _writes = Future<void>.value();

  PresentationPreferences get preferences => _preferences;
  CharacterSelection? get selected => _selected;
  bool get loading => _loading;
  bool get persistent => _storage.persistent;
  String? get notice => _notice;

  Future<CharacterSelection> _default() =>
      _defaultSelection ??= loadDefaultCharacterSelection(_defaultBundle);

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  Future<void> initialize() => _initialization ??= _initialize();

  Future<void> _initialize() async {
    final revision = _revision;
    // Start default loading immediately, also when browser storage is denied.
    final defaultFuture = _default();
    // The controller can be configured before an app starts initialization.
    // Such actions have the same precedence as actions during a restore.
    if (revision != 0) {
      final fallback = await defaultFuture;
      if (!_disposed && revision == _revision) {
        _selected ??= fallback;
        _loading = false;
        _notify();
      }
      return;
    }
    CharacterSelection? restored;
    var preferences = const PresentationPreferences();
    String? notice;
    try {
      final raw = await _storage.read();
      if (raw != null) {
        if (raw.length > (13 << 20) || utf8.encode(raw).length > (13 << 20)) {
          throw const FormatException();
        }
        final state = jsonDecode(raw);
        if (state is! Map<String, dynamic> ||
            state.length != 3 ||
            !state.keys.toSet().containsAll({
              'state_schema',
              'preferences',
              'package',
            }) ||
            state['state_schema'] is! int ||
            state['state_schema'] != 1 ||
            (state['package'] != null && state['package'] is! String)) {
          throw const FormatException();
        }
        preferences = PresentationPreferences.fromJson(state['preferences']);
        if (state['package'] != null) {
          restored = await inspectCharacterPackage(state['package'] as String);
        }
      }
    } catch (_) {
      preferences = const PresentationPreferences();
      notice =
          'Saved presentation could not be restored; the default is available.';
    }
    final fallback = await defaultFuture;
    if (_disposed || revision != _revision) return;
    _selected = restored ?? fallback;
    _preferences = preferences;
    _loading = false;
    _notice =
        notice ??
        (_selected!.pack.fallback
            ? 'Default character unavailable; static artwork is active.'
            : !persistent
            ? 'Presentation changes are kept for this session on this platform.'
            : null);
    _notify();
  }

  Future<CharacterSelection> inspectPackage(String raw) =>
      inspectCharacterPackage(raw);

  Future<void> useCharacter(CharacterSelection selection) async {
    if (_disposed) return;
    final revision = ++_revision;
    _selected = selection;
    _loading = false;
    _notice = persistent
        ? null
        : 'Presentation changes are kept for this session on this platform.';
    _notify();
    await _persist(revision);
  }

  Future<void> useDefault() async {
    if (_disposed) return;
    final revision = ++_revision;
    final selection = await _default();
    if (_disposed || revision != _revision) return;
    _selected = selection;
    _loading = false;
    _notice = selection.pack.fallback
        ? 'Default character unavailable; static artwork is active.'
        : !persistent
        ? 'Presentation changes are kept for this session on this platform.'
        : null;
    _notify();
    await _persist(revision);
  }

  Future<void> setPreferences(PresentationPreferences preferences) async {
    if (_disposed) return;
    if (!preferences.valid) {
      throw const FormatException('Use a color in #RRGGBB format.');
    }
    final revision = ++_revision;
    _preferences = preferences;
    _notice = persistent
        ? null
        : 'Presentation changes are kept for this session on this platform.';
    // A preference change during first load still obtains the default, while
    // preventing a delayed restore from replacing the user's chosen settings.
    final defaultSelection = _selected == null ? await _default() : null;
    if (_disposed || revision != _revision) return;
    _selected ??= defaultSelection;
    _loading = false;
    _notify();
    await _persist(revision);
  }

  Future<void> _persist(int revision) {
    final value = jsonEncode({
      'state_schema': 1,
      'preferences': _preferences.toJson(),
      'package': _selected?.packageJson,
    });
    // Writes run in order. A queued obsolete snapshot is skipped; an in-flight
    // write completes before the current snapshot, so it cannot win afterward.
    _writes = _writes.then((_) async {
      if (_disposed || revision != _revision) return;
      try {
        await _storage.write(value);
      } catch (_) {
        if (!_disposed && revision == _revision) {
          _notice =
              'Presentation is active for this session, but saving locally is unconfirmed.';
          _notify();
        }
      }
    });
    return _writes;
  }

  Future<String> exportPackage() async {
    final selection = _selected ?? await _default();
    return exportCharacterSelection(selection);
  }

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }
}
