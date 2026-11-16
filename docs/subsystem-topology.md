# Subsystem topology — implementation in progress (#566)

The owner's required graph is asymmetric: Converse requires Decisions;
Decisions and Agent can run without Converse. Agent can optionally consume a
separate Decisions policy. Core owns common resources, credentials, registry,
security and transport, and does not imply activation of every role. A mode
labelled **Converse only** means Converse + Decisions without Kinaiya Agent.

The draft implements configuration, launcher selection and a required Decisions
routing-runtime observation for Converse. CLI/TUI and the native Core Options
selector cover every valid mode. Independent consumer policies and the full
qualification matrix remain unfinished. The work
below is part of the same issue, not deferred acceptance or a completion claim.

## Configured selection

The `distribution` object accepts boolean `agent`, `converse` and `decisions`
selectors. Common provider profiles and credentials are not duplicated. The
five active configurations are:

| Mode | Agent | Converse | Decisions |
| --- | --- | --- | --- |
| Decisions alone | false | false | true |
| Agent alone | true | false | false |
| Agent + Decisions | true | false | true |
| Converse + Decisions | false | true | true |
| All three | true | true | true |

Converse=true / Decisions=false is invalid with either Agent value. Invalid
explicit selections are rejected before credential or child-process creation;
configuration bytes remain unchanged. All three false leaves only Core for
setup/off state, not a ready Converse or Agent execution mode.

For compatibility, missing `agent` defaults true, missing `converse` false, and
missing `decisions` follows Converse. Legacy managed Agent-only configuration
therefore remains Agent-only; legacy Converse-only configuration still requires
Decisions. Legacy setup that enables Converse explicitly enables Decisions and retains
its separate model/provider configuration.

Standard/Advanced setup chooses the graph before configuring providers. Disabled
Agent skips model/key discovery, pending credential actions, role-selection
persistence and connection probes. Converse and Decisions retain their own
explicit model/endpoint answers when another role changes. The existing
user-selected reuse shortcut still requires an independent Decisions model.
Noninteractive `--yes --services` accepts the six names documented in the Linux
installation guide and requires separate model arguments for selected roles.
Explicit selection does not activate a disabled role merely because its provider
declaration exists; legacy profile-implied selection remains compatible when the
new selector is omitted.

`setup services` now saves the next-launch graph in the protected Core selector,
preserving the original host configuration bytes, model declarations, credentials
and unrelated settings. It reports `activation: restart_required` and does not
manage or observe running processes. Cancel/EOF writes nothing. Full re-setup
retains an existing disabled Agent model, while fresh setup does not invent a
configured Agent. A conflicting saved Core selection refuses before a full
setup or Converse-section configuration write; change selection explicitly first.

## Core selection owner (draft)

`<data_dir>/core-subsystems.sqlite` is the single native mode override shared by
the local CLI and Session's owner-only `/v1/core/options/subsystems` API. Selection
is either `host` (a pointer to legacy `distribution` defaults, not a copied
configuration) or `saved` (one exact supported mode). Other settings and
credentials remain with their existing owners. No process or model is started
by reading or saving this owner. The [Options selector](core-subsystem-options.md)
uses its dedicated Session transport, exact original-request recovery and
source/next-launch status; it does not depend on Agent availability.

The strict `core_subsystems.v1` contract carries backend identity, monotonic
controller generation, random epoch, revision and a token of the observed
selection/host mode. A save compares those observations and retains its exact
request key and receipt in one SQLite commit. A local CLI peer joins the current
generation; a replacement Session controller advances it and fences old writes.
Original positive receipts remain historical observations. Exact lookup is
available at `/v1/core/options/subsystem-changes/lookup`. An unobserved request
is fenced only by a later native revision/generation/epoch; a host-file token
change alone cannot prove retirement because the host mode can change back.

The CLI persists its exact private request before attempting the save. If commit
acknowledgment is uncertain, reconcile it without retrying:

```sh
kinaiya setup services --lookup-service-change ORIGINAL_KEY --json
```

Lookup does not register a new controller, create or repair a store, or retry a
write. The owner retains at most 1,000 changes and refuses further writes at that
declared bound. Corrupt, aliased, replaced or insecure storage refuses without a
default/reset. Invalid or unavailable host defaults remain explicitly unknown;
an authorized saved selection can repair the selector without rewriting them.

Official start, managed `serve host`/`serve all`, Doctor and setup defaults read
the same source. Managed launch captures one graph before token/process entry
and passes that captured mode to every child. Later saves affect the next
managed launch; a child must not adopt a different graph midway through launch.
`startup_mode` reports the parent's declared capture, not a readiness observation;
standalone Session reports it unknown. `running_state` is always `not_observed`.
Session owns the original settings connection through its lifespan and retires
it even when another owner's shutdown raises.

The current focused/integration union passes 239 tests across ten files,
including six captured launch modes, exact lookup, native writer races,
uncertain commits, controller replacement, storage guards, Session cleanup,
Doctor, wizard and all 36 saved-mode transitions. The three mode-only test
families now inspect the official resolved selection and additionally assert
unchanged host bytes. Their previous 41 failures are retained as the planned
JSON-to-Core ownership contract migration, not hidden by changing runtime guards.

## Earlier setup qualification

The complete setup increment passes **406 related tests** across 20 files,
including every wizard caller and all 36 saved-mode transitions. The actual private CLI rehearsal passes
all six modes and two mode-only edits per mode: **18 original CLI processes**
exit zero with saved profiles and token bytes preserved. Source bytes stay fixed
through the run. [Retained helper, results and source pins](evidence/subsystem-setup-modes-2026-11-15/README.md)
define its setup-only scope; it does not complete the running-service matrix.
The compatibility follow-up retains the original compact first Agent-only
configuration shape and every assertion in eleven existing test files.

Without a saved Core override, `serve host` and `serve all` respect an existing `distribution` selection.
The developer `serve all` default without a `distribution` object still selects
all three roles. Core Session/resources are ordered before consumers, and
Decisions before Converse. Ordering is not proof that a required dependency is
ready. No service, provider or model is started by configuration parsing.

Explicit setup `decisions_profile`/`roles.decisions` enables Decisions even
without Converse (#619). Startup port planning, child-process commands and
doctor now use the same selected graph; an unused provider declaration or URL
does not activate another subsystem. Configured local service ports reach the
actual child listeners. Selected Decisions participates in port-conflict and
unavailable-peer checks. Invalid Converse-without-Decisions selections refuse
before credentials or child creation.

The public Decisions `GET /health/runtime` returns `routing_runtime_ready`, a boolean
derived from the same live-worker/configuration observation as the authenticated
contract below. Startup requires an exact true boolean and matching service
identity, rather than accepting a generic HTTP listener. This unauthenticated
diagnostic grants no authority and never substitutes for Converse's owned,
authenticated dependency monitor. This minimal endpoint cannot enter the model
lease/provider-health path used by the separate `/health` diagnostics. Doctor's
service diagnostic remains explicitly
a loopback TCP observation; it does not claim inference or runtime readiness.

The local six-mode CLI rehearsal starts the actual Session and selected Agent,
Decisions and Converse processes on private configured ports, using empty
synthetic journals. It verifies authenticated Decisions readiness, effective
Converse availability, the original process set and shutdown with every child
reaped. Dispatch policy is explicitly disabled when Kinaiya Agent is absent.
This covers startup/ordinary shutdown of all five valid modes plus Core-only;
managed-resource, external-Agent and complete failure/recovery qualification
remain required below. No model inference or audio is run in that rehearsal.

## Required Decisions runtime

All supported Converse launcher paths, including `serve converse`, attach an
owned monitor to `decisions_url` (default `http://127.0.0.1:8763`). Converse starts
unavailable until that monitor receives a positive authenticated response from
`GET /v1/decisions/readiness`. A listener and a valid process selection alone do
not satisfy this gate. The control plane stays available to report or repair a
degraded service. Embedded runtime objects without service wiring are internal
test/library objects, not evidence for a supported independent deployment.

The strict version-1 `DecisionsReadiness` contract contains the service and
Converse-consumer identities, a 32-hex application incarnation, phase and matching
boolean. Only owner and Converse credentials can read it. The response is
`no-store` and contains no transcripts, task data, credentials or model output.
Readiness requires the app lifespan, live workers with a successful journal tick,
and a valid selected model configuration. A failed tick, lost worker, corrupt
selection, shutdown or missing lifespan reports unavailable. Agent dispatch
being disabled does not disable Converse's routing-runtime readiness.

This endpoint reports **routing-runtime readiness**. It does not assert successful
inference, model/GPU capacity, provider reachability, model accuracy, Agent
availability, task acceptance or task success. Polling reads configuration and
worker state; it does not acquire an inference lease or invoke provider health.
The existing `/health` provider diagnostics and native per-operation refusals
remain separate. Decisions and Agent do not poll Converse and gain no new
required dependency.

The Converse monitor owns one HTTP client and one task, polls every 250 ms, and
bounds each complete response to one second and 4 KiB. It accepts strict JSON,
rejects compression, duplicate/unknown fields, invalid identities, redirects,
unauthenticated responses and inconsistent phases, and makes no transport retry.
Positive observations expire two seconds after the probe begins, so a slow body
cannot extend that allowance. A dead monitor is unavailable even before expiry.
HTTPS or the
configured trusted loopback transport remains responsible for endpoint identity;
this health observation is not an execution-authorization receipt.

Dependency loss, expiry followed by refresh, or a changed Decisions incarnation
advances Converse's existing availability generation. It cancels original text,
speech and native-ASR producers, retires old native clips, and fences playback
through the existing native stop/reconciliation path. Unknown publication or
cleanup is not rewritten as success. Recovery permits fresh admissions; old
captures do not become current again. An uncertain local fence remains
unavailable rather than being cleared by another positive GET. This does not
claim instantaneous remote cancellation or completed physical audio delivery.

A cancelled original ASR task returns a bounded native refusal to a still-live
observer, including an exact duplicate (#615). Cancelling only an observer still
propagates task cancellation without cancelling or replacing the original ASR.
Their race preserves that distinction; original evidence and uncertain journal
effects remain retained and ASR is never replayed. Teardown continues through
independent observers, runtime clients and model owners even if dependency close
itself refuses, while retaining that error.

Owner intent and resource availability remain independent. Resource Wake cannot
bypass the required dependency, recovery does not Wake a sleeping owner, and
dependency changes do not write the saved intent. Health/control responses add
`decisions: {required, ready, state}`. Existing 409 admission refusals identify
an unavailable Decisions runtime instead of misreporting an owner-off state.
Read-only settings and stop/reconciliation controls stay accessible.

Flutter validates the required state and the three-way effective-availability
invariant in both health and owner-control receipts. A valid Wake can confirm
the owner's choice while Decisions keeps execution unavailable; it is not an
unknown write. Invalid, contradictory or unknown dependency data fails closed.
An outage retires existing local audio owners without representing it as
intentional Sleep; recovery does not automatically rearm capture. Legacy
responses without the additive dependency object keep their existing validation.

Verification includes the original missing-endpoint regression, strict malformed
and bounded-body controls, held journal-worker failure/recovery, cancelled
original native ASR/replies, stale captures, owner/resource independence, real
Decisions shutdown/restart across HTTP and the real CLI start-paused scenario.
Controlled providers establish framework behavior only; they are not native
model-quality or hardware playback qualification.

Local checks for this increment: **1,212 related Python tests** passed across 38
files, with the existing Starlette deprecation warning; **1,478 Flutter tests**
passed with process exit 0, including 13 new health/control regressions. Flutter
analysis passed. The original missing endpoint, cancelled-original-ASR, old
process-fixture and client-invariant failures remain retained under local
`output/issue-566-readiness/`; fixture-only setup failures are recorded separately.
Fresh full CI for the resulting draft head remains required.

The first full Python 3.14 run found two outdated fixture assumptions (#617):
effective Converse availability without an observed Decisions runtime, and an
aggregate schema inventory that included the new readiness record. Both original
failures reproduce locally. The corrected setup oracle preserves the unavailable
dependency while separately confirming owner Sleep/Wake revisions, setup refusal,
opaque-alias privacy and zero model-network calls. All 517 existing schema files
are byte-identical to main `abb0f87f`; the new readiness schema has its own exact
byte pin and the original 184-schema count/digest remains unchanged. The five
focused setup, schema, topology and readiness files pass 194 tests. This does not
complete the broader matrix below or replace full CI of the resulting head.

## Remaining full-issue acceptance

- Qualify required-runtime startup, degradation, shutdown, recovery and retained
  producer/physical cleanup across the complete actual-service mode matrix,
  including the managed-resource and external-Agent arrangements. The monitor
  and selected service-boundary cases do not complete that full acceptance.
- Decide and implement separate consumer policy/profile ownership for Converse
  and optional Agent use. Shared provider/model resources must not force shared
  prompts, routing policy or lifecycle. A process-selection flag does not claim
  that an Agent consumer adapter exists.
- The Core Options selector is implemented and qualified separately above.
  Its saved next-launch state and declared startup selection remain distinct
  from actual running-service readiness and the lifecycle matrix.
- Specify and verify scoped events, native handoff, queue/steer/interrupt,
  external-agent independence (#547), and failure behavior without a dependency
  cycle or a silent Converse bypass.
- Verify the full startup/degraded/recovery matrix through actual service
  boundaries. Pure selection/CLI tests do not establish running-service health.

Framework contracts remain separate from editable model behavior (#557).
