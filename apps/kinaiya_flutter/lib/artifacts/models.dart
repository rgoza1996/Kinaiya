/// Detached, bounded declarative values for the Intelligent UI v1 contract.
/// Model output never supplies widgets, executable code, or runtime authority.
library;

import 'dart:convert';

const intelligentArtifactMaxBytes = 64 * 1024;
const intelligentArtifactPublicationMaxBytes = 48 * 1024;
const _maxSafeInteger = 9007199254740991;
const _numberBound = 1e12;

Never _invalid() =>
    throw const FormatException('Unsupported or invalid intelligent artifact');

Map<String, Object?> _object(
  Object? value,
  Set<String> required, [
  Set<String> optional = const {},
]) {
  if (value is! Map || value.length > required.length + optional.length) {
    _invalid();
  }
  final result = <String, Object?>{};
  for (final entry in value.entries) {
    if (entry.key is! String ||
        (!required.contains(entry.key) && !optional.contains(entry.key))) {
      _invalid();
    }
    result[entry.key as String] = entry.value;
  }
  if (!required.every(result.containsKey)) _invalid();
  return result;
}

String _text(Object? value, {int min = 0, int max = 1000}) {
  if (value is! String || value.length > max * 2) _invalid();
  for (var i = 0; i < value.length; i++) {
    final c = value.codeUnitAt(i);
    if (c >= 0xd800 && c <= 0xdbff) {
      if (++i >= value.length) _invalid();
      final low = value.codeUnitAt(i);
      if (low < 0xdc00 || low > 0xdfff) _invalid();
    } else if (c >= 0xdc00 && c <= 0xdfff) {
      _invalid();
    }
  }
  final length = value.runes.length;
  if (length < min || length > max) _invalid();
  if (value.runes.any(
    (c) => (c < 32 || (c >= 127 && c <= 159)) && c != 9 && c != 10,
  )) {
    _invalid();
  }
  return value;
}

String _choice(Object? value, Set<String> choices) {
  if (value is! String || !choices.contains(value)) _invalid();
  return value;
}

String _id(Object? value, {bool component = false}) {
  final text = _text(value, min: 1, max: component ? 64 : 128);
  if (component) {
    final match = RegExp(r'^[A-Za-z][A-Za-z0-9_-]*$').firstMatch(text);
    if (match == null || match.end != text.length) _invalid();
  }
  return text;
}

int _integer(Object? value, {int min = 0}) {
  if (value is! int || value < min || value > _maxSafeInteger) _invalid();
  return value;
}

num _number(Object? value, {num bound = _numberBound}) {
  if (value is! num || !value.isFinite || value.abs() > bound) _invalid();
  return value;
}

List<Object?> _list(Object? value, {int min = 0, required int max}) {
  if (value is! List || value.length < min || value.length > max) _invalid();
  return List<Object?>.from(value);
}

Object? _scalar(Object? value) {
  if (value == null || value is bool) return value;
  if (value is num) return _number(value);
  return _text(value);
}

/// Rejects cycles, unusual objects and excessive work before JSON encoding.
int _jsonBytes(Object? value, {int max = intelligentArtifactMaxBytes}) {
  var visited = 0;
  var bytes = 0;
  final ancestors = <Object>{};
  void visit(Object? item, int depth) {
    if (++visited > 8192 || depth > 12) _invalid();
    if (item == null || item is bool) {
      bytes += 5;
    } else if (item is num) {
      if (!item.isFinite) _invalid();
      bytes += item.toString().length;
    } else if (item is String) {
      if (item.length > max) _invalid();
      bytes += utf8.encode(jsonEncode(item)).length;
    } else if (item is Map) {
      if (item.length > 128 || !ancestors.add(item)) _invalid();
      bytes += 2 + item.length * 2;
      for (final entry in item.entries) {
        if (entry.key is! String) _invalid();
        visit(entry.key, depth + 1);
        visit(entry.value, depth + 1);
      }
      ancestors.remove(item);
    } else if (item is List) {
      if (item.length > 1200 || !ancestors.add(item)) _invalid();
      bytes += 2 + item.length;
      for (final child in item) {
        visit(child, depth + 1);
      }
      ancestors.remove(item);
    } else {
      _invalid();
    }
    if (bytes > max * 2) _invalid();
  }

  visit(value, 0);
  // The preflight budget is deliberately conservative; exact bytes enforce the
  // published JSON limit, including escaped strings and multi-byte characters.
  final encoded = utf8.encode(jsonEncode(value)).length;
  if (encoded > max) _invalid();
  return encoded;
}

class IntelligentArtifactComponent {
  IntelligentArtifactComponent._(this.id, this.type, this.title, this.data);

  factory IntelligentArtifactComponent.fromJson(Object? value) {
    final m = _object(value, {'id', 'type', 'data'}, {'title'});
    final id = _id(m['id'], component: true);
    final type = _choice(m['type'], {
      'text',
      'status',
      'table',
      'chart',
      'diagram',
      'source',
      'number',
      'select',
      'ask',
    });
    final title = _text(m['title'] ?? '', max: 160);
    final data = _componentData(type, m['data']);
    return IntelligentArtifactComponent._(
      id,
      type,
      title,
      Map.unmodifiable(data),
    );
  }

  final String id, type, title;
  final Map<String, Object?> data;

  bool get acceptsAction =>
      type == 'table' ||
      type == 'diagram' ||
      type == 'number' ||
      type == 'select';

  bool acceptsValue(Object? value) {
    switch (type) {
      case 'table':
        final rows = data['rows']! as List;
        return data['selectable'] == true &&
            (value == null ||
                (value is int && value >= 0 && value < rows.length));
      case 'diagram':
        return value is String &&
            (data['nodes']! as List).any(
              (node) => (node as Map)['id'] == value,
            );
      case 'number':
        return value is num &&
            value.isFinite &&
            value >= (data['min']! as num) &&
            value <= (data['max']! as num);
      case 'select':
        return value is String && (data['options']! as List).contains(value);
      default:
        return false;
    }
  }

  Map<String, Object?> toJson() => {
    'id': id,
    'type': type,
    'title': title,
    'data': data,
  };
}

Map<String, Object?> _componentData(String type, Object? value) {
  switch (type) {
    case 'text':
      final m = _object(value, {'text'});
      return {'text': _text(m['text'], min: 1, max: 8000)};
    case 'status':
      final m = _object(value, {'text'}, {'tone'});
      return {
        'text': _text(m['text'], min: 1),
        'tone': _choice(m['tone'] ?? 'neutral', {
          'neutral',
          'success',
          'warning',
          'error',
        }),
      };
    case 'table':
      final m = _object(value, {'columns', 'rows'}, {'selectable'});
      final columns = _list(
        m['columns'],
        min: 1,
        max: 12,
      ).map((v) => _text(v, min: 1, max: 80)).toList();
      final rows = _list(m['rows'], max: 100).map((v) {
        final cells = _list(v, min: columns.length, max: columns.length);
        return List<Object?>.unmodifiable(cells.map(_scalar));
      }).toList();
      final selectable = m['selectable'] ?? true;
      if (selectable is! bool) _invalid();
      return {
        'columns': List<String>.unmodifiable(columns),
        'rows': List<List<Object?>>.unmodifiable(rows),
        'selectable': selectable,
      };
    case 'chart':
      final m = _object(value, {'labels', 'values'}, {'unit', 'style'});
      final labels = _list(
        m['labels'],
        min: 1,
        max: 100,
      ).map((v) => _text(v, max: 80)).toList();
      final values = _list(
        m['values'],
        min: labels.length,
        max: labels.length,
      ).map(_number).toList();
      return {
        'labels': List<String>.unmodifiable(labels),
        'values': List<num>.unmodifiable(values),
        'unit': _text(m['unit'] ?? '', max: 40),
        'style': _choice(m['style'] ?? 'bar', {'bar', 'line'}),
      };
    case 'diagram':
      final m = _object(value, {'nodes', 'edges'});
      final ids = <String>{};
      final nodes = _list(m['nodes'], min: 1, max: 30).map((v) {
        final node = _object(v, {'id', 'label'}, {'detail'});
        final id = _id(node['id']);
        if (!ids.add(id)) _invalid();
        return Map<String, Object?>.unmodifiable({
          'id': id,
          'label': _text(node['label'], max: 160),
          'detail': _text(node['detail'] ?? '', max: 1000),
        });
      }).toList();
      final edges = _list(m['edges'], max: 60).map((v) {
        final edge = _object(v, {'from', 'to'}, {'label'});
        final from = _id(edge['from']);
        final to = _id(edge['to']);
        if (!ids.contains(from) || !ids.contains(to)) _invalid();
        return Map<String, Object?>.unmodifiable({
          'from': from,
          'to': to,
          'label': _text(edge['label'] ?? '', max: 80),
        });
      }).toList();
      return {
        'nodes': List<Map<String, Object?>>.unmodifiable(nodes),
        'edges': List<Map<String, Object?>>.unmodifiable(edges),
      };
    case 'source':
      final m = _object(value, {'label', 'url', 'freshness'}, {'observed_at'});
      final url = _text(m['url'], max: 2000);
      final uri = Uri.tryParse(url);
      if (uri == null ||
          uri.scheme != 'https' ||
          uri.host.isEmpty ||
          uri.userInfo.isNotEmpty ||
          url.contains(RegExp(r'\s'))) {
        _invalid();
      }
      final observed = m.containsKey('observed_at')
          ? _number(m['observed_at'], bound: _maxSafeInteger)
          : null;
      if (observed != null && observed < 0) _invalid();
      return {
        'label': _text(m['label'], min: 1, max: 160),
        'url': url,
        'freshness': _choice(m['freshness'], {
          'static',
          'simulated',
          'delayed',
          'stale',
        }),
        if (observed != null) 'observed_at': observed,
      };
    case 'number':
      final m = _object(value, {'label', 'value', 'min', 'max'}, {'step'});
      final minimum = _number(m['min']);
      final maximum = _number(m['max']);
      final number = _number(m['value']);
      if (minimum > maximum || number < minimum || number > maximum) _invalid();
      final step = m.containsKey('step') ? _number(m['step']) : null;
      if (step != null && step <= 0) _invalid();
      return {
        'label': _text(m['label'], min: 1, max: 160),
        'value': number,
        'min': minimum,
        'max': maximum,
        if (step != null) 'step': step,
      };
    case 'select':
      final m = _object(value, {'label', 'options', 'value'});
      final options = _list(
        m['options'],
        min: 1,
        max: 30,
      ).map((v) => _text(v, min: 1, max: 160)).toList();
      final selected = _text(m['value'], min: 1, max: 160);
      if (options.toSet().length != options.length ||
          !options.contains(selected)) {
        _invalid();
      }
      return {
        'label': _text(m['label'], min: 1, max: 160),
        'options': List<String>.unmodifiable(options),
        'value': selected,
      };
    case 'ask':
      final m = _object(value, {'label', 'message'});
      return {
        'label': _text(m['label'], min: 1, max: 160),
        'message': _text(m['message'], min: 1, max: 2000),
      };
    default:
      _invalid();
  }
}

Map<String, Object?> _artifactObject(Object? value) => _object(
  value,
  {
    'schema_version',
    'artifact_id',
    'session_id',
    'thread_id',
    'epoch',
    'task_id',
    'objective_revision',
    'revision',
    'title',
    'summary',
    'layout',
    'components',
    'created_at',
    'updated_at',
    'content_sha256',
    'content_scope',
    'view_state',
    'view_revision',
  },
  {'task_submit_sequence'},
);

class IntelligentArtifact {
  IntelligentArtifact._({
    required this.supported,
    required this.schemaVersion,
    required this.artifactId,
    required this.sessionId,
    required this.threadId,
    required this.epoch,
    required this.taskId,
    required this.objectiveRevision,
    required this.revision,
    required this.title,
    required this.summary,
    required this.layout,
    required this.components,
    required this.createdAt,
    required this.updatedAt,
    required this.contentSha256,
    required this.contentScope,
    required this.viewState,
    required this.viewRevision,
    required this.taskSubmitSequence,
  });

  factory IntelligentArtifact.fromJson(Object? value) {
    _jsonBytes(value);
    final m = _artifactObject(value);
    if (m['schema_version'] != '1.0') _invalid();
    final title = _text(m['title'], min: 1, max: 160);
    final summary = _text(m['summary'], min: 1, max: 2000);
    final layout = _choice(m['layout'], {'stack', 'grid'});
    final components = _list(
      m['components'],
      min: 1,
      max: 24,
    ).map(IntelligentArtifactComponent.fromJson).toList();
    final ids = components.map((c) => c.id).toSet();
    if (ids.length != components.length) _invalid();
    _jsonBytes({
      'title': title,
      'summary': summary,
      'layout': layout,
      'components': m['components'],
    }, max: intelligentArtifactPublicationMaxBytes);
    if (m['view_state'] is! Map || (m['view_state']! as Map).length > 24) {
      _invalid();
    }
    final state = <String, Object?>{};
    for (final entry in (m['view_state']! as Map).entries) {
      if (entry.key is! String || !ids.contains(entry.key)) _invalid();
      final component = components.firstWhere((c) => c.id == entry.key);
      if (!component.acceptsValue(entry.value)) _invalid();
      state[entry.key as String] = entry.value;
    }
    return _fromMetadata(m, components, state, supported: true);
  }

  /// Retains native identity and bounded literal copy when component content is
  /// unsupported. Components and view-state values are deliberately discarded,
  /// never traversed, and can never become controls through this constructor.
  factory IntelligentArtifact.readableFallback(Object? value) => _fromMetadata(
    _artifactObject(value),
    const [],
    const {},
    supported: false,
  );

  static IntelligentArtifact _fromMetadata(
    Map<String, Object?> m,
    List<IntelligentArtifactComponent> components,
    Map<String, Object?> state, {
    required bool supported,
  }) {
    final hash = _text(m['content_sha256'], min: 64, max: 64);
    if (!RegExp(r'^[a-f0-9]{64}$').hasMatch(hash)) _invalid();
    final created = _number(m['created_at'], bound: _maxSafeInteger);
    final updated = _number(m['updated_at'], bound: _maxSafeInteger);
    if (created < 0 || updated < created) _invalid();
    return IntelligentArtifact._(
      supported: supported,
      schemaVersion: _text(m['schema_version'], min: 1, max: 32),
      artifactId: _id(m['artifact_id']),
      sessionId: _id(m['session_id']),
      threadId: _id(m['thread_id']),
      epoch: _integer(m['epoch'], min: 1),
      taskId: _id(m['task_id']),
      objectiveRevision: _integer(m['objective_revision'], min: 1),
      revision: _integer(m['revision'], min: 1),
      title: _text(m['title'], min: 1, max: 160),
      summary: _text(m['summary'], min: 1, max: 2000),
      layout: _choice(m['layout'], {'stack', 'grid'}),
      components: List.unmodifiable(components),
      createdAt: created,
      updatedAt: updated,
      contentSha256: hash,
      contentScope: _choice(m['content_scope'], {
        'local_only',
        'public_synthetic',
        'private_cloud_authorized',
      }),
      viewState: Map.unmodifiable(state),
      viewRevision: _integer(m['view_revision']),
      taskSubmitSequence: m.containsKey('task_submit_sequence')
          ? _integer(m['task_submit_sequence'])
          : 0,
    );
  }

  static IntelligentArtifact? tryParse(Object? value) {
    try {
      return IntelligentArtifact.fromJson(value);
    } on FormatException {
      return null;
    }
  }

  final bool supported;
  final String schemaVersion, artifactId, sessionId, threadId, taskId;
  final int epoch,
      objectiveRevision,
      revision,
      viewRevision,
      taskSubmitSequence;
  final String title, summary, layout, contentSha256, contentScope;
  final num createdAt, updatedAt;
  final List<IntelligentArtifactComponent> components;
  final Map<String, Object?> viewState;

  Map<String, Object?> toJson() => {
    'schema_version': schemaVersion,
    'artifact_id': artifactId,
    'session_id': sessionId,
    'thread_id': threadId,
    'epoch': epoch,
    'task_id': taskId,
    'objective_revision': objectiveRevision,
    'revision': revision,
    'title': title,
    'summary': summary,
    'layout': layout,
    'components': components.map((c) => c.toJson()).toList(),
    'created_at': createdAt,
    'updated_at': updatedAt,
    'content_sha256': contentSha256,
    'content_scope': contentScope,
    'view_state': viewState,
    'view_revision': viewRevision,
    'task_submit_sequence': taskSubmitSequence,
  };
}
