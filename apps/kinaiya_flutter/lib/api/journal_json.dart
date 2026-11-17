import 'dart:convert';

/// Journal payloads retain finite decimal metadata. Cursor and entry identities
/// require integer wire spelling even on the web, where 1.0 can be an int.
Object? journalJsonDecode(List<int> bytes) => _JournalJsonReader(bytes).read();

class _JournalJsonReader {
  _JournalJsonReader(List<int> bytes)
    : text = utf8.decode(bytes, allowMalformed: false) {
    if (bytes.length > 2 * 1024 * 1024) _bad();
  }

  final String text;
  int at = 0, values = 0;
  static const _payloadCounters = {
    'generation_epoch',
    'next_generation_epoch',
    'window_index',
    'window_revision',
    'revision',
    'source_sequence',
    'source_revision',
    'published_chunks',
  };

  Never _bad() => throw const FormatException('Invalid live journal JSON');

  void _space() {
    while (at < text.length && ' \n\r\t'.contains(text[at])) {
      at++;
    }
  }

  Object? read() {
    final result = _value(0);
    _space();
    if (at != text.length) _bad();
    return result;
  }

  Object? _value(
    int depth, {
    bool payload = false,
    bool payloadRoot = false,
    bool integerOnly = false,
  }) {
    if (depth > 32 || ++values > 65536) _bad();
    _space();
    if (at >= text.length) _bad();
    final c = text[at];
    if (c == '"') return _string();
    if (c == '{') {
      at++;
      final map = <String, Object?>{};
      _space();
      if (at < text.length && text[at] == '}') {
        at++;
        return map;
      }
      while (true) {
        _space();
        if (at >= text.length || text[at] != '"') _bad();
        final key = _string();
        if (key.length > 4096 || map.containsKey(key) || map.length >= 8192) {
          _bad();
        }
        _space();
        if (at >= text.length || text[at++] != ':') _bad();
        final entryPayload = depth == 1 && key == 'payload';
        map[key] = _value(
          depth + 1,
          payload: payload || entryPayload,
          payloadRoot: entryPayload,
          integerOnly: payloadRoot && _payloadCounters.contains(key),
        );
        _space();
        if (at >= text.length) _bad();
        final separator = text[at++];
        if (separator == '}') return map;
        if (separator != ',') _bad();
      }
    }
    if (c == '[') {
      at++;
      final list = <Object?>[];
      _space();
      if (at < text.length && text[at] == ']') {
        at++;
        return list;
      }
      while (true) {
        if (list.length >= 8192) _bad();
        list.add(_value(depth + 1, payload: payload));
        _space();
        if (at >= text.length) _bad();
        final separator = text[at++];
        if (separator == ']') return list;
        if (separator != ',') _bad();
      }
    }
    for (final pair in [('true', true), ('false', false), ('null', null)]) {
      if (text.startsWith(pair.$1, at)) {
        at += pair.$1.length;
        return pair.$2;
      }
    }
    final start = at;
    if (text[at] == '-') at++;
    final digits = at;
    while (_digitAt()) {
      at++;
    }
    if (at == digits) _bad();
    final magnitude = text.substring(digits, at);
    if (magnitude.length > 1 && magnitude.startsWith('0')) _bad();
    var fractional = false;
    if (at < text.length && text[at] == '.') {
      fractional = true;
      at++;
      final first = at;
      while (_digitAt()) {
        at++;
      }
      if (at == first) _bad();
    }
    if (at < text.length && 'eE'.contains(text[at])) {
      fractional = true;
      at++;
      if (at < text.length && '+-'.contains(text[at])) at++;
      final first = at;
      while (_digitAt()) {
        at++;
      }
      if (at == first) _bad();
    }
    if (fractional) {
      if (!payload || integerOnly || at - start > 128) _bad();
      final number = double.tryParse(text.substring(start, at));
      if (number == null || !number.isFinite) _bad();
      return number;
    }
    if (magnitude.length > 16 ||
        (magnitude.length == 16 &&
            magnitude.compareTo('9007199254740991') > 0)) {
      _bad();
    }
    return int.parse(text.substring(start, at));
  }

  bool _digitAt() =>
      at < text.length &&
      text.codeUnitAt(at) >= 48 &&
      text.codeUnitAt(at) <= 57;

  String _string() {
    final start = at++;
    while (at < text.length) {
      final c = text.codeUnitAt(at++);
      if (c == 34) {
        final value = jsonDecode(text.substring(start, at)) as String;
        for (var i = 0; i < value.length; i++) {
          final u = value.codeUnitAt(i);
          if (u >= 0xd800 && u <= 0xdbff) {
            if (++i >= value.length ||
                value.codeUnitAt(i) < 0xdc00 ||
                value.codeUnitAt(i) > 0xdfff) {
              _bad();
            }
          } else if (u >= 0xdc00 && u <= 0xdfff) {
            _bad();
          }
        }
        return value;
      }
      if (c < 32) _bad();
      if (c == 92) {
        if (at >= text.length) _bad();
        at++;
      }
    }
    _bad();
  }
}
