"""Source-aware native identity regressions; no credentials or provider calls."""

import re
from dataclasses import FrozenInstanceError

import pytest

from services.common.auth import Auth


def _tokens():
    return {"owner": "synthetic-owner-credential-" + "a" * 24, "agent": "b" * 32}


def test_native_identity_is_stable_private_and_not_a_public_credential():
    auth = Auth(_tokens())
    identity = auth.media_identity()
    assert auth.media_identity() is identity
    assert re.fullmatch("[0-9a-f]{32}", identity.instance_id)
    assert type(identity.credential_fingerprint) is bytes
    assert len(identity.credential_fingerprint) == 32
    assert identity.credential_fingerprint.hex() not in repr(identity)
    assert all(token not in repr(identity) for token in _tokens().values())
    with pytest.raises(FrozenInstanceError):
        identity.instance_id = "0" * 32


def test_equal_credentials_in_distinct_auth_incarnations_do_not_share_identity():
    first, second = Auth(_tokens()), Auth(_tokens())
    assert first.media_identity().instance_id != second.media_identity().instance_id
    assert (
        first.media_identity().credential_fingerprint
        == second.media_identity().credential_fingerprint
    )


def test_external_configuration_mutation_does_not_replace_native_authentication():
    tokens = _tokens()
    original = tokens["owner"]
    auth = Auth(tokens)
    identity = auth.media_identity()
    tokens["owner"] = "c" * 32
    assert auth.authenticate(original) == "owner"
    assert auth.media_identity() is identity


def test_detected_token_replacement_permanently_fences_only_media_identity():
    auth = Auth(_tokens())
    old = auth.tokens["owner"]
    identity = auth.media_identity()
    auth.tokens["owner"] = "d" * 32
    assert auth.authenticate("d" * 32) == "owner"
    with pytest.raises(ValueError, match="^Native authentication identity is unavailable$"):
        auth.media_identity()
    auth.tokens["owner"] = old
    assert auth.authenticate(old) == "owner"
    with pytest.raises(ValueError, match="^Native authentication identity is unavailable$"):
        auth.media_identity()
    assert Auth(_tokens()).media_identity().instance_id != identity.instance_id


@pytest.mark.parametrize("field", ["token", "role"])
@pytest.mark.parametrize("cached", [False, True])
def test_current_nonbuiltin_comparator_cannot_preserve_native_identity(field, cached):
    auth = Auth(_tokens())
    if cached:
        auth.media_identity()
    compared = []

    class Impostor:
        def __eq__(self, other):
            compared.append(other)
            return True

        def __hash__(self):
            return hash("owner")

    if field == "token":
        auth.tokens["owner"] = Impostor()
    else:
        auth.tokens = {Impostor(): _tokens()["owner"]}
    with pytest.raises(ValueError, match="^Native authentication identity is unavailable$"):
        auth.media_identity()
    assert compared == []


def test_current_string_subclass_is_rejected_before_its_raising_comparator():
    auth = Auth(_tokens())
    auth.media_identity()

    class Impostor(str):
        def __eq__(self, other):
            raise AssertionError("A replacement credential comparator was invoked")

    auth.tokens["owner"] = Impostor(_tokens()["owner"])
    with pytest.raises(ValueError, match="^Native authentication identity is unavailable$"):
        auth.media_identity()
