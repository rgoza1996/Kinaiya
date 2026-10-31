/// WebSocket event stream for the shared journal.
///
/// First frame carries `{token, after, session_id}` so credentials never
/// appear in the URL. Replays ignore old session epochs and out-of-order
/// sequences; callers track cursors per session.
library;

import 'dart:async';
import 'dart:convert';

import 'package:web_socket_channel/web_socket_channel.dart';

import 'models.dart';
import '../config/service_root.dart';

class JournalEventBatch {
  JournalEventBatch(this.entries, this.cursor);
  final List<JournalEntry> entries;
  final int cursor;
}

class EventsSocket {
  EventsSocket({required String sessionBase, required String ownerToken})
    : sessionBase = normalizeServiceRoot(sessionBase),
      _token = ownerToken;

  final String sessionBase;
  final String _token;
  WebSocketChannel? _channel;
  StreamSubscription? _sub;
  bool _closed = false;

  String get _wsBase {
    final uri = Uri.parse(sessionBase);
    final scheme = uri.scheme == 'https' ? 'wss' : 'ws';
    return uri.replace(scheme: scheme).toString();
  }

  /// Connects for [sessionId] replaying after [after]. Emits batches.
  Stream<JournalEventBatch> connect({
    required String sessionId,
    int after = 0,
  }) {
    _closed = false;
    final controller = StreamController<JournalEventBatch>();
    var cursor = after;
    var lastSequence = after;
    _channel = WebSocketChannel.connect(Uri.parse('$_wsBase/v1/events'));
    _channel!.sink.add(
      jsonEncode({'token': _token, 'after': after, 'session_id': sessionId}),
    );
    _sub = _channel!.stream.listen(
      (raw) {
        try {
          final v = raw is String ? jsonDecode(raw) : jsonDecode('$raw');
          final entry = JournalEntry.fromJson(
            (v as Map).cast<String, Object?>(),
          );
          if (entry.sessionId != sessionId) return;
          if (entry.sequence <= lastSequence) return;
          lastSequence = entry.sequence;
          cursor = entry.sequence;
          controller.add(JournalEventBatch([entry], cursor));
        } catch (_) {
          // Ignore malformed frames; the cursor stays put for retry.
        }
      },
      onError: controller.addError,
      onDone: () {
        if (!_closed && !controller.isClosed) controller.close();
      },
      cancelOnError: false,
    );
    return controller.stream;
  }

  Future<void> close() async {
    _closed = true;
    await _sub?.cancel();
    await _channel?.sink.close();
  }
}
