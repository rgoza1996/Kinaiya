"""Real, opt-in Pi SDK integration in a private network and filesystem namespace."""

import asyncio
import base64
import json
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from packages.agent_adapters.external_submission import PiSubmission
from packages.agent_adapters.pi_sdk import PiPeerUnavailable, PiSdkPeer
from packages.contracts.client import JournalClient
from packages.contracts.external_agent import ExternalAgentRevoke, ExternalAgentTarget
from packages.contracts.external_agent_client import ExternalAgentNativeUnavailable
from packages.contracts.models import SessionRequest, SpeechWindow
from packages.providers.text import ScriptedProvider
from services import launcher
from services.common.auth import Auth
from services.converse.api import create_app as converse_app
from services.converse.dependencies import RequiredDecisions
from services.converse.runtime import ConverseRuntime
from services.converse.windows import WindowConverseController
from tests.integration.test_external_agent_session import (
    add,
    bind_to,
    connection,
    native,
    queued_claim,
)
from tests.integration.test_required_decisions_readiness import TOKENS, loopback

ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = os.environ.get("KINAIYA_PI_TEST_SDK_ROOT")
pytestmark = pytest.mark.skipif(
    not SDK_ROOT,
    reason="requires the hash-verified Pi SDK installation prefix in KINAIYA_PI_TEST_SDK_ROOT",
)


def sdk_sandbox(private, previous=None, *, transcript=False):
    node = shutil.which("node")
    assert node and shutil.which("bwrap"), "Pi qualification requires Node and bubblewrap"
    sdk = Path(SDK_ROOT).resolve(strict=True)
    args = [
        "bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--ro-bind",
        str(Path(node).resolve()),
        "/node",
        "--ro-bind",
        str(sdk),
        "/sdk",
        "--ro-bind",
        str(ROOT / "packages/agent_adapters/pi_peer.mjs"),
        "/peer.mjs",
        "--bind",
        str(private),
        "/private",
        "--clearenv",
        "--setenv",
        "HOME",
        "/private/home",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--chdir",
        "/private/workspace",
        "/node",
        "--no-warnings",
        "/peer.mjs",
        *(["--transcript"] if transcript else []),
        "/sdk",
        "/private/workspace",
        "/private/agent",
        "/private/sessions",
    ]
    if previous:
        args.append("/private/" + previous.relative_to(private).as_posix())
    return args


@pytest.fixture
def private(tmp_path):
    tmp_path = tmp_path / "pi"
    tmp_path.mkdir(mode=0o700)
    for relative in ("home", "workspace", "agent", "agent/extensions", "sessions"):
        (tmp_path / relative).mkdir(mode=0o700)
    shutil.copyfile(
        ROOT / "tests/fixtures/pi_bridge_provider.mjs",
        tmp_path / "agent/extensions/fixture.js",
    )
    (tmp_path / "agent/settings.json").write_text(
        json.dumps(
            {
                "defaultProvider": "kinaiya-bridge-fixture",
                "defaultModel": "fixture",
                "retry": {"enabled": False},
            }
        )
    )
    return tmp_path


@asynccontextmanager
async def connect(private, previous=None, *, transcript=False):
    process = await asyncio.create_subprocess_exec(
        *sdk_sandbox(private, previous, transcript=transcript),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=2_097_152,
    )
    try:
        yield process
    finally:
        if process.returncode is None:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()


async def receive(process):
    raw = await asyncio.wait_for(process.stdout.readline(), 10)
    assert raw, (await process.stderr.read()).decode()
    frame = json.loads(raw)
    assert frame["version"] == 1
    return frame


async def request(process, command, sequence, **fields):
    identifier = f"{sequence:032x}"
    frame = {"version": 1, "command": command, "id": identifier, **fields}
    process.stdin.write(json.dumps(frame).encode() + b"\n")
    await process.stdin.drain()
    return identifier


async def read_through(process, predicate):
    frames = []
    while True:
        try:
            frame = await receive(process)
        except TimeoutError:
            pytest.fail(f"Pi did not emit the expected frame after {frames!r}")
        frames.append(frame)
        if predicate(frame):
            return frames
        assert len(frames) < 40


async def close(process, sequence):
    stop_id = await request(process, "close", sequence)
    reply = await receive(process)
    assert (reply["id"], reply["disposition"]) == (stop_id, "stopping")
    assert await asyncio.wait_for(process.wait(), 5) == 0


async def test_real_pi_preserves_literal_instruction_and_reopens_without_replay(private):
    instruction = "/fixture-command literal input\r\nSecond \u2028line"
    settings_path = private / "agent/settings.json"
    original_settings = settings_path.read_bytes()
    async with connect(private) as process:
        ready = await receive(process)
        assert ready["type"] == "ready" and ready["sdk_version"] == "1.5.2"
        initial_session = ready["session_id"]
        run_id = await request(process, "submit", 1, text=instruction)
        result = await read_through(process, lambda frame: frame.get("kind") == "settled")
        assert [frame["disposition"] for frame in result if frame["type"] == "response"] == [
            "accepted"
        ]
        assert [
            (frame["run_id"], frame["text"])
            for frame in result
            if frame.get("kind") == "assistant_message"
        ] == [(run_id, instruction)]
        await request(process, "submit", 1, text=instruction)
        assert (await receive(process))["reason"] == "duplicate_request"
        await close(process, 2)

    (session_file,) = (private / "sessions").glob("*.jsonl")
    async with connect(private, session_file) as process:
        assert (await receive(process))["session_id"] == initial_session
        await request(process, "status", 3)
        status = await receive(process)
        assert status["disposition"] == "observed" and status["busy"] is False
        assert status["run_id"] is None
        await close(process, 4)
    assert settings_path.read_bytes() == original_settings


async def test_real_pi_routes_interrupt_to_the_active_run_and_reports_backend_failure(private):
    async with connect(private) as process:
        assert (await receive(process))["type"] == "ready"
        run_id = await request(process, "submit", 1, text="hold for interrupt")
        started = await read_through(process, lambda frame: frame.get("kind") == "running")
        assert any(frame.get("disposition") == "accepted" for frame in started)

        await request(process, "interrupt", 2, run_id="f" * 32)
        assert (await receive(process))["reason"] == "run_changed"
        stop_id = await request(process, "interrupt", 3, run_id=run_id)
        stopped = await read_through(process, lambda frame: frame.get("kind") == "interrupted")
        assert any(
            frame.get("id") == stop_id and frame.get("disposition") == "requested"
            for frame in stopped
        )
        assert stopped[-1]["run_id"] == run_id

        failed_id = await request(process, "submit", 4, text="synthetic provider failure")
        failed = await read_through(process, lambda frame: frame.get("kind") == "failed")
        assert failed[-1]["run_id"] == failed_id
        await close(process, 5)


async def test_real_pi_refuses_a_missing_exact_owner_selection(private):
    settings = private / "agent/settings.json"
    config = json.loads(settings.read_text())
    config["defaultModel"] = "not-registered"
    settings.write_text(json.dumps(config))
    async with connect(private) as process:
        with pytest.raises(PiPeerUnavailable, match="Pi-selected model is unavailable"):
            await PiSdkPeer.attach(process.stdout, process.stdin)
        assert await asyncio.wait_for(process.wait(), 5) == 1


async def test_python_bridge_transport_preserves_one_real_pi_submission(private):
    instruction = "Python owner through the real Pi SDK \u2602"
    async with connect(private) as process:
        peer = await PiSdkPeer.attach(process.stdout, process.stdin)
        original = "1" * 32
        first, retained = await asyncio.gather(
            peer.submit(original, instruction),
            peer.submit(original, instruction),
        )
        assert first == retained and first.disposition == "accepted"
        async with asyncio.timeout(5):
            while not any(event.kind == "settled" for event in peer.events(0)):
                await asyncio.sleep(0.02)
        output = [event for event in peer.events(0) if event.kind == "assistant_message"]
        assert [(event.run_id, event.text) for event in output] == [(original, instruction)]
        assert (await peer.status("2" * 32)).busy is False
        assert (await peer.request_close("3" * 32)).disposition == "stopping"
        assert await asyncio.wait_for(process.wait(), 5) == 0


async def test_real_pi_requests_transcript_on_demand_from_authenticated_session(private):
    async with native(private.parent / "journal.sqlite") as peers:
        session = peers.store.create_session(SessionRequest(channel="converse"))
        visible = add(peers.store, session, "conversation.user", "Authorized history ☂")
        add(peers.store, session, "conversation.user", "Private history", scope="local_only")
        later = add(
            peers.store, session, "conversation.converse", "Authorized reply.", role="converse"
        )
        consent = await peers.owner.authorize(connection(session, backend=None))
        bound = None
        reads = []

        async def read_transcript(**position):
            reads.append(position)
            return await peers.decisions.read_bound_transcript(bound, **position)

        async with connect(private, transcript=True) as process:
            peer = await PiSdkPeer.attach(
                process.stdout, process.stdin, transcript_reader=read_transcript
            )
            bound = await peers.decisions.bind(bind_to(consent, peer.session_id))

            async def run(identifier, text):
                assert (await peer.submit(identifier, text)).disposition == "accepted"
                async with asyncio.timeout(5):
                    while True:
                        current = [event for event in peer.events(0) if event.run_id == identifier]
                        if any(event.kind == "settled" for event in current):
                            return [
                                event.text for event in current if event.kind == "assistant_message"
                            ]
                        await asyncio.sleep(0.02)

            assert await run("1" * 32, "ordinary turn") == ["ordinary turn"]
            assert reads == []
            (answer,) = await run("2" * 32, "read scoped transcript")
            assert len(reads) == 3
            page = json.loads(answer)
            assert [(entry["event_id"], entry["text"]) for entry in page["entries"]] == [
                (visible.event_id, "Authorized history ☂"),
                (later.event_id, "Authorized reply."),
            ]
            assert page["has_more"] is False

            await peers.owner.revoke(
                ExternalAgentRevoke(
                    contract_version=1,
                    request_id="revoke-test-grant",
                    operation="revoke",
                    attachment_id=consent.attachment_id,
                )
            )
            (answer,) = await run("3" * 32, "read scoped transcript")
            assert len(reads) == 4 and "Converse transcript page is unavailable" in answer
            assert (await peer.request_close("4" * 32)).disposition == "stopping"
            assert await asyncio.wait_for(process.wait(), 5) == 0


async def test_session_claim_cannot_replay_a_real_pi_run_after_peer_restart(private):
    async with native(private.parent / "journal.sqlite") as peers:
        session = peers.store.create_session(SessionRequest(channel="converse"))
        instruction = "/fixture-command recorded Decisions work"
        source = add(peers.store, session, "conversation.user", instruction)
        grant = await peers.owner.authorize(connection(session, backend=None))
        async with connect(private) as process:
            peer = await PiSdkPeer.attach(process.stdout, process.stdin)
            binding = bind_to(grant, peer.session_id)
            await peers.decisions.bind(binding)
            request = queued_claim(peers.store, session, source, binding)
            submission = PiSubmission(peers.decisions, peer)
            original = await submission.submit(request)
            assert original.disposition == "accepted"
            assert (await submission.submit(request)).task.task_id == original.task.task_id
            async with asyncio.timeout(5):
                while not any(event.kind == "settled" for event in peer.events(0)):
                    await asyncio.sleep(0.02)
            output = [event.text for event in peer.events(0) if event.kind == "assistant_message"]
            assert output == [instruction]

            class DropFirstReply:
                async def claim(self, proposal):
                    reply = await peers.decisions.claim(proposal)
                    assert reply.disposition == "original"
                    raise ExternalAgentNativeUnavailable

            second = add(peers.store, session, "conversation.user", "Lost Session reply")
            lost = queued_claim(peers.store, session, second, binding)
            with pytest.raises(ExternalAgentNativeUnavailable):
                await PiSubmission(DropFirstReply(), peer).submit(lost)
            assert (await submission.submit(lost)).disposition == "uncertain"
            assert [
                event.text for event in peer.events(0) if event.kind == "assistant_message"
            ] == output
            assert (await peer.request_close("b" * 32)).disposition == "stopping"
            assert await asyncio.wait_for(process.wait(), 5) == 0

        (session_file,) = (private / "sessions").glob("*.jsonl")
        async with connect(private, session_file) as process:
            peer = await PiSdkPeer.attach(
                process.stdout,
                process.stdin,
                expected_session_id=binding.target.backend_session_id,
            )
            recovered = await PiSubmission(peers.decisions, peer).submit(request)
            assert recovered.task.task_id == original.task.task_id
            assert recovered.disposition == "uncertain" and peer.events(0) == ()
            assert (await peer.status("c" * 32)).busy is False
            assert (await peer.request_close("d" * 32)).disposition == "stopping"
            assert await asyncio.wait_for(process.wait(), 5) == 0


def managed_app(private, peers, monkeypatch, fixture):
    data = private.parent / "decisions-data"
    data.mkdir(mode=0o700)
    node = Path(shutil.which("node")).resolve()
    apps = []
    monkeypatch.setattr(launcher, "load_or_create_tokens", lambda _: TOKENS)
    monkeypatch.setattr(launcher, "provider", lambda *_args, **_kwargs: fixture)
    monkeypatch.setattr(uvicorn, "run", lambda app, **_: apps.append(app))
    config = private.parent / "config.json"
    config.write_text(
        json.dumps(
            {
                "session_url": str(peers.http.base_url).rstrip("/"),
                "external_agent": {
                    "profile_id": "pi-fixture",
                    "node": str(node),
                    "sdk_root": SDK_ROOT,
                    "agent_dir": str(private / "agent"),
                    "workspace": str(private / "workspace"),
                },
            }
        )
    )
    launcher.serve(
        SimpleNamespace(
            data_dir=data,
            workspace=private / "workspace",
            config=config,
            service="decisions",
            port=None,
            start_paused=False,
            startup_subsystem_mode="converse-decisions",
        )
    )
    (app,) = apps
    return app, data


async def test_converse_only_launcher_attaches_real_sdk_and_reopens_empty_session(
    private, monkeypatch
):
    fixture = ScriptedProvider([])
    async with native(private.parent / "journal.sqlite") as peers:
        session = peers.store.create_session(SessionRequest(channel="converse"))
        grant = await peers.owner.authorize(connection(session, backend=None))
        start = "/fixture-command from the recorded managed path"
        provisional = SimpleNamespace(
            target=ExternalAgentTarget(
                attachment_id=grant.attachment_id, backend_session_id="not-observed-yet"
            )
        )
        proposals = [
            queued_claim(
                peers.store,
                session,
                add(peers.store, session, "conversation.user", text),
                provisional,
            ).model_dump(mode="json")
            for text in (start, "hold for interrupt", "A different synthetic task")
        ]
        app, data = managed_app(private, peers, monkeypatch, fixture)
        manager = app.state.external_agent
        attach = {
            "contract_version": 1,
            "request_id": "attach",
            "operation": "attach",
            "attachment_id": grant.attachment_id,
        }
        path = "/v1/decisions/external-agent"
        async with (
            loopback(app) as address,
            httpx.AsyncClient(
                base_url=address,
                headers={"Authorization": "Bearer " + TOKENS["owner"]},
                trust_env=False,
                timeout=15,
            ) as owner,
        ):
            denied = await owner.post(
                path + "/attach",
                json=attach,
                headers={"Authorization": "Bearer " + TOKENS["decisions"]},
            )
            assert denied.status_code == 403 and not (data / "external-agent").exists()
            unrelated = await peers.owner.authorize(
                connection(session, backend=None).model_copy(update={"profile_id": "other"})
            )
            wrong = await owner.post(
                path + "/attach",
                json={
                    **attach,
                    "attachment_id": unrelated.attachment_id,
                },
            )
            assert wrong.status_code == 403 and not (data / "external-agent").exists()
            result = await owner.post(path + "/attach", json=attach)
            assert result.status_code == 200, result.text
            opened = result.json()
            assert opened["state"] == "connected" and opened["capabilities"]["transcript_read"]
            assert (await owner.get("/v1/decisions/status")).json()["enabled"] is False
            first = manager._owned.process
            target = opened["target"]
            disconnect = {
                "contract_version": 1,
                "request_id": "disconnect",
                "operation": "disconnect",
                "target": target,
                "mode": "detach",
            }
            assert (await owner.post(path + "/disconnect", json=disconnect)).json()[
                "disposition"
            ] == "detached"
            reconnected = (await owner.post(path + "/attach", json=attach)).json()
            assert reconnected["target"] == target and manager._owned.process is first
            disconnect["mode"] = "shutdown_owned"
            assert (await owner.post(path + "/disconnect", json=disconnect)).json()[
                "disposition"
            ] == "stopped"
            assert first.returncode is not None
            reattached = await owner.post(path + "/attach", json=attach)
            assert reattached.status_code == 200, reattached.text
            assert reattached.json()["target"] == target and manager._owned.process is not first
            history = data / "external-agent" / grant.attachment_id / "session.jsonl"
            records = [json.loads(line) for line in history.read_text().splitlines()]
            assert records[0]["id"] == target["backend_session_id"]
            assert all(record["type"] != "message" for record in records)
            for item in proposals:
                item["command"]["target"] = target
            proposal, held, different = proposals
            assert (await owner.post(path + "/submit", json=proposal)).status_code == 403
            accepted = await owner.post(
                path + "/submit",
                json=proposal,
                headers={"Authorization": "Bearer " + TOKENS["decisions"]},
            )
            assert accepted.status_code == 200 and accepted.json()["disposition"] == "accepted"
            observed, cursor = [], 0
            async with asyncio.timeout(5):
                while not any(item.get("state") == "settled" for item in observed):
                    result = await owner.post(
                        path + "/events",
                        json={
                            "contract_version": 1,
                            "request_id": "poll",
                            "operation": "events",
                            "target": target,
                            "after": cursor,
                            "limit": 1,
                        },
                    )
                    assert result.status_code == 200, result.text
                    page = result.json()
                    assert page["after"] == cursor and len(page["events"]) <= 1
                    observed.extend(page["events"])
                    cursor = page["next_after"]
                    await asyncio.sleep(0.02)
            assert [item["text"] for item in observed if item["kind"] == "assistant_message"] == [
                start
            ]
            status = await owner.post(
                path + "/status",
                json={
                    "contract_version": 1,
                    "request_id": "status",
                    "operation": "status",
                    "target": target,
                },
            )
            assert status.status_code == 200 and status.json()["tasks"][0]["state"] == "settled"
            decisions_role = {"Authorization": "Bearer " + TOKENS["decisions"]}
            original = await owner.post(path + "/submit", json=held, headers=decisions_role)
            assert original.status_code == 200 and original.json()["disposition"] == "accepted"
            replay = await owner.post(path + "/submit", json=held, headers=decisions_role)
            assert replay.status_code == 200, replay.text
            assert replay.json()["task"]["task_id"] == original.json()["task"]["task_id"]
            refused = await owner.post(path + "/submit", json=different, headers=decisions_role)
            assert refused.status_code == 409 and refused.json()["reason"] == "busy"
            assert (
                peers.store.db.execute("SELECT count(*) FROM external_agent_claims").fetchone()[0]
                == 2
            )
            final = manager._owned.process
        assert final.returncode is not None
        assert fixture.calls == []


async def test_converse_text_and_rolling_audio_route_through_actual_decisions_to_pi(
    private, monkeypatch
):
    typed = "/fixture-command exact typed instruction \u2602"
    spoken = "Public synthetic transcription for Pi"
    audio = b"PUBLIC_SYNTHETIC_AUDIO"
    classifier = ScriptedProvider(
        [
            '{"route":"queue"}',
            '{"conversation":"WAIT_FOR_NEXT_WINDOW","agent_route":"QUEUE_UNTIL_TASK_DONE",'
            '"reason_code":"synthetic_route"}',
        ]
    )

    class Speech:
        calls = 0

        async def transcribe(self, data, filename):
            assert data == audio and filename == "window.wav"
            self.calls += 1
            return {"text": spoken}

    async with native(private.parent / "journal.sqlite") as peers:
        session = peers.store.create_session(SessionRequest(channel="converse"))
        elsewhere = peers.store.create_session(SessionRequest(channel="converse"))
        prior = add(peers.store, session, "conversation.user", "Before attachment")
        grant = await peers.owner.authorize(connection(session, backend=None))
        decisions_app, _ = managed_app(private, peers, monkeypatch, classifier)
        scope = {key: getattr(session, key) for key in ("session_id", "thread_id", "epoch")}
        headers = {"Authorization": "Bearer " + TOKENS["owner"]}
        path = "/v1/decisions/external-agent"
        async with (
            loopback(decisions_app) as decisions,
            httpx.AsyncClient(base_url=decisions, headers=headers, trust_env=False) as owner,
        ):
            attached = await owner.post(
                path + "/attach",
                json={
                    "contract_version": 1,
                    "request_id": "attach-routing",
                    "operation": "attach",
                    "attachment_id": grant.attachment_id,
                },
            )
            assert attached.status_code == 200, attached.text
            target = attached.json()["target"]
            gate = RequiredDecisions(decisions, TOKENS["converse"])
            journal = JournalClient(str(peers.http.base_url).rstrip("/"), TOKENS["converse"])
            converser = ConverseRuntime(
                journal, ScriptedProvider(["I heard your request."]), decisions_dependency=gate
            )
            speech = Speech()
            windows = WindowConverseController(converser)
            cursor = 0

            async def settled(instruction, kind, expected):
                nonlocal cursor
                events, receipt = [], None
                async with asyncio.timeout(10):
                    while receipt is None or not any(
                        item.get("state") == "settled" for item in events
                    ):
                        page = await owner.post(
                            path + "/events",
                            json={
                                "contract_version": 1,
                                "request_id": "observe-routing",
                                "operation": "events",
                                "target": target,
                                "after": cursor,
                                "limit": 8,
                            },
                        )
                        assert page.status_code == 200, page.text
                        data = page.json()
                        cursor = data["next_after"]
                        events.extend(data["events"])
                        matches = peers.store.read(session_id=session.session_id, kinds=[kind])
                        if matches:
                            receipt = matches[-1]
                            assert receipt.payload["state"] == expected, receipt.payload
                        await asyncio.sleep(0.02)
                assert [item["text"] for item in events if item["kind"] == "assistant_message"] == [
                    instruction
                ]
                assert receipt.payload["state"] == expected and receipt.payload["executor"] == "pi"
                assert receipt.payload["external_task_id"] == events[-1]["task_id"]
                assert "native_task_id" not in receipt.payload
                return receipt

            async def wait_for_ready():
                async with asyncio.timeout(5):
                    while not (await owner.get("/v1/decisions/readiness")).json()["ready"]:
                        await asyncio.sleep(0.02)

            try:
                await wait_for_ready()
                await gate.start()
                await gate.refresh()
                assert converser.enabled
                async with (
                    loopback(converse_app(converser, Auth(TOKENS), speech, windows)) as converse,
                    httpx.AsyncClient(
                        base_url=converse, headers=headers, trust_env=False
                    ) as client,
                ):
                    add(
                        peers.store, elsewhere, "conversation.user", "No owner grant for this scope"
                    )
                    turn = await client.post(
                        "/v1/converse/turns",
                        json={
                            **scope,
                            "text": typed,
                            "content_scope": "public_synthetic",
                            "idempotency_key": "fresh-native-text",
                        },
                    )
                    assert turn.status_code == 200, turn.text
                    text_receipt = await settled(typed, "routing.dispatch", "accepted")
                    user = peers.store.entry(text_receipt.payload["source_event_id"])
                    assert user.payload["text"] == typed and user.event_id != prior.event_id

                    body = SpeechWindow(
                        **scope,
                        utterance_id="native-rolling",
                        window_index=0,
                        text="UNTRUSTED_REQUEST_TEXT",
                        start_ms=0,
                        end_ms=1000,
                        user_speaking=True,
                        content_scope="public_synthetic",
                    ).model_dump(mode="json")
                    body["audio_base64"] = base64.b64encode(audio).decode("ascii")
                    voice = await client.post("/v1/converse/audio/windows", json=body)
                    assert voice.status_code == 200, voice.text
                    window = voice.json()["entry"]
                    assert (
                        window["payload"]["text"] == spoken and not voice.json()["retained_audio"]
                    )
                    speech_receipt = await settled(spoken, "routing.window_dispatch", "applied")
                    assert speech_receipt.payload["source_event_id"] == window["event_id"]
                    replay = await client.post("/v1/converse/audio/windows", json=body)
                    assert replay.status_code == 200 and replay.json()["replay"]
                    assert speech.calls == 1
            finally:
                await windows.close()
                await converser.close()
                await gate.close()
                await journal.close()
        assert len(classifier.calls) == 2
        assert (
            peers.store.db.execute("SELECT count(*) FROM external_agent_claims").fetchone()[0] == 2
        )
