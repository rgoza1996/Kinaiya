from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from packages.contracts.models import (
    CommandRequest,
    ConverseRequest,
    SteerRequest,
    Task,
    TaskRequest,
)
from packages.providers.disclosure import combine_scopes, entry_allowed, scope_allowed


@pytest.mark.parametrize("scope", [None, [], {}, "unknown", 1])
def test_malformed_scope_never_authorizes_inference(scope):
    for info in (
        SimpleNamespace(local=True, provider="local", model="local"),
        SimpleNamespace(local=False, provider="meta", model="muse-spark-1.3-contributor"),
        SimpleNamespace(local=False, provider="compatible", model="cloud"),
    ):
        assert not scope_allowed(info, scope)


def test_legacy_entries_stay_local_and_positive_grants_differ_by_provider():
    local = SimpleNamespace(local=True, provider="local", model="local")
    contributor = SimpleNamespace(local=False, provider="meta", model="muse-spark-1.3-contributor")
    ordinary_cloud = SimpleNamespace(local=False, provider="compatible", model="cloud")
    legacy = SimpleNamespace(kind="conversation.user", payload={"text": "synthetic sentinel"})
    assert entry_allowed(local, legacy)
    assert not entry_allowed(contributor, legacy)
    assert not entry_allowed(ordinary_cloud, legacy)
    private = SimpleNamespace(payload={"content_scope": "private_cloud_authorized"})
    assert entry_allowed(ordinary_cloud, private)
    assert not entry_allowed(contributor, private)
    assert entry_allowed(
        contributor, SimpleNamespace(payload={"content_scope": "public_synthetic"})
    )


@pytest.mark.parametrize(
    ("scopes", "expected"),
    [
        ((), "local_only"),
        (("public_synthetic",), "public_synthetic"),
        (("public_synthetic", "private_cloud_authorized"), "private_cloud_authorized"),
        (("public_synthetic", "local_only", "public_synthetic"), "local_only"),
        (("public_synthetic", None), "local_only"),
        (("public_synthetic", []), "local_only"),
    ],
)
def test_derived_scope_cannot_promote_an_input(scopes, expected):
    assert combine_scopes(*scopes) == expected


@pytest.mark.parametrize(
    ("model", "body"),
    [
        (TaskRequest, {"session_id": "s", "thread_id": "t", "epoch": 1, "objective": "fixture"}),
        (Task, {"session_id": "s", "thread_id": "t", "epoch": 1, "objective": "fixture"}),
        (ConverseRequest, {"session_id": "s", "thread_id": "t", "epoch": 1, "text": "fixture"}),
        (CommandRequest, {"session_id": "s", "thread_id": "t", "epoch": 1, "input": "/status"}),
        (SteerRequest, {"instruction": "fixture", "expected_objective_revision": 1}),
    ],
)
def test_typed_input_and_recovered_task_scopes_default_local(model, body):
    body = {**body, "idempotency_key": "scope-fixture"}
    assert model(**body).content_scope == "local_only"
    assert model(**body, content_scope="public_synthetic").content_scope == "public_synthetic"
    with pytest.raises(ValidationError):
        model(**body, content_scope="public")
