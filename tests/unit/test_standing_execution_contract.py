"""Independent #228 execution grammar oracles, frozen before new source read.

Packets/builders are not native claims, admission, liveness or effect settlement.
All vectors and the 32-byte capability are public synthetic test data.
"""

import copy
import hashlib
import importlib
import json
from datetime import UTC, datetime

import pytest

from packages.contracts.agent_submit import submit_entry, submit_request
from packages.contracts.models import AgentModelPin, AppendRequest, JournalEntry

SAFE = 9_007_199_254_740_991
OTHER = "abcdefab-cdef-abcd-efab-cdefabcdefab"
MARKER = "SYNTHETIC-PRIVATE-INVALID-INPUT"
PREPARED = "2026-11-01T08:30:01Z"
SYNTHETIC_SECRET = bytes(range(32))
GOLDEN_HASHES = {
    "occurrence_id": "4447875ff7f04d1592feb1d8676e1c933c9df793b7a0cc7987d0d8a781b03392",
    "intent_digest": "b8a33969f6461351423fef866d79450f3c9fc062e05e934ae0c087681cb1edf4",
    "claim_id": "e8d5339e2ac5a23849bee09619a7f03751e65141ebf61e2e64801836b8174e88",
    "claim_digest": "706c2c2ad143b9e0523eb25fac043f125ec1a3d50413cba82c14e2951d1793fe",
    "capability_digest": "d53537a4bf0c6f59862c2adfbf75616dfd7f06bf9ddbd007d4b80fd465fc3661",
}
GOLDEN_INTENT = json.loads(r"""{
  "schema_version": "1.0",
  "idempotency_key": "standing:synthetic-1",
  "expected_backend": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "expected_instance": "00000000-0000-0000-0000-000000000111",
  "responsibility_id": "00000000-0000-0000-0000-000000000222",
  "authorization_revision": 2,
  "authorization_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "record_digest": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "occurrence": {
    "schema_version": "1.0",
    "local_date": "2026-11-01",
    "due_utc": "2026-11-01T08:30:00Z",
    "utc_offset_seconds": -25200
  },
  "dispatch_epoch": 7,
  "activation_digest": "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1",
  "control_revision": 9,
  "producer": {
    "schema_version": "1.0",
    "request": {
      "schema_version": "1.0",
      "expected_backend": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "expected_instance": "00000000-0000-0000-0000-000000000111",
      "producer_id": "00000000-0000-0000-0000-000000000444",
      "sequence": 2
    },
    "lease_token": "00000000-0000-0000-0000-000000000555",
    "issued_monotonic_ms": 123450,
    "expires_monotonic_ms": 138450
  },
  "executor_id": "00000000-0000-0000-0000-000000000333",
  "capability_sha256": "d53537a4bf0c6f59862c2adfbf75616dfd7f06bf9ddbd007d4b80fd465fc3661",
  "task": {
    "schema_version": "1.0",
    "task_id": "00000000-0000-0000-0000-000000000666",
    "task_session": {
      "session_id": "synthetic-agent-é",
      "thread_id": "synthetic-thread-α",
      "epoch": 3
    },
    "objective": "Inspect café.txt and é.txt literally; preserve 🧭.",
    "content_scope": "public_synthetic",
    "workspace_pin": {
      "schema_version": "1.0",
      "workspace_id": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
      "root_identity": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
      "settings_revision": 4,
      "source": "owner_saved",
      "shell_backend": "bubblewrap",
      "mode": "direct"
    },
    "harness_version": "seed-v1",
    "model_pin": {
      "schema_version": "1.0",
      "profile_id": "synthetic.profile",
      "provider": "synthetic-native",
      "model": "synthetic-model-🧭",
      "local": true,
      "reasoning": null,
      "settings_revision": 7,
      "catalog_binding": {
        "id": "synthetic.profile",
        "generation": 3,
        "configuration_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
      },
      "catalog_revision": 11
    },
    "tool_subset": "native-files-v1"
  }
}""")
GOLDEN_INITIAL = json.loads(r"""{
  "schema_version": "1.0",
  "session_id": "synthetic-agent-é",
  "thread_id": "synthetic-thread-α",
  "epoch": 3,
  "kind": "agent.standing-command",
  "causes": [],
  "task_refs": [
    "00000000-0000-0000-0000-000000000666"
  ],
  "idempotency_key": "agent:00000000-0000-0000-0000-000000000666:agent.standing-command:1",
  "payload": {
    "op": "submit",
    "actor": "standing",
    "task_id": "00000000-0000-0000-0000-000000000666",
    "state": "accepted",
    "session_id": "synthetic-agent-é",
    "thread_id": "synthetic-thread-α",
    "epoch": 3,
    "objective": "Inspect café.txt and é.txt literally; preserve 🧭.",
    "objective_revision": 1,
    "source_event_ids": [],
    "source_revision": 1,
    "requested_content_scope": "public_synthetic",
    "content_scope": "public_synthetic",
    "idempotency_key": "standing:4447875ff7f04d1592feb1d8676e1c933c9df793b7a0cc7987d0d8a781b03392",
    "payload_digest": "b8a33969f6461351423fef866d79450f3c9fc062e05e934ae0c087681cb1edf4",
    "harness_version": "seed-v1",
    "model_pin": {
      "schema_version": "1.0",
      "profile_id": "synthetic.profile",
      "provider": "synthetic-native",
      "model": "synthetic-model-🧭",
      "local": true,
      "reasoning": null,
      "settings_revision": 7,
      "catalog_binding": {
        "id": "synthetic.profile",
        "generation": 3,
        "configuration_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
      },
      "catalog_revision": 11
    },
    "workspace_pin": {
      "schema_version": "1.0",
      "workspace_id": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
      "root_identity": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
      "settings_revision": 4,
      "source": "owner_saved",
      "shell_backend": "bubblewrap",
      "mode": "direct"
    },
    "auxiliary_models": {},
    "plugin_versions": {},
    "explicit_skill": null,
    "standing": {
      "schema_version": "1.0",
      "occurrence_id": "4447875ff7f04d1592feb1d8676e1c933c9df793b7a0cc7987d0d8a781b03392",
      "intent_digest": "b8a33969f6461351423fef866d79450f3c9fc062e05e934ae0c087681cb1edf4",
      "claim_id": "e8d5339e2ac5a23849bee09619a7f03751e65141ebf61e2e64801836b8174e88",
      "claim_digest": "706c2c2ad143b9e0523eb25fac043f125ec1a3d50413cba82c14e2951d1793fe",
      "responsibility_id": "00000000-0000-0000-0000-000000000222",
      "authorization_revision": 2,
      "authorization_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "record_digest": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      "dispatch_epoch": 7,
      "activation_digest": "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1",
      "control_revision": 9,
      "executor_id": "00000000-0000-0000-0000-000000000333",
      "capability_sha256": "d53537a4bf0c6f59862c2adfbf75616dfd7f06bf9ddbd007d4b80fd465fc3661",
      "producer_id": "00000000-0000-0000-0000-000000000444",
      "tool_subset": "native-files-v1"
    }
  }
}""")


def execution():
    try:
        return importlib.import_module("packages.contracts.standing_execution")
    except ModuleNotFoundError as exc:
        if exc.name != "packages.contracts.standing_execution":
            raise
        pytest.fail("Standing execution grammar capability is absent", pytrace=False)


def encoded(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def hashed(domain, value):
    return hashlib.sha256(domain.encode("ascii") + b"\0" + encoded(value)).hexdigest()


def intent():
    return copy.deepcopy(GOLDEN_INTENT)


def occurrence():
    return intent()["occurrence"]


def pin():
    return intent()["task"]["model_pin"]


def task():
    return intent()["task"]


def expected_hashes(original):
    due = hashed(
        "kinaiya.standing.occurrence.v1",
        {
            "backend_id": original["expected_backend"],
            **{
                name: original[name]
                for name in (
                    "responsibility_id",
                    "authorization_revision",
                    "authorization_digest",
                    "record_digest",
                    "occurrence",
                )
            },
        },
    )
    whole = hashed("kinaiya.standing.intent.v1", original)
    identifier = hashed("kinaiya.standing.claim.id.v1", {"intent_digest": whole})
    return {"occurrence_id": due, "intent_digest": whole, "claim_id": identifier}


def claim(original=None, prepared=PREPARED):
    original = intent() if original is None else copy.deepcopy(original)
    values = expected_hashes(original)
    record = {"request": original, **values, "prepared_utc": prepared}
    return {
        "schema_version": "1.0",
        **record,
        "claim_digest": hashed("kinaiya.standing.claim.record.v1", record),
    }


def admission():
    return {"schema_version": "1.0", "disposition": "created", "claim": claim()}


CASES = (
    ("StandingOccurrence", occurrence, 4096),
    ("StandingExecutionModelPin", pin, 8192),
    ("StandingExecutionTask", task, 65536),
    ("StandingExecutionIntent", intent, 131072),
    ("StandingExecutionClaim", claim, 147456),
    ("StandingExecutionClaimAdmission", admission, 163840),
)


def refuses(call, *args):
    with pytest.raises(ValueError) as error:
        call(*args)
    assert MARKER not in str(error.value)
    return str(error.value)


def at(value, path):
    for name in path:
        value = value[name]
    return value


def changed(value, path, replacement):
    value = copy.deepcopy(value)
    at(value, path[:-1])[path[-1]] = replacement
    return value


def reverse_maps(value):
    if isinstance(value, dict):
        return {key: reverse_maps(value[key]) for key in reversed(value)}
    if isinstance(value, list):
        return [reverse_maps(item) for item in value]
    return value


def initial_expected(proof):
    original = proof["request"]
    work = original["task"]
    session = work["task_session"]
    return {
        "schema_version": "1.0",
        **session,
        "kind": "agent.standing-command",
        "causes": [],
        "task_refs": [work["task_id"]],
        "idempotency_key": f"agent:{work['task_id']}:agent.standing-command:1",
        "payload": {
            "op": "submit",
            "actor": "standing",
            "task_id": work["task_id"],
            "state": "accepted",
            **session,
            "objective": work["objective"],
            "objective_revision": 1,
            "source_event_ids": [],
            "source_revision": 1,
            "requested_content_scope": work["content_scope"],
            "content_scope": work["content_scope"],
            "idempotency_key": "standing:" + proof["occurrence_id"],
            "payload_digest": proof["intent_digest"],
            "harness_version": work["harness_version"],
            "model_pin": copy.deepcopy(work["model_pin"]),
            "workspace_pin": copy.deepcopy(work["workspace_pin"]),
            "auxiliary_models": {},
            "plugin_versions": {},
            "explicit_skill": None,
            "standing": {
                "schema_version": "1.0",
                **{
                    name: proof[name]
                    for name in ("claim_id", "claim_digest", "occurrence_id", "intent_digest")
                },
                **{
                    name: original[name]
                    for name in (
                        "responsibility_id",
                        "authorization_revision",
                        "authorization_digest",
                        "record_digest",
                        "dispatch_epoch",
                        "activation_digest",
                        "control_revision",
                        "executor_id",
                        "capability_sha256",
                    )
                },
                "producer_id": original["producer"]["request"]["producer_id"],
                "tool_subset": "native-files-v1",
            },
        },
    }


def legacy_request(actor="owner"):
    raw = copy.deepcopy(GOLDEN_INITIAL)
    raw["kind"] = "agent.command"
    raw["idempotency_key"] = f"agent:{raw['task_refs'][0]}:agent.command:1"
    payload = raw["payload"]
    for name in ("standing", "workspace_pin", "auxiliary_models"):
        payload.pop(name)
    payload.update(actor=actor, idempotency_key="synthetic-old-client-key")
    if actor == "decisions":
        payload["source_event_ids"] = ["synthetic-authenticated-source"]
    logical = {
        name: payload[name]
        for name in (
            "session_id",
            "thread_id",
            "epoch",
            "objective",
            "source_event_ids",
            "source_revision",
            "content_scope",
        )
    }
    # Existing 1.0 digest uses JSON's default ASCII escaping; do not replace it
    # with the new family's ensure_ascii=False canonical hash.
    payload["payload_digest"] = hashlib.sha256(
        json.dumps(logical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return AppendRequest.model_validate(raw)


def task_at_canonical_size(maximum):
    work = task()
    work["objective"] = ""
    remaining = maximum - len(encoded(work))
    assert remaining > 0
    work["objective"] = "\0" * (remaining // 6) + "x" * (remaining % 6)
    assert 1 <= len(work["objective"]) <= 16000
    assert len(encoded(work)) == maximum
    return work


def intent_at_initial_size(maximum):
    original = intent()
    original["task"]["objective"] = ""
    projected = AppendRequest.model_validate(initial_expected(claim(original)))
    remaining = maximum - len(projected.model_dump_json().encode("utf-8"))
    assert remaining > 0
    original["task"]["objective"] = "\0" * (remaining // 6) + "x" * (remaining % 6)
    assert 1 <= len(original["task"]["objective"]) <= 16000
    projected = AppendRequest.model_validate(initial_expected(claim(original)))
    assert len(projected.model_dump_json().encode("utf-8")) == maximum
    assert len(encoded(original["task"])) <= 65536
    return original


def test_literal_stdlib_golden_binds_all_hash_domains_and_exact_initial_projection():
    module = execution()
    original = module.StandingExecutionIntent.model_validate(intent())
    proof = module.StandingExecutionClaim.model_validate(claim())
    assert module.occurrence_id(original) == GOLDEN_HASHES["occurrence_id"]
    assert module.intent_digest(original) == GOLDEN_HASHES["intent_digest"]
    assert module.claim_id(original) == GOLDEN_HASHES["claim_id"]
    assert module.claim_digest(original, PREPARED) == GOLDEN_HASHES["claim_digest"]
    assert (
        module.capability_digest(
            original.expected_backend,
            original.idempotency_key,
            original.executor_id,
            SYNTHETIC_SECRET,
        )
        == GOLDEN_HASHES["capability_digest"]
    )
    assert proof.canonical_bytes() == encoded(claim())
    built = module.standing_initial_request(proof)
    assert isinstance(built, AppendRequest)
    assert built.model_dump(mode="json") == GOLDEN_INITIAL


@pytest.mark.parametrize("name,vector,maximum", CASES)
def test_six_complete_packets_roundtrip_reorder_detach_and_are_frozen(name, vector, maximum):
    kind = getattr(execution(), name)
    raw = vector()
    expected = encoded(raw)
    assert len(expected) < maximum
    parsed = kind.model_validate(raw)
    assert parsed.model_dump(mode="json") == raw
    assert parsed.canonical_bytes() == expected
    assert kind.model_validate(reverse_maps(raw)).canonical_bytes() == expected
    assert kind.from_json(expected).canonical_bytes() == expected
    assert (
        kind.model_validate(kind.model_construct(**parsed.__dict__)).canonical_bytes() == expected
    )
    raw["schema_version"] = "later caller mutation"
    for nested in raw.values():
        if isinstance(nested, dict):
            nested.clear()
            break
    assert parsed.canonical_bytes() == expected
    refuses(setattr, parsed, "schema_version", "1.1")


@pytest.mark.parametrize("name,vector,maximum", CASES)
def test_each_class_enforces_its_raw_cap_including_valid_whitespace(name, vector, maximum):
    kind = getattr(execution(), name)
    body = encoded(vector())
    exact = b" " * (maximum - len(body)) + body
    assert len(exact) == maximum
    assert kind.from_json(exact).canonical_bytes() == body
    refuses(kind.from_json, b" " + exact)


@pytest.mark.parametrize("name,vector,maximum", CASES)
def test_every_top_level_field_is_required_and_the_actual_model_is_closed(name, vector, maximum):
    kind = getattr(execution(), name)
    good = vector()
    for key in good:
        missing = copy.deepcopy(good)
        missing.pop(key)
        refuses(kind.model_validate, missing)
    for version in (None, 1, "1.1"):
        refuses(kind.model_validate, {**good, "schema_version": version})
    refuses(kind.model_validate, {**good, "synthetic_unknown": MARKER})
    model = kind.model_validate(good)
    constructed = kind.model_construct(**model.__dict__)
    constructed.__dict__["synthetic_unknown"] = MARKER
    for bad in (model.model_copy(update={"synthetic_unknown": MARKER}), constructed):
        refuses(bad.canonical_bytes)
        refuses(kind.model_validate, bad)


@pytest.mark.parametrize("name,vector,maximum", CASES)
def test_strict_raw_json_duplicate_encoding_and_nonobject_refusals(name, vector, maximum):
    kind = getattr(execution(), name)
    body = encoded(vector())
    for bad in (
        b'{"schema_version":"1.0",' + body[1:],
        body + b" {}",
        b"\xff" + body,
        body.decode().encode("utf-16"),
        body.decode().encode("utf-32"),
        b"null",
        b"[]",
    ):
        refuses(kind.from_json, bad)
    for bad_type in (body.decode(), bytearray(body), memoryview(body), None):
        refuses(kind.from_json, bad_type)


@pytest.mark.parametrize(
    "path",
    (
        ("task",),
        ("task", "task_session"),
        ("task", "workspace_pin"),
        ("task", "model_pin"),
        ("task", "model_pin", "catalog_binding"),
        ("producer",),
        ("producer", "request"),
        ("occurrence",),
    ),
)
def test_complete_nested_raw_closure_precedes_older_model_constructors(path):
    kind = execution().StandingExecutionIntent
    good = intent()
    for key in at(good, path):
        bad = copy.deepcopy(good)
        at(bad, path).pop(key)
        refuses(kind.model_validate, bad)
    bad = copy.deepcopy(good)
    at(bad, path)["synthetic_unknown"] = MARKER
    refuses(kind.model_validate, bad)
    assert kind.model_validate(good).canonical_bytes() == encoded(good)


@pytest.mark.parametrize(
    "path",
    (
        ("authorization_revision",),
        ("dispatch_epoch",),
        ("control_revision",),
        ("occurrence", "utc_offset_seconds"),
        ("task", "task_session", "epoch"),
        ("task", "workspace_pin", "settings_revision"),
        ("task", "model_pin", "settings_revision"),
        ("task", "model_pin", "catalog_revision"),
        ("task", "model_pin", "catalog_binding", "generation"),
        ("producer", "request", "sequence"),
        ("producer", "issued_monotonic_ms"),
        ("producer", "expires_monotonic_ms"),
    ),
)
def test_all_integer_boundaries_refuse_boolean_float_string_and_unsafe_coercion(path):
    kind = execution().StandingExecutionIntent
    for bad in (True, False, 1.0, "1", SAFE + 1):
        raw = changed(intent(), path, bad)
        refuses(kind.model_validate, raw)
        refuses(kind.from_json, encoded(raw))


def test_safe_counter_endpoints_and_pure_eligibility_nonclaims():
    module = execution()
    raw = intent()
    raw.update(authorization_revision=SAFE, control_revision=SAFE, dispatch_epoch=10000)
    raw["task"]["task_session"]["epoch"] = SAFE
    raw["task"]["workspace_pin"]["settings_revision"] = SAFE - 1
    raw["task"]["model_pin"].update(settings_revision=SAFE, catalog_revision=SAFE - 1)
    raw["task"]["model_pin"]["catalog_binding"]["generation"] = 64
    raw["producer"]["request"]["sequence"] = SAFE
    raw["producer"].update(issued_monotonic_ms=SAFE - 15000, expires_monotonic_ms=SAFE)
    parsed = module.StandingExecutionIntent.model_validate(raw)
    assert parsed.canonical_bytes() == encoded(raw)
    # Calendar/offset correlation, current UTC, eligibility and producer lease
    # liveness require native joined authority, not these pure packets.
    raw["occurrence"].update(local_date="0001-01-01", due_utc="9999-12-31T23:59:59Z")
    assert module.StandingExecutionIntent.model_validate(raw).occurrence.local_date == "0001-01-01"


def test_occurrence_exact_gregorian_text_offset_and_leap_boundaries():
    kind = execution().StandingOccurrence
    for offset in (-86399, 0, 86399):
        raw = {**occurrence(), "utc_offset_seconds": offset}
        assert kind.model_validate(raw).utc_offset_seconds == offset
    leap = {**occurrence(), "local_date": "2000-02-29", "due_utc": "2000-02-29T00:00:00Z"}
    assert kind.model_validate(leap).local_date == "2000-02-29"
    for field, values in (
        ("local_date", ("1900-02-29", "2026-02-30", "0000-01-01", "2026-1-01", "2026-11-01\n")),
        (
            "due_utc",
            (
                "1900-02-29T00:00:00Z",
                "2026-11-01T24:00:00Z",
                "2026-11-01T00:00:60Z",
                "2026-11-01T08:30:00+00:00",
                "2026-11-01T08:30:00.000Z",
                "2026-11-01t08:30:00Z",
                datetime(2026, 11, 1, tzinfo=UTC),
            ),
        ),
        ("utc_offset_seconds", (-86400, 86400, True, 1.0, "0")),
    ):
        for value in values:
            refuses(kind.model_validate, {**occurrence(), field: value})


def test_new_model_pin_has_explicit_nulls_exact_catalog_and_literal_unicode():
    module = execution()
    kind = module.StandingExecutionModelPin
    bare = pin()
    bare.update(catalog_binding=None, catalog_revision=None, reasoning="")
    assert kind.model_validate(bare).model_dump(mode="json") == bare
    bare.update(profile_id="é\n", provider="synthetic\tprovider", model="🧭", reasoning=None)
    assert kind.model_validate(bare).profile_id == "é\n"
    for field, value in (
        ("catalog_binding", None),
        ("catalog_revision", None),
        ("catalog_revision", SAFE),
        ("profile_id", "another-profile"),
        ("local", 1),
        ("local", "true"),
    ):
        refuses(kind.model_validate, {**pin(), field: value})
    for field, maximum in (
        ("profile_id", 80),
        ("provider", 128),
        ("model", 256),
        ("reasoning", 16),
    ):
        raw = {**bare, field: "🧭" * maximum}
        assert getattr(kind.model_validate(raw), field) == raw[field]
        for bad in ("x" * (maximum + 1), "bad\0value", "bad\ud800value", True):
            refuses(kind.model_validate, {**bare, field: bad})
    for field in ("profile_id", "provider", "model"):
        refuses(kind.model_validate, {**bare, field: ""})
    for change in (
        {"generation": 0},
        {"generation": 65},
        {"id": "/profile"},
        {"configuration_sha256": "D" * 64},
    ):
        raw = pin()
        raw["catalog_binding"].update(change)
        refuses(kind.model_validate, raw)
    # The new family cannot use the old pin serializer to hide omitted nulls.
    old = AgentModelPin.model_validate(
        {
            key: value
            for key, value in bare.items()
            if key not in {"catalog_binding", "catalog_revision"}
        }
    )
    refuses(module.StandingExecutionTask.model_validate, {**task(), "model_pin": old})


def test_task_literal_objective_scope_harness_subset_and_session_boundaries():
    kind = execution().StandingExecutionTask
    for objective in ("x", "  é and e\u0301  ", "literal\0objective", "x" * 16000):
        assert kind.model_validate({**task(), "objective": objective}).objective == objective
    for scope in ("local_only", "public_synthetic", "private_cloud_authorized"):
        assert kind.model_validate({**task(), "content_scope": scope}).content_scope == scope
    assert (
        kind.model_validate({**task(), "harness_version": "v-" + "a" * 24}).harness_version
        == "v-" + "a" * 24
    )
    for field, bad in (
        ("objective", ""),
        ("objective", "x" * 16001),
        ("objective", "bad\udffftext"),
        ("harness_version", "v-" + "A" * 24),
        ("harness_version", "seed-v1\n"),
        ("tool_subset", "all-tools"),
        ("content_scope", "public"),
        ("model_pin", None),
        ("task_id", OTHER.upper()),
    ):
        refuses(kind.model_validate, {**task(), field: bad})
    for field in ("session_id", "thread_id"):
        for bad in ("", "x" * 129, "bad\0id", "bad\ud800id"):
            raw = task()
            raw["task_session"][field] = bad
            refuses(kind.model_validate, raw)
    raw = task()
    raw["task_session"]["schema_version"] = "1.0"
    refuses(kind.model_validate, raw)


def test_task_canonical_bound_is_distinct_from_valid_field_character_counts():
    kind = execution().StandingExecutionTask
    exact = task_at_canonical_size(65536)
    assert len(kind.model_validate(exact).canonical_bytes()) == 65536
    over = task_at_canonical_size(65537)
    assert len(over["objective"]) <= 16000
    refuses(kind.model_validate, over)
    constructed = kind.model_construct(**over)
    refuses(constructed.canonical_bytes)


def test_intent_full_native_identity_and_no_inference_from_receipt_time():
    kind = execution().StandingExecutionIntent
    for path, value in (
        (("expected_backend",), "A" * 32),
        (("idempotency_key",), "bad/key"),
        (("idempotency_key",), "x" * 129),
        (("expected_instance",), OTHER.upper()),
        (("responsibility_id",), OTHER + "\n"),
        (("executor_id",), "not-issued-uuid"),
        (("capability_sha256",), "a" * 63),
        (("authorization_digest",), "B" * 64),
        (("record_digest",), "c" * 65),
        (("activation_digest",), None),
        (("dispatch_epoch",), 0),
        (("dispatch_epoch",), 10001),
        (("authorization_revision",), 0),
        (("control_revision",), 0),
        (("producer", "request", "expected_backend"), "f" * 32),
        (("producer", "request", "expected_instance"), OTHER),
        (("producer", "expires_monotonic_ms"), 138451),
    ):
        refuses(kind.model_validate, changed(intent(), path, value))
    # A syntactically valid historical interval does not prove it is live.
    historical = intent()
    historical["producer"].update(issued_monotonic_ms=0, expires_monotonic_ms=15000)
    assert kind.model_validate(historical).producer.expires_monotonic_ms == 15000


def test_occurrence_identity_excludes_dispatch_attempts_but_full_intent_binds_them():
    module = execution()
    original = module.StandingExecutionIntent.model_validate(intent())
    for path, value in (
        (("idempotency_key",), "standing:another-attempt"),
        (("executor_id",), OTHER),
        (("capability_sha256",), "f" * 64),
        (("task", "task_id"), OTHER),
        (("task", "objective"), "A distinct objective"),
        (("task", "workspace_pin", "root_identity"), "1" * 64),
        (("task", "model_pin", "settings_revision"), 8),
        (("producer", "request", "sequence"), 3),
        (("producer", "lease_token"), OTHER),
        (("dispatch_epoch",), 8),
        (("activation_digest",), "2" * 64),
        (("control_revision",), 10),
    ):
        raw = changed(intent(), path, value)
        parsed = module.StandingExecutionIntent.model_validate(raw)
        assert module.occurrence_id(parsed) == module.occurrence_id(original), path
        assert module.intent_digest(parsed) == expected_hashes(raw)["intent_digest"], path
        assert module.intent_digest(parsed) != module.intent_digest(original), path
        assert module.claim_id(parsed) != module.claim_id(original), path
    for path, value in (
        (("responsibility_id",), OTHER),
        (("authorization_revision",), 3),
        (("authorization_digest",), "3" * 64),
        (("record_digest",), "4" * 64),
        (("occurrence", "local_date"), "2026-11-02"),
        (("occurrence", "due_utc"), "2026-11-01T09:30:00Z"),
        (("occurrence", "utc_offset_seconds"), -28800),
    ):
        raw = changed(intent(), path, value)
        parsed = module.StandingExecutionIntent.model_validate(raw)
        assert module.occurrence_id(parsed) == expected_hashes(raw)["occurrence_id"], path
        assert module.occurrence_id(parsed) != module.occurrence_id(original), path
    raw = intent()
    raw["expected_backend"] = raw["producer"]["request"]["expected_backend"] = "f" * 32
    assert module.occurrence_id(
        module.StandingExecutionIntent.model_validate(raw)
    ) != module.occurrence_id(original)


def test_hashes_preserve_unicode_and_reordering_without_mutating_inputs():
    module = execution()
    before = intent()
    first = module.StandingExecutionIntent.model_validate(before)
    second = module.StandingExecutionIntent.model_validate(reverse_maps(before))
    assert (
        module.intent_digest(first)
        == module.intent_digest(second)
        == GOLDEN_HASHES["intent_digest"]
    )
    assert before == GOLDEN_INTENT
    composed = changed(intent(), ("task", "objective"), "café")
    decomposed = changed(intent(), ("task", "objective"), "cafe\u0301")
    assert module.intent_digest(
        module.StandingExecutionIntent.model_validate(composed)
    ) != module.intent_digest(module.StandingExecutionIntent.model_validate(decomposed))


def test_claim_hashes_original_binding_and_prepared_time_are_exact():
    module = execution()
    original = module.StandingExecutionIntent.model_validate(intent())
    for field in ("claim_id", "occurrence_id", "intent_digest", "claim_digest"):
        refuses(module.StandingExecutionClaim.model_validate, {**claim(), field: "0" * 64})
    proof = module.StandingExecutionClaim.model_validate(claim())
    assert proof.validate_request(original).canonical_bytes() == proof.canonical_bytes()
    altered = changed(intent(), ("idempotency_key",), "standing:different-original")
    caller = module.StandingExecutionIntent.model_validate(altered)
    refuses(proof.validate_request, caller)
    alternate_time = "2026-11-01T08:30:02Z"
    later = module.StandingExecutionClaim.model_validate(claim(prepared=alternate_time))
    assert later.claim_id == proof.claim_id
    assert later.occurrence_id == proof.occurrence_id
    assert later.intent_digest == proof.intent_digest
    assert later.claim_digest != proof.claim_digest
    assert module.claim_digest(original, alternate_time) == later.claim_digest
    for bad in (
        PREPARED + "\n",
        "2026-11-01T08:30:01+00:00",
        "2026-02-30T00:00:00Z",
        datetime(2026, 11, 1, tzinfo=UTC),
        None,
    ):
        refuses(module.claim_digest, original, bad)
        refuses(module.StandingExecutionClaim.model_validate, {**claim(), "prepared_utc": bad})


def test_created_and_retained_packets_are_only_bound_immutable_metadata():
    module = execution()
    original = module.StandingExecutionIntent.model_validate(intent())
    for disposition in ("created", "retained"):
        raw = {**admission(), "disposition": disposition}
        parsed = module.StandingExecutionClaimAdmission.model_validate(raw)
        assert parsed.validate_request(original).canonical_bytes() == encoded(raw)
        assert parsed.claim.claim_id == GOLDEN_HASHES["claim_id"]
        different = module.StandingExecutionIntent.model_validate(
            changed(intent(), ("executor_id",), OTHER)
        )
        refuses(parsed.validate_request, different)
    for disposition in (None, True, "accepted", "recorded", "unobserved"):
        refuses(
            module.StandingExecutionClaimAdmission.model_validate,
            {**admission(), "disposition": disposition},
        )


def test_typed_nested_actual_extras_missing_fields_and_known_coercions_cannot_be_dumped_away():
    module = execution()
    original = module.StandingExecutionIntent.model_validate(intent())
    for path in (
        ("task",),
        ("task", "task_session"),
        ("task", "workspace_pin"),
        ("task", "model_pin"),
        ("task", "model_pin", "catalog_binding"),
        ("producer", "request"),
        ("occurrence",),
    ):
        bad = original.model_copy(deep=True)
        nested = bad
        for field in path:
            nested = getattr(nested, field)
        nested.__dict__["synthetic_unknown"] = MARKER
        refuses(bad.canonical_bytes)
        refuses(module.StandingExecutionIntent.model_validate, bad)
        for helper in (module.occurrence_id, module.intent_digest, module.claim_id):
            refuses(helper, bad)
        refuses(module.claim_digest, bad, PREPARED)
    for field in ("catalog_binding", "catalog_revision", "reasoning"):
        bad = original.model_copy(deep=True)
        bad.task.model_pin.__dict__.pop(field)
        refuses(bad.canonical_bytes)
    bad = original.model_copy(deep=True)
    bad.producer.request.__dict__["sequence"] = True
    refuses(bad.canonical_bytes)
    bad = original.model_copy(deep=True)
    bad.task.workspace_pin.__dict__["settings_revision"] = 4.0
    refuses(module.intent_digest, bad)
    proof = module.StandingExecutionClaim.model_validate(claim())
    corrupted = proof.model_copy(update={"synthetic_unknown": MARKER})
    refuses(module.standing_initial_request, corrupted)
    refuses(proof.validate_request, original.model_copy(update={"synthetic_unknown": MARKER}))
    admitted = module.StandingExecutionClaimAdmission.model_validate(admission())
    bad_admitted = admitted.model_copy(update={"claim": corrupted})
    refuses(bad_admitted.validate_request, original)


def test_whole_graph_depth_nodes_invalid_unicode_and_nonjson_objects_fail_generically():
    kind = execution().StandingExecutionIntent
    deep = MARKER
    for _ in range(15):
        deep = [deep]
    for value in (deep, [MARKER] * 2049, {"nested": object()}, "bad\ud800text"):
        refuses(kind.model_validate, changed(intent(), ("task", "objective"), value))
    body = encoded(intent()).replace("Inspect café".encode(), b"Inspect \\ud800")
    refuses(kind.from_json, body)


def test_hash_helpers_revalidate_types_and_capability_has_no_circular_claim_dependency():
    module = execution()
    original = module.StandingExecutionIntent.model_validate(intent())
    for helper in (module.occurrence_id, module.intent_digest, module.claim_id):
        for bad in (None, True, module.StandingOccurrence.model_validate(occurrence())):
            refuses(helper, bad)
    good_args = (
        original.expected_backend,
        original.idempotency_key,
        original.executor_id,
        SYNTHETIC_SECRET,
    )
    assert module.capability_digest(*good_args) == original.capability_sha256
    for index, value in (
        (0, "A" * 32),
        (0, None),
        (1, "bad/key"),
        (1, MARKER + "\0"),
        (2, OTHER.upper()),
        (3, bytes(31)),
        (3, bytes(33)),
        (3, bytearray(SYNTHETIC_SECRET)),
        (3, memoryview(SYNTHETIC_SECRET)),
        (3, MARKER),
    ):
        bad = list(good_args)
        bad[index] = value
        refuses(module.capability_digest, *bad)
    for index, value in (
        (0, "f" * 32),
        (1, "standing:other"),
        (2, OTHER),
        (3, bytes(reversed(range(32)))),
    ):
        different = list(good_args)
        different[index] = value
        assert module.capability_digest(*different) != original.capability_sha256
    assert "secret" not in original.model_dump_json()


def test_initial_builder_is_complete_deterministic_detached_and_explicit_standing():
    module = execution()
    proof = module.StandingExecutionClaim.model_validate(claim())
    before = proof.canonical_bytes()
    first = module.standing_initial_request(proof)
    assert first.model_dump(mode="json") == GOLDEN_INITIAL == initial_expected(claim())
    assert first.kind == "agent.standing-command"
    assert first.payload["actor"] == "standing"
    assert first.causes == first.payload["source_event_ids"] == []
    first.payload["model_pin"]["model"] = "local caller mutation"
    first.payload["workspace_pin"]["root_identity"] = "0" * 64
    first.payload["standing"]["claim_digest"] = "0" * 64
    assert proof.canonical_bytes() == before
    second = module.standing_initial_request(proof)
    assert second.model_dump(mode="json") == GOLDEN_INITIAL
    refuses(submit_request, second)
    refuses(submit_request, second.model_dump(mode="json"))
    native = JournalEntry(
        **second.model_dump(),
        event_id="synthetic-standing-initial-event",
        sequence=1,
        origin="standing-supervisor",
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    refuses(submit_entry, native)


def test_initial_builder_uses_actual_append_serialization_cap_without_truncation():
    module = execution()
    exact = intent_at_initial_size(65536)
    proof = module.StandingExecutionClaim.model_validate(claim(exact))
    before = proof.canonical_bytes()
    built = module.standing_initial_request(proof)
    assert len(built.model_dump_json().encode("utf-8")) == 65536
    assert built.payload["objective"] == exact["task"]["objective"]
    assert built.model_dump(mode="json") == initial_expected(claim(exact))
    too_large = intent_at_initial_size(65537)
    valid_claim = module.StandingExecutionClaim.model_validate(claim(too_large))
    refuses(module.standing_initial_request, valid_claim)
    assert proof.canonical_bytes() == before
    assert valid_claim.request.task.objective == too_large["task"]["objective"]


@pytest.mark.parametrize("actor", ("owner", "decisions"))
def test_existing_owner_decisions_grammar_positive_control_remains_unchanged(actor):
    old = legacy_request(actor)
    before = old.model_dump(mode="json")
    assert submit_request(old).model_dump(mode="json") == before
    assert before["kind"] == "agent.command"
    assert before["payload"]["actor"] == actor
