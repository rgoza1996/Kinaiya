import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:uuid/uuid.dart';
import '../api/capability_settings.dart';
import '../api/capability_client.dart';
import 'design_tokens.dart';

class OptionsCapabilities extends StatefulWidget {
  const OptionsCapabilities({
    super.key,
    required this.api,
    required this.section,
    required this.active,
  });
  final CapabilityApi? api;
  final String section;
  final bool Function() active;
  @override
  State<OptionsCapabilities> createState() => _OptionsCapabilitiesState();
}

class _CapabilityDraft {
  _CapabilityDraft(this.owner, this.base) : disabled = base.disabled.toSet();
  final CapabilityApi owner;
  final CapabilitySnapshot base;
  final Set<String> disabled;
  int edit = 0;
  bool get dirty =>
      jsonEncode(capabilityTools.where(disabled.contains).toList()) !=
      jsonEncode(base.disabled);
  Map<String, Object?> get action => capabilityAction({
    'operation': 'set_tools',
    'disabled': capabilityTools.where(disabled.contains).toList(),
  });
}

class _PendingCapability {
  _PendingCapability(
    this.owner,
    this.body, {
    this.draft,
    this.edit,
    this.originalPage,
  });
  final CapabilityApi owner;
  final Map<String, Object?> body;
  final _CapabilityDraft? draft;
  final int? edit;
  final CapabilitySnapshot? originalPage;
  bool busy = false, dispatched = false;
  CapabilityLookup? lookup;
  CapabilityApi? lookupOwner;
  String notice = 'Sending this exact native request…';
  String get section =>
      capabilityActionSection(body['action'] as Map<String, Object?>);
}

class _OptionsCapabilitiesState extends State<OptionsCapabilities> {
  static final _drafts = <_CapabilityDraft>[];
  static final _pending = <_PendingCapability>[];
  static final _changes = ValueNotifier<int>(0);
  CapabilitySnapshot? _snapshot;
  _CapabilityDraft? _draft;
  bool _reading = false;
  int _read = 0, _after = 0;
  final _previous = <int>[];
  final _reviews = <String, String>{}, _reports = <String, String>{};
  final _reviewEdits = <String, int>{};
  String? _error, _notice;
  bool _current(CapabilityApi api, bool Function() active) =>
      mounted && identical(widget.api, api) && active() && widget.active();
  List<_PendingCapability> get _ownedPendings {
    final api = widget.api;
    return api == null
        ? []
        : _pending
              .where(
                (item) =>
                    api.sameOwner(item.owner) && item.section == widget.section,
              )
              .toList();
  }

  _PendingCapability? get _ownedPending => _ownedPendings.firstOrNull;

  bool _rendered(CapabilitySnapshot page) =>
      mounted &&
      widget.active() &&
      !_reading &&
      identical(_snapshot, page) &&
      page.section == widget.section;
  bool _canAct(
    CapabilitySnapshot page, {
    bool revoke = false,
    String? operation,
    _PendingCapability? pending,
  }) {
    final disable = operation == 'disable_plugin';
    final others = _ownedPendings.where((item) => !identical(item, pending));
    final pendingAllowed = disable
        ? page.pendingOperations.every(
                (item) => item['operation'] == 'evaluate_plugin',
              ) &&
              others.every(
                (item) =>
                    !identical(page, item.originalPage) &&
                    (item.body['action']
                            as Map<String, Object?>)['operation'] ==
                        'evaluate_plugin',
              ) &&
              widget.api?.blocksAction({'operation': 'disable_plugin'}) == false
        : page.pendingCount == 0 &&
              others.isEmpty &&
              widget.api?.hasUnconfirmedSection(page.section) == false;
    return _rendered(page) &&
        widget.api != null &&
        page.writable &&
        page.hostState == 'configured' &&
        page.revision < capabilityRevisionLimit &&
        (revoke || !page.busy) &&
        pendingAllowed;
  }

  bool _canEdit(
    CapabilitySnapshot page,
    _CapabilityDraft draft, {
    _PendingCapability? pending,
  }) =>
      _canAct(page, pending: pending) &&
      identical(_draft, draft) &&
      _drafts.contains(draft) &&
      widget.api!.sameOwner(draft.owner) &&
      draft.base.sameBase(page);
  @override
  void initState() {
    super.initState();
    _changes.addListener(_changed);
    unawaited(_load());
  }

  @override
  void didUpdateWidget(covariant OptionsCapabilities old) {
    super.didUpdateWidget(old);
    if (!identical(old.api, widget.api) || old.section != widget.section) {
      _read++;
      _snapshot = null;
      _draft = null;
      _error = _notice = null;
      _after = 0;
      _previous.clear();
      _reviews.clear();
      _reports.clear();
      _reviewEdits.clear();
      unawaited(_load());
    }
  }

  @override
  void dispose() {
    _read++;
    _changes.removeListener(_changed);
    super.dispose();
  }

  void _changed() {
    if (mounted && widget.active()) {
      setState(() {
        if (widget.section == 'tools' &&
            (_draft == null || !_drafts.contains(_draft))) {
          _draft = null;
          final api = widget.api;
          if (api != null) {
            for (final draft in _drafts) {
              if (api.sameOwner(draft.owner)) {
                _draft = draft;
                break;
              }
            }
          }
        }
      });
    }
  }

  Future<void> _load({int? after}) async {
    final api = widget.api,
        active = widget.active,
        read = ++_read,
        section = widget.section;
    if (api == null || !mounted || !active()) return;
    final cursor = after ?? _after;
    setState(() {
      _reading = true;
      _error = null;
    });
    try {
      final page = await api.inspect(section, after: cursor);
      if (!_current(api, active) ||
          read != _read ||
          widget.section != section) {
        return;
      }
      _CapabilityDraft? owned;
      if (section == 'tools') {
        for (final draft in _drafts) {
          if (api.sameOwner(draft.owner)) {
            owned = draft;
            break;
          }
        }
        if (owned == null || !owned.dirty) {
          if (owned != null) _drafts.remove(owned);
          if (_drafts.length >= 16) {
            for (final draft in List<_CapabilityDraft>.of(_drafts)) {
              if (!draft.dirty &&
                  !_pending.any((p) => identical(p.draft, draft))) {
                _drafts.remove(draft);
                break;
              }
            }
          }
          if (_drafts.length < 16) {
            owned = _CapabilityDraft(api, page);
            _drafts.add(owned);
          } else {
            owned = null;
          }
        }
      }
      setState(() {
        _snapshot = page;
        _draft = owned;
        _after = cursor;
        _reviews.clear();
        _reports.clear();
        _reviewEdits.clear();
        if (section == 'tools' && owned == null) {
          _error =
              'Retained draft storage is full. Existing drafts are preserved.';
        }
      });
      _changes.value++;
    } catch (_) {
      if (_current(api, active) && read == _read) {
        setState(() {
          _snapshot = null;
          _error =
              'Native installed capabilities could not be read. Drafts and original requests are retained. An oversized individual metadata row is not truncated.';
        });
      }
    } finally {
      if (_current(api, active) && read == _read) {
        setState(() => _reading = false);
      }
    }
  }

  void _edit(
    CapabilitySnapshot page,
    _CapabilityDraft draft,
    List<String> names,
    bool enabled,
    int renderedEdit,
  ) {
    if (!_canEdit(page, draft) || draft.edit != renderedEdit) return;
    for (final name in names) {
      if (name == 'finish') continue;
      if (enabled) {
        draft.disabled.remove(name);
      } else {
        draft.disabled.add(name);
      }
    }
    draft.edit++;
    _changes.value++;
  }

  void _discard(CapabilitySnapshot page, _CapabilityDraft draft, int edit) {
    if (!_rendered(page) ||
        _ownedPending != null ||
        widget.api?.hasUnconfirmedSection('tools') != false ||
        !identical(_draft, draft) ||
        draft.edit != edit ||
        !_drafts.contains(draft)) {
      return;
    }
    _drafts.remove(draft);
    _draft = _CapabilityDraft(widget.api!, page);
    _drafts.add(_draft!);
    _changes.value++;
  }

  void _saveTools(CapabilitySnapshot page, _CapabilityDraft draft, int edit) {
    if (!_canEdit(page, draft) || !draft.dirty || draft.edit != edit) return;
    final action = draft.action;
    _begin(
      page,
      action,
      draft: draft,
      edit: edit,
      stillCurrent: (pending) =>
          _canEdit(page, draft, pending: pending) &&
          draft.edit == edit &&
          jsonEncode(draft.action) == jsonEncode(action),
    );
  }

  void _activate(
    CapabilitySnapshot page,
    CapabilityItem item,
    String operation, {
    String? report,
    bool reviewed = false,
    int? renderedEdit,
    String? renderedReport,
    String? renderedReview,
  }) {
    final revoke =
        operation == 'disable_skill' || operation == 'disable_plugin';
    if (!_canAct(page, revoke: revoke, operation: operation) ||
        !page.items.any((row) => identical(row, item))) {
      return;
    }
    final selected = _reports[item.identity] ?? _defaultReport(item);
    final target = _reviewBinding(item, selected);
    final edit = _reviewEdits[item.identity] ?? 0;
    if (item.kind == 'plugin' &&
        (edit != renderedEdit ||
            selected != renderedReport ||
            _reviews[item.identity] != renderedReview)) {
      return;
    }
    if (reviewed && _reviews[item.identity] != target) return;
    final action = item.action(operation, report: report, reviewed: reviewed);
    _begin(
      page,
      action,
      stillCurrent: (pending) =>
          _canAct(
            page,
            revoke: revoke,
            operation: operation,
            pending: pending,
          ) &&
          page.items.any((row) => identical(row, item)) &&
          (_reviewEdits[item.identity] ?? 0) == edit &&
          (_reports[item.identity] ?? _defaultReport(item)) == selected &&
          (!reviewed || _reviews[item.identity] == target) &&
          (report == null ||
              (_reports[item.identity] ?? _defaultReport(item)) == report),
    );
  }

  void _begin(
    CapabilitySnapshot page,
    Map<String, Object?> action, {
    _CapabilityDraft? draft,
    int? edit,
    required bool Function(_PendingCapability pending) stillCurrent,
  }) {
    final api = widget.api;
    if (api == null || _pending.length >= 16) {
      setState(
        () => _error =
            'Retained request storage is full. Reconcile existing requests first.',
      );
      return;
    }
    final item = _PendingCapability(
      api,
      page.change(action, const Uuid().v4()),
      draft: draft,
      edit: edit,
      originalPage: page,
    );
    _pending.add(item);
    _changes.value++;
    unawaited(
      _send(
        item,
        api,
        first: true,
        firstCurrent: () => _ownedPendings.contains(item) && stillCurrent(item),
      ),
    );
  }

  void _confirmed(
    _PendingCapability item,
    CapabilityReceipt receipt,
    CapabilityApi api,
    bool Function() active,
  ) {
    if (!receipt.matches(item.body)) throw const FormatException();
    _pending.remove(item);
    final draft = item.draft;
    if (draft != null &&
        _drafts.contains(draft) &&
        draft.edit == item.edit &&
        draft.base.backend == item.body['expected_backend'] &&
        draft.base.instance == item.body['expected_instance'] &&
        draft.base.revision == item.body['expected_revision'] &&
        draft.base.stateToken == item.body['expected_state'] &&
        jsonEncode(draft.action) == jsonEncode(item.body['action'])) {
      _drafts.remove(draft);
      if (identical(_draft, draft)) _draft = null;
    }
    if (_current(api, active)) {
      setState(
        () => _notice =
            'Native action recorded at revision ${receipt.revision}. Current availability is inspected separately.',
      );
      unawaited(_load());
    }
  }

  Future<void> _send(
    _PendingCapability item,
    CapabilityApi api, {
    bool first = false,
    bool retry = false,
    bool release = false,
    bool Function()? firstCurrent,
  }) async {
    if (item.busy || !_pending.contains(item) || !api.sameOwner(item.owner)) {
      return;
    }
    final active = widget.active;
    item.busy = true;
    item.lookup = null;
    item.lookupOwner = null;
    _changes.value++;
    try {
      if (!_current(api, active) || (first && firstCurrent?.call() != true)) {
        if (first && !item.dispatched) _pending.remove(item);
        return;
      }
      if (!first) {
        final result = await api.lookup(item.body);
        if (result.receipt case final receipt?) {
          _confirmed(item, receipt, api, active);
          return;
        }
        if (!_current(api, active)) return;
        item.lookup = result;
        item.lookupOwner = api;
        if (result.fenced) {
          item.notice =
              'Historical outcome unknown. Native authority confirms this unchanged request cannot make a new commit.';
          if (release) {
            api.releaseFenced(item.body, result);
            _pending.remove(item);
            setState(
              () => _notice =
                  'Local tracking released; the historical outcome remains unknown. Nothing was cancelled or rolled back. Drafts are retained.',
            );
            unawaited(_load());
          }
          return;
        }
        if (result.status == 'pending') {
          item.notice =
              'The native owner retained the original intent. Its outcome is unknown; the effect will not be repeated. This section remains withheld pending native reconciliation.';
          return;
        }
        item.notice =
            'No exact receipt is observed. The original request is not fenced. Check again or explicitly retry it unchanged.';
        if (!retry) return;
      }
      if (!_current(api, active) || (first && firstCurrent?.call() != true)) {
        if (first && !item.dispatched) _pending.remove(item);
        return;
      }
      final receipt = await api.change(item.body);
      _confirmed(item, receipt, api, active);
    } on CapabilityFailure catch (error) {
      item.dispatched |= error.dispatched;
      if (first && !item.dispatched) {
        _pending.remove(item);
        if (_current(api, active)) {
          setState(
            () => _error =
                'The native request was not dispatched. Your selection is retained.',
          );
        }
      } else {
        item.notice =
            'Outcome unconfirmed. The exact action, source/version, authority and original key are retained. Check its native receipt before retrying.';
      }
    } catch (_) {
      item.dispatched = true;
      item.notice =
          'Original request not confirmed. Inspect its exact native receipt; do not create a replacement request.';
    } finally {
      item.busy = false;
      _changes.value++;
      if (_current(api, active)) setState(() {});
    }
  }

  String? _defaultReport(CapabilityItem item) {
    final reports =
        (item.body['reports'] as List?)
            ?.cast<Map<String, Object?>>()
            .where((r) => r['passed'] == true && r['current_binding'] == true)
            .toList() ??
        [];
    return reports.isEmpty ? null : reports.last['report_id'] as String;
  }

  String _reviewBinding(CapabilityItem item, String? report) =>
      jsonEncode({'target': item.body['target_sha256'], 'report': report});

  Widget _recovery(_PendingCapability item) {
    final api = widget.api!, lookup = item.lookup;
    return DesignPanel(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text('Original native request'),
          Text(item.notice),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              OutlinedButton(
                onPressed: item.busy ? null : () => unawaited(_send(item, api)),
                child: Text(item.busy ? 'Checking…' : 'Check original request'),
              ),
              if (lookup?.status == 'unobserved' &&
                  lookup?.fenced == false &&
                  identical(item.lookupOwner, api))
                OutlinedButton(
                  onPressed: item.busy
                      ? null
                      : () => unawaited(_send(item, api, retry: true)),
                  child: const Text('Retry unchanged request'),
                ),
              if (lookup?.fenced == true && identical(item.lookupOwner, api))
                OutlinedButton(
                  onPressed: item.busy
                      ? null
                      : () => unawaited(_send(item, api, release: true)),
                  child: const Text('Release fenced local record'),
                ),
            ],
          ),
          ExpansionTile(
            title: const Text('Request identity'),
            children: [SelectableText(jsonEncode(item.body))],
          ),
        ],
      ),
    );
  }

  Widget _details(CapabilityItem item) => ExpansionTile(
    title: const Text('Source, version and authorization'),
    children: [
      for (final e in item.body.entries.where(
        (e) => !{'description', 'name', 'kind'}.contains(e.key),
      ))
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 3, horizontal: 16),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              SizedBox(width: 170, child: Text(e.key.replaceAll('_', ' '))),
              Expanded(
                child: SelectableText(
                  e.value is String ? e.value as String : jsonEncode(e.value),
                ),
              ),
            ],
          ),
        ),
    ],
  );
  Widget _skill(CapabilitySnapshot page, CapabilityItem item) {
    final row = item.body, enable = row['enabled'] != true;
    final eligible = !enable || {'disabled', 'ready'}.contains(row['status']);
    return DesignPanel(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(item.label, style: Theme.of(context).textTheme.titleMedium),
          Text(item.description),
          Text(
            '${row['version']} · ${row['enabled'] == true ? 'Enabled' : 'Disabled'} · ${row['status']}',
          ),
          Text(
            'Model eligibility: ${row['authorization']}. This is not permission to execute tools.',
          ),
          const SizedBox(height: 8),
          OutlinedButton(
            onPressed:
                eligible &&
                    row['installed'] == true &&
                    _canAct(page, revoke: !enable)
                ? () => _activate(
                    page,
                    item,
                    enable ? 'enable_skill' : 'disable_skill',
                  )
                : null,
            child: Text(enable ? 'Enable installed skill' : 'Disable skill'),
          ),
          if (!eligible)
            const Text(
              'Native setup or an unsupported adapter prevents activation.',
            ),
          _details(item),
        ],
      ),
    );
  }

  Widget _plugin(CapabilitySnapshot page, CapabilityItem item) {
    final row = item.body;
    final reports = (row['reports'] as List)
        .cast<Map<String, Object?>>()
        .where((r) => r['passed'] == true && r['current_binding'] == true)
        .toList();
    final selected = _reports[item.identity] ?? _defaultReport(item);
    final priorReview = _reviews[item.identity];
    final binding = _reviewBinding(item, selected);
    final reviewed = priorReview == binding;
    final edit = _reviewEdits[item.identity] ?? 0;
    bool currentSelection() =>
        _canAct(page) &&
        page.items.any((row) => identical(row, item)) &&
        (_reviewEdits[item.identity] ?? 0) == edit &&
        (_reports[item.identity] ?? _defaultReport(item)) == selected &&
        _reviews[item.identity] == priorReview;
    return DesignPanel(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(item.label, style: Theme.of(context).textTheme.titleMedium),
          Text(item.description),
          Text(
            '${row['version_id']} · ${row['enabled'] == true ? 'Active and enabled' : 'Inactive'} · ${row['approved'] == true ? 'Owner approved' : 'Not currently approved'}',
          ),
          Text(
            'Model eligibility: ${row['authorization']}. Only isolated JSON computation is supported.',
          ),
          if (reports.isNotEmpty)
            DropdownButtonFormField<String>(
              key: ValueKey('${item.identity}:$selected'),
              initialValue: selected,
              decoration: const InputDecoration(
                labelText: 'Current passing owner report',
              ),
              items: [
                for (final report in reports)
                  DropdownMenuItem(
                    value: report['report_id'] as String,
                    child: Text(report['report_id'] as String),
                  ),
              ],
              onChanged: _canAct(page)
                  ? (value) {
                      if (!currentSelection() ||
                          value == null ||
                          !reports.any((r) => r['report_id'] == value)) {
                        return;
                      }
                      setState(() {
                        _reports[item.identity] = value;
                        _reviews.remove(item.identity);
                        _reviewEdits[item.identity] = edit + 1;
                      });
                    }
                  : null,
            ),
          if (reports.isEmpty)
            const Text(
              'No displayed report has a current passing source/manifest/suite binding.',
            ),
          CheckboxListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text(
              'I reviewed this exact version and owner evaluation evidence',
            ),
            value: reviewed,
            onChanged: _canAct(page)
                ? (value) {
                    if (!currentSelection()) {
                      return;
                    }
                    setState(() {
                      if (value == true) {
                        _reviews[item.identity] = binding;
                      } else {
                        _reviews.remove(item.identity);
                      }
                      _reviewEdits[item.identity] = edit + 1;
                    });
                  }
                : null,
          ),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              OutlinedButton(
                onPressed: row['suite_configured'] == true && _canAct(page)
                    ? () => _activate(
                        page,
                        item,
                        'evaluate_plugin',
                        renderedEdit: edit,
                        renderedReport: selected,
                        renderedReview: priorReview,
                      )
                    : null,
                child: const Text('Evaluate installed version'),
              ),
              FilledButton(
                onPressed: selected != null && reviewed && _canAct(page)
                    ? () => _activate(
                        page,
                        item,
                        'promote_plugin',
                        report: selected,
                        reviewed: true,
                        renderedEdit: edit,
                        renderedReport: selected,
                        renderedReview: priorReview,
                      )
                    : null,
                child: const Text('Activate reviewed version'),
              ),
              if (row['rollback_eligible'] == true)
                OutlinedButton(
                  onPressed: reviewed && _canAct(page)
                      ? () => _activate(
                          page,
                          item,
                          'rollback_plugin',
                          reviewed: true,
                          renderedEdit: edit,
                          renderedReport: selected,
                          renderedReview: priorReview,
                        )
                      : null,
                  child: const Text('Restore this previous version'),
                ),
              if (row['enabled'] == true)
                OutlinedButton(
                  onPressed:
                      _canAct(page, revoke: true, operation: 'disable_plugin')
                      ? () => _activate(
                          page,
                          item,
                          'disable_plugin',
                          renderedEdit: edit,
                          renderedReport: selected,
                          renderedReview: priorReview,
                        )
                      : null,
                  child: const Text('Disable plugin'),
                ),
            ],
          ),
          if (row['suite_configured'] != true)
            const Text(
              'An owner-held acceptance suite must be configured by the host. No suite is created or run on opening this page.',
            ),
          if ((row['reports_total'] as int) > 8)
            Text(
              '${row['reports_total']} reports retained; up to eight latest summaries are displayed.',
            ),
          _details(item),
        ],
      ),
    );
  }

  Widget _tools(CapabilitySnapshot page) {
    final draft = _draft;
    if (draft == null) {
      return const Text('A retained editable tool draft is unavailable.');
    }
    final edit = draft.edit;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (!draft.base.sameBase(page)) ...[
          const Text(
            'This draft belongs to an earlier native state. Its choices are retained. Discard it explicitly before editing the current state.',
          ),
          OutlinedButton(
            onPressed: _ownedPending == null
                ? () => _discard(page, draft, edit)
                : null,
            child: const Text('Discard stale tool draft'),
          ),
        ],
        for (final group
            in page.items
                .map((row) => row.body['group'] as String)
                .toSet()) ...[
          Text(group, style: Theme.of(context).textTheme.titleMedium),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: Text('Enable $group operations'),
            value: page.items
                .where(
                  (r) =>
                      r.body['group'] == group && r.body['protected'] != true,
                )
                .every((r) => !draft.disabled.contains(r.identity)),
            onChanged: group != 'core' && _canEdit(page, draft)
                ? (value) => _edit(
                    page,
                    draft,
                    page.items
                        .where((r) => r.body['group'] == group)
                        .map((r) => r.identity)
                        .toList(),
                    value,
                    edit,
                  )
                : null,
          ),
          for (final item in page.items.where((r) => r.body['group'] == group))
            SwitchListTile(
              contentPadding: EdgeInsets.zero,
              title: Text(item.identity),
              subtitle: Text(
                '${item.description}\n${item.body['host_available'] != true
                    ? 'Host capability not configured'
                    : item.body['effective_available'] == true
                    ? 'Currently available'
                    : item.body['requested_enabled'] == true
                    ? 'Withheld by native lifecycle'
                    : 'Disabled by owner'} · authorization: ${item.body['authorization']}',
              ),
              value: !draft.disabled.contains(item.identity),
              onChanged: item.body['protected'] != true && _canEdit(page, draft)
                  ? (value) => _edit(page, draft, [item.identity], value, edit)
                  : null,
            ),
        ],
        Wrap(
          spacing: 8,
          children: [
            FilledButton(
              onPressed: _canEdit(page, draft) && draft.dirty
                  ? () => _saveTools(page, draft, edit)
                  : null,
              child: const Text('Save tool availability'),
            ),
            OutlinedButton(
              onPressed: draft.dirty && _ownedPending == null
                  ? () => _discard(page, draft, edit)
                  : null,
              child: const Text('Discard tool edits'),
            ),
          ],
        ),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    final page = _snapshot, pendings = _ownedPendings;
    final previousCursor = _previous.lastOrNull;
    return Align(
      alignment: Alignment.topLeft,
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(8),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 1050),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                widget.section == 'tools'
                    ? 'Native Agent tool availability'
                    : widget.section == 'skills'
                    ? 'Installed skills'
                    : 'Installed compute plugins',
                style: Theme.of(context).textTheme.titleLarge,
              ),
              const SizedBox(height: 8),
              const Text(
                'Availability, installation and approval do not grant action permissions. Metadata and source descriptions are untrusted.',
              ),
              const SizedBox(height: 8),
              Wrap(
                spacing: 8,
                children: [
                  OutlinedButton(
                    onPressed: _reading || widget.api == null
                        ? null
                        : () => unawaited(_load()),
                    child: Text(_reading ? 'Reading…' : 'Refresh native state'),
                  ),
                ],
              ),
              if (widget.api == null)
                const Text(
                  'Connect to the native Agent owner to inspect this section.',
                ),
              if (_error != null) Text(_error!),
              if (_notice != null) Text(_notice!),
              for (final pending in pendings) _recovery(pending),
              if (page != null) ...[
                if (page.hostState != 'configured')
                  Text(
                    'This native ${widget.section} host is ${page.hostState}. Opening Options does not enable or install it.',
                  ),
                if (page.busy)
                  const Text(
                    'Native work is still owned. New activation and availability changes require settled work; supported revocation remains separate.',
                  ),
                if (page.pendingCount > 0 && pendings.isEmpty)
                  const Text(
                    'Native lifecycle intent remains unresolved. Its gateways are withheld; matching current state is not proof of the earlier outcome.',
                  ),
                if (page.section == 'plugins' &&
                    (page.pendingCount > 0 || pendings.isNotEmpty))
                  const Text(
                    'Refresh native state before requesting Disable. Native revocation can proceed alongside evaluation-only intents; every original evaluation remains unresolved until its exact receipt is confirmed.',
                  ),
                if (page.items.isEmpty && page.hostState == 'configured')
                  const Text('No installed records on this page.'),
                if (page.section == 'tools') _tools(page),
                if (page.section == 'skills')
                  for (final item in page.items) _skill(page, item),
                if (page.section == 'plugins')
                  for (final item in page.items) _plugin(page, item),
                const SizedBox(height: 12),
                Wrap(
                  spacing: 8,
                  children: [
                    if (previousCursor != null)
                      OutlinedButton(
                        onPressed: _reading
                            ? null
                            : () {
                                if (!_rendered(page) ||
                                    _previous.isEmpty ||
                                    _previous.last != previousCursor) {
                                  return;
                                }
                                final after = _previous.removeLast();
                                unawaited(_load(after: after));
                              },
                        child: const Text('Previous page'),
                      ),
                    if (page.nextAfter != null)
                      OutlinedButton(
                        onPressed: _reading
                            ? null
                            : () {
                                if (!_rendered(page)) return;
                                _previous.add(_after);
                                unawaited(_load(after: page.nextAfter));
                              },
                        child: const Text('Next page'),
                      ),
                  ],
                ),
                ExpansionTile(
                  title: const Text('Native authority'),
                  children: [
                    SelectableText(
                      'Backend ${page.backend}\nInstance ${page.instance}\nRevision ${page.revision}\nState ${page.stateToken}',
                    ),
                  ],
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}
