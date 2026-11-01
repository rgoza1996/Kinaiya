"""Emit language-neutral schemas used by Python and Flutter clients."""

import json
from pathlib import Path

from packages.contracts import (
    agent_control,
    agent_export,
    agent_submit,
    authorizations,
    auxiliary_attempts,
    auxiliary_routes,
    capability_settings,
    companion,
    context,
    context_usage,
    converse_models,
    converse_setup,
    decisions,
    decisions_models,
    memory,
    memory_catalog,
    memory_context,
    memory_operations,
    models,
    standing_dispatch,
    standing_execution,
    standing_work,
    stt_models,
    stt_settlement,
    tool_permissions,
    tts_models,
    workspace_settings,
)

target = Path(__file__).resolve().parents[1] / "packages/contracts/schemas"
target.mkdir(parents=True, exist_ok=True)
for module in (
    models,
    companion,
    memory,
    memory_catalog,
    memory_context,
    memory_operations,
    context,
    agent_export,
    agent_submit,
):
    for name in dir(module):
        cls = getattr(module, name)
        if isinstance(cls, type) and issubclass(cls, models.WireModel):
            (target / f"{name}.json").write_text(
                json.dumps(cls.model_json_schema(), indent=2) + "\n"
            )

# This owner document uses its explicit contract_version instead of the legacy
# event schema_version. Nested setup types are included in the root's $defs.
(target / "ConverseSetup.json").write_text(
    json.dumps(converse_setup.ConverseSetup.model_json_schema(), indent=2) + "\n"
)

# These strict, immutable control types deliberately require every wire field;
# they do not inherit the legacy event defaults from WireModel.
for name in (
    "AgentModeChange",
    "AgentControlSnapshot",
    "AgentModeReceipt",
    "AgentModeLookupResult",
    "AgentTaskIdentity",
    "AgentSteerChange",
    "AgentSteerSource",
    "AgentSteerAdmissionRequest",
    "AgentSteerReceipt",
    "AgentSteerLookupResult",
):
    cls = getattr(agent_control, name)
    (target / f"{name}.json").write_text(json.dumps(cls.model_json_schema(), indent=2) + "\n")

# Owner policy requests are strict frozen models, separate from journal events.
for cls in (
    context_usage.ContextCatalogBinding,
    context_usage.ContextModel,
    context_usage.ContextScope,
    context_usage.ContextMeasurement,
    context_usage.ContextSelection,
    context_usage.ContextObservation,
    context_usage.ContextObservationRow,
    context_usage.ContextUsageSnapshot,
    authorizations.AuthorizationIdentity,
    authorizations.AuthorizationRevoke,
    authorizations.AuthorizationRevokeReceipt,
    authorizations.AuthorizationLookup,
    authorizations.AuthorizationJournalProof,
    tool_permissions.PermissionScope,
    tool_permissions.ToolPermissionChange,
    tool_permissions.NativePermissionTarget,
    tool_permissions.MCPPermissionTarget,
    tool_permissions.NativePermissionBinding,
    tool_permissions.MCPPermissionBinding,
    tool_permissions.ToolPermissionTargetChange,
    auxiliary_routes.AuxiliaryRouteScope,
    auxiliary_routes.AuxiliaryRouteCandidate,
    auxiliary_routes.AuxiliaryRoute,
    auxiliary_routes.AuxiliaryRouteChange,
    auxiliary_attempts.AttemptRoute,
    auxiliary_attempts.AuxiliaryAttempt,
    converse_models.ConverseModelSelection,
    converse_models.ConverseModelChange,
    converse_models.RoleModelPin,
    stt_models.STTModelSelection,
    stt_models.STTModelChange,
    models.STTModelPin,
    tts_models.TTSModelSelection,
    tts_models.TTSModelChange,
    tts_models.TTSModelPin,
    tts_models.TTSSynthesisAdmission,
    stt_settlement.STTWorkerReservation,
    stt_settlement.STTWorkerOperation,
    stt_settlement.STTWorkerProof,
    stt_settlement.STTPublication,
    stt_settlement.STTWorkerBinding,
    stt_settlement.STTAdmissionReconcile,
    stt_settlement.STTAdmissionInventory,
    stt_settlement.STTAdmissionResult,
    decisions_models.DecisionsModelSelection,
    decisions_models.DecisionsModelChange,
    decisions_models.DecisionsModelPin,
    workspace_settings.WorkspaceSelection,
    workspace_settings.WorkspaceChange,
    workspace_settings.WorkspacePin,
    capability_settings.ToolAvailabilityAction,
    capability_settings.SkillActivationAction,
    capability_settings.PluginActivationAction,
    capability_settings.CapabilityChange,
    standing_work.StandingTaskSession,
    standing_work.StandingQuietHours,
    standing_work.StandingDelivery,
    standing_work.DailySchedule,
    standing_work.StandingAuthorization,
    standing_work.StandingChange,
    standing_work.StandingChangeReceipt,
    standing_work.StandingRecord,
    standing_work.StandingCurrent,
    standing_work.StandingResponsibilitySnapshot,
    standing_work.StandingChangeLookup,
    standing_work.StandingResponsibilityPage,
    standing_work.DailyOccurrence,
    standing_dispatch.StandingProducerRenewal,
    standing_dispatch.StandingProducerReceipt,
    standing_dispatch.StandingDispatchState,
    standing_dispatch.StandingDispatchActivation,
    standing_dispatch.StandingDispatchActivationReceipt,
    standing_dispatch.StandingDispatchActivationLookup,
    standing_dispatch.StandingDispatchStop,
    standing_dispatch.StandingDispatchStopProof,
    standing_execution.StandingOccurrence,
    standing_execution.StandingExecutionModelPin,
    standing_execution.StandingExecutionTask,
    standing_execution.StandingExecutionIntent,
    standing_execution.StandingExecutionClaim,
    standing_execution.StandingExecutionClaimAdmission,
):
    (target / f"{cls.__name__}.json").write_text(
        json.dumps(cls.model_json_schema(), indent=2) + "\n"
    )

# Private capture records use their existing canonical key ordering.
for cls in (decisions.DecisionsCallStart, decisions.DecisionsCallFinish):
    (target / f"{cls.__name__}.json").write_text(
        json.dumps(cls.model_json_schema(), indent=2, sort_keys=True) + "\n"
    )
