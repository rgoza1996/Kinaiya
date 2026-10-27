/// Versioned character pack manifest and validator.
///
/// Honest baseline: the single shipped artwork frame plus deterministic
/// state-based motion. A spritesheet pack (pack_schema 2) adds real
/// body/mouth frames; missing or invalid packs fall back to the single
/// frame with a visible note, never a silent substitution or a crash.
///
/// v1 manifests (`pack_schema: 1` with `poses`) remain accepted.
/// v2 manifests (`pack_schema: 2` with `sheets`/`animations`) are validated
/// strictly: exact types (no string coercion), finite numeric ranges,
/// strict bundled-asset paths, SHA-256, PNG header dimensions and rectangle
/// bounds checked BEFORE decoding, plus texture/memory/count budgets.
/// Images are untrusted: decoding is bounded and decoded dimensions are
/// re-checked; codecs/images are disposed and stale async completions are
/// dropped by the renderer.
library;

import 'dart:convert';
import 'dart:math' as math;
import 'dart:typed_data';
import 'dart:ui' show Rect;

import 'package:crypto/crypto.dart';
import 'package:flutter/services.dart' show AssetBundle, rootBundle;

const String packAssetPath = 'assets/character/pack.json';

/// v1 schema (poses) and v2 schema (sheets + animations).
const int packSchemaVersion = 1;
const int packSchemaV2 = 2;

/// Shared budgets. v2 limits come from the fixed lead contract.
class PackBounds {
  const PackBounds({
    this.maxImageBytes = 8 << 20,
    this.maxPoses = 24,
    this.maxJsonBytes = 64 << 10,
    this.maxSheets = 8,
    this.maxAnimations = 16,
    this.maxFramesPerAnimation = 64,
    this.maxFramesTotal = 256,
    this.maxTextureEdge = 4096,
    this.maxTotalPixels = 16 * 1024 * 1024,
    this.maxTotalBytes = 8 << 20,
    this.minFrameMs = 50,
    this.maxFrameMs = 5000,
  });
  final int maxImageBytes;
  final int maxPoses;
  final int maxJsonBytes;
  final int maxSheets;
  final int maxAnimations;
  final int maxFramesPerAnimation;
  final int maxFramesTotal;
  final int maxTextureEdge;
  final int maxTotalPixels;
  final int maxTotalBytes;
  final int minFrameMs;
  final int maxFrameMs;
}

class PackPose {
  PackPose({required this.name, required this.asset, this.sha256 = ''});
  final String name;
  final String asset;
  final String sha256;
}

class PackProvenance {
  PackProvenance({
    required this.sourceAsset,
    required this.sourceSha256,
    required this.generator,
    required this.promptFile,
  });
  final String sourceAsset;
  final String sourceSha256;
  final String generator;
  final String promptFile;
}

/// One spritesheet. Geometry is explicit from the manifest; the renderer
/// must never assume a fixed texture size.
class PackSheet {
  PackSheet({
    required this.id,
    required this.asset,
    required this.sha256,
    required this.width,
    required this.height,
    required this.frameWidth,
    required this.frameHeight,
  });

  final String id;
  final String asset;
  final String sha256;
  final int width;
  final int height;
  final int frameWidth;
  final int frameHeight;

  int get columns => width ~/ frameWidth;
  int get rows => height ~/ frameHeight;
  int get capacity => columns * rows;

  bool containsFrame(int frameId) => frameId >= 0 && frameId < capacity;

  /// Top-left origin of [frameId] in sheet pixels.
  List<int> frameOrigin(int frameId) {
    final col = frameId % columns;
    final row = frameId ~/ columns;
    return <int>[col * frameWidth, row * frameHeight];
  }

  /// Source rectangle for [frameId] in sheet pixels.
  Rect frameRect(int frameId) {
    final o = frameOrigin(frameId);
    return Rect.fromLTWH(
      o[0].toDouble(),
      o[1].toDouble(),
      frameWidth.toDouble(),
      frameHeight.toDouble(),
    );
  }
}

class PackAnimation {
  PackAnimation({
    required this.sheet,
    required this.frames,
    required this.frameMs,
    required this.loop,
  });
  final String sheet;
  final List<int> frames;
  final int frameMs;
  final bool loop;
}

class CharacterPack {
  CharacterPack({
    required this.version,
    required this.name,
    required this.fallbackAsset,
    required this.poses,
    required this.fallback,
    this.note = '',
    this.packId = '',
    this.packVersion = '',
    this.author = '',
    this.license = '',
    this.provenance,
    this.themePrimary = '',
    this.themeBackground = '',
    this.anchorX = 0.5,
    this.anchorY = 0.9,
    this.scaleMin = 0.5,
    this.scaleMax = 2.0,
    Map<String, PackSheet>? sheets,
    Map<String, PackAnimation>? animations,
    this.fallbackAnimation = 'idle',
  }) : sheets = sheets ?? <String, PackSheet>{},
       animations = animations ?? <String, PackAnimation>{};

  final int version;
  final String name;
  final String fallbackAsset;
  final List<PackPose> poses;
  final bool fallback;
  final String note;

  // v2 metadata (empty for v1 packs).
  final String packId;
  final String packVersion;
  final String author;
  final String license;
  final PackProvenance? provenance;
  final String themePrimary;
  final String themeBackground;
  final double anchorX;
  final double anchorY;
  final double scaleMin;
  final double scaleMax;
  final Map<String, PackSheet> sheets;
  final Map<String, PackAnimation> animations;
  final String fallbackAnimation;

  bool get isSpritesheet =>
      version == packSchemaV2 &&
      !fallback &&
      sheets.isNotEmpty &&
      animations.isNotEmpty;

  /// Presentation-only semantic mapping. Unknown semantics fall back to
  /// [fallbackAnimation] (idle). `working.reason` maps to `thinking` as a
  /// visible work phase only; it never fabricates task state.
  String resolveAnimationId(String mood) {
    if (animations.containsKey(mood)) return mood;
    if (mood == 'working.reason' && animations.containsKey('thinking')) {
      return 'thinking';
    }
    if (animations.containsKey(fallbackAnimation)) return fallbackAnimation;
    if (animations.containsKey('idle')) return 'idle';
    return '';
  }

  PackAnimation? animationFor(String mood) {
    final id = resolveAnimationId(mood);
    if (id.isEmpty) return null;
    return animations[id];
  }

  PackSheet? sheetForAnimation(String animationId) {
    final anim = animations[animationId];
    if (anim == null) return null;
    return sheets[anim.sheet];
  }

  /// Position index (0..frames.length-1) for [elapsed] since the animation
  /// started. Pure and deterministic; looping wraps, non-looping clamps.
  static int frameIndexFor(PackAnimation anim, Duration elapsed) {
    if (anim.frames.isEmpty) return 0;
    if (anim.frameMs <= 0) return 0;
    final step = elapsed.inMilliseconds ~/ anim.frameMs;
    if (anim.loop) return step % anim.frames.length;
    return math.min(step, anim.frames.length - 1);
  }

  /// Deterministic motion phase for a UI state (v1 fallback motion).
  /// Pure function of the state name: no randomness, no network.
  static double motionPhase(String state) {
    var h = 0;
    for (final c in state.codeUnits) {
      h = ((h * 31) + c) & 0x7fffffff;
    }
    return (h % 360).toDouble();
  }
}

/// PNG dimensions parsed from the IHDR header WITHOUT decoding the image.
/// Returns null when the bytes are not a valid PNG.
List<int>? pngDimensions(Uint8List bytes) {
  // Signature (8) + IHDR length (4) + 'IHDR' (4) + width (4) + height (4).
  if (bytes.lengthInBytes < 33) return null;
  const sig = <int>[137, 80, 78, 71, 13, 10, 26, 10];
  for (var i = 0; i < sig.length; i++) {
    if (bytes[i] != sig[i]) return null;
  }
  // IHDR must be the first chunk.
  final ihdrLen =
      (bytes[8] << 24) | (bytes[9] << 16) | (bytes[10] << 8) | bytes[11];
  if (ihdrLen != 13) return null;
  if (bytes[12] != 0x49 ||
      bytes[13] != 0x48 ||
      bytes[14] != 0x44 ||
      bytes[15] != 0x52) {
    return null;
  }
  final w =
      (bytes[16] << 24) | (bytes[17] << 16) | (bytes[18] << 8) | bytes[19];
  final h =
      (bytes[20] << 24) | (bytes[21] << 16) | (bytes[22] << 8) | bytes[23];
  if (w <= 0 || h <= 0) return null;
  return <int>[w, h];
}

bool isSha256Hex(String s) => RegExp(r'^[0-9a-fA-F]{64}$').hasMatch(s);

bool isHexColor(String s) => RegExp(r'^#[0-9a-fA-F]{6}$').hasMatch(s);

/// Strict bundled-asset path: under `assets/`, no traversal, absolute
/// paths, URLs, backslashes or empty segments.
bool isStrictAssetPath(String p) {
  if (p.isEmpty || p.length > 256) return false;
  if (p.contains('\\') || p.contains(':')) return false;
  if (p.startsWith('/') || !p.startsWith('assets/')) return false;
  if (p.contains('..')) return false;
  if (!RegExp(r'^[A-Za-z0-9._/\-]+$').hasMatch(p)) return false;
  if (p.contains('//')) return false;
  for (final seg in p.split('/')) {
    if (seg.isEmpty || seg == '.' || seg == '..') return false;
  }
  return true;
}

/// Loads and validates the pack; falls back to the single shipped frame.
///
/// [bundle] and [manifestJson] are an injectable seam for deterministic
/// tests with synthetic bounded PNG assets. Production passes neither and
/// reads the bundled manifest via the ambient [rootBundle].
Future<CharacterPack> loadCharacterPack({
  PackBounds bounds = const PackBounds(),
  AssetBundle? bundle,
  String? manifestJson,
}) async {
  const fallbackAsset = 'assets/mascot/kinaiya-mascot.png';
  CharacterPack fallback(String note) => CharacterPack(
    version: packSchemaVersion,
    name: 'Kinaiya',
    fallbackAsset: fallbackAsset,
    poses: <PackPose>[],
    fallback: true,
    note: note,
  );

  final effective = bundle ?? rootBundle;
  late final String raw;
  try {
    if (manifestJson != null) {
      raw = manifestJson;
    } else {
      raw = await effective.loadString(packAssetPath);
    }
  } catch (_) {
    return fallback('Pack manifest unreadable; single-frame fallback active.');
  }
  if (utf8.encode(raw).length > bounds.maxJsonBytes) {
    return fallback('Pack manifest too large; single-frame fallback active.');
  }
  late final Map<String, dynamic> json;
  try {
    final decoded = jsonDecode(raw);
    if (decoded is! Map) {
      return fallback('Pack manifest malformed; single-frame fallback active.');
    }
    json = Map<String, dynamic>.from(decoded);
  } catch (_) {
    return fallback('Pack manifest malformed; single-frame fallback active.');
  }

  final schema = json['pack_schema'];
  // Exact types: the schema must be an int, never a coerced string.
  if (schema is! int) {
    return fallback('Pack schema $schema unsupported; using single frame.');
  }
  if (schema == packSchemaVersion) {
    return _loadV1(json, effective, bounds, fallbackAsset);
  }
  if (schema == packSchemaV2) {
    return _loadV2(json, effective, bounds, fallbackAsset);
  }
  return fallback('Pack schema $schema unsupported; using single frame.');
}

Future<CharacterPack> _loadV1(
  Map<String, dynamic> json,
  AssetBundle bundle,
  PackBounds bounds,
  String fallbackAsset,
) async {
  CharacterPack fallback(String note) => CharacterPack(
    version: packSchemaVersion,
    name: 'Kinaiya',
    fallbackAsset: fallbackAsset,
    poses: <PackPose>[],
    fallback: true,
    note: note,
  );

  var name = 'Kinaiya';
  if (json.containsKey('name')) {
    final v = json['name'];
    if (v is! String) {
      return fallback('Pack manifest malformed; single-frame fallback active.');
    }
    if (v.isNotEmpty) name = v;
  }

  final list = json['poses'];
  final poses = <PackPose>[];
  if (list != null) {
    if (list is! List) {
      return fallback('Pack manifest malformed; single-frame fallback active.');
    }
    if (list.length > bounds.maxPoses) {
      return fallback('Pack declares too many poses; using single frame.');
    }
    for (final p in list) {
      if (p is! Map) continue;
      final m = Map<String, dynamic>.from(p);
      final n = m['name'];
      final a = m['asset'];
      final s = m['sha256'];
      if (n is! String || n.isEmpty) continue;
      if (a is! String || !isStrictAssetPath(a)) continue;
      if (s != null && s is! String) continue;
      final sha = (s as String?) ?? '';
      if (sha.isNotEmpty && !isSha256Hex(sha)) continue;
      poses.add(PackPose(name: n, asset: a, sha256: sha));
    }
  }
  // Verify each pose asset exists, fits bounds, and matches its checksum.
  final verified = <PackPose>[];
  for (final pose in poses) {
    try {
      final data = await bundle.load(pose.asset);
      if (data.lengthInBytes > bounds.maxImageBytes) continue;
      if (pose.sha256.isNotEmpty) {
        final digest = sha256.convert(data.buffer.asUint8List()).toString();
        if (digest != pose.sha256.toLowerCase()) continue;
      }
      verified.add(pose);
    } catch (_) {
      continue; // Missing pose assets are skipped, not fatal.
    }
  }
  if (verified.isEmpty) {
    return fallback('No verified poses; single-frame fallback active.');
  }
  return CharacterPack(
    version: packSchemaVersion,
    name: name,
    fallbackAsset: fallbackAsset,
    poses: verified,
    fallback: false,
  );
}

Future<CharacterPack> _loadV2(
  Map<String, dynamic> json,
  AssetBundle bundle,
  PackBounds bounds,
  String fallbackAsset,
) async {
  CharacterPack fallback(String note) => CharacterPack(
    version: packSchemaV2,
    name: 'Kinaiya',
    fallbackAsset: fallbackAsset,
    poses: <PackPose>[],
    fallback: true,
    note: note,
  );

  String req(String key) {
    final v = json[key];
    if (v is! String || v.isEmpty) throw const FormatException('bad field');
    return v;
  }

  late final String packId;
  late final String packVersion;
  late final String name;
  late final String author;
  late final String license;
  late final String fallbackAnimation;
  try {
    packId = req('pack_id');
    packVersion = req('version');
    name = req('name');
    author = req('author');
    license = req('license');
    fallbackAnimation = req('fallback_animation');
  } catch (_) {
    return fallback('Pack manifest malformed; single-frame fallback active.');
  }
  if (packId.length > 128 ||
      packVersion.length > 64 ||
      name.length > 128 ||
      author.length > 256 ||
      license.length > 256) {
    return fallback('Pack manifest malformed; single-frame fallback active.');
  }

  // Provenance and rights are required.
  final prov = json['provenance'];
  if (prov is! Map) {
    return fallback('Pack provenance missing; single-frame fallback active.');
  }
  final pm = Map<String, dynamic>.from(prov);
  final sourceAsset = pm['source_asset'];
  final sourceSha = pm['source_sha256'];
  final generator = pm['generator'];
  final promptFile = pm['prompt_file'];
  if (sourceAsset is! String ||
      !isStrictAssetPath(sourceAsset) ||
      sourceSha is! String ||
      !isSha256Hex(sourceSha) ||
      generator is! String ||
      generator.isEmpty ||
      promptFile is! String ||
      !isStrictAssetPath(promptFile)) {
    return fallback('Pack provenance missing; single-frame fallback active.');
  }

  // Theme colors must be exact #RRGGBB strings.
  final theme = json['theme'];
  if (theme is! Map) {
    return fallback('Pack theme missing; single-frame fallback active.');
  }
  final tm = Map<String, dynamic>.from(theme);
  final primary = tm['primary'];
  final background = tm['background'];
  if (primary is! String ||
      !isHexColor(primary) ||
      background is! String ||
      !isHexColor(background)) {
    return fallback('Pack theme missing; single-frame fallback active.');
  }

  // Voice profile must be an explicit null (no personality import).
  if (!json.containsKey('voice_profile') || json['voice_profile'] != null) {
    return fallback(
      'Pack voice profile must be null; single-frame fallback active.',
    );
  }

  // Anchor and scale limits: finite numerics in range.
  num anchorX;
  num anchorY;
  num scaleMin;
  num scaleMax;
  try {
    final anchor = json['anchor'];
    final scales = json['scale_limits'];
    if (anchor is! List || anchor.length != 2) throw const FormatException();
    if (scales is! List || scales.length != 2) throw const FormatException();
    bool finiteNum(Object? v) => v is num && v.isFinite;
    if (!finiteNum(anchor[0]) || !finiteNum(anchor[1])) {
      throw const FormatException();
    }
    if (!finiteNum(scales[0]) || !finiteNum(scales[1])) {
      throw const FormatException();
    }
    final ax0 = anchor[0];
    final ay0 = anchor[1];
    final s0 = scales[0];
    final s1 = scales[1];
    if (ax0 is! num || ay0 is! num || s0 is! num || s1 is! num) {
      throw const FormatException();
    }
    anchorX = ax0;
    anchorY = ay0;
    scaleMin = s0;
    scaleMax = s1;
    if (anchorX < 0 ||
        anchorX > 1 ||
        anchorY < 0 ||
        anchorY > 1 ||
        scaleMin <= 0 ||
        scaleMax <= 0 ||
        scaleMin > scaleMax) {
      throw const FormatException();
    }
  } catch (_) {
    return fallback('Pack geometry invalid; single-frame fallback active.');
  }

  // Sheets.
  final sheetsRaw = json['sheets'];
  if (sheetsRaw is! List ||
      sheetsRaw.isEmpty ||
      sheetsRaw.length > bounds.maxSheets) {
    return fallback('Pack sheets invalid; single-frame fallback active.');
  }
  // Keep maxPoses meaningful for v2: it still bounds the declaration size.
  if (sheetsRaw.length > bounds.maxPoses) {
    return fallback('Pack declares too many poses; using single frame.');
  }
  final sheets = <String, PackSheet>{};
  for (final entry in sheetsRaw) {
    if (entry is! Map) {
      return fallback('Pack sheets invalid; single-frame fallback active.');
    }
    final m = Map<String, dynamic>.from(entry);
    final id = m['id'];
    final asset = m['asset'];
    final sha = m['sha256'];
    final w = m['width'];
    final h = m['height'];
    final fw = m['frame_width'];
    final fh = m['frame_height'];
    // Exact int types: doubles or strings are rejected, never coerced.
    if (id is! String ||
        id.isEmpty ||
        asset is! String ||
        !isStrictAssetPath(asset) ||
        !asset.endsWith('.png') ||
        sha is! String ||
        !isSha256Hex(sha) ||
        w is! int ||
        h is! int ||
        fw is! int ||
        fh is! int) {
      return fallback('Pack sheets invalid; single-frame fallback active.');
    }
    if (sheets.containsKey(id)) {
      return fallback('Pack sheets invalid; single-frame fallback active.');
    }
    if (w <= 0 ||
        h <= 0 ||
        fw <= 0 ||
        fh <= 0 ||
        w > bounds.maxTextureEdge ||
        h > bounds.maxTextureEdge ||
        fw > w ||
        fh > h ||
        w % fw != 0 ||
        h % fh != 0) {
      return fallback(
        'Pack frame geometry out of bounds; single-frame fallback active.',
      );
    }
    sheets[id] = PackSheet(
      id: id,
      asset: asset,
      sha256: sha,
      width: w,
      height: h,
      frameWidth: fw,
      frameHeight: fh,
    );
  }

  // Animations.
  final animsRaw = json['animations'];
  if (animsRaw is! Map ||
      animsRaw.isEmpty ||
      animsRaw.length > bounds.maxAnimations) {
    return fallback('Pack animations invalid; single-frame fallback active.');
  }
  if (animsRaw.length > bounds.maxPoses) {
    return fallback('Pack declares too many poses; using single frame.');
  }
  final animations = <String, PackAnimation>{};
  var totalFrames = 0;
  for (final kv in animsRaw.entries) {
    final key = kv.key;
    final val = kv.value;
    if (key is! String || key.isEmpty || val is! Map) {
      return fallback('Pack animations invalid; single-frame fallback active.');
    }
    final m = Map<String, dynamic>.from(val);
    final sheetId = m['sheet'];
    final frames = m['frames'];
    final frameMs = m['frame_ms'];
    final loop = m['loop'];
    if (sheetId is! String || !sheets.containsKey(sheetId)) {
      return fallback('Pack animations invalid; single-frame fallback active.');
    }
    if (frames is! List ||
        frames.isEmpty ||
        frames.length > bounds.maxFramesPerAnimation) {
      return fallback('Pack animations invalid; single-frame fallback active.');
    }
    // Exact int frame ids, in sheet bounds.
    final sheet = sheets[sheetId]!;
    final ids = <int>[];
    for (final f in frames) {
      if (f is! int || !sheet.containsFrame(f)) {
        return fallback(
          'Pack frame geometry out of bounds; single-frame fallback active.',
        );
      }
      ids.add(f);
    }
    if (frameMs is! int ||
        frameMs < bounds.minFrameMs ||
        frameMs > bounds.maxFrameMs) {
      return fallback(
        'Pack animation timing invalid; single-frame fallback active.',
      );
    }
    if (loop is! bool) {
      return fallback('Pack animations invalid; single-frame fallback active.');
    }
    totalFrames += ids.length;
    if (totalFrames > bounds.maxFramesTotal) {
      return fallback(
        'Pack declares too many frames; single-frame fallback active.',
      );
    }
    animations[key] = PackAnimation(
      sheet: sheetId,
      frames: List<int>.unmodifiable(ids),
      frameMs: frameMs,
      loop: loop,
    );
  }

  // Required animation set: the fixed contract plus the fallback target.
  for (final required in <String>[
    'idle',
    'listening',
    'thinking',
    'speaking',
    fallbackAnimation,
  ]) {
    if (!animations.containsKey(required)) {
      return fallback(
        'Pack animations incomplete; single-frame fallback active.',
      );
    }
  }

  // Total pixel budget from manifest geometry (before any decode).
  var totalPixels = 0;
  for (final s in sheets.values) {
    totalPixels += s.width * s.height;
    if (totalPixels > bounds.maxTotalPixels) {
      return fallback(
        'Pack exceeds texture budget; single-frame fallback active.',
      );
    }
  }

  // Encoded bytes, checksums, PNG header dimensions and rectangle bounds
  // BEFORE decoding. Any failure falls back the whole pack.
  var totalBytes = 0;
  for (final s in sheets.values) {
    late final ByteData data;
    try {
      data = await bundle.load(s.asset);
    } catch (_) {
      return fallback('Pack sheet missing; single-frame fallback active.');
    }
    final bytes = data.buffer.asUint8List();
    if (bytes.lengthInBytes > bounds.maxImageBytes) {
      return fallback('Pack sheet too large; single-frame fallback active.');
    }
    totalBytes += bytes.lengthInBytes;
    if (totalBytes > bounds.maxTotalBytes) {
      return fallback(
        'Pack exceeds texture budget; single-frame fallback active.',
      );
    }
    final digest = sha256.convert(bytes).toString();
    if (digest != s.sha256.toLowerCase()) {
      return fallback('Pack checksum mismatch; single-frame fallback active.');
    }
    final dims = pngDimensions(bytes);
    if (dims == null || dims[0] != s.width || dims[1] != s.height) {
      return fallback(
        'Pack dimensions mismatch; single-frame fallback active.',
      );
    }
  }

  return CharacterPack(
    version: packSchemaV2,
    name: name,
    fallbackAsset: fallbackAsset,
    poses: <PackPose>[],
    fallback: false,
    packId: packId,
    packVersion: packVersion,
    author: author,
    license: license,
    provenance: PackProvenance(
      sourceAsset: sourceAsset,
      sourceSha256: sourceSha.toLowerCase(),
      generator: generator,
      promptFile: promptFile,
    ),
    themePrimary: primary,
    themeBackground: background,
    anchorX: anchorX.toDouble(),
    anchorY: anchorY.toDouble(),
    scaleMin: scaleMin.toDouble(),
    scaleMax: scaleMax.toDouble(),
    sheets: sheets,
    animations: animations,
    fallbackAnimation: fallbackAnimation,
  );
}
