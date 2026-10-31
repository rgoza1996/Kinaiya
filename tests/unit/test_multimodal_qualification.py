"""No inference/device/cloud: strict synthetic smoke-runner transport checks."""

import copy
import hashlib
import json

import httpx
import pytest

from scripts import qualify_local_multimodal as qual


@pytest.fixture
def directory(tmp_path):
    # Mock transport never decodes these explicitly synthetic placeholder bytes.
    names = ["image.png", "beeps.wav", "clip.mp4", "frames/11.png"]
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"synthetic transport fixture: " + name.encode())
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "synthetic": True,
                "fixture_version": 3,
                "files": {
                    name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
                    for name in names
                },
            }
        )
    )
    return tmp_path


def typed(choice="respond", confidence=0.9):
    others = [name for name in ("respond", "wait", "ignore") if name != choice]
    return {
        "answers": {
            "red_square": {"type": "noul", "noul": 0.95},
            "speech": {"type": "noul", "noul": 0.05},
            "route": {
                "type": "choice",
                "choice": choice,
                "confidence": confidence,
                "probabilities": {
                    choice: confidence,
                    **dict.fromkeys(others, (1 - confidence) / 2),
                },
            },
        }
    }


def chat(text="The red square moves right and there are 3 beeps."):
    return {"choices": [{"finish_reason": "stop", "message": {"content": text}}]}


@pytest.mark.parametrize(
    "root",
    [
        "https://127.0.0.1:1234",
        "http://example.com",
        "http://127.0.0.1.evil",
        "http://user:secret@127.0.0.1",
        "http://127.0.0.1/v1",
        "http://127.0.0.1?key=secret",
        "http://localhost/#fragment",
        "file:///tmp/model",
        "http://127.0.0.1:0",
        "http://127.0.0.1:70000",
    ],
)
def test_remote_credentialed_or_ambiguous_roots_refuse(root):
    with pytest.raises(ValueError):
        qual.local_root(root)


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b"[]", b"null"])
def test_json_requires_finite_unambiguous_object(raw):
    with pytest.raises(ValueError):
        qual.decode(raw)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"answers": []},
        {"answers": {"red_square": []}},
        {"choices": {}},
        {"choices": [None]},
        {"choices": [{"message": None}]},
    ],
)
def test_malformed_model_responses_never_match(directory, payload):
    for case in qual.cases(directory, "converse", "decisions"):
        assert qual.inspect_response(case, payload)["expected_observed"] is False


def test_probabilities_and_confidence_must_be_consistent(directory):
    case = next(c for c in qual.cases(directory, "c", "d") if c["path"] == "/v1/systemone")
    assert qual.inspect_response(case, typed())["expected_observed"]
    invalid = []
    for mutate in [
        lambda p: p["answers"]["speech"].update(noul=True),
        lambda p: p["answers"]["route"].update(confidence=float("nan")),
        lambda p: p["answers"]["route"].update(choice="run_shell"),
        lambda p: p["answers"]["route"].update(confidence=0.7),
        lambda p: p["answers"]["route"]["probabilities"].update(ignore=1),
    ]:
        payload = copy.deepcopy(typed())
        mutate(payload)
        invalid.append(payload)
    for payload in invalid:
        assert not qual.inspect_response(case, payload)["valid_typed_output"]


@pytest.mark.parametrize(
    "choice,confidence,expected",
    [
        ("respond", 0.9, True),
        ("respond", 0.7, False),
        ("wait", 0.9, False),
        ("ignore", 0.9, False),
    ],
)
def test_paired_route_is_typed_and_no_fallback_inference(directory, choice, confidence, expected):
    calls = []

    def handler(request):
        calls.append((request.url.port, request.url.path))
        if request.url.path == "/v1/systemone":
            return httpx.Response(200, json=typed(choice, confidence))
        return httpx.Response(200, json=chat())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = qual.paired_pipeline(
            client,
            qual.cases(directory, "c", "d"),
            "http://127.0.0.1:8815",
            "http://127.0.0.1:8816",
        )
    assert result["converse_dispatched"] is expected
    assert calls == [(8816, "/v1/systemone")] + (
        [(8815, "/v1/chat/completions")] if expected else []
    )
    assert result["native_app_or_permissions_tested"] is False


def test_runner_binds_manifest_aliases_origins_and_preserves_failed_observation(directory):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/v1/models":
            alias = "c" if request.url.port == 8815 else "d"
            return httpx.Response(200, json={"data": [{"id": alias}]})
        if request.url.path == "/v1/systemone":
            return httpx.Response(200, json=typed())
        data = json.loads(request.content)
        parts = data["messages"][-1]["content"]
        text = "2 beeps" if any(part["type"] == "input_video" for part in parts) else None
        return httpx.Response(200, json=chat(text) if text else chat())

    report = qual.run(
        directory,
        "http://127.0.0.1:8815",
        "c",
        "d",
        native=True,
        decisions_base="http://127.0.0.1:8816",
        transport=httpx.MockTransport(handler),
    )
    assert report["all_observations_match"] is False
    assert report["camera_frame_replay_tested"] is True
    assert report["physical_camera_tested"] is report["integrated_converse_tested"] is False
    video = next(c for c in report["cases"] if c["id"] == "video_soundtrack")
    assert video["text"] == "2 beeps" and not video["expected_observed"]
    assert report["paired_pipeline"]["converse_dispatched"]
    assert len(calls) == 11  # two catalogs, seven cases, paired Decisions then Converse
    (directory / "image.png").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        qual.run(directory, "http://127.0.0.1", "c", "d", native=True)


def test_transport_bounds_redirects_and_missing_aliases(directory):
    def handler(request):
        return httpx.Response(302, headers={"Location": "https://example.com"}, json={})

    with pytest.raises(ValueError, match="discovery refused"):
        qual.run(
            directory,
            "http://127.0.0.1",
            "c",
            "d",
            native=True,
            transport=httpx.MockTransport(handler),
        )
    with pytest.raises(ValueError, match="already be loaded"):
        qual.run(
            directory,
            "http://127.0.0.1",
            "c",
            "d",
            native=True,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []})),
        )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b" " * (qual.MAX_RESPONSE_BYTES + 1))
        )
    ) as client:
        with pytest.raises(ValueError, match="exceeds bound"):
            qual.request(client, "GET", "http://127.0.0.1")


def test_source_media_read_is_bounded(tmp_path):
    path = tmp_path / "oversized.png"
    path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="exceeds"):
        qual.encoded(path)
    (tmp_path / "manifest.json").write_bytes(b" " * 65537)
    with pytest.raises(ValueError, match="manifest exceeds"):
        qual.run(tmp_path, "http://127.0.0.1", "c", "d", native=True)


def test_transcription_must_match_words_not_merely_contain_keywords(directory):
    (directory / "speech.wav").write_bytes(b"synthetic audio transport placeholder")
    selected = qual.cases(directory, "c", "d", include_speech=True)
    case = next(c for c in selected if c["id"] == "speech_transcription")
    assert qual.inspect_response(case, chat(qual.SPEECH_TEXT))["expected_observed"]
    assert not qual.inspect_response(
        case,
        chat(
            "I cannot create a spreadsheet, but here are three columns and a square moving right."
        ),
    )["expected_observed"]


@pytest.mark.parametrize("profile", ["lfm3", "gemma4"])
def test_documented_asr_and_audio_order_are_explicit(directory, profile):
    (directory / "speech.wav").write_bytes(b"synthetic transport fixture")
    selected = qual.cases(directory, "c", "d", include_speech=True, prompt_profile=profile)
    asr = next(c for c in selected if c["id"] == "speech_transcription")["body"]["messages"]
    if profile == "lfm3":
        assert asr[0]["content"] == "Perform ASR in English."
        assert [p["type"] for p in asr[-1]["content"]] == ["input_audio"]
    else:
        assert "English" in asr[-1]["content"][0]["text"]
        assert asr[-1]["content"][-1]["type"] == "input_audio"
        paired = next(c for c in selected if c["id"] == "speech_image")
        assert [p["type"] for p in paired["body"]["messages"][-1]["content"]] == [
            "image_url",
            "text",
            "input_audio",
        ]
    assert qual.transcript_words("Three columns.") == qual.transcript_words("3 columns")


@pytest.mark.parametrize("choice,expected_calls", [("respond", 3), ("wait", 2)])
def test_explicit_asr_bridge_keeps_transcript_untrusted_and_gates_answer(
    directory, choice, expected_calls
):
    (directory / "speech.wav").write_bytes(b"synthetic")
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(200, json=chat(qual.SPEECH_TEXT))
        if request.url.path == "/v1/systemone":
            assert "Untrusted recognition hypothesis" in body["state"]
            assert len(body["files"]) == 1 and body["files"][0].startswith("data:image/")
            return httpx.Response(200, json=typed(choice))
        assert all(p["type"] != "input_audio" for p in body["messages"][-1]["content"])
        return httpx.Response(
            200, json=chat("A spreadsheet with three columns; the square is red.")
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = qual.asr_bridge_preflight(
            client,
            qual.cases(directory, "c", "d", include_speech=True),
            "http://127.0.0.1:8815",
            "http://127.0.0.1:8816",
        )
    assert len(calls) == expected_calls
    assert result["converse_dispatched"] is (choice == "respond")
    assert result["native_app_or_permissions_tested"] is False
