# ADR0105 — Native Agent operating modes

Status: accepted and implemented in
[PR227](https://github.com/rgoza1996/Kinaiya/pull/227). Exact-head
[CI350](https://github.com/rgoza1996/Kinaiya/actions/runs/41283718366), independent
Codex review and actual merge-tree verification passed. Broader M7 remains open.
Requirement: Roadmap §13.1 and §19.2; [#217](https://github.com/rgoza1996/Kinaiya/issues/217),
prerequisite to [M7](https://github.com/rgoza1996/Kinaiya/issues/10).

## Problem

The launcher's `agent_enabled` flag suppresses Decisions dispatch but leaves
direct Agent task admission available. Neither Agent health nor Converse intent
is a global work-admission policy. Standing work must not introduce a hidden
exception to the owner's Off setting. Existing steering changes pending RAM
state before its awaited journal append, so an append-time mode refusal alone
would leave queued work behind.

## Decision

Session owns persisted mode and an explicit control-writer generation in the
same protected SQLite database that accepts Agent work. Serialize mode changes
and new work at native acceptance; preserve exact historical proof first.
Register control writers explicitly, preserve issued/retired generation history,
and reconcile immutable full requests across restart without reapplying history.
An auxiliary Store open must not retire the actual controller.

Start with Off and on-demand, and the explicit policy to finish already accepted
work. Autonomous is unavailable until a real authorized standing producer exists.
Mode grants no tool, disclosure, spending or delivery permission. Existing Stop,
inspection, approvals and recovery remain available independently of Converse.

Steering needs a durable intent before any RAM goal installation or worker wake.
Bind its complete original request, initial native task acceptance and current
source observations. A retained receipt is historical proof only. After a lost
acknowledgement, retain scoped uncertainty; inspection and Stop/confirmed task
retirement allow a new task. Same-task continuation requires a later native
consumption/retirement protocol that fences a late original acknowledgement.

The [versioned API](../agent-control-api.md) defines roles, strict wire limits,
full-request proof, irreversible fences and bounded client ownership. A compact
Agent Chat control observes mode separately from health; it does not redesign
Options or move permissions into the mode switch.

## Verification boundary

Pure wire contracts were independently checked before native/client delegation.
The unchanged independent native regressions and additional proof/bootstrap
fault probes pass on the combined source, including actual workspace replacement
and approval/expiry integration. Actual Session/Agent HTTP and SQLite work proves
accepted-before-Off completion, new-admission refusal and independent Stop; the
actual Dart client additionally covers restart, historical proof and database
replacement. All 958 Flutter tests, analysis and formatting passed on byte-exact
client files. [Evidence](../../evaluation/evidence/agent-operating-modes-2026-10-31.json)
retains initial failures, corrections, source pins and verification boundaries.

Final-head CI is a separate required gate. These deterministic checks use scripted
model responses and synthetic inputs; they do not qualify model quality or human
conversation. Standing schedules, inbox/notifications, physical calls, general
execution leases and external exactly-once effects are outside this prerequisite.
