import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../api/client.dart';
import '../api/decisions_client.dart';
import '../api/decisions_models.dart';
import '../state/app_state.dart';

/// Owner evidence inspector. Navigation and copying never classify or dispatch.
class DecisionsView extends StatefulWidget {
  const DecisionsView({super.key, required this.state});
  final AppState state;
  @override
  State<DecisionsView> createState() => _DecisionsViewState();
}

class _DecisionsViewState extends State<DecisionsView> {
  final _search = TextEditingController(),
      _session = TextEditingController(),
      _timezone = TextEditingController(text: 'UTC');
  KinaiyaClient? _client;
  int _generation = 0, _listNonce = 0, _detailNonce = 0;
  String _source = 'all', _outcome = '', _dateMode = 'any';
  DateTime? _from, _to;
  Map<String, String> _filters = {'timezone': 'UTC'};
  DecisionPage? _page;
  DecisionDetail? _detail;
  String? _selected, _listError, _detailError;
  bool _loading = false, _detailLoading = false;
  int? _through, _before;
  final List<int?> _previous = [];
  ModalRoute<void>? _evidenceRoute;
  void _dismissEvidence() {
    final route = _evidenceRoute;
    _evidenceRoute = null;
    if (route != null) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (route.isActive) route.navigator?.removeRoute(route);
      });
    }
  }

  AppState get _s => widget.state;
  bool _owns(KinaiyaClient api, int generation) =>
      mounted &&
      identical(_s.client, api) &&
      _s.connectionGeneration == generation;
  @override
  void initState() {
    super.initState();
    _s.addListener(_connection);
    _connection();
  }

  @override
  void didUpdateWidget(covariant DecisionsView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.state, _s)) {
      oldWidget.state.removeListener(_connection);
      _client = null;
      _generation = -1;
      _s.addListener(_connection);
      _connection();
    }
  }

  void _connection() {
    if (identical(_client, _s.client) &&
        _generation == _s.connectionGeneration) {
      return;
    }
    _dismissEvidence();
    _filters = {'timezone': 'UTC'};
    _search.clear();
    _session.clear();
    _timezone.text = 'UTC';
    _source = 'all';
    _outcome = '';
    _dateMode = 'any';
    _from = null;
    _to = null;
    _client = _s.client;
    _generation = _s.connectionGeneration;
    _listNonce++;
    _detailNonce++;
    _page = null;
    _detail = null;
    _selected = null;
    _listError = null;
    _detailError = null;
    _loading = false;
    _detailLoading = false;
    _through = null;
    _before = null;
    _previous.clear();
    if (mounted) setState(() {});
    if (_client != null) unawaited(_load());
  }

  @override
  void dispose() {
    _s.removeListener(_connection);
    _dismissEvidence();
    _search.dispose();
    _session.dispose();
    _timezone.dispose();
    _listNonce++;
    _detailNonce++;
    super.dispose();
  }

  String _date(DateTime d) =>
      '${d.year.toString().padLeft(4, '0')}-${d.month.toString().padLeft(2, '0')}-${d.day.toString().padLeft(2, '0')}';
  void _apply() {
    if (_loading) return;
    if (_dateMode != 'any' &&
        (_from == null || (_dateMode == 'range' && _to == null))) {
      setState(
        () => _listError = 'Choose the source date or both range dates.',
      );
      return;
    }
    if (_from != null &&
        _to != null &&
        _dateMode == 'range' &&
        _to!.isBefore(_from!)) {
      setState(() => _listError = 'The end date must follow the start date.');
      return;
    }
    _filters = {
      'timezone': _timezone.text.trim(),
      'source': _source,
      if (_search.text.isNotEmpty) 'search': _search.text,
      if (_session.text.trim().isNotEmpty) 'session_id': _session.text.trim(),
      if (_outcome.isNotEmpty) 'outcome': _outcome,
      if (_dateMode != 'any') 'from_date': _date(_from!),
      if (_dateMode != 'any')
        'to_date': _date(_dateMode == 'single' ? _from! : _to!),
    };
    _through = null;
    _before = null;
    _previous.clear();
    unawaited(_load());
  }

  Future<void> _load() async {
    final api = _client;
    if (api == null || _loading) return;
    final generation = _generation, nonce = ++_listNonce;
    setState(() {
      _loading = true;
      _listError = null;
      _selected = null;
      _detail = null;
      _detailError = null;
      _detailNonce++;
    });
    try {
      final page = await api.decisionInspections.list(
        _filters,
        through: _through,
        before: _before,
      );
      if (!_owns(api, generation) || nonce != _listNonce) return;
      if ((_through != null && page.through != _through) ||
          (_before != null && page.rows.any((r) => r.sequence >= _before!))) {
        throw const FormatException('Invalid continuation');
      }
      setState(() {
        _page = page;
        _through = page.through;
      });
    } catch (e) {
      if (_owns(api, generation) && nonce == _listNonce) {
        setState(() {
          _page = null;
          _listError = e is DecisionsFailure
              ? e.message
              : 'Invalid Decisions evidence returned by this host.';
        });
      }
    } finally {
      if (_owns(api, generation) && nonce == _listNonce) {
        setState(() => _loading = false);
      }
    }
  }

  void _select(String id) {
    setState(() {
      _selected = id;
      _detail = null;
      _detailError = null;
      _detailNonce++;
    });
    if (!_detailLoading) unawaited(_loadDetail());
  }

  Future<void> _loadDetail({bool older = false}) async {
    final api = _client, id = _selected;
    if (api == null || id == null || _detailLoading) return;
    final generation = _generation, nonce = _detailNonce, prior = _detail;
    setState(() {
      _detailLoading = true;
      _detailError = null;
    });
    try {
      final detail = await api.decisionInspections.detail(
        id,
        through: older ? prior?.through : null,
        before: older ? prior?.next : null,
      );
      if (!_owns(api, generation) || nonce != _detailNonce || _selected != id) {
        return;
      }
      if (older &&
          (detail.through != prior?.through ||
              detail.calls.any((c) => c.sequence >= prior!.next!))) {
        throw const FormatException('Invalid call continuation');
      }
      setState(() => _detail = detail);
    } catch (e) {
      if (_owns(api, generation) && nonce == _detailNonce && _selected == id) {
        setState(
          () => _detailError = e is DecisionsFailure
              ? e.message
              : 'Invalid Decisions detail returned by this host.',
        );
      }
    } finally {
      if (_owns(api, generation)) {
        setState(() => _detailLoading = false);
        if (_selected != null && (_selected != id || nonce != _detailNonce)) {
          unawaited(_loadDetail());
        }
      }
    }
  }

  Future<void> _pick(bool end) async {
    final d = await showDatePicker(
      context: context,
      initialDate: (end ? _to : _from) ?? DateTime.now(),
      firstDate: DateTime(1970),
      lastDate: DateTime(2100),
    );
    if (mounted && d != null) {
      setState(() {
        if (end) {
          _to = d;
        } else {
          _from = d;
        }
      });
    }
  }

  Widget _field(String label, TextEditingController c, {int max = 128}) =>
      SizedBox(
        width: 220,
        child: TextField(
          controller: c,
          maxLength: max,
          decoration: InputDecoration(labelText: label, counterText: ''),
          onSubmitted: (_) => _apply(),
        ),
      );
  Widget _dropdown({
    required String value,
    Widget? hint,
    required List<DropdownMenuItem<String>> items,
    ValueChanged<String?>? onChanged,
  }) => SizedBox(
    width: 240,
    child: DefaultTextStyle.merge(
      maxLines: 1,
      overflow: TextOverflow.ellipsis,
      child: DropdownButton<String>(
        isExpanded: true,
        itemHeight: 80,
        value: value,
        hint: hint,
        items: items,
        onChanged: onChanged,
      ),
    ),
  );
  @override
  Widget build(BuildContext context) => Material(
    child: Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Wrap(
            spacing: 12,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              Text(
                'Decisions',
                style: Theme.of(context).textTheme.headlineSmall,
              ),
              const Text('Trace what Kinaiya considered and decided'),
              IconButton(
                tooltip: 'Refresh decisions',
                onPressed: _client == null || _loading
                    ? null
                    : () {
                        _through = null;
                        _before = null;
                        _previous.clear();
                        unawaited(_load());
                      },
                icon: const Icon(Icons.refresh),
              ),
            ],
          ),
          if (_client == null)
            const Expanded(
              child: Center(
                child: Text('Connect to inspect recorded decisions.'),
              ),
            )
          else
            Expanded(
              child: LayoutBuilder(
                builder: (context, box) {
                  final wide =
                      box.maxWidth /
                          MediaQuery.textScalerOf(
                            context,
                          ).scale(1).clamp(1, 2) >=
                      1050;
                  final filters = Wrap(
                    spacing: 12,
                    runSpacing: 8,
                    crossAxisAlignment: WrapCrossAlignment.center,
                    children: [
                      _field(
                        'Search decisions or source text',
                        _search,
                        max: 200,
                      ),
                      _field('Session ID', _session),
                      _dropdown(
                        value: _source,
                        hint: const Text('Source'),
                        items: const [
                          DropdownMenuItem(
                            value: 'all',
                            child: Text('All sources'),
                          ),
                          DropdownMenuItem(
                            value: 'conversation',
                            child: Text('Converse'),
                          ),
                          DropdownMenuItem(
                            value: 'agent',
                            child: Text('Agent'),
                          ),
                        ],
                        onChanged: (v) => setState(() => _source = v!),
                      ),
                      _dropdown(
                        value: _outcome,
                        items:
                            [
                                  '',
                                  'ignore',
                                  'queue',
                                  'steer',
                                  'interrupt',
                                  'refused',
                                  'error',
                                  'no_inference',
                                  'unavailable',
                                  'succeeded',
                                  'failed',
                                ]
                                .map(
                                  (v) => DropdownMenuItem(
                                    value: v,
                                    child: Text(v.isEmpty ? 'All outcomes' : v),
                                  ),
                                )
                                .toList(),
                        onChanged: (v) => setState(() => _outcome = v!),
                      ),
                      _dropdown(
                        value: _dateMode,
                        items: const [
                          DropdownMenuItem(
                            value: 'any',
                            child: Text('Any source date'),
                          ),
                          DropdownMenuItem(
                            value: 'single',
                            child: Text('Single source date'),
                          ),
                          DropdownMenuItem(
                            value: 'range',
                            child: Text('Source date range'),
                          ),
                        ],
                        onChanged: (v) => setState(() => _dateMode = v!),
                      ),
                      if (_dateMode != 'any')
                        TextButton(
                          onPressed: () => _pick(false),
                          child: Text(
                            _from == null ? 'From date' : _date(_from!),
                          ),
                        ),
                      if (_dateMode == 'range')
                        TextButton(
                          onPressed: () => _pick(true),
                          child: Text(_to == null ? 'To date' : _date(_to!)),
                        ),
                      _field('IANA timezone', _timezone),
                      FilledButton(
                        onPressed: _loading ? null : _apply,
                        child: const Text('Apply filters'),
                      ),
                    ],
                  );
                  final timeline = _timeline();
                  final detail = _detailPanel();
                  return Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      ConstrainedBox(
                        constraints: BoxConstraints(
                          maxHeight: box.maxHeight * .45,
                        ),
                        child: SingleChildScrollView(child: filters),
                      ),
                      if (_page?.rangeStart != null)
                        Text(
                          'Source date: [${_page!.rangeStart}, ${_page!.rangeEnd}) · ${_page!.timezone}',
                          style: Theme.of(context).textTheme.bodySmall,
                        ),
                      const SizedBox(height: 8),
                      Expanded(
                        child: wide
                            ? Row(
                                crossAxisAlignment: CrossAxisAlignment.stretch,
                                children: [
                                  Expanded(flex: 3, child: timeline),
                                  const VerticalDivider(),
                                  Expanded(flex: 2, child: detail),
                                ],
                              )
                            : (_selected == null ? timeline : detail),
                      ),
                    ],
                  );
                },
              ),
            ),
        ],
      ),
    ),
  );
  Widget _timeline() => Column(
    children: [
      if (_loading) const LinearProgressIndicator(),
      if (_listError != null) _error(_listError!, () => unawaited(_load())),
      Expanded(
        child: _page == null || _page!.rows.isEmpty
            ? Center(
                child: Text(
                  _loading
                      ? 'Reading recorded sources…'
                      : 'No recorded sources match these filters.',
                ),
              )
            : SingleChildScrollView(
                scrollDirection: Axis.horizontal,
                child: SizedBox(
                  width:
                      1800 *
                      MediaQuery.textScalerOf(context).scale(1).clamp(1, 2),
                  child: SingleChildScrollView(
                    child: DataTable(
                      showCheckboxColumn: false,
                      dataRowMinHeight:
                          72 * MediaQuery.textScalerOf(context).scale(1),
                      dataRowMaxHeight:
                          96 * MediaQuery.textScalerOf(context).scale(1),
                      columns: [
                        'Time (UTC)',
                        'Session ID',
                        'Source',
                        'Decision / result',
                        'Confidence',
                        'Model',
                        'Provider',
                      ].map((s) => DataColumn(label: Text(s))).toList(),
                      rows: _page!.rows
                          .map(
                            (r) => DataRow(
                              selected: r.eventId == _selected,
                              onSelectChanged: (_) => _select(r.eventId),
                              cells: [
                                DataCell(
                                  Text(
                                    DateTime.parse(
                                      r.createdAt,
                                    ).toUtc().toIso8601String(),
                                  ),
                                ),
                                DataCell(_cell(r.sessionId, 110)),
                                DataCell(
                                  Text(r.isAgent ? 'Agent' : 'Converse'),
                                ),
                                DataCell(
                                  _cell(
                                    '${r.outcome ?? r.disposition}\n${r.preview}',
                                    230,
                                  ),
                                ),
                                DataCell(Text(r.confidenceLabel)),
                                DataCell(_cell(r.model ?? 'Unavailable', 120)),
                                DataCell(
                                  _cell(r.provider ?? 'Unavailable', 100),
                                ),
                              ],
                            ),
                          )
                          .toList(),
                    ),
                  ),
                ),
              ),
      ),
      Row(
        children: [
          IconButton(
            tooltip: 'Newer sources',
            onPressed: _loading || _previous.isEmpty
                ? null
                : () {
                    _before = _previous.removeLast();
                    unawaited(_load());
                  },
            icon: const Icon(Icons.chevron_left),
          ),
          Expanded(
            child: Text(
              '${_page?.rows.length ?? 0}',
              semanticsLabel: '${_page?.rows.length ?? 0} sources in this page',
              textAlign: TextAlign.center,
            ),
          ),
          IconButton(
            tooltip: 'Older sources',
            onPressed:
                _loading ||
                    _page == null ||
                    _page!.exhausted ||
                    _previous.length >= 1000
                ? null
                : () {
                    _previous.add(_before);
                    _before = _page!.next;
                    unawaited(_load());
                  },
            icon: const Icon(Icons.chevron_right),
          ),
        ],
      ),
    ],
  );
  Widget _cell(String s, double width) => SizedBox(
    width: width,
    child: Tooltip(
      message: s,
      child: Text(s, maxLines: 2, overflow: TextOverflow.ellipsis),
    ),
  );
  Widget _error(String text, VoidCallback retry) => Wrap(
    crossAxisAlignment: WrapCrossAlignment.center,
    children: [
      Text(text),
      TextButton(onPressed: retry, child: const Text('Retry')),
    ],
  );
  Widget _detailPanel() {
    final d = _detail;
    return SingleChildScrollView(
      key: ValueKey('decision-detail-$_selected'),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (_selected != null)
            TextButton.icon(
              onPressed: () => setState(() {
                _selected = null;
                _detail = null;
                _detailNonce++;
              }),
              icon: const Icon(Icons.arrow_back),
              label: const Text('Back to sources'),
            ),
          if (_detailLoading) const LinearProgressIndicator(),
          if (_detailError != null)
            _error(_detailError!, () => unawaited(_loadDetail())),
          if (d == null)
            Text(
              _selected == null
                  ? 'Select a source to inspect its recorded evidence.'
                  : 'Reading source evidence…',
            )
          else ...[
            Text(
              d.summary.outcome ?? d.summary.disposition,
              style: Theme.of(context).textTheme.titleLarge,
            ),
            SelectableText(
              'Source: ${d.summary.eventId}\nSession: ${d.summary.sessionId}\nThread: ${d.summary.threadId} · epoch ${d.summary.epoch}\nRevision: ${d.summary.revision ?? "Unavailable"}\n${d.summary.createdAt}\nDisposition: ${d.summary.disposition}\nDecision: ${d.summary.decisionId ?? "Unavailable"}\nConfidence: ${d.summary.confidenceLabel}\nCapture: ${d.summary.captureState}',
            ),
            const Text(
              'Confidence is a recorded model signal, not calibrated certainty.',
            ),
            _literal(
              d.summary.isAgent ? 'Agent event' : 'Source transcript / event',
              d.source,
            ),
            if (d.summary.isAgent)
              const Text(
                'No Decisions inference. This is a native Agent observation.',
              ),
            if (d.calls.isEmpty && !d.summary.isAgent)
              const Text(
                'Historical submitted script and model output are unavailable. Current settings do not reconstruct them.',
              ),
            for (final c in d.calls) _call(c),
            Wrap(
              spacing: 12,
              children: [
                TextButton(
                  onPressed: _detailLoading
                      ? null
                      : () => unawaited(_loadDetail()),
                  child: const Text('Newest calls'),
                ),
                TextButton(
                  onPressed: _detailLoading || d.exhausted
                      ? null
                      : () => unawaited(_loadDetail(older: true)),
                  child: const Text('Older calls'),
                ),
              ],
            ),
            _literal('Native routing / execution receipts', d.receipts),
            if (d.receiptsTruncated)
              const Text(
                'Additional native receipts exist beyond this bounded detail.',
              ),
            const Text(
              'Routing is separate from Agent acceptance and authoritative completion.',
            ),
          ],
        ],
      ),
    );
  }

  Widget _call(DecisionCall c) => Card(
    child: Padding(
      padding: const EdgeInsets.all(12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Call ${c.data['attempt']} · ${c.status == 'started' ? 'Unknown completion' : c.status}',
            style: Theme.of(context).textTheme.titleMedium,
          ),
          SelectableText(
            'Call ID: ${c.id}\nAttempt ID: ${c.data['attempt_id']}\nProvider: ${c.data['provider'] ?? "Unavailable"}\nModel: ${c.data['model'] ?? "Unavailable"}\nArtifact: ${c.data['artifact_sha256'] ?? "Unavailable"}\nStarted: ${c.data['created_at']}\nCompleted: ${c.data['completed_at'] ?? "Unavailable"}\nCapture: ${c.captureState}',
          ),
          if (c.status == 'started')
            const Text(
              'No completion was recorded. A timeout does not prove provider processing stopped.',
            ),
          if (c.data['reused_from_call_id'] != null)
            SelectableText(
              'Reused from call ${c.data['reused_from_call_id']}. No new request was submitted.',
            ),
          if (c.data['reused_from_source_event_id'] is String)
            TextButton(
              onPressed: () =>
                  _select(c.data['reused_from_source_event_id'] as String),
              child: const Text('Inspect original inference'),
            ),
          _literal('Script passed to model', c.data['request']),
          _literal('Raw model output', c.data['raw_response']),
          _literal('Validated output', c.data['validated_output']),
          if (c.data['error_type'] != null)
            SelectableText('Native error type: ${c.data['error_type']}'),
        ],
      ),
    ),
  );
  Future<void> _showEvidence(String label, String text) async {
    final api = _client, generation = _generation;
    if (api == null || _evidenceRoute != null) return;
    late final DialogRoute<void> owned;
    owned = DialogRoute<void>(
      context: context,
      builder: (context) {
        return ListenableBuilder(
          listenable: _s,
          builder: (context, _) => !_owns(api, generation)
              ? const SizedBox.shrink()
              : Dialog(
                  child: Column(
                    children: [
                      Text(label),
                      Expanded(
                        child: SingleChildScrollView(
                          padding: const EdgeInsets.all(16),
                          child: SelectableText(text),
                        ),
                      ),
                      TextButton(
                        onPressed: () => Navigator.pop(context),
                        child: const Text('Close evidence'),
                      ),
                    ],
                  ),
                ),
        );
      },
    );
    _evidenceRoute = owned;
    await Navigator.of(context).push<void>(owned);
    if (identical(_evidenceRoute, owned)) _evidenceRoute = null;
  }

  Widget _literal(String label, Object? value) {
    final text = value == null
        ? 'Unavailable'
        : value is String
        ? value
        : decisionsLiteral(value);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Wrap(
            crossAxisAlignment: WrapCrossAlignment.center,
            spacing: 8,
            children: [
              Text(label, style: Theme.of(context).textTheme.titleSmall),
              TextButton(
                onPressed: value == null
                    ? null
                    : () async {
                        try {
                          await Clipboard.setData(ClipboardData(text: text));
                          if (mounted) {
                            ScaffoldMessenger.maybeOf(context)?.showSnackBar(
                              const SnackBar(
                                content: Text('Copied literal evidence'),
                              ),
                            );
                          }
                        } catch (_) {
                          if (mounted) {
                            ScaffoldMessenger.maybeOf(context)?.showSnackBar(
                              const SnackBar(
                                content: Text('Could not copy evidence'),
                              ),
                            );
                          }
                        }
                      },
                child: Text('Copy $label'),
              ),
              if (value != null)
                TextButton(
                  onPressed: () => _showEvidence(label, text),
                  child: const Text('View full evidence'),
                ),
            ],
          ),
          ConstrainedBox(
            constraints: const BoxConstraints(maxHeight: 240),
            child: SingleChildScrollView(child: SelectableText(text)),
          ),
        ],
      ),
    );
  }
}
