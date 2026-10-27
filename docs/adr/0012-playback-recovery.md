# ADR 0012: Speech validity across Converse process restart

Issue #30 was reproduced against the real SQLite journal with scripted
inference. Both interrupted and ungraceful restart accepted an old speech
chunk: initialization restored its old integer as the currently valid
playback generation. Concurrent first initialization also created two epoch
objects. Three regression cases failed before the repair.

Converse now serializes initial playback state per session. When recent
history contains a prior generation, it advances the boundary and persists a
`converse.safety` restart marker before exposing the new state. Interrupt
records include both the interrupted and next generation. A failed marker
write leaves no accepted cached epoch.

Every speech chunk and grounded outcome notice additionally carries the
runtime's fresh random `runtime_id`. Speech synthesis/delivery validates this
against the serving runtime as well as the existing event origin, generation,
session epoch and outcome checks. The integer counter is not sufficient
authority: bounded history may omit an old value, and concurrent runtimes may
observe the same value. An earlier runtime's chunk cannot become valid again
even if an integer is reused. Already recorded text remains history; old audio
cannot be synthesized by a new runtime. New chunks remain usable.

Five new durable-journal tests cover interrupted/ungraceful restart,
serialized initialization and repeated barriers, history eviction followed by
a fresh turn, and storage failure before committing a barrier. Together with
the ten existing coordination tests, 15 focused tests pass. Full offline
verification including the merged skills slice passes 294 tests.

This enforces server-side authorization of future synthesis. It cannot remove
audio already buffered on an offline client or prove physical playback stopped.
Resource handoff still needs client cancellation, worker termination and measured
release. Multiple active Converse replicas/load balancing are not a supported
voice ownership protocol; the runtime id fails closed on cross-instance chunks.
No paid inference was needed for this repair. The roadmap is unchanged.
