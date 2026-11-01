# 0090: Scoped owner rules for native tool admission

October 31, 2026. Implementation under review for
[#208](https://github.com/rgoza1996/Kinaiya/issues/208). This increment does not
close the full precise-permissions or Options delivery groups.

## Scope and authority

The initial targets are the exact native `read_file`, `write_file` and `shell`
operations. Owner rules select Allow, Ask, Deny or inheritance for one target
and native scope. Precedence is task objective revision, session/thread/epoch,
approved workspace, Agent default, then host configuration. Task and session
identity come from the native runtime and journal. Sessions originating from
Converse can scope Agent work without granting Converse execution authority.

These are operation rules. A `read_file` Deny does not prohibit reads by search,
context selection, a shell process, MCP or a plugin. Network, mounted filesystem
access, protected roots, read-only restrictions, isolation, disclosure and
uncommitted-speech authorization retain their existing owners and ceilings.
MCP and plugin rule targets remain unfinished in this increment.

Rules apply at the next operation admission. Previously obtained task context
remains available under the existing disclosure policy. A changed rule does not
erase that context, recall exported bytes, stop an already admitted command,
undo a write or establish the absence of remote effects.

## Persistence and native admission

`AgentToolPermissions` owns a protected SQLite store outside the worker
workspace. Native scope objects and rule targets are closed, typed and bounded.
The store retains at most 1,024 rules and 10,000 exact operation receipts. Full
capacity refuses a new operation without silently discarding unresolved state.
Saved retired scopes remain inspectable and their exact rules can be reset.
Reset changes only one rule and restores inheritance.

Changes carry the observed revision, native state fingerprint and a fresh
idempotency key. Historical exact replay is checked before current scope or CAS
validation and never reapplies a stale rule. New saves check current authority,
then commit the rule, incremented revision and receipt together without an
await inside the transaction. Receipts bind the original state/revision, exact
scope, tool, mode and key. Receipt lookup cannot launch work or change a rule.

The supervisor captures the effective decision and native permission
fingerprint for the exact action. The actual read/write/shell broker validates
its arguments and rechecks current authority synchronously before admission.
A shared invocation object is single-use and retired on lexical exit; copied
asynchronous contexts cannot extend its lifetime or use it for a different tool.
Rule Allow is recorded as owner-rule authority, never a fabricated user click.
Ask consumes an exact human approval even when broader host grants or YOLO
would ordinarily allow the operation. Independent speech consent still needs
human review. Host hard Deny and native access restrictions remain final.

Permission changes invalidate stale primary/auxiliary inference and tool
catalog snapshots before further use. They do not retrospectively remove
previously obtained context. Approval expiry and renewal use
[ADR0092](0092-approval-expiry-renewal.md); renewal never authorizes a changed
action or launches inference.

## Owner API and embedded Options

The owner API adds three routes below `/v1/agent/options`:

- `GET /tool-permissions` resolves and inspects a native scope and its three
  effective rows. `POST /tool-permissions` changes one exact scoped rule.
- `GET /tool-permission-scopes` pages saved scopes, including retired ones.
- `GET /tool-permission-changes/{key}` reads an exact historical receipt.

The embedded Permissions editor distinguishes scoped tool rules from the
existing service-wide controls and actual execution restrictions. It presents
the native effective value, source, inheritance, hard-denial state, scope and
change boundary. It does not navigate to Home bubbles or silently apply edits.

An unconfirmed dispatched save retains its exact body and key in bounded RAM
for its Agent/Session backend and owner. Checking an absent receipt is not proof
of failure. Current scope state and a historical save receipt are displayed as
different facts. Other unrelated settings retain their own owners and stores.

The snapshot includes the native store instance identity. After checking the
exact receipt, a fresh revision strictly greater than the original expected
revision on that same instance fences any new commit from the unchanged old
CAS request. The owner may explicitly release that RAM tracking record while
its historical outcome stays unknown. A different instance or changed state
fingerprint alone is insufficient. Releasing tracking never releases actual
outstanding HTTP ownership or claims cancellation, failure or rollback.

## Evidence and remaining work

Source review and static checks are being collected with the implementation.
No new automated cases or local automated test run are implied by this ADR.
The PR must record actual exact-head CI and preview evidence separately.
General filesystem read policy, network scopes, individual MCP/plugin tools,
permission profiles and broader acceptance remain open in the
[delivery plan](../options-delivery-plan.md).
