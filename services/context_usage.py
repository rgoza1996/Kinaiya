"""Bounded process-local observations; inspection performs no inference or counting."""

import sqlite3
from collections import deque
from datetime import UTC, datetime
from threading import RLock
from types import SimpleNamespace
from uuid import uuid4

from packages.contracts.context_usage import (
    MAX_COUNT,
    ContextMeasurement,
    ContextModel,
    ContextObservation,
    ContextObservationRow,
    ContextScope,
    ContextSelection,
    ContextUsageSnapshot,
)
from packages.providers.prompt_budget import PromptTokenCount


def captured_model(provider, pin=None):
    """Only captured native declarations; never inspect endpoints or credentials."""
    info = provider.info
    if pin is not None and (pin.provider, pin.model, pin.local) != (
        info.provider,
        info.model,
        info.local,
    ):
        raise ValueError("Captured model identity changed")
    return ContextModel(
        source=(
            "unmanaged" if pin is None else "catalog" if pin.catalog_binding is not None else "host"
        ),
        provider=info.provider,
        model=info.model,
        local=info.local,
        reasoning=None if pin is None else pin.reasoning,
        settings_revision=None if pin is None else pin.settings_revision,
        catalog_revision=None if pin is None else pin.catalog_revision,
        catalog_binding=None
        if pin is None or pin.catalog_binding is None
        else pin.catalog_binding.model_dump(mode="json"),
        profile_id=None if pin is None else getattr(pin, "profile_id", None),
    )


class ContextObservations:
    def __init__(self, role):
        self.role, self.instance = role, uuid4().hex
        self._rows = deque(maxlen=128)
        self._sequence = 0
        self._lock = RLock()

    @staticmethod
    def new_operation():
        return uuid4().hex

    def record(self, *, operation_id, phase, scope, model, measurement, selection):
        # Observability must not change inference admission. Invalid/oversized
        # metadata is omitted; it never becomes guessed usage or source text.
        try:
            return self._record(
                operation_id=operation_id,
                phase=phase,
                scope=scope,
                model=model() if callable(model) else model,
                measurement=measurement,
                selection=selection,
            )
        except (TypeError, ValueError):
            return None

    def _record(self, *, operation_id, phase, scope, model, measurement, selection):
        if type(measurement) is not PromptTokenCount or not measurement.fits:
            return None
        with self._lock:
            if self._sequence >= MAX_COUNT:
                return None
            row = ContextObservation(
                schema_version="1.0",
                id=uuid4().hex,
                operation_id=operation_id,
                sequence=self._sequence + 1,
                role=self.role,
                phase=phase,
                observed_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                scope=ContextScope.model_validate(scope),
                model=ContextModel.model_validate(model),
                measurement=ContextMeasurement(
                    input_tokens=measurement.input_tokens,
                    context_tokens=measurement.context_tokens,
                    reserved_output_tokens=measurement.reserved_output_tokens,
                    headroom_tokens=measurement.context_tokens
                    - measurement.input_tokens
                    - measurement.reserved_output_tokens,
                    request_sha256=measurement.request_sha256,
                    binding_sha256=measurement.binding_sha256,
                ),
                selection=ContextSelection.model_validate(selection),
            )
            self._rows.append(row)
            self._sequence = row.sequence
            return row

    def snapshot(self, *, measurement_mode, configuration_status):
        with self._lock:
            rows, sequence, retained = (
                tuple(reversed(self._rows))[:32],
                self._sequence,
                len(self._rows),
            )
        result = ContextUsageSnapshot(
            schema_version="1.0",
            role=self.role,
            instance_id=self.instance,
            latest_sequence=sequence,
            retained_count=retained,
            storage="process_memory",
            measurement_mode=measurement_mode,
            observations=tuple(
                ContextObservationRow(
                    observation=row, configuration_status=configuration_status(row)
                )
                for row in rows
            ),
        )
        return result.model_dump(mode="json")


def agent_context_snapshot(runtime):
    cache = {}

    def status(row):
        if row.model.source == "unmanaged" or runtime.model_settings is None:
            return "unknown"
        harness = row.scope.harness_version
        if harness not in cache:
            try:
                info, pin = runtime.model_settings.selection_info(harness)
                cache[harness] = captured_model(SimpleNamespace(info=info), pin)
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                cache[harness] = None
        current = cache[harness]
        if current is None:
            return "unknown"
        return (
            "current"
            if current.model_dump(exclude={"catalog_revision"})
            == row.model.model_dump(exclude={"catalog_revision"})
            else "obsolete"
        )

    return runtime.context_observations.snapshot(
        measurement_mode="measured" if runtime.context_observations._sequence else "unknown",
        configuration_status=status,
    )


def converse_context_snapshot(runtime):
    current = None
    if runtime.model_settings is not None:
        try:
            snapshot = runtime.model_settings.snapshot()
            info, selection = snapshot["selected_info"], snapshot["selection"]
            if info is not None:
                current = ContextModel(
                    source=selection["source"],
                    provider=info["provider"],
                    model=info["model"],
                    local=info["local"],
                    reasoning=snapshot["selected_reasoning"],
                    settings_revision=snapshot["revision"],
                    catalog_binding=selection["catalog_binding"],
                    catalog_revision=0 if selection["catalog_binding"] is not None else None,
                    profile_id=None,
                )
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            pass

    def status(row):
        if current is None or row.model.source == "unmanaged":
            return "unknown"
        return (
            "current"
            if current.model_dump(exclude={"catalog_revision"})
            == row.model.model_dump(exclude={"catalog_revision"})
            else "obsolete"
        )

    return runtime.context_observations.snapshot(
        measurement_mode="measured" if runtime.context_budgeted else "legacy_unmeasured",
        configuration_status=status,
    )
