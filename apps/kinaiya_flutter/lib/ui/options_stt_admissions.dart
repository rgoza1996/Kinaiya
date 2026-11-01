import 'dart:async';

import 'package:flutter/material.dart';

import '../api/stt_admissions.dart';
import '../api/stt_model_client.dart';

class OptionsSttAdmissions extends StatefulWidget {
  const OptionsSttAdmissions({
    super.key,
    required this.api,
    required this.active,
    required this.onSettled,
  });
  final SttModelsApi? api;
  final bool Function() active;
  final VoidCallback onSettled;

  @override
  State<OptionsSttAdmissions> createState() => _OptionsSttAdmissionsState();
}

class _AdmissionCheck {
  _AdmissionCheck(this.owner, this.body);
  final SttModelsApi owner;
  final Map<String, Object?> body;
  bool inFlight = false;
  String notice = 'Checking the original speech operation…';
}

class _OptionsSttAdmissionsState extends State<OptionsSttAdmissions> {
  static final _pending = <_AdmissionCheck>[];
  static final _changes = ValueNotifier<int>(0);
  SttAdmissionInventory? _inventory;
  String? _error, _notice;
  bool _reading = false, _opened = false;
  int _read = 0;

  bool _current(SttModelsApi api, bool Function() active) =>
      mounted && identical(widget.api, api) && active() && widget.active();

  List<_AdmissionCheck> get _checks {
    final api = widget.api;
    return api == null
        ? const []
        : _pending.where((item) => api.sameOwner(item.owner)).toList();
  }

  void _changed() {
    if (mounted) setState(() {});
  }

  @override
  void initState() {
    super.initState();
    _changes.addListener(_changed);
  }

  @override
  void didUpdateWidget(covariant OptionsSttAdmissions oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.api, widget.api)) {
      _read++;
      _inventory = null;
      _error = _notice = null;
      _reading = false;
      if (_opened) unawaited(_load());
    }
  }

  @override
  void dispose() {
    _read++;
    _changes.removeListener(_changed);
    super.dispose();
  }

  Future<void> _load() async {
    final api = widget.api, active = widget.active, read = ++_read;
    if (api == null || !active()) return;
    setState(() {
      _reading = true;
      _error = null;
    });
    try {
      final next = await api.admissions();
      if (!_current(api, active) || read != _read) return;
      setState(() => _inventory = next);
    } catch (_) {
      if (_current(api, active) && read == _read) {
        setState(() {
          _inventory = null;
          _error =
              'Speech cleanup records are unavailable. Earlier checks are retained.';
        });
      }
    } finally {
      if (_current(api, active) && read == _read) {
        setState(() => _reading = false);
      }
    }
  }

  Future<void> _check(Map<String, Object?> original) async {
    final api = widget.api, active = widget.active;
    if (api == null || !active()) return;
    final body = sttAdmissionRequest(original);
    _AdmissionCheck? prior;
    for (final item in _pending) {
      if (api.sameOwner(item.owner) &&
          item.body['expected_backend'] == body['expected_backend'] &&
          item.body['admission_id'] == body['admission_id'] &&
          item.body['identity_sha256'] == body['identity_sha256']) {
        prior = item;
        break;
      }
    }
    if (prior?.inFlight == true) return;
    if (prior == null && _pending.length >= 64) {
      setState(
        () => _error =
            'Retained cleanup checks are full. Resolve an earlier check first.',
      );
      return;
    }
    final record = prior ?? _AdmissionCheck(api, body);
    if (prior == null) _pending.add(record);
    record.inFlight = true;
    record.notice = 'Checking the original speech operation…';
    _changes.value++;
    try {
      if (!_current(api, active)) {
        if (prior == null) _pending.remove(record);
        return;
      }
      final result = await api.reconcileAdmission(record.body);
      if (result.retired) {
        _pending.remove(record);
        if (_current(api, active)) {
          setState(() => _notice = 'Native speech cleanup was confirmed.');
          widget.onSettled();
          unawaited(_load());
        }
      } else {
        record.notice = result.reason;
      }
    } on SttModelFailure catch (error) {
      record.notice = error.dispatched
          ? 'The cleanup check was not confirmed. Check the same operation again.'
          : 'The cleanup check could not start. The original operation is retained.';
    } catch (_) {
      record.notice =
          'The cleanup check was not confirmed. The original operation is retained.';
    } finally {
      record.inFlight = false;
      _changes.value++;
    }
  }

  @override
  Widget build(BuildContext context) {
    final api = widget.api, inventory = _inventory, checks = _checks;
    return ExpansionTile(
      title: const Text('Speech cleanup'),
      subtitle: const Text(
        'Inspect unfinished transcription and saved transcript operations.',
      ),
      onExpansionChanged: (opened) {
        _opened = opened;
        if (opened && inventory == null && !_reading) unawaited(_load());
      },
      childrenPadding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
      children: [
        Align(
          alignment: Alignment.topLeft,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text(
                'Cleanup checks query the original worker and its saved transcript. Unconfirmed work keeps model changes blocked.',
              ),
              const SizedBox(height: 8),
              OutlinedButton.icon(
                onPressed: api != null && widget.active() && !_reading
                    ? _load
                    : null,
                icon: const Icon(Icons.refresh),
                label: const Text('Refresh cleanup records'),
              ),
              if (_reading) const LinearProgressIndicator(),
              if (_error != null) Text(_error!),
              if (_notice != null) Text(_notice!),
              if (inventory != null && inventory.admissions.isEmpty)
                const Text(
                  'No unresolved speech admissions are reported by this service.',
                ),
              if (inventory != null)
                for (final row in inventory.admissions)
                  Padding(
                    padding: const EdgeInsets.only(top: 12),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          row.active
                              ? 'Speech operation is active'
                              : 'Speech cleanup is unconfirmed',
                        ),
                        Text(row.reason),
                        ExpansionTile(
                          title: const Text('Operation details'),
                          children: [
                            SelectableText(
                              'Admission ${row.id}\nWorker ${row.workerInstance ?? 'Not recorded'}\nOperation ${row.operationId ?? 'Not recorded'}\nTranscript state ${row.publicationState}',
                            ),
                          ],
                        ),
                        OutlinedButton(
                          onPressed:
                              widget.active() &&
                                  row.reconcilable &&
                                  !row.active &&
                                  !checks.any(
                                    (p) =>
                                        p.inFlight &&
                                        p.body['admission_id'] == row.id,
                                  )
                              ? () => _check(row.request(inventory.backendId))
                              : null,
                          child: const Text('Check cleanup'),
                        ),
                      ],
                    ),
                  ),
              for (final check in checks)
                Padding(
                  padding: const EdgeInsets.only(top: 12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(check.notice),
                      SelectableText(
                        'Original admission ${check.body['admission_id']}',
                      ),
                      OutlinedButton(
                        onPressed: widget.active() && !check.inFlight
                            ? () => _check(check.body)
                            : null,
                        child: const Text('Check original operation again'),
                      ),
                    ],
                  ),
                ),
            ],
          ),
        ),
      ],
    );
  }
}
