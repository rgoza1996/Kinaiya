# Subsystem topology — implementation in progress (#566)

The owner's required graph is asymmetric: Converse requires Decisions;
Decisions and Agent can run without Converse. Agent can optionally consume a
separate Decisions policy. Core owns common resources, credentials, registry,
security and transport, and does not imply activation of every role. A mode
labelled **Converse only** means Converse + Decisions without Kinaiya Agent.

This change starts the implementation with configuration and launcher selection.
It does **not yet complete** required dependency readiness/degraded/recovery
enforcement or independent Decisions consumer policies. The remaining work below
is part of the same issue, not deferred acceptance or a completion claim.

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
Decisions. Setup that enables Converse explicitly enables Decisions and retains
its separate model/provider configuration.

`serve host` and `serve all` both respect an existing `distribution` selection.
The developer `serve all` default without a `distribution` object still selects
all three roles. Core Session/resources are ordered before consumers, and
Decisions before Converse. Ordering is not proof that a required dependency is
ready. No service, provider or model is started by configuration parsing.

## Remaining full-issue acceptance

- Required Decisions startup readiness, live degradation, shutdown and recovery
  must gate actual Converse execution, including individually launched services.
  The existing independent supervisor alone does not enforce that requirement.
- Decide and implement separate consumer policy/profile ownership for Converse
  and optional Agent use. Shared provider/model resources must not force shared
  prompts, routing policy or lifecycle. A process-selection flag does not claim
  that an Agent consumer adapter exists.
- Expose the complete mode selection in installer/TUI and Options, preserving
  saved/effective state, protected writes and existing source/receipt semantics.
- Specify and verify scoped events, native handoff, queue/steer/interrupt,
  external-agent independence (#547), and failure behavior without a dependency
  cycle or a silent Converse bypass.
- Verify the full startup/degraded/recovery matrix through actual service
  boundaries. Pure selection/CLI tests do not establish running-service health.

Framework contracts remain separate from editable model behavior (#557).
