"""Frozen independent proof of a cancellation/deadline during final SQL validation."""

import importlib
import json
import threading
from types import SimpleNamespace

import pytest
from test_standing_dispatch_owner import (
    database_state,
    ledger_counts,
)
from test_standing_dispatch_owner import (
    native as native,
)

from packages.contracts.standing_work import StandingUnavailable


class NativeSeconds:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value


class AfterWriteReadPulse:
    """Signal only after real DML and a real subsequent control-state read.

    This models cancellation or elapsed native deadline during the final
    metadata I/O. It does not replace the transaction, a row or a native result.
    """

    def __init__(self, connection, event, seconds, signal):
        self.connection = connection
        self.event = event
        self.seconds = seconds
        self.signal = signal
        self.before = database_state(connection)
        self.written = False
        self.fired = False
        self.in_transaction_at_pulse = False
        self.at_pulse = None

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, statement, args=()):
        cursor = self.connection.execute(statement, args)
        normalized = " ".join(statement.upper().split())
        verb = normalized.split(None, 1)[0]
        if verb in {"UPDATE", "INSERT", "DELETE", "REPLACE"}:
            self.written = True
        if (
            self.written
            and not self.fired
            and verb == "SELECT"
            and "FROM AGENT_CONTROL_STATE" in normalized
        ):
            self.fired = True
            self.in_transaction_at_pulse = self.connection.in_transaction
            self.at_pulse = database_state(self.connection)
            assert self.in_transaction_at_pulse
            assert self.at_pulse != self.before
            if self.signal == "cancel":
                self.event.set()
            elif self.signal == "deadline":
                self.seconds.value = 10.01
        return cursor


@pytest.mark.parametrize("operation", ["activate", "renew"])
@pytest.mark.parametrize("signal", ["control", "cancel", "deadline"])
def test_final_metadata_io_cannot_commit_after_cancellation_or_absolute_deadline(
    native, monkeypatch, operation, signal
):
    owner = native.owner()
    if operation == "activate":
        owner.renew(native.renewal())
        request = native.activation(owner)
    else:
        request = native.renewal()
    module = importlib.import_module("services.session.standing_dispatch")
    seconds = NativeSeconds()
    # Only this native module's elapsed clock is controlled. asyncio, SQLite,
    # the standard time module and other service clocks remain untouched.
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=seconds.monotonic))
    cancelled = threading.Event()
    connection = native.store.db
    pulse = AfterWriteReadPulse(connection, cancelled, seconds, signal)
    native.store.db = pulse
    result = failure = None
    try:
        try:
            result = getattr(owner, operation)(request, deadline=10.0, cancelled=cancelled.is_set)
        except Exception as error:
            failure = error
        after = database_state(connection)
        facts = {
            "operation": operation,
            "signal": signal,
            "real_post_dml_read_fired": pulse.fired,
            "real_transaction_at_pulse": pulse.in_transaction_at_pulse,
            "rows_changed_at_pulse": pulse.at_pulse != pulse.before,
            "cancelled_at_return": cancelled.is_set(),
            "native_elapsed_clock_at_return": seconds.value,
            "failure_type": type(failure).__name__ if failure is not None else None,
            "native_result_type": type(result).__name__ if result is not None else None,
            "database_unchanged": after == pulse.before,
            "native_counts_after": ledger_counts(connection),
            "in_transaction_after": connection.in_transaction,
        }
        print(json.dumps(facts, sort_keys=True), flush=True)
        assert pulse.fired, "The intended post-DML native validation boundary was not reached"
        assert pulse.in_transaction_at_pulse
        assert pulse.at_pulse != pulse.before
        assert not connection.in_transaction
        if signal == "control":
            assert failure is None
            assert result.validate_request(request) == result
            assert after != pulse.before
        else:
            assert isinstance(failure, StandingUnavailable), facts
            assert result is None
            assert after == pulse.before
    finally:
        native.store.db = connection
