from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from services.common.auth import Auth
from services.converse.api import create_app


@pytest.mark.parametrize(
    "scope", [None, "local_only", "public_synthetic", "private_cloud_authorized"]
)
def test_local_transcription_retains_owner_scope_even_if_backend_reports_public(scope):
    class LocalSpeech:
        calls = 0

        async def transcribe(self, _data, _name):
            self.calls += 1
            return {"text": "public synthetic test audio", "content_scope": "public_synthetic"}

    speech = LocalSpeech()
    token = "scope-transport-owner-credential"
    app = create_app(SimpleNamespace(enabled=True), Auth({"owner": token}), speech)
    with TestClient(app) as client:
        response = client.post(
            "/v1/converse/transcribe",
            files={"file": ("fixture.wav", b"synthetic audio fixture", "audio/wav")},
            data={"content_scope": scope} if scope is not None else {},
            headers={"Authorization": "Bearer " + token},
        )
    assert response.status_code == 200
    assert response.json()["content_scope"] == (scope or "local_only")
    assert speech.calls == 1


def test_invalid_transcription_scope_is_rejected_before_local_provider():
    class LocalSpeech:
        async def transcribe(self, _data, _name):
            raise AssertionError("Invalid scope reached speech provider")

    token = "scope-transport-owner-credential"
    app = create_app(SimpleNamespace(enabled=True), Auth({"owner": token}), LocalSpeech())
    with TestClient(app) as client:
        response = client.post(
            "/v1/converse/transcribe",
            files={"file": ("fixture.wav", b"synthetic audio fixture", "audio/wav")},
            data={"content_scope": "public"},
            headers={"Authorization": "Bearer " + token},
        )
    assert response.status_code == 422
