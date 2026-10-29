import pytest

from packages.providers.prompt_budget import (
    MAX_SAFE_COUNT,
    PromptCountUnavailable,
    PromptTokenCount,
    binding_digest,
    request_digest,
)

BINDING = {
    "provider": "managed-gguf",
    "model": "synthetic",
    "profile": "synthetic-profile",
    "manifest_sha256": "a" * 64,
    "context_tokens": 2048,
    "max_new_tokens": 128,
    "resources_url": "http://127.0.0.1:9999",
}
BODY = {
    "model": "synthetic-alias",
    "messages": [{"role": "system", "content": "Public 🌙 personality"}],
    "max_tokens": 128,
    "stream": True,
}


def count(tokens=1920):
    return PromptTokenCount(tokens, 2048, 128, request_digest(BODY), binding_digest(BINDING))


def test_complete_count_reserves_output_and_exact_context_boundary():
    assert count().fits
    assert not count(1921).fits
    assert not count(MAX_SAFE_COUNT).fits
    assert PromptTokenCount.from_wire(count().to_wire(), body=BODY, binding=BINDING) == count()


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, "12", None, MAX_SAFE_COUNT + 1])
def test_count_refuses_nonpositive_or_unsafe_types(value):
    with pytest.raises(PromptCountUnavailable):
        count(value)


@pytest.mark.parametrize(
    "change",
    [
        {"version": True},
        {"version": 2},
        {"extra": 1},
        {"input_tokens": True},
        {"context_tokens": True},
        {"context_tokens": 4096},
        {"reserved_output_tokens": 64},
        {"request_sha256": "b" * 64},
        {"binding_sha256": "b" * 64},
        {"binding_sha256": "A" * 64},
    ],
)
def test_wire_identity_and_limits_are_exact(change):
    with pytest.raises(PromptCountUnavailable):
        PromptTokenCount.from_wire({**count().to_wire(), **change}, body=BODY, binding=BINDING)


def test_request_pin_includes_roles_order_unicode_and_generation_options():
    original = request_digest(BODY)
    for changed in [
        {**BODY, "messages": [{"role": "user", "content": BODY["messages"][0]["content"]}]},
        {**BODY, "messages": [{"role": "system", "content": "Public 🌙 personality!"}]},
        {**BODY, "stream": False},
        {**BODY, "model": "other-alias"},
        {**BODY, "max_tokens": 64},
    ]:
        assert request_digest(changed) != original
    assert request_digest(dict(reversed(list(BODY.items())))) == original
    assert binding_digest({**BINDING, "resources_url": "http://localhost:1234"}) == binding_digest(
        BINDING
    )
    assert binding_digest({**BINDING, "manifest_sha256": "b" * 64}) != binding_digest(BINDING)
