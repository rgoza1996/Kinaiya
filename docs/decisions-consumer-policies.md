# Independent Decisions consumer policies (#566, #567)

This increment adds **read-only configuration inspection** for an optional Agent
advisory policy. Request execution, an Agent client, protected Options writes and
the complete service/resource/external-agent matrix remain unfinished. Neither
issue is complete. A policy declaration never activates Agent or Decisions.

## Ownership and compatibility

| Setting | Owner | Existing/new source |
| --- | --- | --- |
| Shared provider configuration and credential references | Core | Existing provider catalog |
| Converse finalized/window routing preferences | Decisions, Converse consumer | Existing `decisions_prompt`, unchanged |
| Converse classifier selection and protected receipts | Decisions, Converse consumer | Existing `decisions-model.sqlite`, unchanged |
| Optional Agent advisory instructions/model selection | Decisions, Agent consumer | New host `decisions_agent_policy` snapshot |
| Agent execution model, auxiliary models and permissions | Agent | Existing owners, unchanged |

`decisions_agent_policy` is absent/null by default. It never falls back to
Converse's classifier, the Agent execution model or an auxiliary default. An
invalid optional declaration reports `invalid_host` through its own inspection
endpoint without replacing the valid Converse settings. The instruction string
is preserved exactly, including whitespace and an empty preference. It has an
explicit 8,192-byte UTF-8 resource bound and rejects NUL/invalid Unicode.

Example with a synthetic catalog binding (replace all three identity fields
with the exact observed Core generation):

```json
{
  "decisions_agent_policy": {
    "contract_version": 1,
    "consumer": "agent",
    "protocol": "measured_agent_advice_v1",
    "catalog_binding": {
      "id": "synthetic-agent-advisor",
      "generation": 1,
      "configuration_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    },
    "reasoning": "none",
    "instructions": "Provide advice for the supplied Agent context.",
    "content_scope": "local_only"
  }
}
```

The proposed `measured_agent_advice_v1` protocol is separate from Converse's
finalized/window label formats. Its declaration does not advertise an executable
capability. Generic measured-text catalog inspection currently accepts the
existing LM Studio and Meta adapters with their existing reasoning/context
constraints; no adapter, credential or model connection is opened by inspection.
The configured instruction disclosure scope must also permit the declared
provider. A catalog binding retains its exact generation/configuration digest;
rotation or retirement can make it unavailable without selecting another model.

## Native inspection

`GET /v1/decisions/options/agent-policy` requires **owner** authentication and
returns a closed version-1 `DecisionsAgentPolicyState` with `Cache-Control:
no-store`. It includes the active launch snapshot and its canonical SHA-256,
the source (`host`, `unconfigured`, `invalid_host`), and the current metadata-only
catalog observation (`eligible`, `unavailable`, `not_selected`). Invalid raw host
values and underlying exception details are never reflected in the response.

`eligible` means the exact declaration passes metadata/protocol and instruction
disclosure checks. It does not prove a usable credential, reachable provider,
available resource lease, model output or task success. Every response explicitly
sets `observation: catalog_metadata_only`, `change_boundary: restart_required`
and `request_admission: unavailable`. Inspection does not write settings, reset a
saved Converse selection, migrate receipts, or probe inference. Host edits take
effect at the next Decisions process start; current catalog retirement is
observed on each read.

## Remaining integration

The [native Agent upload-admission increment](decisions-agent-admission.md)
adds current-task/source/disclosure checks and one coherent SQLite snapshot.
It supplies a prerequisite for the eventual measured provider callback and does
not enable advice request execution.

Before enabling requests, wire native admission into each actual provider upload,
deliver complete qualified history, and retain measured prompts, original-operation
ownership, exact recovery after an uncertain response, bounded retention and
confirmed cleanup. Preserve raw model output;
the optional advice cannot grant permissions, execute a handoff, determine task
success or impose Converse's routing format. Agent startup must remain usable
when this optional consumer is absent or unavailable. Required Converse readiness
remains tied to its original classifier runtime.

Protected per-consumer persistence, Options navigation/migration and real
multi-consumer failure/recovery qualification remain part of #566/#567. External
Agent adapters remain tracked in #547. This document does not claim their
integration or model quality qualification.
