# ADR 0014: Separate owner authentication from inference disclosure

Status: accepted experimental client boundary; fixes #35 and tracks #8/#13.
Roadmap 14.1–14.2 and the independent-runtime invariants remain authoritative.

Flutter previously combined Agent and Converse health into one flag. A healthy
service could enable submission to the other service even when that role's health
or provider metadata was unavailable. An absent `local` field also escaped the
cloud-disclosure check.

Each connection starts with both inference roles blocked. An authenticated session
request enables deterministic owner controls. Independently successful service
checks must provide a boolean `local` and nonempty string provider/model identifiers
before that role earns inference permission. The local profile accepts verified
local roles; Contributor accepts local roles or exactly `meta` /
`muse-spark-1.3-contributor`; general cloud development accepts verified cloud
metadata. Missing, malformed, unavailable and unconsented roles fail closed without
disabling the other authorized role.

Plain Agent input, steering, explicit skill invocations and commands with unknown
execution classes require Agent permission. The canonical registry marks both
`/skill` and dynamic skill commands as `skill_invocation`. Status, stop and
action-specific approval remain owner-authenticated deterministic controls.
A `/model` mutation clears the old grant before dispatch; an acknowledged successful
switch followed by a fresh positive provider check restores it. A lost acknowledgment
leaves inference blocked until revalidation.

Converse text, recording uploads, microphone input and queued synthesis require
Converse permission. Awaited session/history, recording and playback operations
recheck connection ownership and permission before submission. A recording completed
after reconnect cannot upload to the replacement backend. Converse still treats
slash-like text as ordinary conversation.

The first-party speech bridge carries an internal service credential. Its URL is
restricted to a loopback HTTP origin; it bypasses ambient proxies and refuses
redirects. Remote speech adapters require their own scoped credentials and explicit
disclosure integration, which this local bridge does not implement.

## Verification and limits

`provider_consent_test.dart` exercises real client verification over synthetic HTTP
responses and widget submission paths: mixed roles, malformed metadata, exact
Contributor matching, Enter/buttons, deterministic controls, uncertain model switches
and delayed audio/history. Counters assert zero downstream inference for denied
input. Command-registry integration tests verify execution classification.
Speech-boundary tests exercise rejected URLs, redirects and a real local HTTP server
with hostile ambient proxy settings. No paid inference is needed.

Provider metadata is an authenticated service declaration, not proof of a remote
host's actual routing or every downstream provider. Backend provider grants,
Decisions configuration, source disclosure policy and action authorization remain
separate trusted boundaries. This client check does not revoke already submitted
work, grant access, establish multi-user security or complete production hardening.
