/// Bounded, presentation-only character packages. Assets never escape memory.
library;

import 'dart:convert';
import 'dart:ui' as ui;

import 'package:crypto/crypto.dart';
import 'package:flutter/services.dart';

import 'pack.dart';

const int maxCharacterPackageBytes = 12 << 20;
const int _maxFiles = 12;
const int _maxFileBytes = 8 << 20;
const int _maxManifestBytes = 64 << 10;
const int _maxPromptBytes = 64 << 10;
const int _maxPngPixels = 24 * 1024 * 1024;

class CharacterPackageException implements Exception {
  const CharacterPackageException();
  String get message => 'Character package is invalid or exceeds local limits.';
  @override
  String toString() => message;
}

/// Only this library can create an activatable selection, after validation.
final class CharacterSelection {
  CharacterSelection._(this.pack, this.bundle, this.packageJson, this._export);
  final CharacterPack pack;
  final AssetBundle bundle;
  final String? packageJson;
  final String? _export;
}

/// Owns copies on entry and return. Missing keys never fall through to an
/// application asset, filesystem, URL, or another bundle.
class _MemoryBundle extends AssetBundle {
  _MemoryBundle(Map<String, Uint8List> files)
    : _files = Map.unmodifiable({
        for (final entry in files.entries)
          entry.key: Uint8List.fromList(entry.value),
      });
  final Map<String, Uint8List> _files;
  @override
  Future<ByteData> load(String key) async {
    final bytes = _files[key];
    if (bytes == null) throw const CharacterPackageException();
    return ByteData.sublistView(Uint8List.fromList(bytes));
  }
}

Never _invalid() => throw const CharacterPackageException();

/// Reject ambiguous keys and excessive structure before jsonDecode allocates a
/// tree. File strings remain bounded by the package byte limit. Valid manifests
/// need fewer than ten nesting levels and no array longer than 64 entries.
class _JsonGuard {
  _JsonGuard(this.raw);
  final String raw;
  int offset = 0;

  void _space() {
    while (offset < raw.length &&
        const [9, 10, 13, 32].contains(raw.codeUnitAt(offset))) {
      offset++;
    }
  }

  String _string({bool key = false}) {
    final start = offset++;
    while (offset < raw.length) {
      final char = raw.codeUnitAt(offset++);
      if (char == 92) {
        offset++;
      } else if (char == 34) {
        if (!key) return '';
        if (offset - start > 2048) _invalid();
        return jsonDecode(raw.substring(start, offset)) as String;
      }
    }
    return _invalid();
  }

  void value([int depth = 0]) {
    if (depth > 16) _invalid();
    _space();
    if (offset >= raw.length) _invalid();
    final char = raw.codeUnitAt(offset);
    if (char == 34) {
      _string();
      return;
    }
    if (char == 123 || char == 91) {
      final object = char == 123;
      final end = object ? 125 : 93;
      final keys = <String>{};
      var count = 0;
      offset++;
      _space();
      if (offset < raw.length && raw.codeUnitAt(offset) == end) {
        offset++;
        return;
      }
      while (offset < raw.length) {
        if (++count > 256) _invalid();
        _space();
        if (object) {
          if (raw.codeUnitAt(offset) != 34 || !keys.add(_string(key: true))) {
            _invalid();
          }
          _space();
          if (offset >= raw.length || raw.codeUnitAt(offset++) != 58) {
            _invalid();
          }
        }
        value(depth + 1);
        _space();
        if (offset >= raw.length) _invalid();
        final next = raw.codeUnitAt(offset++);
        if (next == end) return;
        if (next != 44) _invalid();
      }
      _invalid();
    }
    final start = offset;
    while (offset < raw.length &&
        !const [9, 10, 13, 32, 44, 93, 125].contains(raw.codeUnitAt(offset))) {
      offset++;
    }
    if (offset == start) _invalid();
  }

  void check() {
    value();
    _space();
    if (offset != raw.length) _invalid();
  }
}

Object? _canonical(Object? value) {
  if (value is Map<String, dynamic>) {
    final keys = value.keys.toList()..sort();
    return {for (final key in keys) key: _canonical(value[key])};
  }
  if (value is List) return value.map(_canonical).toList();
  return value;
}

Map<String, dynamic> _object(Object? value, Set<String> keys) {
  if (value is! Map<String, dynamic> ||
      value.keys.any((key) => !keys.contains(key))) {
    _invalid();
  }
  return value;
}

void _manifestShape(Map<String, dynamic> manifest) {
  _object(manifest, {
    'pack_schema',
    'pack_id',
    'version',
    'name',
    'author',
    'license',
    'provenance',
    'theme',
    'voice_profile',
    'fallback_animation',
    'anchor',
    'scale_limits',
    'sheets',
    'animations',
  });
  if (manifest['pack_schema'] != 2 || manifest['pack_schema'] is! int) {
    _invalid();
  }
  _object(manifest['provenance'], {
    'source_asset',
    'source_sha256',
    'generator',
    'prompt_file',
  });
  _object(manifest['theme'], {'primary', 'background'});
  final sheets = manifest['sheets'];
  final animations = manifest['animations'];
  if (sheets is! List ||
      sheets.isEmpty ||
      sheets.length > 8 ||
      animations is! Map<String, dynamic> ||
      animations.length > 16) {
    _invalid();
  }
  for (final sheet in sheets) {
    _object(sheet, {
      'id',
      'asset',
      'sha256',
      'width',
      'height',
      'frame_width',
      'frame_height',
    });
  }
  for (final entry in animations.entries) {
    if (entry.key.isEmpty || entry.key.length > 128) _invalid();
    _object(entry.value, {'sheet', 'frames', 'frame_ms', 'loop'});
  }
  final generator = (manifest['provenance'] as Map)['generator'];
  if (generator is! String || generator.length > 1024) _invalid();
}

int _u32(Uint8List bytes, int offset) =>
    ByteData.sublistView(bytes).getUint32(offset);

int _crc32(Uint8List bytes, int start, int end) {
  var crc = 0xffffffff;
  for (var i = start; i < end; i++) {
    crc ^= bytes[i];
    for (var bit = 0; bit < 8; bit++) {
      crc = (crc & 1) == 1 ? (crc >> 1) ^ 0xedb88320 : crc >> 1;
    }
  }
  return (crc ^ 0xffffffff) & 0xffffffff;
}

/// Validate all PNG structure, CRCs and allocation budgets before any codec is
/// instantiated. APNG chunks are rejected even when a codec ignores animation.
List<int> _pngHeader(Uint8List bytes) {
  final dimensions = pngDimensions(bytes);
  if (dimensions == null || dimensions.any((edge) => edge > 4096)) _invalid();
  var offset = 8;
  var dataSeen = false;
  var ended = false;
  while (offset < bytes.length) {
    if (offset + 12 > bytes.length) _invalid();
    final length = _u32(bytes, offset);
    final end = offset + 12 + length;
    if (end > bytes.length) _invalid();
    final type = String.fromCharCodes(bytes.sublist(offset + 4, offset + 8));
    if (!RegExp(r'^[A-Za-z]{4}$').hasMatch(type) ||
        type == 'acTL' ||
        type == 'fcTL' ||
        type == 'fdAT' ||
        (type == 'IHDR' && offset != 8)) {
      _invalid();
    }
    if (_crc32(bytes, offset + 4, end - 4) != _u32(bytes, end - 4)) {
      _invalid();
    }
    if (type == 'IDAT') dataSeen = true;
    if (type == 'IEND') {
      if (length != 0 || end != bytes.length) _invalid();
      ended = true;
    }
    offset = end;
  }
  if (!dataSeen || !ended) _invalid();
  return dimensions;
}

Future<void> _decodePng(Uint8List bytes, List<int> dimensions) async {
  ui.Codec? codec;
  ui.Image? image;
  try {
    codec = await ui.instantiateImageCodec(bytes);
    if (codec.frameCount != 1) _invalid();
    image = (await codec.getNextFrame()).image;
    if (image.width != dimensions[0] || image.height != dimensions[1]) {
      _invalid();
    }
  } finally {
    image?.dispose();
    codec?.dispose();
  }
}

CharacterPack _freeze(CharacterPack pack) => CharacterPack(
  version: pack.version,
  name: pack.name,
  fallbackAsset: pack.fallbackAsset,
  poses: List.unmodifiable(pack.poses),
  fallback: pack.fallback,
  note: pack.note,
  packId: pack.packId,
  packVersion: pack.packVersion,
  author: pack.author,
  license: pack.license,
  provenance: pack.provenance,
  themePrimary: pack.themePrimary,
  themeBackground: pack.themeBackground,
  anchorX: pack.anchorX,
  anchorY: pack.anchorY,
  scaleMin: pack.scaleMin,
  scaleMax: pack.scaleMax,
  sheets: Map.unmodifiable(pack.sheets),
  animations: Map.unmodifiable(pack.animations),
  fallbackAnimation: pack.fallbackAnimation,
);

/// Fully validates and decodes a self-contained v2 package without activating it.
Future<CharacterSelection> inspectCharacterPackage(String raw) async {
  try {
    if (raw.length > maxCharacterPackageBytes ||
        utf8.encode(raw).length > maxCharacterPackageBytes) {
      _invalid();
    }
    _JsonGuard(raw).check();
    final envelope = _object(jsonDecode(raw), {
      'package_schema',
      'manifest',
      'files',
    });
    if (envelope.length != 3 ||
        envelope['package_schema'] is! int ||
        envelope['package_schema'] != 1) {
      _invalid();
    }
    final manifest = _object(envelope['manifest'], {
      'pack_schema',
      'pack_id',
      'version',
      'name',
      'author',
      'license',
      'provenance',
      'theme',
      'voice_profile',
      'fallback_animation',
      'anchor',
      'scale_limits',
      'sheets',
      'animations',
    });
    final manifestJson = jsonEncode(_canonical(manifest));
    if (utf8.encode(manifestJson).length > _maxManifestBytes) _invalid();
    _manifestShape(manifest);
    final entries = envelope['files'];
    if (entries is! List || entries.isEmpty || entries.length > _maxFiles) {
      _invalid();
    }
    final files = <String, Uint8List>{};
    var totalBytes = 0;
    for (final value in entries) {
      final entry = _object(value, {'path', 'base64'});
      final path = entry['path'];
      final encoded = entry['base64'];
      if (entry.length != 2 ||
          path is! String ||
          !isStrictAssetPath(path) ||
          files.containsKey(path) ||
          encoded is! String ||
          encoded.length > ((_maxFileBytes + 2) ~/ 3) * 4 ||
          !RegExp(
            r'^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$',
          ).hasMatch(encoded)) {
        _invalid();
      }
      final bytes = base64Decode(encoded);
      if (base64Encode(bytes) != encoded) _invalid();
      totalBytes += bytes.length;
      if (totalBytes > _maxFileBytes) _invalid();
      files[path] = bytes;
    }
    final provenance = manifest['provenance'] as Map<String, dynamic>;
    final source = provenance['source_asset'];
    final prompt = provenance['prompt_file'];
    if (source is! String ||
        !source.endsWith('.png') ||
        prompt is! String ||
        !isStrictAssetPath(source) ||
        !isStrictAssetPath(prompt)) {
      _invalid();
    }
    final imagePaths = <String>{source};
    for (final value in manifest['sheets'] as List) {
      final asset = (value as Map)['asset'];
      if (asset is! String || !asset.endsWith('.png')) _invalid();
      imagePaths.add(asset);
    }
    final expected = <String>{...imagePaths, prompt};
    if (files.length != expected.length ||
        !files.keys.toSet().containsAll(expected)) {
      _invalid();
    }
    final promptBytes = files[prompt]!;
    if (promptBytes.length > _maxPromptBytes) _invalid();
    utf8.decode(promptBytes, allowMalformed: false);
    if (sha256.convert(files[source]!).toString() !=
        (provenance['source_sha256'] as String?)?.toLowerCase()) {
      _invalid();
    }
    var pixels = 0;
    final dimensions = <String, List<int>>{};
    for (final path in imagePaths) {
      final size = _pngHeader(files[path]!);
      pixels += size[0] * size[1];
      if (pixels > _maxPngPixels) _invalid();
      dimensions[path] = size;
    }
    final bundle = _MemoryBundle(files);
    final pack = await loadCharacterPack(
      bundle: bundle,
      manifestJson: manifestJson,
    );
    if (pack.fallback || !pack.isSpritesheet) _invalid();
    for (final path in imagePaths) {
      await _decodePng(files[path]!, dimensions[path]!);
    }
    final paths = files.keys.toList()..sort();
    final canonical = jsonEncode({
      'package_schema': 1,
      'manifest': _canonical(manifest),
      'files': [
        for (final path in paths)
          {'path': path, 'base64': base64Encode(files[path]!)},
      ],
    });
    return CharacterSelection._(_freeze(pack), bundle, canonical, canonical);
  } catch (_) {
    // Never surface JSON, paths, engine details or third-party metadata as an
    // error. This is also the boundary for decoder/parser failures.
    throw const CharacterPackageException();
  }
}

/// Reads only the trusted, application-declared default assets, then applies
/// the same checks as an import. A broken bundled default retains static art.
Future<CharacterSelection> loadDefaultCharacterSelection(
  AssetBundle bundle,
) async {
  try {
    final raw = await bundle.loadString(packAssetPath);
    if (raw.length > _maxManifestBytes ||
        utf8.encode(raw).length > _maxManifestBytes) {
      _invalid();
    }
    final manifest = jsonDecode(raw) as Map<String, dynamic>;
    _manifestShape(manifest);
    final provenance = manifest['provenance'] as Map<String, dynamic>;
    final paths = <String>{
      provenance['source_asset'] as String,
      provenance['prompt_file'] as String,
      for (final sheet in manifest['sheets'] as List)
        (sheet as Map)['asset'] as String,
    };
    if (paths.any((path) => !isStrictAssetPath(path)) ||
        paths.length > _maxFiles) {
      _invalid();
    }
    final files = <Map<String, String>>[];
    var totalBytes = 0;
    for (final path in paths) {
      final data = await bundle.load(path);
      totalBytes += data.lengthInBytes;
      if (totalBytes > _maxFileBytes) _invalid();
      final bytes = data.buffer.asUint8List(
        data.offsetInBytes,
        data.lengthInBytes,
      );
      files.add({'path': path, 'base64': base64Encode(bytes)});
    }
    final validated = await inspectCharacterPackage(
      jsonEncode({'package_schema': 1, 'manifest': manifest, 'files': files}),
    );
    return CharacterSelection._(
      validated.pack,
      validated.bundle,
      null,
      validated._export,
    );
  } catch (_) {
    return CharacterSelection._(
      _freeze(
        CharacterPack(
          version: 1,
          name: 'Kinaiya',
          fallbackAsset: 'assets/mascot/kinaiya-mascot.png',
          poses: const [],
          fallback: true,
          note: 'Default character unavailable; static artwork is active.',
        ),
      ),
      bundle,
      null,
      null,
    );
  }
}

String exportCharacterSelection(CharacterSelection selection) =>
    selection._export ?? (throw const CharacterPackageException());
