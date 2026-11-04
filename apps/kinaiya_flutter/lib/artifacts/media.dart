/// Native-owned, authenticated media bytes. No model URL or executable content.
library;

import 'dart:typed_data';

import 'package:flutter/material.dart';

import 'media_surface_stub.dart'
    if (dart.library.html) 'media_surface_web.dart'
    as surface;
import 'models.dart';

class ArtifactMedia {
  ArtifactMedia(this.bytes, {required this.variant, required this.sha256});
  final Uint8List bytes;
  final String variant, sha256;
}

typedef ArtifactMediaLoader =
    Future<ArtifactMedia> Function(
      IntelligentArtifact artifact,
      String assetId,
      String variant,
    );

class ArtifactMediaPlayer extends StatefulWidget {
  const ArtifactMediaPlayer({
    super.key,
    required this.artifact,
    required this.assetId,
    this.load,
  });
  final IntelligentArtifact artifact;
  final String assetId;
  final ArtifactMediaLoader? load;
  @override
  State<ArtifactMediaPlayer> createState() => _ArtifactMediaPlayerState();
}

class _ArtifactMediaPlayerState extends State<ArtifactMediaPlayer> {
  ArtifactMedia? _media;
  bool _loading = false;
  String? _error;
  int _generation = 0;

  @override
  void didUpdateWidget(ArtifactMediaPlayer oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.assetId != widget.assetId ||
        oldWidget.artifact.artifactId != widget.artifact.artifactId ||
        oldWidget.artifact.revision != widget.artifact.revision ||
        oldWidget.artifact.sessionId != widget.artifact.sessionId ||
        oldWidget.artifact.threadId != widget.artifact.threadId ||
        oldWidget.artifact.epoch != widget.artifact.epoch) {
      _generation++;
      _media = null;
      _loading = false;
      _error = null;
    }
  }

  Future<void> _load(String variant) async {
    if (_loading || widget.load == null) return;
    final generation = ++_generation;
    setState(() {
      _loading = true;
      _error = null;
      _media = null;
    });
    try {
      final media = await widget.load!(
        widget.artifact,
        widget.assetId,
        variant,
      );
      if (!mounted || generation != _generation) return;
      setState(() {
        _media = media;
      });
    } catch (_) {
      if (mounted && generation == _generation) {
        setState(() {
          _error = 'This scoped video could not be verified.';
        });
      }
    } finally {
      if (mounted && generation == _generation) {
        setState(() {
          _loading = false;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) => Column(
    crossAxisAlignment: CrossAxisAlignment.stretch,
    children: [
      const Text(
        'Original video and native SAM hand highlights. Playback is muted initially.',
      ),
      if (surface.supported)
        Wrap(
          spacing: 8,
          children: [
            for (final variant in ['highlighted', 'source'])
              OutlinedButton.icon(
                key: ValueKey('artifact-media-$variant'),
                onPressed: _loading || widget.load == null
                    ? null
                    : () => _load(variant),
                icon: Icon(
                  variant == 'source'
                      ? Icons.movie_outlined
                      : Icons.back_hand_outlined,
                ),
                label: Text(
                  variant == 'source'
                      ? 'Original video'
                      : 'SAM hand highlights',
                ),
              ),
          ],
        )
      else
        const Text('Video playback is available in the web app.'),
      if (_loading) const LinearProgressIndicator(),
      if (_error != null) Text(_error!),
      if (_media != null) ...[
        Text(
          _media!.variant == 'source'
              ? 'Original source'
              : 'SAM hand highlights',
        ),
        AspectRatio(
          aspectRatio: 16 / 9,
          child: surface.mediaSurface(_media!.bytes),
        ),
      ],
      const Text('This presentation does not establish task success.'),
    ],
  );
}
