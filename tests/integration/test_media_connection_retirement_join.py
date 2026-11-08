"""Join the real owner-channel original through its native publication.

The requirement/plan was frozen before baseline ownership inspection. New
candidate production code/proposal was not read. Existing native fixture/source
exposure is disclosed; no synthetic caller flag supplies a cleanup proof.
"""

import asyncio
import sys
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_session_media_owner import _WAIT, _stack, _typed_receipt

from services.session.media_clients import MediaSessionError


@asynccontextmanager
async def _recording(env):
    source, grant = await env.admit()
    async with env.connected(source, grant) as channel:
        assert (await env.put(env.put_header(source), b"abcdef", grant)).status_code == 200
        sealed, request = await env.seal(source, grant)
        _typed_receipt(sealed, "SealRequest", "Seal", request)
        native = env.owner._sources[source["body"]["source_id"]]
        connection = native.connection
        assert env.owner.event_hub._connections[connection.instance] is connection
        assert connection.started.is_set()
        yield SimpleNamespace(
            source=source,
            native=native,
            connection=connection,
            channel=channel,
            upload=env.uploads[source["body"]["source_id"]],
        )


async def _rows(env, connection):
    def read_original():
        with env.store.lock:
            row = env.store.db.execute(
                "SELECT d.state,d.publication_state,r.state,r.spool_bytes "
                "FROM media_dependencies d JOIN media_reservations r ON r.source_id=d.source_id "
                "WHERE d.dependency_id=? AND d.source_id=? AND d.kind='owner_channel' "
                "AND d.original_instance=?",
                (connection.instance, connection.source_id, connection.instance),
            ).fetchone()
            assert row is not None, "The original native owner-channel dependency vanished"
            return tuple(row)

    return await env.owner.run_native(read_original)


async def _stop_original(env, case, key):
    status = await env.current_status(case.source)
    request = env.control_request(case.source, "stop", key=key, fence=status["body"]["fence"])
    result = _typed_receipt(
        await env.post("/v1/media/control", request), "Control", "Status", request
    )
    assert result["body"]["desired"] == "stopped"
    task = case.native.delete_task
    assert isinstance(task, asyncio.Task)
    return task


def _physical_joined(connection):
    assert connection.joined is True
    assert connection.fenced is True
    for original in (connection.receiver, connection.sender):
        assert isinstance(original, asyncio.Task)
        assert original.done() and not original.cancelled()


async def _join_owned_after_release(primary, *tasks):
    """Observe actual retained tasks without replacing a primary test failure."""
    errors = []
    for task in dict.fromkeys(task for task in tasks if task is not None):
        try:
            assert isinstance(task, asyncio.Task)
            await asyncio.wait_for(asyncio.shield(task), _WAIT)
        except BaseException as error:
            if primary is not None:
                primary.add_note("Additional owned-task join error: " + type(error).__name__)
            else:
                errors.append(error)
    if errors:
        raise BaseExceptionGroup("Original connection tasks did not positively retire", errors)


async def test_cleanup_joins_same_held_connection_publication_before_any_result(tmp_path):
    async with _stack(tmp_path) as env:
        async with _recording(env) as case:
            loop = asyncio.get_running_loop()
            entered = asyncio.Event()
            release = threading.Event()
            seen = []
            native_publish = env.owner.retire_connection_locked

            def held_publish(connection):
                seen.append(connection)
                loop.call_soon_threadsafe(entered.set)
                # This is before the original SQL boundary: no Store lock is
                # held while the authenticated stop enrolls its cleanup task.
                # Every path below releases this exact worker in finally.
                release.wait()
                return native_publish(connection)

            with patch.object(env.owner, "retire_connection_locked", new=held_publish):
                try:
                    await case.channel.incoming.put({"type": "websocket.disconnect", "code": 1000})
                    await asyncio.wait_for(entered.wait(), _WAIT)
                    original = case.connection.retirement_task
                    assert isinstance(original, asyncio.Task)
                    assert seen == [case.connection]
                    _physical_joined(case.connection)
                    assert not original.done() and not release.is_set()
                    assert case.native.delete_task is None, (
                        "The intended cleanup ordering was not reached"
                    )
                    before = await _rows(env, case.connection)
                    assert before[:2] != ("settled", "settled")
                    assert before[2] != "released" and before[3] == 6

                    cleanup = await _stop_original(env, case, "stop-during-original-publication")
                    assert case.native.delete_task is cleanup
                    # Pending observation only. This cancels neither original,
                    # and its existing fixture bound is no cleanup/timing proof.
                    done, pending = await asyncio.wait({cleanup}, timeout=_WAIT)
                    assert not release.is_set() and not original.done()
                    assert not done and pending == {cleanup}, (
                        "Cleanup returned before its original connection publication retired"
                    )
                    assert case.upload.deleted is False and env.spool.reserved_bytes == 6
                    assert (await _rows(env, case.connection))[:2] != ("settled", "settled")

                    release.set()
                    assert await asyncio.wait_for(asyncio.shield(original), _WAIT) is True
                    assert case.connection.retirement_task is original
                    assert await asyncio.wait_for(asyncio.shield(cleanup), _WAIT) is True
                    assert case.native.delete_task is cleanup
                    assert (await _rows(env, case.connection)) == (
                        "settled",
                        "settled",
                        "released",
                        6,
                    )
                    assert case.upload.deleted is True and env.spool.reserved_bytes == 0
                    assert case.connection.instance not in env.owner.event_hub._connections
                    assert seen == [case.connection]
                finally:
                    # Release before connection/lifespan teardown, including a
                    # cold-baseline early-False assertion failure.
                    primary = sys.exception()
                    release.set()
                    await _join_owned_after_release(
                        primary,
                        case.connection.retirement_task,
                        case.native.delete_task,
                        case.channel.task,
                    )


async def test_finished_positive_original_survives_hub_registry_removal(tmp_path):
    async with _stack(tmp_path) as env:
        async with _recording(env) as case:
            await case.channel.incoming.put({"type": "websocket.disconnect", "code": 1000})
            await case.channel.close()
            original = case.connection.retirement_task
            assert isinstance(original, asyncio.Task)
            assert original.done() and not original.cancelled()
            assert original.result() is True
            _physical_joined(case.connection)
            assert case.connection.instance not in env.owner.event_hub._connections
            assert (await _rows(env, case.connection))[:2] == ("settled", "settled")
            assert case.upload.deleted is False and env.spool.reserved_bytes == 6

            cleanup = await _stop_original(env, case, "stop-after-original-hub-removal")
            assert await asyncio.wait_for(asyncio.shield(cleanup), _WAIT) is True
            assert case.connection.retirement_task is original
            assert original.result() is True
            assert case.native.delete_task is cleanup
            assert (await _rows(env, case.connection)) == ("settled", "settled", "released", 6)
            assert case.upload.deleted is True and env.spool.reserved_bytes == 0


@pytest.mark.parametrize("after_commit", [False, True])
async def test_failed_original_never_releases_source_even_if_sql_publication_committed(
    tmp_path, after_commit
):
    # The fixture may dispose backing handles only after all actual users join;
    # its intent flag does not turn native publication uncertainty into success.
    async with _stack(tmp_path, allow_unknown_shutdown=True) as env:
        seen = []
        fault = MediaSessionError("unavailable")
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        native_publish = env.owner.retire_connection_locked
        case = None
        original = None

        def failed_publish(connection):
            seen.append(connection)
            loop.call_soon_threadsafe(entered.set)
            if after_commit:
                native_publish(connection)
            raise fault

        with patch.object(env.owner, "retire_connection_locked", new=failed_publish):
            try:
                async with _recording(env) as case:
                    await case.channel.incoming.put({"type": "websocket.disconnect", "code": 1000})
                    await asyncio.wait_for(entered.wait(), _WAIT)
                    original = case.connection.retirement_task
                    assert isinstance(original, asyncio.Task)
                    with pytest.raises(MediaSessionError) as failed:
                        await asyncio.wait_for(asyncio.shield(original), _WAIT)
                    assert failed.value is fault and original.exception() is fault
                    assert seen == [case.connection]
                    _physical_joined(case.connection)
                    before = await _rows(env, case.connection)
                    if after_commit:
                        assert before[:2] == ("settled", "settled")
                    else:
                        assert before[:2] != ("settled", "settled")
                    assert before[2] != "released" and before[3] == 6

                    cleanup = await _stop_original(env, case, "stop-after-failed-original")
                    assert await asyncio.wait_for(asyncio.shield(cleanup), _WAIT) is False
                    assert case.connection.retirement_task is original
                    assert original.exception() is fault
                    assert case.native.delete_task is cleanup
                    assert case.upload.deleted is False and env.spool.reserved_bytes == 6
                    assert (await _rows(env, case.connection))[2] != "released"
            except (MediaSessionError, RuntimeError) as error:
                # An intentional publication fault can also end the real ASGI
                # owner task with a transport error. Accept only that exact
                # observed task exception; never swallow an assertion/setup error.
                assert case is not None and original is not None
                assert case.channel.task.done() and not case.channel.task.cancelled()
                assert case.channel.task.exception() is error
                assert original.done() and original.exception() is fault

            assert case is not None and original is not None
            assert seen == [case.connection]
            assert case.connection.retirement_task is original
            assert case.upload.deleted is False and env.spool.reserved_bytes == 6
            assert await env.owner.close() is False
            assert case.connection.retirement_task is original
            assert original.exception() is fault
            assert case.upload.deleted is False and env.spool.reserved_bytes == 6
            assert seen == [case.connection]
