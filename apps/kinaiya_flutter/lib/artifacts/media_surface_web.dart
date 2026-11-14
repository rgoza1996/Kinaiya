// Trusted compiled player only. The generated artifact supplies no URI or HTML.
// ignore_for_file: deprecated_member_use, avoid_web_libraries_in_flutter
import 'dart:html' as html;
import 'dart:typed_data';

import 'package:flutter/material.dart';

const supported = true;
Widget mediaSurface(Uint8List bytes) => _Video(bytes, key: ObjectKey(bytes));

class _Video extends StatefulWidget {
  const _Video(this.bytes, {super.key});
  final Uint8List bytes;
  @override
  State<_Video> createState() => _VideoState();
}

class _VideoState extends State<_Video> {
  late final String _url;
  html.VideoElement? _video;
  @override
  void initState() {
    super.initState();
    _url = html.Url.createObjectUrlFromBlob(
      html.Blob([widget.bytes], 'video/mp4'),
    );
  }

  @override
  void dispose() {
    final video = _video;
    if (video != null) {
      video.pause();
      video.removeAttribute('src');
      video.load();
    }
    html.Url.revokeObjectUrl(_url);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => HtmlElementView.fromTagName(
    tagName: 'video',
    onElementCreated: (element) {
      if (!mounted) return;
      final video = _video = element as html.VideoElement;
      video.controls = true;
      video.autoplay = false;
      video.muted = true;
      video.preload = 'metadata';
      video.setAttribute('playsinline', '');
      video.style.width = '100%';
      video.style.height = '100%';
      video.style.objectFit = 'contain';
      video.src = _url;
    },
  );
}
