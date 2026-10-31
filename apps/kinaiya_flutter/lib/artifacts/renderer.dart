/// Native presentation of validated Intelligent UI data. This library has no
/// service client, URI launcher, web view, evaluator, or inference dependency.
library;

import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../ui/design_tokens.dart';
import 'models.dart';

typedef ArtifactAction =
    FutureOr<void> Function(String componentId, Object? value);

class IntelligentArtifactRenderer extends StatefulWidget {
  const IntelligentArtifactRenderer({
    super.key,
    required this.artifact,
    this.enabled = true,
    this.onAction,
    this.onAsk,
    this.onEditingChanged,
  }) : raw = null;

  /// Older clients can retain a bounded title/summary even for an unknown
  /// protocol version. Invalid data never reaches interactive component code.
  const IntelligentArtifactRenderer.fromRaw({
    super.key,
    required this.raw,
    this.enabled = true,
    this.onAction,
    this.onAsk,
    this.onEditingChanged,
  }) : artifact = null;

  final IntelligentArtifact? artifact;
  final Object? raw;
  final bool enabled;
  final ArtifactAction? onAction;
  final ValueChanged<String>? onAsk;
  final ValueChanged<bool>? onEditingChanged;

  @override
  State<IntelligentArtifactRenderer> createState() =>
      _IntelligentArtifactRendererState();
}

class _IntelligentArtifactRendererState
    extends State<IntelligentArtifactRenderer> {
  final Set<String> _editing = {};
  final Map<String, int> _settledActions = {};
  IntelligentArtifact? _parsed;

  @override
  void initState() {
    super.initState();
    _parsed = widget.artifact ?? IntelligentArtifact.tryParse(widget.raw);
  }

  @override
  void didUpdateWidget(IntelligentArtifactRenderer oldWidget) {
    super.didUpdateWidget(oldWidget);
    final previous = _parsed;
    _parsed = widget.artifact ?? IntelligentArtifact.tryParse(widget.raw);
    if (previous?.artifactId != _parsed?.artifactId ||
        previous?.revision != _parsed?.revision ||
        !widget.enabled) {
      _settledActions.clear();
      if (_editing.isNotEmpty) {
        _editing.clear();
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (mounted && _editing.isEmpty) widget.onEditingChanged?.call(false);
        });
      }
    }
  }

  void _editingChanged(String id, bool editing) {
    final wasEditing = _editing.isNotEmpty;
    if (editing) {
      _editing.add(id);
    } else {
      _editing.remove(id);
    }
    if (wasEditing != _editing.isNotEmpty) {
      widget.onEditingChanged?.call(_editing.isNotEmpty);
    }
  }

  @override
  Widget build(BuildContext context) {
    final artifact = _parsed;
    if (artifact == null || !artifact.supported) {
      final raw = widget.raw;
      return _ArtifactCard(
        title:
            artifact?.title ??
            _fallbackText(raw, 'title', 160) ??
            'Generated result',
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if ((artifact?.summary ?? _fallbackText(raw, 'summary', 2000))
                case final summary?)
              _ReadableText(summary),
            const SizedBox(height: 12),
            const Text(
              'This content is unsupported or invalid. Its interactive '
              'controls are unavailable.',
            ),
          ],
        ),
      );
    }
    return Column(
      key: ValueKey('artifact-${artifact.artifactId}-${artifact.revision}'),
      crossAxisAlignment: CrossAxisAlignment.stretch,
      mainAxisSize: MainAxisSize.min,
      children: [
        Semantics(
          header: true,
          child: Text(
            artifact.title,
            style: Theme.of(context).textTheme.headlineSmall,
          ),
        ),
        const SizedBox(height: 8),
        _ReadableText(artifact.summary),
        if (!widget.enabled) ...[
          const SizedBox(height: 12),
          const Text('Rich rendering is off. Readable result data follows.'),
        ],
        const SizedBox(height: 16),
        LayoutBuilder(
          builder: (context, constraints) {
            final grid =
                widget.enabled &&
                artifact.layout == 'grid' &&
                constraints.maxWidth >= 800 &&
                MediaQuery.textScalerOf(context).scale(16) <= 24;
            if (grid) {
              final width = (constraints.maxWidth - 16) / 2;
              return Wrap(
                spacing: 16,
                runSpacing: 16,
                children: [
                  for (final component in artifact.components)
                    SizedBox(
                      width: width,
                      child: _component(artifact, component),
                    ),
                ],
              );
            }
            return Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                for (var i = 0; i < artifact.components.length; i++) ...[
                  if (i > 0) const SizedBox(height: 16),
                  _component(artifact, artifact.components[i]),
                ],
              ],
            );
          },
        ),
      ],
    );
  }

  Widget _component(
    IntelligentArtifact artifact,
    IntelligentArtifactComponent component,
  ) {
    final data = component.data;
    void act(Object? value) {
      if (component.acceptsValue(value)) {
        final result = widget.onAction?.call(component.id, value);
        if (result is Future<void>) {
          // A resolved owner action is a reconciliation point even when a
          // rejection leaves the authoritative value and revision unchanged.
          unawaited(
            result.whenComplete(() {
              if (mounted &&
                  _parsed?.artifactId == artifact.artifactId &&
                  _parsed?.sessionId == artifact.sessionId &&
                  _parsed?.threadId == artifact.threadId &&
                  _parsed?.epoch == artifact.epoch) {
                setState(
                  () => _settledActions[component.id] =
                      (_settledActions[component.id] ?? 0) + 1,
                );
              }
            }),
          );
        }
      }
    }

    final Object? current = artifact.viewState.containsKey(component.id)
        ? artifact.viewState[component.id]
        : data['value'];
    Widget child;
    if (!widget.enabled) {
      child = _ReadableText(_plain(component, current: current));
    } else {
      switch (component.type) {
        case 'text':
          child = _ReadableText(data['text']! as String);
        case 'status':
          final tone = data['tone']! as String;
          final colors = Theme.of(context).colorScheme;
          final icon = switch (tone) {
            'success' => Icons.check_circle_outline,
            'warning' => Icons.warning_amber_rounded,
            'error' => Icons.error_outline,
            _ => Icons.info_outline,
          };
          child = Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Icon(
                icon,
                color: tone == 'error' ? colors.error : colors.primary,
              ),
              const SizedBox(width: 10),
              Expanded(
                child: _ReadableText('${_capitalized(tone)}: ${data['text']}'),
              ),
            ],
          );
        case 'table':
          child = _ArtifactTable(
            component: component,
            reconciliation: _settledActions[component.id] ?? 0,
            selected: current is int ? current : null,
            onAction: widget.onAction == null ? null : act,
            onEditingChanged: (value) => _editingChanged(component.id, value),
          );
        case 'chart':
          child = _ArtifactChart(component: component);
        case 'diagram':
          child = _ArtifactDiagram(
            component: component,
            reconciliation: _settledActions[component.id] ?? 0,
            selected: current as String?,
            onAction: widget.onAction == null ? null : act,
          );
        case 'source':
          child = _ReadableText(_sourceText(data));
        case 'number':
          child = _ArtifactNumber(
            component: component,
            value: current! as num,
            onAction: widget.onAction == null ? null : act,
            onEditingChanged: (value) => _editingChanged(component.id, value),
          );
        case 'select':
          child = _ArtifactSelect(
            component: component,
            reconciliation: _settledActions[component.id] ?? 0,
            value: current! as String,
            onAction: widget.onAction == null ? null : act,
          );
        case 'ask':
          child = Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              OutlinedButton.icon(
                key: ValueKey('artifact-ask-${component.id}'),
                onPressed: widget.onAsk == null
                    ? null
                    : () => widget.onAsk!(data['message']! as String),
                icon: const Icon(Icons.chat_bubble_outline),
                label: Text(data['label']! as String),
              ),
              const SizedBox(height: 6),
              const Text('Adds a draft to your composer for review.'),
            ],
          );
        default:
          child = const Text('Unsupported component');
      }
    }
    return _ArtifactCard(
      key: ValueKey(
        '${artifact.artifactId}-${artifact.revision}-${component.id}',
      ),
      title: component.title,
      child: child,
    );
  }
}

String? _fallbackText(Object? value, String key, int max) {
  if (value is! Map || value[key] is! String) return null;
  final raw = value[key]! as String;
  // An invalid envelope is never traversed or serialized into fallback output.
  final limited = raw.substring(0, math.min(raw.length, max * 2));
  return String.fromCharCodes(
    limited.runes
        .where(
          (c) =>
              c >= 32 &&
                  !(c >= 127 && c <= 159) &&
                  !(c >= 0xd800 && c <= 0xdfff) ||
              c == 9 ||
              c == 10,
        )
        .take(max),
  );
}

String _capitalized(String value) =>
    value.isEmpty ? value : '${value[0].toUpperCase()}${value.substring(1)}';
String _cell(Object? value) => value == null ? '—' : '$value';
ChipAnimationStyle? _chipAnimationStyle(BuildContext context) =>
    MediaQuery.disableAnimationsOf(context)
    ? ChipAnimationStyle(
        enableAnimation: AnimationStyle.noAnimation,
        selectAnimation: AnimationStyle.noAnimation,
        avatarDrawerAnimation: AnimationStyle.noAnimation,
        deleteDrawerAnimation: AnimationStyle.noAnimation,
      )
    : null;

String _sourceText(Map<String, Object?> data) {
  final observed = data['observed_at'];
  String time = '';
  if (observed is num) {
    if (observed.abs() <= 8640000000000) {
      time =
          '\nObserved: ${DateTime.fromMillisecondsSinceEpoch((observed * 1000).round(), isUtc: true).toIso8601String()}';
    } else {
      time = '\nObserved: $observed Unix seconds';
    }
  }
  return '${data['label']}\n${data['url']}\n'
      'Freshness: ${data['freshness']}$time';
}

String _plain(IntelligentArtifactComponent c, {Object? current}) {
  final d = c.data;
  switch (c.type) {
    case 'text':
      return d['text']! as String;
    case 'status':
      return '${_capitalized(d['tone']! as String)}: ${d['text']}';
    case 'table':
      final columns = (d['columns']! as List).cast<String>();
      final rows = (d['rows']! as List).cast<List<Object?>>();
      if (rows.isEmpty) return '${columns.join(' · ')}\nNo rows.';
      return [
        for (var i = 0; i < rows.length; i++)
          'Row ${i + 1}\n${[for (var j = 0; j < columns.length; j++) '${columns[j]}: ${_cell(rows[i][j])}'].join('\n')}',
      ].join('\n\n');
    case 'chart':
      final labels = d['labels']! as List;
      final values = d['values']! as List;
      return [
        for (var i = 0; i < labels.length; i++)
          '${labels[i]}: ${values[i]} ${d['unit']}',
      ].join('\n');
    case 'diagram':
      final nodes = (d['nodes']! as List).cast<Map<String, Object?>>();
      final edges = (d['edges']! as List).cast<Map<String, Object?>>();
      final labels = {for (final node in nodes) node['id']: node['label']};
      return [
        for (final node in nodes) '${node['label']}\n${node['detail']}',
        for (final edge in edges)
          '${labels[edge['from']]} → ${labels[edge['to']]}: ${edge['label']}',
      ].join('\n');
    case 'source':
      return _sourceText(d);
    case 'number':
      return '${d['label']}: ${current ?? d['value']} '
          '(range ${d['min']} to ${d['max']})';
    case 'select':
      return '${d['label']}: ${current ?? d['value']}\n'
          'Options: ${(d['options']! as List).join(', ')}';
    case 'ask':
      return '${d['label']}\nDraft: ${d['message']}';
    default:
      return 'Unsupported component';
  }
}

class _ArtifactCard extends StatelessWidget {
  const _ArtifactCard({super.key, required this.title, required this.child});
  final String title;
  final Widget child;

  @override
  Widget build(BuildContext context) => DesignPanel(
    padding: const EdgeInsets.all(16),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      mainAxisSize: MainAxisSize.min,
      children: [
        if (title.isNotEmpty) ...[
          Semantics(
            header: true,
            child: Text(title, style: Theme.of(context).textTheme.titleMedium),
          ),
          const SizedBox(height: 12),
        ],
        child,
      ],
    ),
  );
}

/// Selection is a long-press action too, so single-line readable text keeps
/// the same minimum touch height as the surrounding native controls.
class _ReadableText extends StatelessWidget {
  const _ReadableText(this.text);
  final String text;

  @override
  Widget build(BuildContext context) => ConstrainedBox(
    constraints: const BoxConstraints(minHeight: 48),
    child: SelectableText(text),
  );
}

class _ArtifactTable extends StatefulWidget {
  const _ArtifactTable({
    required this.component,
    required this.reconciliation,
    required this.selected,
    required this.onAction,
    required this.onEditingChanged,
  });
  final IntelligentArtifactComponent component;
  final int reconciliation;
  final int? selected;
  final ValueChanged<Object?>? onAction;
  final ValueChanged<bool> onEditingChanged;

  @override
  State<_ArtifactTable> createState() => _ArtifactTableState();
}

class _ArtifactTableState extends State<_ArtifactTable> {
  String _filter = '';
  int _sortColumn = 0;
  bool _descending = false;
  int? _selected;

  @override
  void initState() {
    super.initState();
    _selected = widget.selected;
  }

  @override
  void didUpdateWidget(_ArtifactTable oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.selected != widget.selected ||
        oldWidget.reconciliation != widget.reconciliation ||
        !identical(oldWidget.component, widget.component)) {
      _selected = widget.selected;
    }
  }

  @override
  Widget build(BuildContext context) {
    final c = widget.component;
    final columns = (c.data['columns']! as List).cast<String>();
    final rows = (c.data['rows']! as List).cast<List<Object?>>();
    final matching = [
      for (var i = 0; i < rows.length; i++)
        if (rows[i].any(
          (v) => _cell(v).toLowerCase().contains(_filter.toLowerCase()),
        ))
          i,
    ];
    matching.sort((a, b) {
      final x = rows[a][_sortColumn], y = rows[b][_sortColumn];
      var result = x is num && y is num
          ? x.compareTo(y)
          : _cell(x).compareTo(_cell(y));
      if (_descending) result = -result;
      return result == 0 ? a.compareTo(b) : result;
    });
    final selectable = c.data['selectable'] == true && widget.onAction != null;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Focus(
          onFocusChange: widget.onEditingChanged,
          child: TextField(
            key: ValueKey('artifact-table-filter-${c.id}'),
            maxLength: 160,
            maxLengthEnforcement: MaxLengthEnforcement.enforced,
            decoration: const InputDecoration(
              labelText: 'Filter rows',
              prefixIcon: Icon(Icons.search),
              counterText: '',
            ),
            onChanged: (value) => setState(() => _filter = value),
          ),
        ),
        const SizedBox(height: 10),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: [
            const Text('Sort by'),
            for (var i = 0; i < columns.length; i++)
              OutlinedButton(
                key: ValueKey('artifact-sort-${c.id}-$i'),
                onPressed: () => setState(() {
                  _descending = _sortColumn == i ? !_descending : false;
                  _sortColumn = i;
                }),
                child: Text(
                  '${columns[i]}${_sortColumn == i ? (_descending ? ' ↓' : ' ↑') : ''}',
                ),
              ),
            if (_selected != null && selectable)
              TextButton(
                onPressed: () {
                  setState(() => _selected = null);
                  widget.onAction!(null);
                },
                child: const Text('Clear selection'),
              ),
          ],
        ),
        const SizedBox(height: 8),
        Text('${matching.length} of ${rows.length} rows'),
        const SizedBox(height: 8),
        if (matching.isEmpty) const Text('No matching rows.'),
        for (final index in matching)
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: Semantics(
              selected: _selected == index,
              child: Material(
                color: _selected == index
                    ? Theme.of(context).colorScheme.secondaryContainer
                    : Theme.of(context).colorScheme.surface,
                borderRadius: BorderRadius.circular(8),
                child: InkWell(
                  key: ValueKey('artifact-row-${c.id}-$index'),
                  borderRadius: BorderRadius.circular(8),
                  onTap: !selectable
                      ? null
                      : () {
                          final value = _selected == index ? null : index;
                          setState(() => _selected = value);
                          widget.onAction!(value);
                        },
                  child: Padding(
                    padding: const EdgeInsets.all(12),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        Text(
                          'Row ${index + 1}${_selected == index ? ' · Selected' : ''}',
                          style: Theme.of(context).textTheme.labelLarge,
                        ),
                        for (var j = 0; j < columns.length; j++)
                          Padding(
                            padding: const EdgeInsets.only(top: 4),
                            child: Text(
                              '${columns[j]}: ${_cell(rows[index][j])}',
                            ),
                          ),
                      ],
                    ),
                  ),
                ),
              ),
            ),
          ),
      ],
    );
  }
}

class _ArtifactChart extends StatefulWidget {
  const _ArtifactChart({required this.component});
  final IntelligentArtifactComponent component;
  @override
  State<_ArtifactChart> createState() => _ArtifactChartState();
}

class _ArtifactChartState extends State<_ArtifactChart> {
  bool _dataVisible = false;
  int? _selected;
  @override
  Widget build(BuildContext context) {
    final c = widget.component;
    final labels = (c.data['labels']! as List).cast<String>();
    final values = (c.data['values']! as List).cast<num>();
    final unit = c.data['unit']! as String;
    final colors = Theme.of(context).colorScheme;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(
          '${_capitalized(c.data['style']! as String)} chart'
          '${unit.isEmpty ? '' : ' · $unit'}',
        ),
        const SizedBox(height: 8),
        Semantics(
          label:
              'Chart with ${values.length} data points. '
              'Use the data controls below to inspect exact values.',
          image: true,
          child: SizedBox(
            height: 160,
            child: CustomPaint(
              painter: _ChartPainter(
                values: values,
                line: c.data['style'] == 'line',
                color: colors.primary,
                baseline: colors.outlineVariant,
                selected: _selected,
              ),
            ),
          ),
        ),
        if (_selected case final index?)
          Text(
            '${labels[index]}: ${values[index]} $unit',
            key: ValueKey('artifact-chart-selected-${c.id}'),
          ),
        const SizedBox(height: 8),
        Align(
          alignment: Alignment.centerLeft,
          child: TextButton.icon(
            key: ValueKey('artifact-chart-data-${c.id}'),
            onPressed: () => setState(() => _dataVisible = !_dataVisible),
            icon: Icon(
              _dataVisible ? Icons.expand_less : Icons.table_rows_outlined,
            ),
            label: Text(_dataVisible ? 'Hide chart data' : 'Show chart data'),
          ),
        ),
        if (_dataVisible)
          for (var i = 0; i < values.length; i++)
            TextButton(
              key: ValueKey('artifact-chart-point-${c.id}-$i'),
              style: TextButton.styleFrom(alignment: Alignment.centerLeft),
              onPressed: () => setState(() => _selected = i),
              child: Text('${labels[i]}: ${values[i]} $unit'),
            ),
      ],
    );
  }
}

class _ChartPainter extends CustomPainter {
  const _ChartPainter({
    required this.values,
    required this.line,
    required this.color,
    required this.baseline,
    required this.selected,
  });
  final List<num> values;
  final bool line;
  final Color color, baseline;
  final int? selected;

  @override
  void paint(Canvas canvas, Size size) {
    const inset = 8.0;
    final height = math.max(0.0, size.height - inset * 2);
    final width = math.max(0.0, size.width - inset * 2);
    final low = math.min(
      0.0,
      values.reduce((a, b) => a < b ? a : b).toDouble(),
    );
    final high = math.max(
      0.0,
      values.reduce((a, b) => a > b ? a : b).toDouble(),
    );
    final span = high == low ? 1.0 : high - low;
    double y(num value) => inset + height * (high - value) / span;
    final zero = y(0);
    canvas.drawLine(
      Offset(inset, zero),
      Offset(inset + width, zero),
      Paint()
        ..color = baseline
        ..strokeWidth = 1,
    );
    final step = width / values.length;
    final path = Path();
    final paint = Paint()
      ..color = color
      ..strokeWidth = 2;
    for (var i = 0; i < values.length; i++) {
      final x = inset + step * (i + .5);
      final point = Offset(x, y(values[i]));
      if (line) {
        if (i == 0) {
          path.moveTo(point.dx, point.dy);
        } else {
          path.lineTo(point.dx, point.dy);
        }
        canvas.drawCircle(point, selected == i ? 5 : 3, paint);
      } else {
        final rect = Rect.fromLTRB(
          inset + step * i + step * .12,
          math.min(zero, point.dy),
          inset + step * (i + 1) - step * .12,
          math.max(zero, point.dy) + (values[i] == 0 ? 1 : 0),
        );
        canvas.drawRect(
          rect,
          paint
            ..color = selected == null || selected == i
                ? color
                : color.withValues(alpha: .45),
        );
      }
    }
    if (line) canvas.drawPath(path, paint..style = PaintingStyle.stroke);
  }

  @override
  bool shouldRepaint(_ChartPainter oldDelegate) =>
      values != oldDelegate.values ||
      line != oldDelegate.line ||
      color != oldDelegate.color ||
      baseline != oldDelegate.baseline ||
      selected != oldDelegate.selected;
}

class _ArtifactDiagram extends StatefulWidget {
  const _ArtifactDiagram({
    required this.component,
    required this.reconciliation,
    required this.selected,
    required this.onAction,
  });
  final IntelligentArtifactComponent component;
  final int reconciliation;
  final String? selected;
  final ValueChanged<Object?>? onAction;

  @override
  State<_ArtifactDiagram> createState() => _ArtifactDiagramState();
}

class _ArtifactDiagramState extends State<_ArtifactDiagram> {
  String? _selected;

  @override
  void initState() {
    super.initState();
    _selected = widget.selected;
  }

  @override
  void didUpdateWidget(_ArtifactDiagram oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.selected != widget.selected ||
        oldWidget.reconciliation != widget.reconciliation ||
        !identical(oldWidget.component, widget.component)) {
      _selected = widget.selected;
    }
  }

  @override
  Widget build(BuildContext context) {
    final component = widget.component;
    final nodes = (component.data['nodes']! as List)
        .cast<Map<String, Object?>>();
    final edges = (component.data['edges']! as List)
        .cast<Map<String, Object?>>();
    final labels = {for (final node in nodes) node['id']: node['label']};
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        const Text('Nodes'),
        const SizedBox(height: 8),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            for (final node in nodes)
              ChoiceChip(
                key: ValueKey('artifact-node-${component.id}-${node['id']}'),
                label: Text(node['label']! as String),
                chipAnimationStyle: _chipAnimationStyle(context),
                selected: _selected == node['id'],
                onSelected: widget.onAction == null
                    ? null
                    : (_) {
                        setState(() => _selected = node['id']! as String);
                        widget.onAction!(node['id']);
                      },
                materialTapTargetSize: MaterialTapTargetSize.padded,
              ),
          ],
        ),
        if (_selected != null) ...[
          const SizedBox(height: 12),
          for (final node in nodes.where((n) => n['id'] == _selected))
            _ReadableText('${node['label']}\n${node['detail']}'),
        ],
        const SizedBox(height: 12),
        const Text('Relationships'),
        if (edges.isEmpty) const Text('No relationships supplied.'),
        for (final edge in edges)
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Text(
              '${labels[edge['from']]} → ${labels[edge['to']]}'
              '${edge['label'] == '' ? '' : ': ${edge['label']}'}',
            ),
          ),
      ],
    );
  }
}

class _ArtifactNumber extends StatefulWidget {
  const _ArtifactNumber({
    required this.component,
    required this.value,
    required this.onAction,
    required this.onEditingChanged,
  });
  final IntelligentArtifactComponent component;
  final num value;
  final ValueChanged<Object?>? onAction;
  final ValueChanged<bool> onEditingChanged;
  @override
  State<_ArtifactNumber> createState() => _ArtifactNumberState();
}

class _ArtifactNumberState extends State<_ArtifactNumber> {
  late final TextEditingController _controller;
  String? _error;
  @override
  void initState() {
    super.initState();
    _controller = TextEditingController(text: '${widget.value}');
  }

  @override
  void didUpdateWidget(_ArtifactNumber oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.value != widget.value) {
      _controller.text = '${widget.value}';
      _error = null;
    }
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  void _apply() {
    final value = num.tryParse(_controller.text.trim());
    if (!widget.component.acceptsValue(value)) {
      setState(
        () => _error =
            'Enter a finite number from '
            '${widget.component.data['min']} to ${widget.component.data['max']}.',
      );
      return;
    }
    setState(() => _error = null);
    widget.onAction?.call(value);
  }

  @override
  Widget build(BuildContext context) {
    final c = widget.component;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Focus(
          onFocusChange: widget.onEditingChanged,
          child: TextField(
            key: ValueKey('artifact-number-${c.id}'),
            controller: _controller,
            enabled: widget.onAction != null,
            maxLength: 48,
            maxLengthEnforcement: MaxLengthEnforcement.enforced,
            keyboardType: const TextInputType.numberWithOptions(
              decimal: true,
              signed: true,
            ),
            decoration: InputDecoration(
              labelText: c.data['label']! as String,
              helperText:
                  'Range ${c.data['min']} to ${c.data['max']}'
                  '${c.data['step'] == null ? '' : ' · Step ${c.data['step']}'}',
              errorText: _error,
              counterText: '',
            ),
            onSubmitted: widget.onAction == null ? null : (_) => _apply(),
          ),
        ),
        const SizedBox(height: 8),
        Align(
          alignment: Alignment.centerLeft,
          child: OutlinedButton(
            key: ValueKey('artifact-number-apply-${c.id}'),
            onPressed: widget.onAction == null ? null : _apply,
            child: const Text('Apply value'),
          ),
        ),
      ],
    );
  }
}

class _ArtifactSelect extends StatefulWidget {
  const _ArtifactSelect({
    required this.component,
    required this.reconciliation,
    required this.value,
    required this.onAction,
  });
  final IntelligentArtifactComponent component;
  final int reconciliation;
  final String value;
  final ValueChanged<Object?>? onAction;
  @override
  State<_ArtifactSelect> createState() => _ArtifactSelectState();
}

class _ArtifactSelectState extends State<_ArtifactSelect> {
  late String _selected;
  @override
  void initState() {
    super.initState();
    _selected = widget.value;
  }

  @override
  void didUpdateWidget(_ArtifactSelect oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.value != widget.value ||
        oldWidget.reconciliation != widget.reconciliation ||
        !identical(oldWidget.component, widget.component)) {
      _selected = widget.value;
    }
  }

  @override
  Widget build(BuildContext context) => Column(
    crossAxisAlignment: CrossAxisAlignment.stretch,
    children: [
      Text(widget.component.data['label']! as String),
      const SizedBox(height: 8),
      Wrap(
        spacing: 8,
        runSpacing: 8,
        children: [
          for (final option
              in (widget.component.data['options']! as List).cast<String>())
            ChoiceChip(
              key: ValueKey('artifact-option-${widget.component.id}-$option'),
              label: Text(option),
              chipAnimationStyle: _chipAnimationStyle(context),
              selected: _selected == option,
              materialTapTargetSize: MaterialTapTargetSize.padded,
              onSelected: widget.onAction == null
                  ? null
                  : (_) {
                      setState(() => _selected = option);
                      widget.onAction!(option);
                    },
            ),
        ],
      ),
    ],
  );
}
