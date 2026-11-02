"""Independent Codex #228 phase1 oracles, frozen before implementation import.

These prove pure grammar and embedded-request identity only. They do not prove
owner authentication, a caller-held original request, native save or dispatch.
"""

import copy
import hashlib
import json

import pytest

from packages.contracts.standing_work import (
    DailyOccurrence,
    DailySchedule,
    StandingAuthorization,
    StandingChange,
    StandingChangeReceipt,
    StandingDelivery,
    StandingQuietHours,
    StandingTaskSession,
    authorization_digest,
    canonical_bytes,
    standing_json,
)

JS_SAFE = 9_007_199_254_740_991
BACKEND = "a" * 32
INSTANCE = "01234567-89ab-cdef-0123-456789abcdef"
RESPONSIBILITY = "12345678-1234-4321-abcd-123456789abc"
OTHER_INSTANCE = "fedcba98-7654-3210-fedc-ba9876543210"
ERRORS = (ValueError, TypeError)


def schedule(**changes):
    return {
        "kind": "daily_local",
        "timezone": "UTC",
        "local_time": "09:30",
        "fold_policy": "earlier",
        "gap_policy": "skip",
        "grace_seconds": 120,
        "missed_run_policy": "latest_within_grace",
        "start_local_date": "2024-01-01",
        "valid_until_utc": "2024-01-03T00:00:00Z",
        **changes,
    }


def authorization(**changes):
    return {
        "label": "  Café  ",
        "objective": "Inspect café.txt\nKeep e\u0301 and é distinct.\0literal",
        "content_scope": "local_only",
        "enabled": True,
        "task_session": {"session_id": " session-α ", "thread_id": "thread-1", "epoch": 2},
        "workspace_pin": {
            "schema_version": "1.0",
            "workspace_id": "b" * 64,
            "root_identity": "c" * 64,
            "settings_revision": 5,
            "source": "owner_saved",
            "shell_backend": "bubblewrap",
            "mode": "direct",
        },
        "schedule": schedule(),
        "delivery": {
            "kind": "in_app",
            "attention": "routine",
            "preview": "status_only",
            "quiet_hours": None,
        },
        "overlap_policy": "one_unresolved_per_authorization",
        "maximum_runs": 3,
        "inference_authorization": "configured_agent_profile",
        **changes,
    }


def change(**changes):
    return {
        "schema_version": "1.0",
        "idempotency_key": "standing:synthetic-1",
        "expected_backend": BACKEND,
        "expected_instance": INSTANCE,
        "expected_revision": 3,
        "responsibility_id": RESPONSIBILITY,
        "authorization": authorization(),
        **changes,
    }


def independent_digest(request, revision):
    """The frozen domain formula, using only stdlib and our literal fixture."""
    payload = {
        "responsibility_id": request["responsibility_id"],
        "revision": revision,
        "request": request,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(b"kinaiya.standing.authorization.v1\0" + encoded).hexdigest()


def receipt(request=None, **changes):
    request = change() if request is None else copy.deepcopy(request)
    revision = request["expected_revision"] + 1
    return {
        "schema_version": "1.0",
        "status": "responsibility_saved",
        "backend_id": request["expected_backend"],
        "service_instance": request["expected_instance"],
        "revision": revision,
        "request": request,
        "authorization_digest": independent_digest(request, revision),
        **changes,
    }


def mutated(value, path, replacement):
    result = copy.deepcopy(value)
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement
    return result


@pytest.mark.parametrize(
    ("model", "raw"),
    [
        (StandingTaskSession, authorization()["task_session"]),
        (
            StandingQuietHours,
            {"timezone": "UTC", "start_local_time": "22:00", "end_local_time": "07:00"},
        ),
        (StandingDelivery, authorization()["delivery"]),
        (DailySchedule, schedule()),
        (StandingAuthorization, authorization()),
        (StandingChange, change()),
        (StandingChangeReceipt, receipt()),
        (DailyOccurrence, {"local_date": "2024-01-01", "due_utc": "2024-01-01T09:30:00Z"}),
    ],
)
def test_every_declared_field_is_required_objects_are_closed_and_frozen(model, raw):
    parsed = model.model_validate(copy.deepcopy(raw))
    for field in raw:
        missing = copy.deepcopy(raw)
        missing.pop(field)
        with pytest.raises(ERRORS):
            model.model_validate(missing)
    with pytest.raises(ERRORS):
        model.model_validate({**copy.deepcopy(raw), "unexpected": "synthetic"})
    with pytest.raises(ERRORS):
        setattr(parsed, next(iter(raw)), raw[next(iter(raw))])


def test_literal_unicode_nul_and_whitespace_are_not_sanitized_or_normalized():
    parsed = StandingChange.model_validate(change())
    assert parsed.authorization.label == "  Café  "
    assert parsed.authorization.task_session.session_id == " session-α "
    assert (
        parsed.authorization.objective == "Inspect café.txt\nKeep e\u0301 and é distinct.\0literal"
    )
    assert "e\u0301" in parsed.authorization.objective and "é" in parsed.authorization.objective
    decoded = json.loads(canonical_bytes(parsed))
    assert decoded["authorization"]["objective"].endswith("\0literal")
    assert standing_json('{"é":1,"e\\u0301":2," key ":3}'.encode()) == {
        "é": 1,
        "e\u0301": 2,
        " key ": 3,
    }


@pytest.mark.parametrize("scope", ["public_synthetic", "private_cloud_authorized", "local_only"])
def test_each_existing_disclosure_scope_is_explicit_and_valid(scope):
    parsed = StandingAuthorization.model_validate(authorization(content_scope=scope))
    assert parsed.content_scope == scope


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("schema_version",), "1.1"),
        (("schema_version",), 1),
        (("idempotency_key",), ""),
        (("idempotency_key",), "k" * 129),
        (("idempotency_key",), "key/unsafe"),
        (("expected_backend",), "A" * 32),
        (("expected_backend",), "a" * 31),
        (("expected_instance",), INSTANCE.upper()),
        (("expected_instance",), "1" * 32),
        (("responsibility_id",), "not-a-uuid"),
        (("expected_revision",), True),
        (("expected_revision",), "3"),
        (("expected_revision",), 3.0),
        (("expected_revision",), -1),
        (("expected_revision",), JS_SAFE),
        (("authorization", "label"), ""),
        (("authorization", "label"), "x" * 201),
        (("authorization", "objective"), ""),
        (("authorization", "objective"), "🚀" * 16001),
        (("authorization", "content_scope"), "cloud"),
        (("authorization", "enabled"), 1),
        (("authorization", "enabled"), "true"),
        (("authorization", "task_session", "session_id"), ""),
        (("authorization", "task_session", "session_id"), "bad\0id"),
        (("authorization", "task_session", "thread_id"), "🚀" * 129),
        (("authorization", "task_session", "epoch"), True),
        (("authorization", "task_session", "epoch"), 0),
        (("authorization", "task_session", "epoch"), JS_SAFE + 1),
        (("authorization", "workspace_pin", "workspace_id"), "g" * 64),
        (("authorization", "workspace_pin", "settings_revision"), True),
        (("authorization", "workspace_pin", "settings_revision"), JS_SAFE),
        (("authorization", "schedule", "kind"), "weekly_local"),
        (("authorization", "schedule", "timezone"), "Not/A_Zone"),
        (("authorization", "schedule", "timezone"), " UTC"),
        (("authorization", "schedule", "fold_policy"), "later"),
        (("authorization", "schedule", "gap_policy"), "shift_forward"),
        (("authorization", "schedule", "grace_seconds"), True),
        (("authorization", "schedule", "grace_seconds"), 0),
        (("authorization", "schedule", "grace_seconds"), 86401),
        (("authorization", "schedule", "missed_run_policy"), "all"),
        (("authorization", "delivery", "kind"), "os_push"),
        (("authorization", "delivery", "attention"), "urgent"),
        (("authorization", "delivery", "preview"), "full_text"),
        (("authorization", "delivery", "quiet_hours"), {}),
        (("authorization", "maximum_runs"), True),
        (("authorization", "maximum_runs"), 0),
        (("authorization", "maximum_runs"), 33),
        (("authorization", "overlap_policy"), "parallel"),
        (("authorization", "inference_authorization"), "free_local_model"),
    ],
)
def test_strict_identity_bounds_and_unimplemented_policy_values_refuse(path, replacement):
    with pytest.raises(ERRORS):
        StandingChange.model_validate(mutated(change(), path, replacement))


@pytest.mark.parametrize("field", ["local_time", "start_local_time", "end_local_time"])
@pytest.mark.parametrize("spelling", ["1:30", "01:3", "24:00", "01:30:00", " 01:30", "01:30 "])
def test_exact_ascii_minute_spelling(field, spelling):
    if field == "local_time":
        model, raw = DailySchedule, schedule(local_time=spelling)
    else:
        model = StandingQuietHours
        raw = {
            "timezone": "UTC",
            "start_local_time": "22:00",
            "end_local_time": "07:00",
            field: spelling,
        }
    with pytest.raises(ERRORS):
        model.model_validate(raw)


@pytest.mark.parametrize(
    "spelling", ["2024-2-01", "2024-02-30", "2023-02-29", "0000-01-01", "2024-01-01 "]
)
def test_real_exact_civil_dates(spelling):
    with pytest.raises(ERRORS):
        DailySchedule.model_validate(schedule(start_local_date=spelling))


@pytest.mark.parametrize(
    "spelling",
    [
        "2024-01-03T00:00:00+00:00",
        "2024-01-03T00:00:00.0Z",
        "2024-01-03T00:00:00z",
        "2024-01-03 00:00:00Z",
        "2024-02-30T00:00:00Z",
        "2024-01-02T23:59:60Z",
    ],
)
def test_real_exact_utc_seconds_without_offset_fraction_or_leap_second(spelling):
    with pytest.raises(ERRORS):
        DailySchedule.model_validate(schedule(valid_until_utc=spelling))


def test_quiet_interval_must_have_two_distinct_boundaries():
    with pytest.raises(ERRORS):
        StandingQuietHours.model_validate(
            {"timezone": "UTC", "start_local_time": "07:00", "end_local_time": "07:00"}
        )


def test_at_most_32_inclusive_civil_dates_is_distinct_from_native_save_horizon():
    accepted = DailySchedule.model_validate(schedule(valid_until_utc="2024-02-01T00:00:00Z"))
    assert accepted.start_local_date == "2024-01-01"
    with pytest.raises(ERRORS):
        DailySchedule.model_validate(schedule(valid_until_utc="2024-02-02T00:00:00Z"))


@pytest.mark.parametrize(
    "raw",
    [
        b"[]",
        b"true",
        b"null",
        b'{"a":1,"a":2}',
        b'{"na\\u006de":1,"name":2}',
        b'{"a":{"x":1,"x":2}}',
        b'{"n":1.0}',
        b'{"n":1e0}',
        b'{"n":NaN}',
        b'{"n":Infinity}',
        b'{"n":9007199254740992}',
        b'{"n":-9007199254740992}',
        b'{"s":"\\ud800"}',
        b'{"\\udfff":1}',
        b"\xff",
        b"\xef\xbb\xbf{}",
        b"{} trailing",
        b"{}{}",
        b'{"x":' * 1000 + b"0",
    ],
)
def test_raw_parser_refuses_ambiguous_unsafe_or_malformed_json(raw):
    with pytest.raises(ERRORS):
        standing_json(raw)


@pytest.mark.parametrize("raw", ["{}", bytearray(b"{}"), memoryview(b"{}")])
def test_raw_parser_requires_bytes(raw):
    with pytest.raises(ERRORS):
        standing_json(raw)


@pytest.mark.parametrize("maximum", [True, 0, -1, 2.0, "2"])
def test_parser_byte_limit_is_a_strict_positive_integer(maximum):
    with pytest.raises(ERRORS):
        standing_json(b"{}", maximum=maximum)


def test_raw_byte_bound_is_inclusive_and_does_not_depend_on_canonical_compaction():
    raw = json.dumps(change(), ensure_ascii=False).encode()
    padded = raw + b" " * (131072 - len(raw))
    parsed = StandingChange.model_validate(standing_json(padded))
    assert parsed.authorization.label == "  Café  "
    with pytest.raises(ERRORS):
        standing_json(padded + b" ")
    assert standing_json(b"{}", maximum=2) == {}
    with pytest.raises(ERRORS):
        standing_json(b"{}", maximum=1)


def test_domain_digest_has_a_frozen_independent_unicode_nul_vector():
    request = change()
    expected = "cea7acd363f1b38d876a06bb31727003fab1cd7d0092c90b90269a13d5a5ad90"
    assert independent_digest(request, 4) == expected
    assert authorization_digest(StandingChange.model_validate(request), 4) == expected
    changed_key = change(idempotency_key="standing:synthetic-2")
    assert changed_key["authorization"] == request["authorization"]
    assert authorization_digest(StandingChange.model_validate(changed_key), 4) != expected


@pytest.mark.parametrize("revision", [True, "4", 4.0, JS_SAFE + 1])
def test_digest_revision_cannot_use_boolean_coercion_or_unsafe_numbers(revision):
    with pytest.raises(ERRORS):
        authorization_digest(StandingChange.model_validate(change()), revision)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "1.1"),
        ("status", "accepted"),
        ("backend_id", "d" * 32),
        ("service_instance", OTHER_INSTANCE),
        ("revision", 0),
        ("revision", 5),
        ("revision", "4"),
        ("authorization_digest", "0" * 64),
        ("authorization_digest", "A" * 64),
    ],
)
def test_receipt_requires_exact_backend_instance_next_revision_and_digest(field, value):
    with pytest.raises(ERRORS):
        StandingChangeReceipt.model_validate(receipt(**{field: value}))


def test_receipt_revision_true_is_not_the_integer_one():
    request = change(expected_revision=0)
    with pytest.raises(ERRORS):
        StandingChangeReceipt.model_validate(receipt(request, revision=True))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("idempotency_key",), "standing:changed"),
        (("responsibility_id",), OTHER_INSTANCE),
        (("authorization", "objective"), "Different literal goal"),
        (("authorization", "workspace_pin", "settings_revision"), 6),
        (("authorization", "delivery", "attention"), "important"),
        (("authorization", "schedule", "local_time"), "09:31"),
    ],
)
def test_embedded_request_cannot_change_under_a_retained_authorization_digest(path, value):
    proof = receipt()
    proof["request"] = mutated(proof["request"], path, value)
    with pytest.raises(ERRORS):
        StandingChangeReceipt.model_validate(proof)


def test_construct_and_copy_bypasses_are_revalidated_at_canonical_and_digest_boundaries():
    valid = StandingChange.model_validate(change())
    constructed_valid = StandingChange.model_construct(**change())
    assert canonical_bytes(constructed_valid) == canonical_bytes(valid)
    bad_outer = valid.model_copy(update={"expected_revision": True})
    bad_nested = valid.model_copy(
        update={"authorization": valid.authorization.model_copy(update={"enabled": 1})}
    )
    bad_pin = valid.authorization.workspace_pin.model_copy(update={"settings_revision": True})
    bad_workspace = valid.model_copy(
        update={"authorization": valid.authorization.model_copy(update={"workspace_pin": bad_pin})}
    )
    bad_constructed = StandingChange.model_construct(**change(expected_revision=True))
    for candidate in [bad_outer, bad_nested, bad_workspace, bad_constructed]:
        with pytest.raises(ERRORS):
            StandingChange.model_validate(candidate)
        with pytest.raises(ERRORS):
            canonical_bytes(candidate)
        with pytest.raises(ERRORS):
            authorization_digest(candidate, 4)
    proof = StandingChangeReceipt.model_validate(receipt())
    with pytest.raises(ERRORS):
        canonical_bytes(proof.model_copy(update={"authorization_digest": "0" * 64}))


def copied_with_surviving_extra(model, path):
    """Use public copy/attribute APIs; do not rely on Pydantic private storage."""
    if not path:
        copied = model.model_copy(update={"unexpected_authority": "synthetic-extra"})
        assert copied.unexpected_authority == "synthetic-extra"
        return copied
    field, *remaining = path
    child = copied_with_surviving_extra(getattr(model, field), remaining)
    return model.model_copy(update={field: child})


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("authorization",),
        ("authorization", "task_session"),
        ("authorization", "workspace_pin"),
        ("authorization", "schedule"),
        ("authorization", "delivery"),
        ("authorization", "delivery", "quiet_hours"),
    ],
)
def test_surviving_unknown_copy_fields_cannot_disappear_at_request_proof_boundaries(path):
    raw = change()
    raw["authorization"]["delivery"]["quiet_hours"] = {
        "timezone": "UTC",
        "start_local_time": "22:00",
        "end_local_time": "07:00",
    }
    forged = copied_with_surviving_extra(StandingChange.model_validate(raw), path)
    with pytest.raises(ERRORS):
        StandingChange.model_validate(forged)
    with pytest.raises(ERRORS):
        canonical_bytes(forged)
    with pytest.raises(ERRORS):
        authorization_digest(forged, 4)


@pytest.mark.parametrize("path", [(), ("request",), ("request", "authorization")])
def test_surviving_unknown_copy_fields_cannot_disappear_from_receipts(path):
    forged = copied_with_surviving_extra(StandingChangeReceipt.model_validate(receipt()), path)
    with pytest.raises(ERRORS):
        StandingChangeReceipt.model_validate(forged)
    with pytest.raises(ERRORS):
        canonical_bytes(forged)


@pytest.mark.parametrize("character", ["\0", "🚀", '"\\\n\r\t'])
def test_maximum_unicode_and_escaped_grants_close_under_request_receipt_and_proof_caps(character):
    raw = change(idempotency_key="k" * 128, expected_revision=JS_SAFE - 1)
    auth = raw["authorization"]
    auth["label"] = "\x01" * 200
    auth["objective"] = (character * 16001)[:16000]
    auth["maximum_runs"] = 32
    auth["task_session"] = {"session_id": "\x02" * 128, "thread_id": "\x03" * 128, "epoch": JS_SAFE}
    auth["workspace_pin"]["settings_revision"] = JS_SAFE - 1
    auth["schedule"] = schedule(grace_seconds=86400, valid_until_utc="2024-02-01T00:00:00Z")
    request = StandingChange.model_validate(raw)
    request_bytes = canonical_bytes(request)
    if character == "\0":
        assert len(request_bytes) > 95000
    assert len(request_bytes) <= 114688
    proof = StandingChangeReceipt.model_validate(receipt(raw))
    receipt_bytes = canonical_bytes(proof)
    assert len(receipt_bytes) <= 131072
    assert proof.revision == JS_SAFE
    envelope = {
        "request": json.loads(request_bytes),
        "receipt": json.loads(receipt_bytes),
        "metadata": "m" * 32768,
    }
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) <= 262144


def test_public_failures_do_not_echo_private_synthetic_input_or_grow_with_it():
    parser_messages, model_messages = [], []
    for marker in ["private-synthetic-A", "private-synthetic-B" * 1000]:
        raw = json.dumps({"a": marker}).encode()[:-1] + b',"a":0}'
        with pytest.raises(ERRORS) as error:
            standing_json(raw)
        assert marker not in str(error.value)
        parser_messages.append(str(error.value))
        valid = StandingChange.model_validate(change())
        forged = valid.model_copy(
            update={
                "authorization": valid.authorization.model_copy(update={"content_scope": marker})
            }
        )
        with pytest.raises(ERRORS) as error:
            canonical_bytes(forged)
        assert marker not in str(error.value)
        model_messages.append(str(error.value))
    assert parser_messages[0] == parser_messages[1]
    assert model_messages[0] == model_messages[1]
