# 0063: Slowly floating Converse setup controls

Issue [#148](https://github.com/rgoza1996/Kinaiya/issues/148) requests randomized
starting locations and gentle physical motion for the five existing setup
bubbles. Their declarations, host reads and local visibility preference keep
the boundaries in ADR0060. This change owns presentation only.

A bounded local simulation gives circles slow velocities and wall, obstacle and
pair collisions. Every newly mounted setup gets fresh random starts. Fixed
header/footer controls and the central character area remain clear in wide
layouts. Narrow layouts provide a taller scrollable field; large text sizes
increase bubble diameter. Impossible geometry falls back to ordinary readable
flow. Resize establishes a new valid arrangement without changing control identity.

Movement pauses while a bubble is hovered, pressed or focused and while any
popover is open. Keyboard order follows Model, Reasoning, Inputs, Speech,
Decisions independently of position; Escape restores the invoker. The existing
Animations preference, reduced motion, OS disable-animations, inactive lifecycle
and TickerMode suppress the ticker. Background elapsed time is discarded; step
time is bounded. Dismissal disposes the ticker. Continuous motion tests pump
bounded frames rather than waiting for an intentionally ongoing animation.

The separate owner-approved Decisions traceability design is recorded in
[its planned requirements](../decisions-traceability.md). No example Decisions
timeline or generated rationale is supplied by this presentation change.
