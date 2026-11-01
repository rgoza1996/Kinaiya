# Frozen task-limits contract sketch

Owner direction, November1: stop new feature implementation and move the remaining
Options goal to GitHub issues. This branch preserves five partial files against
main a6f24c110071ddbff63c459e7a023cdcadffab5a. It is NOT delivered or ready to merge.

- New strict settings/change/pin declarations and unused ContextVar ownership hooks.
- Optional native Task pin and accepted-submit shape seams, preserving old omission
  and logical request digests; no public TaskRequest permission to supply a pin.
- Three schema registrations, with NO generated schema files.

Missing: protected settings/ticket store, native revision/receipt recovery, actual
admission slots, watchdog/final expiry guards, process/actor/transport cleanup and
restart reconciliation, API, launcher wiring, UI, ADR0106, independent review and CI.
No tests or model/device calls were run. AST parsing of these five Python sources
only confirms syntax; it is not import, schema, behavior or compatibility proof.

Proposed defaults4 actual tasks/3600seconds and bounds1–64/1–86400 require native
implementation. Approval waits count; steering never resets the captured duration.
Reserve before adapter construction, keep historical lookup usable when full and
retain capacity through uncertain admission and actual cleanup. Task terminal
state, cancellation, timeout or PID absence must never be used as settlement proof.
Do not create fresh workers or timers for historical tasks after restart. Shared
host providers require real request/cleanup ownership, not only catalog lease state.

The parent issue208 remains open. Resume from its linked task-limits issue; this
sketch may be replaced if a reviewed design requires different native boundaries.
