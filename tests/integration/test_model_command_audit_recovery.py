"""Independent real-audit and outer-result races for #191 command recovery."""

import asyncio
import json
import sqlite3

import pytest
from test_model_command_uncertainty import (
    NativeFailure,
    assert_no_current_reads,
    assert_unconfirmed,
    command_http,
    command_row,
    forbid_current_state,
    model_rows,
    native_error,
    restart_models,
)
from test_model_commands_authority import operation_key, request
from test_model_commands_authority import owner as owner


def audits(owner):
    return owner.store.read(kinds=["agent.command"])


def historical_proof(owner, command, saved):
    entries = audits(owner)
    original_key = "command:" + command.idempotency_key
    originals = [entry for entry in entries if entry.idempotency_key == original_key]
    proofs = [entry for entry in entries if entry.idempotency_key != original_key]
    assert len(originals) == len(proofs) == 1
    proof = proofs[0]
    assert proof.origin == "agent-supervisor"
    assert (proof.session_id, proof.thread_id, proof.epoch) == (
        command.session_id,
        command.thread_id,
        command.epoch,
    )
    assert proof.payload["input"] == command.input
    assert proof.payload["content_scope"] == "local_only"
    assert proof.payload["response"]["result"] == {
        "model_settings_receipt": saved,
        "recovered": True,
    }
    return proof


async def drain(task):
    if task is None:
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
    except (Exception, asyncio.CancelledError):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_original_audit_commit_with_lost_ack_recovers_using_distinct_idempotent_proof(
    owner, tmp_path, monkeypatch
):
    command = request(owner)
    append = owner.runtime.journal.append
    lost_ack = False

    async def append_then_lose_ack(body):
        nonlocal lost_ack
        result = await append(body)
        if body.idempotency_key == "command:" + command.idempotency_key and not lost_ack:
            lost_ack = True
            raise ConnectionError("Synthetic acknowledgment lost after real audit commit")
        return result

    monkeypatch.setattr(owner.runtime.journal, "append", append_then_lose_ack)
    with pytest.raises(type(native_error("unavailable"))):
        await owner.commands.execute(command)
    assert lost_ack and len(audits(owner)) == 1
    assert command_row(owner, command)[1] is None
    saved = owner.settings.receipt(operation_key(command))
    assert saved["schema_version"] == "1.1" and saved["revision"] == 1
    current = await restart_models(owner, tmp_path, profiles=False)
    before = model_rows(current)
    guards = forbid_current_state(owner, monkeypatch)

    result = await owner.commands.execute(command)
    assert result["status"] == "ok"
    assert result["result"] == {"model_settings_receipt": saved, "recovered": True}
    assert json.loads(command_row(owner, command)[1]) == result
    proof = historical_proof(owner, command, saved)
    original_audits = [entry.model_dump(mode="json") for entry in audits(owner)]
    assert await owner.commands.execute(command) == result
    assert [entry.model_dump(mode="json") for entry in audits(owner)] == original_audits
    assert historical_proof(owner, command, saved).event_id == proof.event_id
    assert model_rows(current) == before
    assert_no_current_reads(owner, guards)


@pytest.mark.parametrize("winner", ["original", "recovery"])
async def test_original_and_recovery_both_return_the_one_durable_outer_cas_winner(
    owner, monkeypatch, winner
):
    command = request(owner)
    append = owner.runtime.journal.append
    original_entered, proof_entered = asyncio.Event(), asyncio.Event()
    release_original, release_proof = asyncio.Event(), asyncio.Event()
    original = recovery = proof_waiter = None

    async def suspend_actual_audit_ack(body):
        result = await append(body)
        if body.idempotency_key == "command:" + command.idempotency_key:
            original_entered.set()
            await release_original.wait()
        else:
            proof_entered.set()
            await release_proof.wait()
        return result

    monkeypatch.setattr(owner.runtime.journal, "append", suspend_actual_audit_ack)
    try:
        original = asyncio.create_task(owner.commands.execute(command))
        await asyncio.wait_for(original_entered.wait(), timeout=5)
        assert len(audits(owner)) == 1 and command_row(owner, command)[1] is None
        saved = owner.settings.receipt(operation_key(command))
        before = model_rows(owner.settings)
        guards = forbid_current_state(owner, monkeypatch)
        recovery = asyncio.create_task(owner.commands.execute(command))
        proof_waiter = asyncio.create_task(proof_entered.wait())
        done, _ = await asyncio.wait(
            {recovery, proof_waiter}, timeout=5, return_when=asyncio.FIRST_COMPLETED
        )
        assert proof_waiter in done, "Recovery must append historical proof before completing"
        assert len(audits(owner)) == 2 and command_row(owner, command)[1] is None
        historical_proof(owner, command, saved)

        if winner == "original":
            first, second = original, recovery
            release_original.set()
        else:
            first, second = recovery, original
            release_proof.set()
        winning_result = await asyncio.wait_for(asyncio.shield(first), timeout=5)
        assert winning_result["status"] == "ok"
        assert json.loads(command_row(owner, command)[1]) == winning_result
        release_original.set()
        release_proof.set()
        losing_result = await asyncio.wait_for(asyncio.shield(second), timeout=5)
        assert losing_result == winning_result
        assert json.loads(command_row(owner, command)[1]) == winning_result
        assert await owner.commands.execute(command) == winning_result
        assert model_rows(owner.settings) == before and len(audits(owner)) == 2
        assert_no_current_reads(owner, guards)
    finally:
        release_original.set()
        release_proof.set()
        await drain(original)
        await drain(recovery)
        await drain(proof_waiter)


class CommandResultFailure:
    """Fail after the real outer result UPDATE, preserving its uncommitted proof."""

    def __init__(self, connection):
        self.connection = connection
        self.fired = False
        self.result_at_failure = None
        self.transaction_at_failure = False

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)

    def execute(self, sql, *args):
        result = self.connection.execute(sql, *args)
        if " ".join(sql.lower().split()).startswith("update commands set result"):
            self.fired = True
            self.transaction_at_failure = self.connection.in_transaction
            self.result_at_failure = self.connection.execute(
                "SELECT result FROM commands"
            ).fetchone()[0]
            assert self.transaction_at_failure and self.result_at_failure is not None
            raise sqlite3.OperationalError("Synthetic outer result UPDATE lost acknowledgment")
        return result


async def test_outer_completion_fault_stays_unconfirmed_and_reuses_existing_proof_on_retry(
    owner, tmp_path, monkeypatch
):
    command = request(owner)
    failure = NativeFailure(owner.settings, native_error("unavailable"), after_commit=True)
    monkeypatch.setattr(owner.settings, "change", failure)
    with pytest.raises(type(failure.error)):
        await owner.commands.execute(command)
    assert len(failure.receipts) == 1 and command_row(owner, command)[1] is None
    saved = failure.receipts[0]
    current = await restart_models(owner, tmp_path, profiles=False)
    before = model_rows(current)
    guards = forbid_current_state(owner, monkeypatch)
    connection = owner.commands.db
    fault = CommandResultFailure(connection)
    monkeypatch.setattr(owner.commands, "db", fault)
    async with command_http(owner) as client:
        response = await client.post("/v1/commands/execute", json=command.model_dump(mode="json"))
    assert fault.fired and fault.transaction_at_failure
    assert json.loads(fault.result_at_failure)["result"] == {
        "model_settings_receipt": saved,
        "recovered": True,
    }
    assert command_row(owner, command)[1] is None and not connection.in_transaction
    assert len(audits(owner)) == 1 and model_rows(current) == before
    proof = audits(owner)[0]
    assert proof.payload["response"]["result"]["model_settings_receipt"] == saved
    assert_unconfirmed(response)

    monkeypatch.setattr(owner.commands, "db", connection)
    result = await owner.commands.execute(command)
    assert result["result"] == {"model_settings_receipt": saved, "recovered": True}
    assert json.loads(command_row(owner, command)[1]) == result
    assert len(audits(owner)) == 1 and audits(owner)[0].event_id == proof.event_id
    assert model_rows(current) == before
    assert_no_current_reads(owner, guards)
