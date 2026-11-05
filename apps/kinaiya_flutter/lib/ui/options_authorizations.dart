import 'dart:async';

import 'package:flutter/material.dart';
import 'package:uuid/uuid.dart';

import '../api/authorizations.dart';
import '../api/tool_permissions_client.dart';

class OptionsAuthorizations extends StatefulWidget {
  const OptionsAuthorizations({
    super.key,
    required this.api,
    required this.active,
  });
  final ToolPermissionsApi? api;
  final bool Function() active;
  @override
  State<OptionsAuthorizations> createState() => _OptionsAuthorizationsState();
}

class _PendingRevocation {
  _PendingRevocation(this.owner, this.body);
  final ToolPermissionsApi owner;
  final Map<String, Object?> body;
  bool inFlight = false, dispatched = false;
  AuthorizationReceipt? receipt;
  String notice = 'Revocation has not been confirmed.';
  String get id => AuthorizationIdentity.fromJson(body['identity']).id;
}

class _OptionsAuthorizationsState extends State<OptionsAuthorizations> {
  static final _pending = <_PendingRevocation>[];
  static final _changes = ValueNotifier<int>(0);
  AuthorizationsPage? _page;
  List<AuthorizationRow> _rows = [];
  String? _error, _notice;
  bool _reading = false;
  int _read = 0;
  List<_PendingRevocation> get _owned => widget.api == null
      ? []
      : _pending.where((item) => item.owner.sameOwner(widget.api!)).toList();
  bool _current(ToolPermissionsApi api, bool Function() active) =>
      mounted && identical(widget.api, api) && active() && widget.active();

  @override
  void initState() {
    super.initState();
    _changes.addListener(_changed);
    unawaited(_load());
  }

  @override
  void didUpdateWidget(covariant OptionsAuthorizations oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.api, widget.api)) {
      _read++;
      _page = null;
      _rows = [];
      _reading = false;
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
    if (mounted && widget.active()) setState(() {});
  }

  Future<void> _load({bool more = false}) async {
    final api = widget.api, active = widget.active, page = _page;
    if (api == null ||
        !widget.active() ||
        _reading ||
        (more && page?.nextAfter == null)) {
      return;
    }
    final read = ++_read;
    setState(() {
      _reading = true;
      _error = null;
    });
    try {
      final next = await api.authorizations(
        after: more ? page!.nextAfter : null,
      );
      if (!_current(api, active) || read != _read) return;
      if (more &&
          (next.instance != page!.instance || !identical(_page, page))) {
        throw const FormatException();
      }
      setState(() {
        _rows = more ? [..._rows, ...next.authorizations] : next.authorizations;
        _page = next;
      });
    } catch (_) {
      if (_current(api, active) && read == _read) {
        setState(
          () => _error =
              'Authorization activity is unavailable. Existing requests remain retained.',
        );
      }
    } finally {
      if (_current(api, active) && read == _read) {
        setState(() => _reading = false);
      }
    }
  }

  Future<void> _begin(AuthorizationsPage rendered, AuthorizationRow row) async {
    final api = widget.api, active = widget.active;
    if (api == null ||
        !_current(api, active) ||
        !identical(_page, rendered) ||
        !_rows.any((item) => identical(item, row)) ||
        row.state != 'approved_unspent' ||
        _owned.any((item) => item.id == row.identity.id)) {
      return;
    }
    if (_pending.length >= 16) {
      setState(
        () => _error =
            'Resolve retained revocation requests before creating another.',
      );
      return;
    }
    final body = authorizationRevoke({
      'schema_version': '1.0',
      'broker_instance': rendered.instance,
      'idempotency_key': 'revoke:${const Uuid().v4()}',
      'identity': row.identity.body,
      'expected_revision': row.revision,
      'expected_state': 'approved_unspent',
    });
    final item = _PendingRevocation(api, body);
    _pending.add(item);
    await _send(item, first: true, rendered: rendered, row: row);
  }

  Future<void> _send(
    _PendingRevocation item, {
    bool first = false,
    AuthorizationsPage? rendered,
    AuthorizationRow? row,
  }) async {
    final api = widget.api, active = widget.active;
    if (api == null ||
        !_current(api, active) ||
        !item.owner.sameOwner(api) ||
        item.inFlight ||
        !_pending.contains(item)) {
      return;
    }
    if (first &&
        (!identical(_page, rendered) ||
            row == null ||
            !_rows.any((value) => identical(value, row)) ||
            row.state != 'approved_unspent')) {
      _pending.remove(item);
      _changes.value++;
      return;
    }
    item.inFlight = true;
    _changes.value++;
    try {
      // ValueNotifier listeners may synchronously replace the connection or
      // rendered inventory. A first dispatch needs its original render again.
      if (first &&
          (!_current(api, active) ||
              !identical(_page, rendered) ||
              row == null ||
              !_rows.any((value) => identical(value, row)) ||
              row.state != 'approved_unspent' ||
              !_pending.contains(item))) {
        _pending.remove(item);
        return;
      }
      AuthorizationReceipt? receipt;
      if (!first) {
        final lookup = await api.lookupAuthorization(item.body);
        receipt = lookup.receipt;
        if (receipt == null &&
            lookup.instance != item.body['broker_instance']) {
          item.notice =
              'No exact historical receipt was observed. The original broker is unavailable; outcome remains unknown.';
          return;
        }
        if (!_current(api, active)) return;
      }
      if (receipt == null || !receipt.settled) {
        item.dispatched = true;
        receipt = await api.revokeAuthorization(item.body);
      }
      item.receipt = receipt;
      item.notice = receipt.status == 'revoked'
          ? 'Permit revoked. This does not cancel an already admitted operation. Journal: ${receipt.journalStatus}.'
          : 'No revocation was applied. Native observed state: ${receipt.observedState}.';
      if (receipt.settled) {
        _pending.remove(item);
        if (_current(api, active)) setState(() => _notice = item.notice);
      }
      if (_current(api, active)) unawaited(_load());
    } on ToolPermissionFailure catch (error) {
      if (first && !error.dispatched) {
        _pending.remove(item);
      } else {
        item.notice =
            'Outcome is unconfirmed. Check the exact original request; a missing row is not proof.';
      }
    } catch (_) {
      item.notice =
          'Outcome is unconfirmed. The exact original request remains retained.';
    } finally {
      item.inFlight = false;
      _changes.value++;
    }
  }

  @override
  Widget build(BuildContext context) {
    final page = _page, pending = _owned;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const SizedBox(height: 24),
        Row(
          children: [
            Expanded(
              child: Text(
                'Authorization activity',
                style: Theme.of(context).textTheme.titleLarge,
              ),
            ),
            TextButton(
              onPressed: _reading ? null : () => _load(),
              child: const Text('Refresh'),
            ),
          ],
        ),
        const Text(
          'One-use owner reviews are separate from broad service grants and scoped rules. Revocation prevents a future spend; it does not stop admitted work.',
        ),
        if (_error != null)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 8),
            child: Text(_error!),
          ),
        if (_notice != null)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 8),
            child: Text(_notice!),
          ),
        if (page != null) ...[
          const SizedBox(height: 12),
          const Text(
            'Broad service grants',
            style: TextStyle(fontWeight: FontWeight.w600),
          ),
          Text(
            'File writes: ${page.broad['write_file']} · Shell: ${page.broad['shell']} · Network: ${page.broad['network'] == true ? 'allowed' : 'denied'} · YOLO: ${page.broad['yolo'] == true ? 'on' : 'off'}',
          ),
          Text(
            'Workspace: ${page.broad['workspace_available'] == true ? 'available' : 'unavailable'} · Read-only roots: ${page.broad['readonly_roots']}',
          ),
          const Text(
            'Service configuration only. Scoped rules and containment still apply; isolation is checked for each operation.',
          ),
          const SizedBox(height: 16),
          const Text(
            'Exact one-use authorizations',
            style: TextStyle(fontWeight: FontWeight.w600),
          ),
          if (_rows.isEmpty)
            const Text(
              'No retained native authorizations. This is not proof about earlier operations.',
            ),
          for (final row in _rows)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 8),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          '${row.identity.tool} · ${row.state.replaceAll('_', ' ')}',
                        ),
                        SelectableText(
                          'Task ${row.identity.taskId} · ${row.identity.id}',
                        ),
                        Text(
                          'Objective ${row.identity.body['objective_revision']} · Epoch ${row.identity.body['epoch']} · Expires ${DateTime.fromMillisecondsSinceEpoch((row.expiresAt * 1000).round()).toLocal()}',
                        ),
                        Tooltip(
                          message:
                              'Exact arguments SHA256: ${row.identity.body['arguments_sha256']}\nAuthenticated source SHA256: ${row.identity.body['source_sha256']}',
                          child: const Text(
                            'Exact arguments and source bound by native hashes',
                          ),
                        ),
                      ],
                    ),
                  ),
                  if (row.state == 'approved_unspent')
                    OutlinedButton(
                      onPressed:
                          _owned.any((item) => item.id == row.identity.id)
                          ? null
                          : () => _begin(page, row),
                      child: const Text('Revoke'),
                    ),
                ],
              ),
            ),
          if (page.nextAfter != null)
            TextButton(
              onPressed: _reading ? null : () => _load(more: true),
              child: const Text('Load more authorizations'),
            ),
        ],
        for (final item in pending)
          Card(
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Original request · ${item.id}'),
                  Text(item.notice),
                  TextButton(
                    onPressed: item.inFlight ? null : () => _send(item),
                    child: Text(
                      item.inFlight ? 'Checking…' : 'Check original request',
                    ),
                  ),
                ],
              ),
            ),
          ),
      ],
    );
  }
}
