"""Independent pure standing export oracles; metadata is not native authority.

Frozen before consulting the new implementation. The existing packet factories
are public synthetic data; no provider, native admission or inference is used.
"""

import copy
import hashlib
import importlib
import json

import pytest
from test_standing_admission_contract import CREATED, EVENT
from test_standing_admission_contract import proof as existing_proof
from test_standing_execution_contract import GOLDEN_INTENT, MARKER, SAFE, claim, encoded

from packages.contracts.agent_export import (
    AgentExportAdmission,
    AgentExportProviderInfo,
    AgentExportRequest,
)
from packages.contracts.models import ProviderInfo
from packages.contracts.standing_admission import StandingInitialProof
from packages.contracts.standing_execution import StandingExecutionClaim, standing_initial_request

REQUEST_MAX = 262144
ADMISSION_MAX = 73728
GOLDEN_EXPORT_DIGEST = "148d3312bfd575a038e2d5a1aa1f9af405843c59733440eb3d8874d59e1bf55b"


def export_module():
    try:
        return importlib.import_module("packages.contracts.standing_export")
    except ModuleNotFoundError as error:
        if error.name != "packages.contracts.standing_export":
            raise
        pytest.fail("Pure standing export contract capability is absent", pytrace=False)


def stdlib_digest(original):
    return hashlib.sha256(b"kinaiya.standing.export.v1\0" + encoded(original)).hexdigest()


def old_digest(original):
    return hashlib.sha256(encoded(original)).hexdigest()


def initial_vector(*, task_changes=None, metadata=None, prepared=None):
    if task_changes is None and metadata is None and prepared is None:
        return existing_proof()
    intent = copy.deepcopy(GOLDEN_INTENT)
    if task_changes:
        intent["task"].update(copy.deepcopy(task_changes))
    retained = claim(intent) if prepared is None else claim(intent, prepared=prepared)
    initial = standing_initial_request(StandingExecutionClaim.model_validate(retained))
    identity = {
        "event_id": EVENT,
        "sequence": 17,
        "origin": "standing-supervisor",
        "created_at": CREATED,
    }
    identity.update(metadata or {})
    native = {**initial.model_dump(mode="json"), **identity}
    digest = hashlib.sha256(
        b"kinaiya.standing.initial.v1\0"
        + encoded(
            {
                "claim_id": retained["claim_id"],
                "claim_digest": retained["claim_digest"],
                "entry": native,
            }
        )
    ).hexdigest()
    return {
        "schema_version": "1.0",
        "claim": retained,
        **{name: identity[name] for name in ("event_id", "sequence", "created_at")},
        "entry_digest": digest,
    }


def request_vector(*, initial=None, export_changes=None):
    initial = existing_proof() if initial is None else copy.deepcopy(initial)
    task = initial["claim"]["request"]["task"]
    session, pin = task["task_session"], task["model_pin"]
    export = {
        "schema_version": "1.0",
        "task_id": task["task_id"],
        "objective_revision": 1,
        "session_id": session["session_id"],
        "thread_id": session["thread_id"],
        "epoch": session["epoch"],
        "source_event_ids": [],
        "source_revisions": {},
        "context_event_ids": [],
        "content_scope": "public_synthetic",
        "provider": {
            "schema_version": "1.0",
            "provider": pin["provider"],
            "model": pin["model"],
            "local": pin["local"],
            "streaming": False,
        },
        "request_sha256": "12" * 32,
        "purpose": "complete_count",
        "request_nonce": "standing-export:é:count-1",
    }
    export.update(copy.deepcopy(export_changes or {}))
    return {"schema_version": "1.0", "initial": initial, "export": export}


def admission_vector(original=None, *, effective=None, journal_sequence=17):
    original = request_vector() if original is None else copy.deepcopy(original)
    rank = {"public_synthetic": 0, "private_cloud_authorized": 1, "local_only": 2}
    floor = max(
        (
            original["initial"]["claim"]["request"]["task"]["content_scope"],
            original["export"]["content_scope"],
        ),
        key=rank.__getitem__,
    )
    admitted = {
        **copy.deepcopy(original["export"]),
        "binding_sha256": old_digest(original["export"]),
        "effective_scope": floor if effective is None else effective,
        "journal_sequence": journal_sequence,
    }
    return {
        "schema_version": "1.0",
        "request_digest": stdlib_digest(original),
        "admission": admitted,
    }


def refuses(invoke, *arguments):
    with pytest.raises(ValueError) as error:
        invoke(*arguments)
    assert MARKER not in str(error.value)
    assert error.value.__cause__ is None


def strict_request(module, raw=None):
    return module.StandingExportRequest.model_validate(request_vector() if raw is None else raw)


def test_stdlib_full_golden_and_existing_packet_controls_without_the_new_module():
    original = request_vector()
    assert len(original) == 3 and len(original["export"]) == 14
    assert len(original["export"]["provider"]) == 5
    assert stdlib_digest(original) == GOLDEN_EXPORT_DIGEST
    assert (
        StandingInitialProof.model_validate(original["initial"]).model_dump(mode="json")
        == original["initial"]
    )
    old = AgentExportRequest.model_validate(original["export"])
    assert old.binding_digest() == old_digest(original["export"])
    admitted = admission_vector(original)
    assert len(admitted["admission"]) == 17
    assert (
        AgentExportAdmission.model_validate(admitted["admission"])
        .validate_request(old)
        .model_dump(mode="json")
        == admitted["admission"]
    )
    assert encoded(original) == encoded(json.loads(encoded(original)))


@pytest.mark.parametrize(
    "kind_name,factory,maximum",
    [
        ("StandingExportRequest", request_vector, REQUEST_MAX),
        ("StandingExportAdmission", admission_vector, ADMISSION_MAX),
    ],
)
def test_two_complete_immutable_packets_have_exact_raw_bounds_and_detached_canonical_bytes(
    kind_name, factory, maximum
):
    module = export_module()
    kind = getattr(module, kind_name)
    raw = factory()
    parsed = kind.model_validate(raw)
    body = encoded(raw)
    assert parsed.canonical_bytes() == body and len(body) < maximum
    exact = b" " * (maximum - len(body)) + body
    assert len(exact) == maximum
    assert kind.from_json(exact).canonical_bytes() == body
    refuses(kind.from_json, b" " + exact)
    for field in raw:
        missing = copy.deepcopy(raw)
        missing.pop(field)
        refuses(kind.model_validate, missing)
        null = copy.deepcopy(raw)
        null[field] = None
        refuses(kind.model_validate, null)
    refuses(kind.model_validate, {**raw, "unexpected": MARKER})
    for version in (1, True, "1.1", None):
        refuses(kind.model_validate, {**raw, "schema_version": version})
    refuses(setattr, parsed, "schema_version", "1.1")
    raw.clear()
    assert parsed.canonical_bytes() == body
    if kind_name == "StandingExportAdmission":
        original = strict_request(module)
        result = parsed.validate_request(original)
        assert type(result) is kind and result.canonical_bytes() == body


@pytest.mark.parametrize(
    "kind_name,factory,nested_key",
    [
        ("StandingExportRequest", request_vector, "export"),
        ("StandingExportAdmission", admission_vector, "admission"),
    ],
)
def test_raw_old_nested_fields_and_provider_defaults_are_all_explicit(
    kind_name, factory, nested_key
):
    kind = getattr(export_module(), kind_name)
    vector = factory()
    for location in ((nested_key,), (nested_key, "provider")):
        container = vector
        for part in location:
            container = container[part]
        for field in container:
            raw = copy.deepcopy(vector)
            parent = raw
            for part in location:
                parent = parent[part]
            parent.pop(field)
            refuses(kind.model_validate, raw)
            refuses(kind.from_json, encoded(raw))


@pytest.mark.parametrize(
    "change",
    [
        "task_id",
        "session_id",
        "thread_id",
        "epoch",
        "objective_revision",
        "sources",
        "source_revisions",
        "context_event_ids",
        "provider",
        "model",
        "local",
    ],
)
def test_initial_identity_model_and_empty_source_context_family_are_exact(change):
    kind = export_module().StandingExportRequest
    raw = request_vector()
    body = raw["export"]
    if change in {"task_id", "session_id", "thread_id"}:
        body[change] = "different-" + change
    elif change in {"epoch", "objective_revision"}:
        body[change] += 1
    elif change == "sources":
        body["source_event_ids"], body["source_revisions"] = (
            ["synthetic-source"],
            {"synthetic-source": 1},
        )
    elif change == "source_revisions":
        body["source_revisions"] = {"synthetic-source": 1}
    elif change == "context_event_ids":
        body[change] = ["synthetic-context"]
    elif change in {"provider", "model"}:
        body["provider"][change] = "different-" + change
    else:
        body["provider"]["local"] = not body["provider"]["local"]
    refuses(kind.model_validate, raw)


def test_streaming_and_count_generation_are_explicit_request_data_not_model_identity():
    module = export_module()
    hashes = set()
    for streaming in (False, True):
        for purpose in ("complete_count", "complete_generation"):
            raw = request_vector(export_changes={"purpose": purpose})
            raw["export"]["provider"]["streaming"] = streaming
            parsed = strict_request(module, raw)
            assert parsed.export.provider.streaming is streaming
            assert parsed.export.provider.model == parsed.initial.claim.request.task.model_pin.model
            assert module.standing_export_digest(parsed) == stdlib_digest(raw)
            hashes.add(module.standing_export_digest(parsed))
    assert len(hashes) == 4


def test_genuine_typed_old_defaults_are_existing_values_but_actual_missing_graph_is_not():
    module = export_module()
    raw = request_vector()
    old_raw = copy.deepcopy(raw["export"])
    old_raw.pop("schema_version")
    old_raw.pop("context_event_ids")
    old_raw["provider"].pop("schema_version")
    old_raw["provider"].pop("streaming")
    old = AgentExportRequest.model_validate(old_raw)
    assert {"schema_version", "context_event_ids"}.isdisjoint(old.__pydantic_fields_set__)
    assert {"schema_version", "streaming"}.isdisjoint(old.provider.__pydantic_fields_set__)
    assert old.model_dump(mode="json") == raw["export"]
    original = module.StandingExportRequest.model_validate(
        {
            "schema_version": "1.0",
            "initial": StandingInitialProof.model_validate(raw["initial"]),
            "export": old,
        }
    )
    assert original.canonical_bytes() == encoded(raw)
    for target, field in (
        ("export", "schema_version"),
        ("export", "context_event_ids"),
        ("provider", "streaming"),
    ):
        bad = old.model_copy(deep=True)
        (bad.__dict__ if target == "export" else bad.provider.__dict__).pop(field)
        refuses(
            module.StandingExportRequest.model_validate,
            {"schema_version": "1.0", "initial": original.initial, "export": bad},
        )


def test_actual_typed_old_graph_forgeries_are_rejected_before_any_lossy_dump():
    module = export_module()
    original = strict_request(module)
    admitted = module.StandingExportAdmission.model_validate(admission_vector())
    old = original.export

    class ExportSubclass(AgentExportRequest):
        pass

    class ProviderSubclass(AgentExportProviderInfo):
        pass

    wrongs = [
        old.model_copy(update={"unknown": MARKER}),
        old.model_copy(update={"epoch": True}),
        old.model_copy(update={"source_event_ids": ()}),
        old.model_copy(update={"provider": old.provider.model_copy(update={"unknown": MARKER})}),
        old.model_copy(update={"provider": old.provider.model_copy(update={"streaming": 1})}),
        old.model_copy(update={"provider": old.provider.model_dump(mode="python")}),
        old.model_copy(
            update={"provider": ProviderInfo.model_validate(old.provider.model_dump(mode="python"))}
        ),
        old.model_copy(
            update={
                "provider": ProviderSubclass.model_validate(old.provider.model_dump(mode="python"))
            }
        ),
        ExportSubclass.model_validate(old.model_dump(mode="python")),
        AgentExportAdmission.model_validate(admission_vector()["admission"]),
    ]
    field_set = old.model_copy(deep=True)
    field_set.__pydantic_fields_set__.add("unknown")
    wrongs.append(field_set)
    extras = old.model_copy(deep=True)
    object.__setattr__(extras, "__pydantic_extra__", {"unknown": MARKER})
    wrongs.append(extras)
    for bad in wrongs:
        supplied = {"schema_version": "1.0", "initial": original.initial, "export": bad}
        refuses(module.StandingExportRequest.model_validate, supplied)
        copied = original.model_copy(update={"export": bad})
        refuses(copied.canonical_bytes)
        refuses(module.standing_export_digest, copied)
        refuses(admitted.validate_request, copied)
    for copied in (
        original.model_copy(update={"unknown": MARKER}),
        original.model_copy(
            update={"initial": original.initial.model_copy(update={"sequence": True})}
        ),
        original.model_copy(
            update={
                "initial": original.initial.model_copy(
                    update={"claim": original.initial.claim.model_copy(update={"unknown": MARKER})}
                )
            }
        ),
    ):
        refuses(copied.canonical_bytes)
        refuses(module.standing_export_digest, copied)
        refuses(admitted.validate_request, copied)
    assert original.canonical_bytes() == encoded(request_vector())


@pytest.mark.parametrize(
    "metadata",
    [
        {"event_id": "00000000-0000-0000-0000-000000000778"},
        {"sequence": 18},
        {"created_at": "2026-11-01T08:30:02Z"},
        {"prepared": "2026-11-01T08:30:00Z"},
    ],
)
def test_admission_digest_binds_entire_initial_proof_even_when_old_export_is_identical(metadata):
    module = export_module()
    original = strict_request(module)
    if "prepared" in metadata:
        initial = initial_vector(prepared=metadata["prepared"])
    else:
        initial = initial_vector(metadata=metadata)
    alternate_raw = request_vector(initial=initial)
    alternate = strict_request(module, alternate_raw)
    assert alternate.export == original.export
    assert AgentExportAdmission.model_validate(admission_vector()["admission"]).validate_request(
        alternate.export
    )
    assert module.standing_export_digest(alternate) == stdlib_digest(alternate_raw)
    assert module.standing_export_digest(alternate) != module.standing_export_digest(original)
    admitted = module.StandingExportAdmission.model_validate(admission_vector())
    refuses(admitted.validate_request, alternate)


def test_full_original_hash_is_canonical_order_sensitive_to_values_and_never_unicode_normalized():
    module = export_module()
    raw = request_vector()
    original = strict_request(module, raw)
    assert module.standing_export_digest(original) == GOLDEN_EXPORT_DIGEST
    reordered = json.loads(json.dumps(raw, ensure_ascii=True))
    reordered = dict(reversed(list(reordered.items())))
    assert module.standing_export_digest(strict_request(module, reordered)) == GOLDEN_EXPORT_DIGEST
    hashes = set()
    for field, values in (
        ("request_nonce", ("café", "cafe\u0301")),
        ("purpose", ("complete_count", "complete_generation")),
        ("request_sha256", ("12" * 32, "34" * 32)),
    ):
        for value in values:
            changed = copy.deepcopy(raw)
            changed["export"][field] = value
            parsed = strict_request(module, changed)
            assert module.standing_export_digest(parsed) == stdlib_digest(changed)
            hashes.add(module.standing_export_digest(parsed))
    assert len(hashes) == 5  # Purpose/count and the unchanged request hash share the original.
    raw["export"]["provider"]["model"] = "mutated caller copy"
    assert original.canonical_bytes() == encoded(request_vector())
    for bad in (None, request_vector(), object()):
        refuses(module.standing_export_digest, bad)


@pytest.mark.parametrize(
    "captured,requested,expected",
    [
        ("public_synthetic", "public_synthetic", "public_synthetic"),
        ("public_synthetic", "private_cloud_authorized", "private_cloud_authorized"),
        ("public_synthetic", "local_only", "local_only"),
        ("private_cloud_authorized", "public_synthetic", "private_cloud_authorized"),
        ("private_cloud_authorized", "private_cloud_authorized", "private_cloud_authorized"),
        ("private_cloud_authorized", "local_only", "local_only"),
        ("local_only", "public_synthetic", "local_only"),
        ("local_only", "private_cloud_authorized", "local_only"),
        ("local_only", "local_only", "local_only"),
    ],
)
def test_effective_disclosure_floor_folds_captured_and_requested_scopes(
    captured, requested, expected
):
    module = export_module()
    raw = request_vector(
        initial=initial_vector(task_changes={"content_scope": captured}),
        export_changes={"content_scope": requested},
    )
    original = strict_request(module, raw)
    admitted = module.StandingExportAdmission.model_validate(
        admission_vector(raw, effective=expected)
    )
    assert admitted.validate_request(original).admission.effective_scope == expected
    stricter = module.StandingExportAdmission.model_validate(
        admission_vector(raw, effective="local_only")
    )
    assert stricter.validate_request(original).admission.effective_scope == "local_only"
    if expected != "public_synthetic":
        refuses(
            lambda: module.StandingExportAdmission.model_validate(
                admission_vector(raw, effective="public_synthetic")
            ).validate_request(original)
        )


@pytest.mark.parametrize(
    "provider,model,local,scope,allowed",
    [
        ("synthetic-native", "local-fixture", True, "local_only", True),
        ("cloud-fixture", "ordinary-model", False, "public_synthetic", True),
        ("cloud-fixture", "ordinary-model", False, "private_cloud_authorized", True),
        ("cloud-fixture", "ordinary-model", False, "local_only", False),
        ("meta", "ordinary-model", False, "private_cloud_authorized", False),
        ("meta", "ordinary-model", False, "public_synthetic", True),
        ("cloud-fixture", "Contributor-actual", False, "private_cloud_authorized", False),
    ],
)
def test_response_checks_actual_provider_permission_at_the_captured_floor(
    provider, model, local, scope, allowed
):
    module = export_module()
    pin = copy.deepcopy(GOLDEN_INTENT["task"]["model_pin"])
    pin.update(provider=provider, model=model, local=local)
    raw = request_vector(
        initial=initial_vector(task_changes={"content_scope": scope, "model_pin": pin})
    )
    original = strict_request(module, raw)  # Proposal itself does not grant export.
    if allowed:
        response = module.StandingExportAdmission.model_validate(
            admission_vector(raw, effective=scope)
        )
        assert response.validate_request(original).admission.provider.local is local
    else:
        refuses(
            lambda: module.StandingExportAdmission.model_validate(
                admission_vector(raw, effective=scope)
            ).validate_request(original)
        )


def test_admission_echo_and_full_old_binding_are_rechecked_without_accepting_partial_success():
    module = export_module()
    original = strict_request(module)
    valid = admission_vector()
    changes = {
        "task_id": "other",
        "session_id": "other",
        "thread_id": "other",
        "epoch": 4,
        "objective_revision": 2,
        "request_nonce": "other-nonce",
        "request_sha256": "34" * 32,
        "purpose": "complete_generation",
        "binding_sha256": "45" * 32,
        "provider": {**valid["admission"]["provider"], "streaming": True},
    }
    for field, value in changes.items():
        raw = copy.deepcopy(valid)
        raw["admission"][field] = value
        refuses(
            lambda raw=raw: module.StandingExportAdmission.model_validate(raw).validate_request(
                original
            )
        )
    for digest in (None, "A" * 64, "a" * 63, True, "0" * 64):
        raw = {**copy.deepcopy(valid), "request_digest": digest}
        refuses(
            lambda raw=raw: module.StandingExportAdmission.model_validate(raw).validate_request(
                original
            )
        )
    for sequence in (0, SAFE):
        accepted = module.StandingExportAdmission.model_validate(
            admission_vector(journal_sequence=sequence)
        )
        assert accepted.validate_request(original).admission.journal_sequence == sequence
    for sequence in (True, -1, SAFE + 1, "17", 17.0):
        raw = admission_vector(journal_sequence=sequence)
        refuses(module.StandingExportAdmission.model_validate, raw)


def test_actual_admission_graph_is_closed_and_revalidated_on_response_binding():
    module = export_module()
    original = strict_request(module)
    response = module.StandingExportAdmission.model_validate(admission_vector())
    old = response.admission

    class AdmissionSubclass(AgentExportAdmission):
        pass

    for bad in (
        old.model_copy(update={"unknown": MARKER}),
        old.model_copy(update={"journal_sequence": True}),
        old.model_copy(update={"provider": old.provider.model_copy(update={"local": 1})}),
        old.model_copy(update={"provider": old.provider.model_copy(update={"unknown": MARKER})}),
        old.model_copy(update={"provider": old.provider.model_dump(mode="python")}),
        old.model_copy(
            update={"provider": ProviderInfo.model_validate(old.provider.model_dump(mode="python"))}
        ),
    ):
        copied = response.model_copy(update={"admission": bad})
        refuses(copied.canonical_bytes)
        refuses(copied.validate_request, original)
    bad_fields = old.model_copy(deep=True)
    bad_fields.__pydantic_fields_set__.add("unknown")
    for bad in (
        bad_fields,
        original.export,
        AdmissionSubclass.model_validate(old.model_dump(mode="python")),
    ):
        copied = response.model_copy(update={"admission": bad})
        refuses(copied.canonical_bytes)
        refuses(copied.validate_request, original)
    copied = response.model_copy(update={"unknown": MARKER})
    refuses(copied.canonical_bytes)
    refuses(copied.validate_request, original)
    assert response.canonical_bytes() == encoded(admission_vector())


@pytest.mark.parametrize(
    "kind_name,factory,maximum",
    [
        ("StandingExportRequest", request_vector, REQUEST_MAX),
        ("StandingExportAdmission", admission_vector, ADMISSION_MAX),
    ],
)
def test_strict_raw_json_and_oversized_constructed_canonical_graphs_fail_without_echo(
    kind_name, factory, maximum
):
    kind = getattr(export_module(), kind_name)
    raw = factory()
    body = encoded(raw)
    for bad in (
        b'{"schema_version":"1.0",' + body[1:],
        body.replace(b'"epoch":3', b'"epoch":3.0', 1),
        body.replace(b'"epoch":3', b'"epoch":9007199254740992', 1),
        body.replace(b'"epoch":3', b'"epoch":NaN', 1),
        b"\xff" + body,
        b"\xef\xbb\xbf" + body,
        body + b" {}",
        b"null",
        b"[]",
        b'{"unknown":"\\ud800"}',
        b'{"\\udfff":"' + MARKER.encode() + b'"}',
        b"[" * 20 + b"0" + b"]" * 20,
        b'{"unknown":[' + b",".join([b"null"] * 2049) + b"]}",
    ):
        assert bad != body
        refuses(kind.from_json, bad)
    for bad in (body.decode(), bytearray(body), memoryview(body)):
        refuses(kind.from_json, bad)
    parsed = kind.model_validate(raw)
    if kind_name == "StandingExportRequest":
        bad = parsed.model_copy(
            update={
                "export": parsed.export.model_copy(
                    update={"request_nonce": MARKER + "🧭" * maximum}
                )
            }
        )
    else:
        bad = parsed.model_copy(
            update={
                "admission": parsed.admission.model_copy(
                    update={"request_nonce": MARKER + "🧭" * maximum}
                )
            }
        )
    refuses(bad.canonical_bytes)
    assert len(parsed.canonical_bytes()) < maximum


def test_valid_unicode_is_literal_and_unsafe_strings_counters_and_type_coercions_refuse():
    module = export_module()
    raw = request_vector(export_changes={"request_nonce": "literal-𝄞-e\u0301"})
    parsed = module.StandingExportRequest.from_json(json.dumps(raw, ensure_ascii=True).encode())
    assert parsed.export.request_nonce == "literal-𝄞-e\u0301"
    assert parsed.canonical_bytes() == encoded(raw)
    for path, value in (
        ("request_nonce", "bad\0nonce"),
        ("request_nonce", "\ud800"),
        ("epoch", True),
        ("epoch", "3"),
        ("objective_revision", 1.0),
    ):
        bad = request_vector(export_changes={path: value})
        refuses(module.StandingExportRequest.model_validate, bad)
    for field, value in (("local", 1), ("streaming", "false"), ("model", "\udfff")):
        bad = request_vector()
        bad["export"]["provider"][field] = value
        refuses(module.StandingExportRequest.model_validate, bad)
