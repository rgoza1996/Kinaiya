"""Converse typed-disclosure enforcement (issue 95). Synthetic fixtures only.

Covers the Converse-owned slice: explicit request scope checked against the
actual selected provider before any provider call, sticky retained-history
floors, derived reply floors from actually consumed history + current input +
personality, custom-persona locality, and window/outcome floors. No network,
no models, no Meta; spy providers stand in for local/Contributor/ordinary-cloud.
"""

from types import SimpleNamespace

import pytest

from packages.contracts.models import JournalEntry, ProviderInfo
from packages.providers.disclosure import combine_scopes, entry_allowed, scope_allowed
from services.converse.history import typed_history, typed_history_scoped
from services.converse.runtime import ConverseRuntime

SOURCE = SimpleNamespace(session_id="s", thread_id="t", epoch=1)
LOCAL = ProviderInfo(provider="synthetic", model="local-fixture", local=True)
CONTRIBUTOR = ProviderInfo(provider="meta", model="synthetic-contributor", local=False)
ORDINARY = ProviderInfo(provider="compatible", model="synthetic-cloud", local=False)


def entry(
    sequence,
    *,
    kind="conversation.user",
    text=None,
    scope="public_synthetic",
    origin=None,
    **payload,
):
    base = {"text": text or f"synthetic {sequence}", "revision": 1}
    if scope is not None:
        base["content_scope"] = scope
    base.update(payload)
    return JournalEntry(
        session_id=SOURCE.session_id,
        thread_id=SOURCE.thread_id,
        epoch=SOURCE.epoch,
        sequence=sequence,
        event_id=f"ev-{sequence}",
        kind=kind,
        origin=origin or ("converse" if kind != "conversation.user" else "authenticated-user"),
        payload=base,
        idempotency_key=f"k-{sequence}",
    )


def revision(sequence, target, **payload):
    return entry(
        sequence,
        kind="conversation.revision",
        target_event_id=target.event_id,
        revision=2,
        **payload,
    )


def _init(provider, kwargs):
    return ConverseRuntime(SimpleNamespace(), provider, **kwargs)


def test_missing_current_scope_never_calls_contributor():
    rt = _init(SimpleNamespace(info=CONTRIBUTOR), {})
    req = SimpleNamespace(content_scope="local_only", text="synthetic private bytes")
    with pytest.raises(ValueError):
        rt._check_typed_disclosure(req)


@pytest.mark.parametrize("scope", ["local_only", "private_cloud_authorized"])
def test_nonpublic_current_scope_never_calls_contributor(scope):
    rt = _init(SimpleNamespace(info=CONTRIBUTOR), {})
    with pytest.raises(ValueError):
        rt._check_typed_disclosure(SimpleNamespace(content_scope=scope, text="synthetic"))


def test_explicit_public_current_scope_reaches_contributor_gate():
    rt = _init(SimpleNamespace(info=CONTRIBUTOR), {})
    assert (
        rt._check_typed_disclosure(
            SimpleNamespace(content_scope="public_synthetic", text="synthetic")
        )
        == "public_synthetic"
    )


def test_ordinary_cloud_accepts_explicit_private_but_not_local_only():
    rt = _init(SimpleNamespace(info=ORDINARY), {})
    assert (
        rt._check_typed_disclosure(
            SimpleNamespace(content_scope="private_cloud_authorized", text="s")
        )
        == "private_cloud_authorized"
    )
    with pytest.raises(ValueError):
        rt._check_typed_disclosure(SimpleNamespace(content_scope="local_only", text="s"))


def test_local_provider_accepts_all_valid_scopes():
    rt = _init(SimpleNamespace(info=LOCAL), {})
    for scope in ("local_only", "public_synthetic", "private_cloud_authorized"):
        assert rt._check_typed_disclosure(SimpleNamespace(content_scope=scope, text="s")) == scope


def test_denied_text_not_echoed_in_error():
    rt = _init(SimpleNamespace(info=CONTRIBUTOR), {})
    secret = "synthetic-secret-bytes-123"
    with pytest.raises(ValueError) as exc:
        rt._check_typed_disclosure(SimpleNamespace(content_scope="local_only", text=secret))
    assert secret not in str(exc.value)


def test_public_turn_excludes_legacy_and_private_history():
    legacy = entry(1, text="legacy bytes", scope=None)
    private = entry(2, text="private bytes", scope="local_only")
    public = entry(3, text="public bytes", scope="public_synthetic")
    messages, consumed = typed_history_scoped([legacy, private, public], SOURCE, CONTRIBUTOR, 12)
    assert messages == [{"role": "user", "content": "public bytes"}]
    assert consumed == ["public_synthetic"]
    assert typed_history([legacy, private, public], SOURCE, CONTRIBUTOR, 12) == messages


def test_original_and_correction_floors_sticky():
    original = entry(1, text="base", scope="local_only")
    fix = revision(2, original, text="relabeled public", scope="public_synthetic")
    assert typed_history([original, fix], SOURCE, CONTRIBUTOR, 12) == []
    assert typed_history([original, fix], SOURCE, LOCAL, 12) == [
        {"role": "user", "content": "relabeled public"}
    ]
    # Missing correction scope is also local-only and cannot restore.
    original2 = entry(1, text="base", scope="public_synthetic")
    fix2 = revision(2, original2, text="relabeled", scope=None)
    assert typed_history([original2, fix2], SOURCE, CONTRIBUTOR, 12) == []
    derived = combine_scopes("local_only", "public_synthetic")
    assert derived == "local_only"


def test_local_reply_consuming_private_history_stays_local():
    rt = _init(SimpleNamespace(info=LOCAL), {})
    consumed = ["private_cloud_authorized", "public_synthetic"]
    derived = rt._derived_reply_scope(consumed, "public_synthetic")
    assert derived == "private_cloud_authorized"
    reply = entry(9, kind="conversation.converse", text="derived reply", scope=derived)
    assert typed_history([reply], SOURCE, CONTRIBUTOR, 12) == []
    assert typed_history([reply], SOURCE, ORDINARY, 12) == [
        {"role": "assistant", "content": "derived reply"}
    ]


def test_custom_private_persona_refused_before_call_even_for_public_input():
    rt = _init(SimpleNamespace(info=CONTRIBUTOR), {"personality": "custom persona bytes"})
    assert rt.personality_scope == "local_only"
    with pytest.raises(ValueError) as exc:
        rt._check_typed_disclosure(SimpleNamespace(content_scope="public_synthetic", text="hello"))
    assert "custom persona bytes" not in str(exc.value)
    local_rt = _init(SimpleNamespace(info=LOCAL), {"personality": "custom persona bytes"})
    assert (
        local_rt._check_typed_disclosure(
            SimpleNamespace(content_scope="public_synthetic", text="hello")
        )
        == "public_synthetic"
    )


def test_builtin_persona_is_public_but_derived_floor_cannot_upgrade():
    rt = _init(SimpleNamespace(info=CONTRIBUTOR), {})
    assert rt.personality_scope == "public_synthetic"
    assert rt._derived_reply_scope(["local_only"], "public_synthetic") == "local_only"
    assert rt._derived_reply_scope(["public_synthetic"], "public_synthetic") == "public_synthetic"


def test_window_and_outcome_floors_do_not_upgrade():
    assert combine_scopes("public_synthetic", "local_only") == "local_only"
    task_scope = "private_cloud_authorized"
    combined = combine_scopes(task_scope, "public_synthetic", "local_only")
    assert combined == "local_only"
    assert combine_scopes(task_scope, "public_synthetic") == "private_cloud_authorized"
    assert scope_allowed(ORDINARY, "private_cloud_authorized")
    assert not scope_allowed(CONTRIBUTOR, "private_cloud_authorized")
    assert entry_allowed(LOCAL, SimpleNamespace(payload={"text": "legacy"}))
    assert not entry_allowed(CONTRIBUTOR, SimpleNamespace(payload={"text": "legacy"}))
