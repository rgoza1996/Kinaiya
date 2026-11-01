/// Exact owner auxiliary-route configuration; declarations are not readiness.
library;

import 'dart:convert';
import 'converse_models.dart'
    show ConverseCatalogBinding, ConverseCatalogProfile;
import 'tool_permissions.dart' show permissionJsonDecode;

const auxiliaryPurposes = [
  'analysis',
  'compression',
  'title_generation',
  'delegation',
  'harness_design',
];
const auxiliaryReasoning = [
  'none',
  'minimal',
  'low',
  'medium',
  'high',
  'xhigh',
];
const auxiliaryRevisionLimit = 9007199254740990;
Never _bad() => throw const FormatException('Invalid auxiliary route metadata');

Map<String, Object?> _object(Object? value, Set<String> keys) {
  if (value is! Map<String, Object?> ||
      value.length != keys.length ||
      !keys.containsAll(value.keys)) {
    _bad();
  }
  return value;
}

String _match(Object? value, String pattern) {
  if (value is! String) _bad();
  final match = RegExp(pattern).firstMatch(value);
  if (match == null || match.start != 0 || match.end != value.length) _bad();
  return value;
}

int _int(Object? value, int min) {
  if (value is! int || value < min || value > auxiliaryRevisionLimit) _bad();
  return value;
}

bool _bool(Object? value) {
  if (value is! bool) _bad();
  return value;
}

String _authority(Object? value) => _match(value, r'[a-f0-9]{32}');
String _hash(Object? value) => _match(value, r'[a-f0-9]{64}');
String auxiliaryRouteKey(Object? value) =>
    _match(value, r'[A-Za-z0-9:._-]{1,128}');
String? _effort(Object? value) {
  if (value != null && !auxiliaryReasoning.contains(value)) _bad();
  return value as String?;
}

class AuxiliaryScope {
  AuxiliaryScope.fromJson(Object? value) {
    final raw = _object(value, {'purpose', 'harness_version'});
    if (!auxiliaryPurposes.contains(raw['purpose'])) _bad();
    purpose = raw['purpose'] as String;
    harness = raw['harness_version'] == null
        ? null
        : _match(raw['harness_version'], r'seed-v1|v-[a-f0-9]{24}');
  }
  AuxiliaryScope(String purpose, String? harness)
    : this.fromJson({'purpose': purpose, 'harness_version': harness});
  late final String purpose;
  late final String? harness;
  Map<String, Object?> get body =>
      Map.unmodifiable({'purpose': purpose, 'harness_version': harness});
  bool matches(AuxiliaryScope other) =>
      purpose == other.purpose && harness == other.harness;
}

class AuxiliaryCandidate {
  AuxiliaryCandidate.fromJson(Object? value) {
    final raw = _object(value, {'catalog_binding', 'reasoning', 'cloud_scope'});
    binding = ConverseCatalogBinding.fromJson(raw['catalog_binding']);
    reasoning = _effort(raw['reasoning']);
    if (!{
      'local_only',
      'public_synthetic',
      'private_cloud_authorized',
    }.contains(raw['cloud_scope'])) {
      _bad();
    }
    cloudScope = raw['cloud_scope'] as String;
  }
  AuxiliaryCandidate(this.binding, this.reasoning, this.cloudScope);
  late final ConverseCatalogBinding binding;
  late final String? reasoning;
  late final String cloudScope;
  Map<String, Object?> get body => Map.unmodifiable({
    'catalog_binding': binding.body,
    'reasoning': reasoning,
    'cloud_scope': cloudScope,
  });
  bool matches(AuxiliaryCandidate other) =>
      binding.matches(other.binding) &&
      reasoning == other.reasoning &&
      cloudScope == other.cloudScope;
}

class AuxiliaryRoute {
  AuxiliaryRoute.fromJson(Object? value) {
    final raw = _object(value, {'candidates'}), rows = raw['candidates'];
    if (rows is! List || rows.isEmpty || rows.length > 3) _bad();
    candidates = List.unmodifiable(rows.map(AuxiliaryCandidate.fromJson));
    if (candidates.map((e) => e.binding.identity).toSet().length !=
        candidates.length) {
      _bad();
    }
  }
  AuxiliaryRoute(List<AuxiliaryCandidate> rows)
    : this.fromJson({'candidates': rows.map((e) => e.body).toList()});
  late final List<AuxiliaryCandidate> candidates;
  Map<String, Object?> get body => Map.unmodifiable({
    'candidates': List.unmodifiable(candidates.map((e) => e.body)),
  });
  bool matches(AuxiliaryRoute other) =>
      candidates.length == other.candidates.length &&
      List.generate(
        candidates.length,
        (i) => candidates[i].matches(other.candidates[i]),
      ).every((e) => e);
}

bool auxiliaryRouteSame(AuxiliaryRoute? a, AuxiliaryRoute? b) =>
    a == null ? b == null : b != null && a.matches(b);

class AuxiliaryRoutesSnapshot {
  AuxiliaryRoutesSnapshot.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'backend_id',
      'instance_id',
      'writable',
      'revision',
      'state_token',
      'scope',
      'route',
      'effective_route',
      'source',
      'catalog_profiles',
      'catalog_error',
      'change_boundary',
      'qualification',
      'readiness',
      'limits',
    });
    if (raw['schema_version'] != '1.0' ||
        raw['change_boundary'] != 'new_tasks' ||
        raw['qualification'] != 'not_checked' ||
        raw['readiness'] != 'unknown') {
      _bad();
    }
    backend = _authority(raw['backend_id']);
    instance = _authority(raw['instance_id']);
    writable = _bool(raw['writable']);
    revision = _int(raw['revision'], 0);
    stateToken = _hash(raw['state_token']);
    scope = AuxiliaryScope.fromJson(raw['scope']);
    route = raw['route'] == null ? null : AuxiliaryRoute.fromJson(raw['route']);
    effective = raw['effective_route'] == null
        ? null
        : AuxiliaryRoute.fromJson(raw['effective_route']);
    if (!{'global', 'harness', 'legacy'}.contains(raw['source'])) _bad();
    source = raw['source'] as String;
    if ((source == 'legacy' && (route != null || effective != null)) ||
        (source != 'legacy' && effective == null) ||
        (source == 'harness' && (scope.harness == null || route == null)) ||
        (source == 'global' && scope.harness != null && route != null) ||
        (route != null && !auxiliaryRouteSame(route, effective)) ||
        (scope.harness == null &&
            (source == 'harness' || !auxiliaryRouteSame(route, effective)))) {
      _bad();
    }
    final rows = raw['catalog_profiles'];
    if (rows is! List || rows.length > 32) _bad();
    profiles = List.unmodifiable(
      rows.map((row) {
        if (row is! Map<String, Object?> ||
            row['auxiliary_selectable'] is! bool ||
            row['auxiliary_selectable'] != row['selectable']) {
          _bad();
        }
        return ConverseCatalogProfile.fromJson(
          Map<String, Object?>.of(row)..remove('auxiliary_selectable'),
        );
      }),
    );
    if (profiles.map((e) => e.id).toSet().length != profiles.length) _bad();
    catalogError = raw['catalog_error'] as String?;
    if (catalogError != null &&
        (catalogError!.isEmpty || catalogError!.length > 512)) {
      _bad();
    }
    final limits = _object(raw['limits'], {'candidates', 'routes', 'receipts'});
    if (_int(limits['candidates'], 1) != 3 ||
        _int(limits['routes'], 1) != 1000 ||
        _int(limits['receipts'], 1) != 10000) {
      _bad();
    }
  }
  late final String backend, instance, stateToken, source;
  late final String? catalogError;
  late final int revision;
  late final bool writable;
  late final AuxiliaryScope scope;
  late final AuxiliaryRoute? route, effective;
  late final List<ConverseCatalogProfile> profiles;
  ConverseCatalogProfile? profile(AuxiliaryCandidate candidate) {
    for (final item in profiles) {
      if (item.binding.matches(candidate.binding)) return item;
    }
    return null;
  }

  bool permits(AuxiliaryRoute? route) =>
      route == null ||
      route.candidates.every((candidate) {
        final p = profile(candidate);
        return p != null &&
            p.selectable &&
            {'meta', 'lmstudio'}.contains(p.provider.provider) &&
            (candidate.reasoning == null ||
                p.reasoningOptions.contains(candidate.reasoning)) &&
            candidate.cloudScope ==
                (p.provider.local ? 'local_only' : 'public_synthetic') &&
            (p.provider.provider != 'meta' || !p.provider.local);
      });
  Map<String, Object?> change(AuxiliaryRoute? value, String key) =>
      auxiliaryRouteChange({
        'schema_version': '1.0',
        'expected_backend': backend,
        'expected_instance': instance,
        'expected_revision': revision,
        'expected_state': stateToken,
        'idempotency_key': key,
        'scope': scope.body,
        'route': value?.body,
      });
}

Map<String, Object?> auxiliaryRouteChange(Object? value) {
  final raw = _object(value, {
    'schema_version',
    'expected_backend',
    'expected_instance',
    'expected_revision',
    'expected_state',
    'idempotency_key',
    'scope',
    'route',
  });
  if (raw['schema_version'] != '1.0') _bad();
  return Map.unmodifiable({
    'schema_version': '1.0',
    'expected_backend': _authority(raw['expected_backend']),
    'expected_instance': _authority(raw['expected_instance']),
    'expected_revision': _int(raw['expected_revision'], 0),
    'expected_state': _hash(raw['expected_state']),
    'idempotency_key': auxiliaryRouteKey(raw['idempotency_key']),
    'scope': AuxiliaryScope.fromJson(raw['scope']).body,
    'route': raw['route'] == null
        ? null
        : AuxiliaryRoute.fromJson(raw['route']).body,
  });
}

class AuxiliaryRouteReceipt {
  AuxiliaryRouteReceipt.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'status',
      'backend_id',
      'instance_id',
      'idempotency_key',
      'expected_revision',
      'expected_state',
      'revision',
      'scope',
      'route',
      'change_boundary',
    });
    if (raw['schema_version'] != '1.0' ||
        raw['status'] != 'auxiliary_route_saved' ||
        raw['change_boundary'] != 'new_tasks') {
      _bad();
    }
    request = auxiliaryRouteChange({
      'schema_version': '1.0',
      'expected_backend': raw['backend_id'],
      'expected_instance': raw['instance_id'],
      'expected_revision': raw['expected_revision'],
      'expected_state': raw['expected_state'],
      'idempotency_key': raw['idempotency_key'],
      'scope': raw['scope'],
      'route': raw['route'],
    });
    revision = _int(raw['revision'], 1);
    if (revision != (request['expected_revision'] as int) + 1) _bad();
  }
  late final Map<String, Object?> request;
  late final int revision;
  String get key => request['idempotency_key'] as String;
  bool matches(Map<String, Object?> body) =>
      jsonEncode(request) == jsonEncode(auxiliaryRouteChange(body));
}

class AuxiliaryRouteLookup {
  AuxiliaryRouteLookup.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'backend_id',
      'request',
      'status',
      'receipt',
      'mutation_fenced',
    });
    if (raw['schema_version'] != '1.0' ||
        !{'recorded', 'unobserved'}.contains(raw['status'])) {
      _bad();
    }
    request = auxiliaryRouteChange(raw['request']);
    if (_authority(raw['backend_id']) != request['expected_backend']) _bad();
    receipt = raw['receipt'] == null
        ? null
        : AuxiliaryRouteReceipt.fromJson(raw['receipt']);
    fenced = _bool(raw['mutation_fenced']);
    if ((raw['status'] == 'recorded') != (receipt != null) ||
        (receipt != null && (!receipt!.matches(request) || !fenced))) {
      _bad();
    }
  }
  late final Map<String, Object?> request;
  late final AuxiliaryRouteReceipt? receipt;
  late final bool fenced;
  bool matches(Map<String, Object?> body) =>
      jsonEncode(request) == jsonEncode(auxiliaryRouteChange(body));
}

Object? auxiliaryRouteJsonDecode(List<int> bytes) {
  final value = permissionJsonDecode(bytes);
  void check(Object? child) {
    if (child is String) {
      for (var i = 0; i < child.length; i++) {
        final c = child.codeUnitAt(i);
        if (c >= 0xd800 && c <= 0xdbff) {
          if (++i >= child.length ||
              child.codeUnitAt(i) < 0xdc00 ||
              child.codeUnitAt(i) > 0xdfff) {
            _bad();
          }
        } else if (c >= 0xdc00 && c <= 0xdfff) {
          _bad();
        }
        if (c < 32 || c == 127) _bad();
      }
    } else if (child is Map) {
      for (final e in child.entries) {
        check(e.key);
        check(e.value);
      }
    } else if (child is List) {
      for (final item in child) {
        check(item);
      }
    }
  }

  check(value);
  return value;
}
