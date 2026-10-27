/// Versioned character pack manifest and validator.
///
/// The honest baseline is the single shipped artwork frame plus
/// deterministic state-based motion. A full multi-pose library is not
/// claimed: missing or invalid packs fall back to the single frame with a
/// visible note, never a silent substitution.
library;

import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:flutter/services.dart' show rootBundle;

const String packAssetPath = 'assets/character/pack.json';
const int packSchemaVersion = 1;

/// Bounds a pack must satisfy to be accepted.
class PackBounds {
  const PackBounds({this.maxImageBytes = 8 << 20, this.maxPoses = 24});
  final int maxImageBytes;
  final int maxPoses;
}

class PackPose {
  PackPose({required this.name, required this.asset, this.sha256 = ''});
  final String name;
  final String asset;
  final String sha256;
}

class CharacterPack {
  CharacterPack({
    required this.version,
    required this.name,
    required this.fallbackAsset,
    required this.poses,
    required this.fallback,
    this.note = '',
  });

  final int version;
  final String name;
  final String fallbackAsset;
  final List<PackPose> poses;
  final bool fallback;
  final String note;

  /// Deterministic motion phase for a UI state (idle/speaking/listening/
  /// thinking). Pure function of the state name: no randomness, no network.
  static double motionPhase(String state) {
    var h = 0;
    for (final c in state.codeUnits) {
      h = ((h * 31) + c) & 0x7fffffff;
    }
    return (h % 360).toDouble();
  }
}

/// Loads and validates the pack; falls back to the single shipped frame.
Future<CharacterPack> loadCharacterPack({PackBounds bounds = const PackBounds()}) async {
  const fallbackAsset = 'assets/mascot/kinaiya-mascot.png';
  try {
    final raw = await rootBundle.loadString(packAssetPath);
    final json = jsonDecode(raw) as Map<String, dynamic>;
    if (json['pack_schema'] != packSchemaVersion) {
      return _fallback('Pack schema ${json['pack_schema']} unsupported; using single frame.');
    }
    final poses = <PackPose>[];
    final list = json['poses'];
    if (list is List) {
      if (list.length > bounds.maxPoses) {
        return _fallback('Pack declares too many poses; using single frame.');
      }
      for (final p in list) {
        final m = (p as Map).cast<String, dynamic>();
        poses.add(PackPose(
          name: '${m['name']}',
          asset: '${m['asset']}',
          sha256: '${m['sha256'] ?? ''}',
        ));
      }
    }
    // Verify each pose asset exists, fits bounds, and matches its checksum.
    final verified = <PackPose>[];
    for (final pose in poses) {
      try {
        final data = await rootBundle.load(pose.asset);
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
      return _fallback('No verified poses; single-frame fallback active.');
    }
    return CharacterPack(
      version: packSchemaVersion,
      name: '${json['name'] ?? 'Kinaiya'}',
      fallbackAsset: fallbackAsset,
      poses: verified,
      fallback: false,
    );
  } catch (_) {
    return _fallback('Pack manifest unreadable; single-frame fallback active.');
  }
}

CharacterPack _fallback(String note) => CharacterPack(
      version: packSchemaVersion,
      name: 'Kinaiya',
      fallbackAsset: 'assets/mascot/kinaiya-mascot.png',
      poses: <PackPose>[],
      fallback: true,
      note: note,
    );
