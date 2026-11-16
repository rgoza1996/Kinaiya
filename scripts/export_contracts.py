"""Emit language-neutral schemas used by Python and Flutter clients."""

import json
from pathlib import Path

from pydantic import TypeAdapter

from packages.contracts import (
    agent_control,
    agent_export,
    agent_submit,
    authorizations,
    auxiliary_attempts,
    auxiliary_routes,
    capability_settings,
    capture_modes,
    companion,
    context,
    context_usage,
    converse_models,
    converse_prompts,
    converse_setup,
    core_subsystems,
    decisions,
    decisions_attempts,
    decisions_models,
    decisions_prompts,
    journal_wake,
    live_captures,
    live_decisions,
    live_decisions_attempts,
    live_decisions_pending,
    live_effects,
    live_readiness,
    live_streams,
    live_windows,
    media,
    memory,
    memory_catalog,
    memory_context,
    memory_operations,
    models,
    standing_admission,
    standing_closure,
    standing_dispatch,
    standing_execution,
    standing_export,
    standing_resource_finalization,
    standing_resources,
    standing_work,
    stt_models,
    stt_settlement,
    subsystem_readiness,
    task_context,
    tool_permissions,
    transcript_scan,
    tts_models,
    whistle_stream_owner,
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

# Journal wake hints are separate from transcript/publication schemas.
for name in ("JournalWakeCursor", "JournalWakeWait", "JournalWakeObservation"):
    cls = getattr(journal_wake, name)
    (target / f"{name}.json").write_text(json.dumps(cls.model_json_schema(), indent=2) + "\n")

# Five-outcome labels/bindings never change the frozen four-outcome v1 models.
for name in ("LiveWindowLabels", "LiveWindowDecision"):
    cls = getattr(live_decisions, name)
    (target / f"{name}.json").write_text(json.dumps(cls.model_json_schema(), indent=2) + "\n")

# Atomic source/capture pairs have their own version, without changing v1 transcript bytes.
for name in (
    "LiveWindowPublication",
    "LiveWindowPublicationRef",
    "LiveWindowScope",
    "LiveWindowCapture",
    "LiveWindowPublicationProof",
    "LiveWindowPublicationState",
    "LiveWindowCandidate",
):
    cls = getattr(live_windows, name)
    (target / f"{name}.json").write_text(json.dumps(cls.model_json_schema(), indent=2) + "\n")

# Native capture permission has no model/provider or physical-stop authority.
for name in (
    "LiveCaptureBackend",
    "LiveCaptureRegistration",
    "LiveCaptureReceipt",
    "LiveCaptureFence",
    "LiveCaptureFenceReceipt",
    "LiveCaptureState",
    "LiveCaptureSourceState",
):
    cls = getattr(live_captures, name)
    (target / f"{name}.json").write_text(json.dumps(cls.model_json_schema(), indent=2) + "\n")

# Live inference has explicit v2 source/admission/result bindings; v1 stays frozen.
for name in (
    "LiveDecisionsSource",
    "LiveDecisionsAttemptLookup",
    "LiveDecisionsAttemptAdmit",
    "LiveDecisionsAttemptFinish",
    "LiveDecisionsAttempt",
    "LiveDecisionsAttemptState",
    "LiveDecisionsPublication",
    "LiveDecisionsOutcome",
    "LiveDecisionsPublicationState",
):
    cls = getattr(live_decisions_attempts, name)
    (target / f"{name}.json").write_text(json.dumps(cls.model_json_schema(), indent=2) + "\n")

# Bounded live discovery is a recheck hint, never inference or effect authority.
for name in (
    "LiveEffectQuery",
    "LiveEffectAdmit",
    "LiveEffectReceipt",
    "LiveEffectEvidence",
    "LiveEffectStart",
    "LiveEffectStarted",
    "LiveEffectFinish",
    "LiveEffectRecord",
    "LiveEffectState",
):
    cls = getattr(live_effects, name)
    (target / f"{name}.json").write_text(json.dumps(cls.model_json_schema(), indent=2) + "\n")

for name in ("LiveDecisionsPendingQuery", "LiveDecisionsPendingPage"):
    cls = getattr(live_decisions_pending, name)
    (target / f"{name}.json").write_text(json.dumps(cls.model_json_schema(), indent=2) + "\n")

(target / "LiveDecisionsReadiness.json").write_text(
    json.dumps(live_readiness.LiveDecisionsReadiness.model_json_schema(), indent=2) + "\n"
)
for name in (
    "LiveStreamCapability",
    "LiveStreamIdentity",
    "LiveStreamScope",
    "LiveStreamBegin",
    "LiveStreamPcm",
    "LiveStreamFinish",
    "LiveStreamCancel",
    "LiveStreamLookup",
    "LiveStreamProof",
    "LiveStreamCancellation",
):
    cls = getattr(live_streams, name)
    (target / f"{name}.json").write_text(json.dumps(cls.model_json_schema(), indent=2) + "\n")

# Owner policy requests are strict frozen models, separate from journal events.
for cls in (
    capture_modes.CaptureModeSelection,
    capture_modes.CaptureModeChange,
    capture_modes.CaptureModeSnapshot,
    capture_modes.CaptureModeReceipt,
    capture_modes.CaptureModeLookup,
    subsystem_readiness.DecisionsReadiness,
    core_subsystems.CoreSubsystemSelection,
    core_subsystems.CoreSubsystemChange,
    core_subsystems.CoreSubsystemSnapshot,
    core_subsystems.CoreSubsystemReceipt,
    core_subsystems.CoreSubsystemLookup,
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
    converse_prompts.ConversePromptConfig,
    decisions_prompts.DecisionsPromptConfig,
    task_context.TaskContextSource,
    task_context.TaskContextRequest,
    task_context.TaskContextObservation,
    task_context.TaskContextResponse,
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
    decisions_attempts.DecisionsSource,
    decisions_attempts.DecisionsAttemptProvider,
    decisions_attempts.DecisionsAttemptLookup,
    decisions_attempts.DecisionsAttemptAdmit,
    decisions_attempts.DecisionsAttemptFinish,
    decisions_attempts.DecisionsAttempt,
    decisions_attempts.DecisionsAttemptState,
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
    standing_admission.StandingInitialProof,
    standing_admission.StandingTaskAdmission,
    standing_closure.StandingAdmissionClosure,
    standing_export.StandingExportRequest,
    standing_export.StandingExportAdmission,
    standing_resources.TaskResourceQuery,
    standing_resources.TaskResourceContext,
    standing_resources.StandingResourceReservation,
    standing_resources.StandingResourceReservationReceipt,
    standing_resources.StandingResourceObservation,
    standing_resource_finalization.StandingResourceBindingRequest,
    standing_resource_finalization.StandingResourceBindingReceipt,
    standing_resource_finalization.StandingResourceFinalizeRequest,
    standing_resource_finalization.StandingResourceFinalizeContext,
    standing_resource_finalization.StandingResourceFinalization,
    standing_resource_finalization.StandingResourceFinalizationReceipt,
    transcript_scan.TranscriptScanRequest,
    transcript_scan.TranscriptScanPage,
):
    (target / f"{cls.__name__}.json").write_text(
        json.dumps(cls.model_json_schema(), indent=2) + "\n"
    )

# Private capture records use their existing canonical key ordering.
for cls in (decisions.DecisionsCallStart, decisions.DecisionsCallFinish):
    (target / f"{cls.__name__}.json").write_text(
        json.dumps(cls.model_json_schema(), indent=2, sort_keys=True) + "\n"
    )

# Media has an exact concrete registry, including named generic roots.
for name, cls in media.MEDIA_SCHEMAS.items():
    (target / f"{name}.json").write_text(
        json.dumps(cls.model_json_schema(), indent=2, sort_keys=True) + "\n"
    )

for name, alias in media.MEDIA_SCHEMA_ALIASES.items():
    (target / f"{name}.json").write_text(
        json.dumps(TypeAdapter(alias).json_schema(), indent=2, sort_keys=True) + "\n"
    )

# Incremental ownership is versioned separately from unary STT settlement.
for name in (
    "WhistleStreamCapability",
    "WhistleStreamReservation",
    "WhistleStreamOriginal",
    "WhistleStreamAcknowledgment",
    "WhistleStreamProof",
    "ConverseStreamLookup",
    "ConverseStreamCapability",
    "ConverseStreamProof",
    "StreamPublication",
    "StreamPublicationProof",
):
    cls = getattr(whistle_stream_owner, name)
    (target / f"{name}.json").write_text(json.dumps(cls.model_json_schema(), indent=2) + "\n")
