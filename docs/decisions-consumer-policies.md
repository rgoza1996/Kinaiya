# Independent Decisions consumer policies (#566, #567)

Decisions owns the optional Agent advice policy separately from the Converse
classifier. A policy declaration applies to the next launch of selected roles.
Core still owns which roles launch.

## Ownership and compatibility

| Setting | Owner | Existing/new source |
| --- | --- | --- |
| Shared provider configuration and credential references | Core | Existing provider catalog |
| Converse finalized/window routing preferences | Decisions, Converse consumer | Existing `decisions_prompt`, unchanged |
| Converse classifier selection and protected receipts | Decisions, Converse consumer | Existing `decisions-model.sqlite`, unchanged |
| Optional Agent advisory instructions/model selection | Decisions, Agent consumer | Protected `decisions-agent-policy.sqlite` override and legacy host declaration |
| Agent execution model, auxiliary models and permissions | Agent | Existing owners, unchanged |

`decisions_agent_policy` is absent/null by default. It is the legacy host
source for Agent advice. An invalid declaration reports `invalid_host` in the
host observation. The instruction string preserves whitespace and empty
preferences. It has an 8,192-byte UTF-8 resource bound and rejects NUL or
invalid Unicode.

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

## Protected settings

The protected store starts with a host pointer. Saving a policy stores one
complete declaration and its exact Core catalog binding. Explicit disable
overrides even a configured host policy. Reset restores the host pointer.
The SQLite owner commits the selection and the idempotent save receipt
together. Historical receipts retain request and policy digests and source
metadata. The current selection retains the instruction text.

All three settings methods require the owner credential and return
`Cache-Control: no-store`.

| Method and path under `/v1/decisions/options` | Contract |
| --- | --- |
| `GET /agent-advice-settings` | `DecisionsAgentPolicySettingsSnapshot` with legacy host declaration, policy captured at this Decisions launch, and saved next-launch selection |
| `POST /agent-advice-settings` | `DecisionsAgentPolicyChange` to exact `DecisionsAgentPolicyReceipt` or a bound `DecisionsAgentPolicyRefusal` with HTTP 400 |
| `POST /agent-advice-settings/lookup` | Exact original `DecisionsAgentPolicyChange` to `DecisionsAgentPolicyLookup` |

Writes compare the backend, Decisions settings instance, and revision.
An equal original key and digest can observe its historical receipt. A
different digest under the same key conflicts. A missing receipt alone leaves
the original uncertain. A newer revision or replacement settings instance
can permanently fence an unobserved original. The store retains 1,000 saves.
The API accepts identity JSON up to 64 KiB with a five-second body deadline.
Owner authentication is checked again after the body is read. Responses are
bounded at 160 KiB.

The native owner can refuse a selected catalog declaration before
persistence. That response carries the exact original request digest
and versioned reason `selection_unavailable`. The client releases the
original after validating the refusal and confirming transport cleanup.

## Options

Open **Options → Decisions → Agent advice** to inspect the active policy
and the selection for the next launch. Each observation can show its
instructions, reasoning setting, disclosure scope and exact Core model
generation. The editor offers the host policy, explicit disable, and a
saved policy referencing an existing Core profile. A retired generation
stays visible in the policy until the owner selects a current profile.

A save compares the observed Decisions backend, instance and revision.
Drafts stay associated with the observed revision across navigation.
When the revision changes, the owner can explicitly adopt the current
policy before saving. An uncertain dispatched request keeps its exact
original in memory across same-owner reconnect. **Check original save**
looks up that original. A matching receipt or a native fence releases it.

Restart Agent and Decisions to apply saved selection on a combined launch.
The running Decisions instance holds its captured policy until it stops.
Agent reads the protected selection at startup to determine whether to
advertise the optional capability. Settings access failure disables the
optional launch route and leaves independent Agent work and the Converse
classifier available under their respective owners.

`GET /agent-policy` retains the original version-1 legacy host projection.
Its `request_admission: unavailable` is part of that metadata contract.
Active policy and current catalog status appear in the new settings snapshot.
`eligible` describes catalog, protocol and instruction disclosure metadata.
The owner runtime endpoint separately reports availability for enrollment.

## Optional runtime

The [native Agent upload-admission increment](decisions-agent-admission.md)
adds current-task/source/disclosure checks and one coherent SQLite snapshot.
The [internal operation owner](decisions-agent-operations.md) binds that
callback to measured execution, exact recovery and actual cleanup. Selecting
both Agent and Decisions with a resolved launch policy enables the optional route
for ordinary [Agent workers](decisions-agent-advice-client.md). The owner can
inspect runtime and catalog availability through the separate
`GET /v1/decisions/options/agent-advice-runtime` endpoint.

Advice returns raw, untrusted model text. Native Agent retains ownership of
permissions, handoffs, and task success. Agent starts independently when the
optional consumer is absent or unavailable. Required Converse readiness remains
tied to its original classifier runtime.

The complete multi-consumer failure and recovery matrix remains part
of #566/#567. External Agent adapters remain tracked in #547.

The thirteen-file native suite passed 374 tests on Python 3.13.18 and
3.14.10. The final 28-case focused run on each interpreter covers a lost
save response with exact receipt recovery, retired catalog generations,
maximum-size instructions, reauthentication during upload, and selection
across service restarts. The export added six settings contracts and
reproduced all 614 prior schemas byte for byte.

The Options suite passed 1,566 Flutter tests with six live-service
cases skipped for their environment requirements. Flutter analysis
and the release web build passed. The affected native suite passed
132 tests on each of Python 3.13.18 and 3.14.10. The loopback Dart and
Python check verified complete 8,192-byte instructions, a lost save
response, same-owner reconnect, one durable receipt, and the exact
native refusal. The refusal contract added one schema with all 620
previous schemas preserved.
