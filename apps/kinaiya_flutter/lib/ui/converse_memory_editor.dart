/// Explicit owner-curated immutable note form. Drafts are held by the owner UI
/// in bounded RAM and are never sent until the owner chooses Create note.
library;

import 'dart:convert';

import 'package:flutter/material.dart';

import '../api/memory_models.dart';

class ConverseMemoryDraft {
  int version = 0;
  String? lastOutcome;
  String kind = 'assertion';
  String body = '';
  String factKey = '';
  String factType = 'text';
  String factValue = '';
  String sourceInput = '';
  String? sourceEventId;
  int? sourceRevision;
  String? sourcePreview;
  Object? sourceOwner;
  int? sourceGeneration;
  String? revisesNodeId;
  final Set<String> prerequisiteIds = {};

  bool get hasContent =>
      body.isNotEmpty ||
      factKey.isNotEmpty ||
      factValue.isNotEmpty ||
      sourceInput.isNotEmpty ||
      prerequisiteIds.isNotEmpty ||
      revisesNodeId != null ||
      kind != 'assertion';

  void clearSource() {
    sourceEventId = null;
    sourceRevision = null;
    sourcePreview = null;
    sourceOwner = null;
    sourceGeneration = null;
  }

  void clear() {
    version++;
    lastOutcome = null;
    kind = 'assertion';
    body = '';
    factKey = '';
    factType = 'text';
    factValue = '';
    sourceInput = '';
    revisesNodeId = null;
    prerequisiteIds.clear();
    clearSource();
  }

  void revise(MemoryNode node) {
    clear();
    kind = node.kind;
    body = node.body ?? '';
    revisesNodeId = node.nodeId;
    prerequisiteIds.addAll(node.prerequisiteIds);
    final fact = node.fact;
    if (fact != null) {
      factKey = fact.key;
      final value = fact.value;
      if (value == null) {
        factType = 'null';
      } else if (value is bool) {
        factType = 'boolean';
        factValue = '$value';
      } else if (value is int) {
        factType = 'integer';
        factValue = '$value';
      } else {
        factValue = value as String;
      }
    }
  }

  Object? get typedValue => switch (factType) {
    'text' => factValue,
    'integer' => int.tryParse(factValue),
    'boolean' => factValue == 'true',
    _ => null,
  };

  MemoryFact? get fact => kind == 'inference'
      ? null
      : MemoryFact.fromJson({
          'schema_version': '1.0',
          'key': factKey,
          'value': typedValue,
        });

  String? validate(Set<String> activeIds) {
    if (sourceEventId == null || sourceRevision == null) {
      return 'Inspect a finalized user source before creating a note.';
    }
    if (!_textValid(body, 2048) || body.trim().isEmpty) {
      return 'Note text must contain 1–2048 UTF-8 bytes; only newline and tab controls are allowed.';
    }
    if (prerequisiteIds.length > 8 ||
        prerequisiteIds.any((id) => !activeIds.contains(id))) {
      return 'Choose at most eight currently active prerequisites.';
    }
    if (kind == 'inference') {
      return prerequisiteIds.isEmpty
          ? 'An inference requires at least one active prerequisite.'
          : null;
    }
    final keyMatch = RegExp(r'^[A-Za-z0-9_.:/-]{1,128}$').firstMatch(factKey);
    if (keyMatch == null || keyMatch.end != factKey.length) {
      return 'Use a fact key with 1–128 ASCII letters, numbers, or _ . : / -.';
    }
    if (factType == 'text' && !_textValid(factValue, 512)) {
      return 'Fact text must use at most 512 UTF-8 bytes.';
    }
    if (factType == 'integer') {
      final value = int.tryParse(factValue);
      final match = RegExp(r'^-?(0|[1-9][0-9]*)$').firstMatch(factValue);
      if (match == null ||
          match.end != factValue.length ||
          value == null ||
          value.abs() > memoryMaxSafeInteger) {
        return 'Use a whole number within ±9,007,199,254,740,991.';
      }
    }
    if (factType == 'boolean' && !const {'true', 'false'}.contains(factValue)) {
      return 'Choose true or false for the boolean fact.';
    }
    return null;
  }

  static bool _textValid(String text, int max) {
    for (var i = 0; i < text.length; i++) {
      final unit = text.codeUnitAt(i);
      if (unit >= 0xd800 && unit <= 0xdbff) {
        if (++i >= text.length ||
            text.codeUnitAt(i) < 0xdc00 ||
            text.codeUnitAt(i) > 0xdfff) {
          return false;
        }
      } else if (unit >= 0xdc00 && unit <= 0xdfff) {
        return false;
      }
      if ((unit < 32 || (unit >= 127 && unit <= 159)) &&
          unit != 9 &&
          unit != 10) {
        return false;
      }
    }
    return utf8.encode(text).length <= max;
  }
}

class ConverseMemoryEditor extends StatefulWidget {
  const ConverseMemoryEditor({
    super.key,
    required this.draft,
    required this.entries,
    required this.sources,
    required this.enabled,
    required this.canCreate,
    required this.busy,
    required this.sourceBusy,
    required this.onChanged,
    required this.onInspectSource,
    required this.onCreate,
    required this.onDiscard,
    this.problem,
  });

  final ConverseMemoryDraft draft;
  final List<MemorySummary> entries;
  final Map<String, String> sources;
  final bool enabled, canCreate, busy, sourceBusy;
  final VoidCallback onChanged, onCreate, onDiscard;
  final ValueChanged<String> onInspectSource;
  final String? problem;

  @override
  State<ConverseMemoryEditor> createState() => _ConverseMemoryEditorState();
}

class _ConverseMemoryEditorState extends State<ConverseMemoryEditor> {
  late final _source = TextEditingController(text: widget.draft.sourceInput);
  late final _body = TextEditingController(text: widget.draft.body);
  late final _key = TextEditingController(text: widget.draft.factKey);
  late final _value = TextEditingController(text: widget.draft.factValue);

  @override
  void dispose() {
    _source.dispose();
    _body.dispose();
    _key.dispose();
    _value.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final draft = widget.draft;
    final writable = widget.enabled && !widget.busy;
    final active = widget.entries.where((entry) => entry.state == 'active');
    final activeIds = active.map((entry) => entry.nodeId).toSet();
    final validation = draft.validate(activeIds);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              draft.revisesNodeId == null
                  ? 'Create a note'
                  : 'Create a revised note',
              style: Theme.of(context).textTheme.titleMedium,
            ),
            const SizedBox(height: 8),
            const Text(
              'Owner-curated · Local only. Notes are immutable. A revision creates a new note; '
              'the original remains until you explicitly forget it. Observations report what '
              'you observed and are not native verification.',
            ),
            if (draft.revisesNodeId != null)
              SelectableText('Draft based on note ${draft.revisesNodeId}.'),
            const SizedBox(height: 12),
            if (widget.sources.isNotEmpty)
              DropdownButtonFormField<String>(
                key: const ValueKey('memory-source-picker'),
                initialValue: null,
                isExpanded: true,
                decoration: const InputDecoration(
                  labelText: 'Recent user message',
                ),
                items: widget.sources.entries
                    .map(
                      (entry) => DropdownMenuItem(
                        value: entry.key,
                        child: Text(
                          entry.value,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                        ),
                      ),
                    )
                    .toList(),
                onChanged: !writable || widget.sourceBusy
                    ? null
                    : (value) {
                        if (value == null) return;
                        _source.text = value;
                        draft.sourceInput = value;
                        draft.clearSource();
                        widget.onInspectSource(value);
                      },
              ),
            const SizedBox(height: 8),
            TextField(
              key: const ValueKey('memory-source-id'),
              controller: _source,
              enabled: writable && !widget.sourceBusy,
              maxLength: 128,
              decoration: const InputDecoration(
                labelText: 'Source event ID',
                helperText:
                    'An existing finalized user message in this conversation.',
              ),
              onChanged: (value) {
                draft.sourceInput = value;
                draft.clearSource();
                widget.onChanged();
              },
            ),
            Align(
              alignment: Alignment.centerLeft,
              child: OutlinedButton.icon(
                onPressed:
                    !writable || widget.sourceBusy || _source.text.isEmpty
                    ? null
                    : () => widget.onInspectSource(_source.text),
                icon: const Icon(Icons.fact_check_outlined),
                label: Text(
                  widget.sourceBusy ? 'Inspecting source…' : 'Inspect source',
                ),
              ),
            ),
            if (draft.sourceEventId != null)
              SelectableText(
                'Inspected source ${draft.sourceEventId}, revision ${draft.sourceRevision}\n'
                '${draft.sourcePreview ?? ''}',
              ),
            const SizedBox(height: 12),
            DropdownButtonFormField<String>(
              initialValue: draft.kind,
              decoration: const InputDecoration(labelText: 'Note kind'),
              items: const [
                DropdownMenuItem(value: 'assertion', child: Text('Assertion')),
                DropdownMenuItem(
                  value: 'observation',
                  child: Text('Observation'),
                ),
                DropdownMenuItem(value: 'inference', child: Text('Inference')),
              ],
              onChanged: !writable
                  ? null
                  : (value) {
                      if (value == null) return;
                      draft.kind = value;
                      widget.onChanged();
                    },
            ),
            const SizedBox(height: 12),
            TextField(
              key: const ValueKey('memory-note-body'),
              controller: _body,
              enabled: writable,
              minLines: 3,
              maxLines: 8,
              maxLength: 2048,
              decoration: const InputDecoration(
                labelText: 'Note text',
                helperText: 'At most 2048 UTF-8 bytes.',
              ),
              onChanged: (value) {
                draft.body = value;
                widget.onChanged();
              },
            ),
            if (draft.kind != 'inference') ...[
              const SizedBox(height: 12),
              TextField(
                controller: _key,
                enabled: writable,
                maxLength: 128,
                decoration: const InputDecoration(
                  labelText: 'Fact key',
                  helperText: 'For example: owner.preferred_language',
                ),
                onChanged: (value) {
                  draft.factKey = value;
                  widget.onChanged();
                },
              ),
              const SizedBox(height: 8),
              DropdownButtonFormField<String>(
                initialValue: draft.factType,
                decoration: const InputDecoration(labelText: 'Fact value type'),
                items: const [
                  DropdownMenuItem(value: 'text', child: Text('Text')),
                  DropdownMenuItem(value: 'integer', child: Text('Integer')),
                  DropdownMenuItem(value: 'boolean', child: Text('Boolean')),
                  DropdownMenuItem(value: 'null', child: Text('Null')),
                ],
                onChanged: !writable
                    ? null
                    : (value) {
                        if (value == null) return;
                        draft.factType = value;
                        if (value == 'boolean' &&
                            !{'true', 'false'}.contains(draft.factValue)) {
                          draft.factValue = 'false';
                          _value.text = 'false';
                        }
                        widget.onChanged();
                      },
              ),
              const SizedBox(height: 8),
              if (draft.factType == 'boolean')
                DropdownButtonFormField<String>(
                  initialValue: {'true', 'false'}.contains(draft.factValue)
                      ? draft.factValue
                      : null,
                  decoration: const InputDecoration(labelText: 'Boolean value'),
                  items: const [
                    DropdownMenuItem(value: 'true', child: Text('True')),
                    DropdownMenuItem(value: 'false', child: Text('False')),
                  ],
                  onChanged: !writable
                      ? null
                      : (value) {
                          draft.factValue = value ?? '';
                          _value.text = draft.factValue;
                          widget.onChanged();
                        },
                )
              else if (draft.factType != 'null')
                TextField(
                  controller: _value,
                  enabled: writable,
                  maxLength: 512,
                  minLines: 1,
                  maxLines: draft.factType == 'text' ? 3 : 1,
                  decoration: const InputDecoration(labelText: 'Fact value'),
                  onChanged: (value) {
                    draft.factValue = value;
                    widget.onChanged();
                  },
                ),
            ],
            const SizedBox(height: 12),
            Text('Prerequisites (${draft.prerequisiteIds.length}/8)'),
            const Text(
              'Dependencies are immutable. Forgetting a prerequisite also erases every dependent note.',
            ),
            if (active.isEmpty) const Text('No active prerequisite notes.'),
            for (final entry in active)
              CheckboxListTile(
                contentPadding: EdgeInsets.zero,
                value: draft.prerequisiteIds.contains(entry.nodeId),
                dense: true,
                title: Text(
                  '#${entry.ordinal} · ${entry.preview ?? entry.nodeId}',
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                ),
                onChanged:
                    !writable ||
                        (!draft.prerequisiteIds.contains(entry.nodeId) &&
                            draft.prerequisiteIds.length >= 8)
                    ? null
                    : (value) {
                        if (value == true) {
                          draft.prerequisiteIds.add(entry.nodeId);
                        } else {
                          draft.prerequisiteIds.remove(entry.nodeId);
                        }
                        widget.onChanged();
                      },
              ),
            if (draft.prerequisiteIds.any((id) => !activeIds.contains(id)))
              TextButton(
                onPressed: !writable
                    ? null
                    : () {
                        draft.prerequisiteIds.removeWhere(
                          (id) => !activeIds.contains(id),
                        );
                        widget.onChanged();
                      },
                child: const Text('Remove unavailable prerequisites'),
              ),
            if (widget.problem != null) Text(widget.problem!),
            if (validation != null) Text(validation),
            const SizedBox(height: 12),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                FilledButton.icon(
                  key: const ValueKey('memory-create-note'),
                  onPressed: writable && widget.canCreate && validation == null
                      ? widget.onCreate
                      : null,
                  icon: const Icon(Icons.add),
                  label: Text(widget.busy ? 'Saving…' : 'Create note'),
                ),
                OutlinedButton(
                  onPressed: widget.busy ? null : widget.onDiscard,
                  child: const Text('Discard draft'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
