# Framework contracts and user-owned model behavior

Kinaiya gives independent Converse, Decisions and Agent models a reliable
framework. It does not promise that different user-selected models think,
route or speak alike. This records the owner decision in
[#557](https://github.com/rgoza1996/Kinaiya/issues/557).

Converse owns personality and dialogue. Decisions selects immediate routing
and companion timing but grants no execution permission and determines no task
success. Agent independently performs permitted work. Users select models and
providers for each role; changing one role must not silently replace another.

The host's [Converse instructions](converse-task-context.md) and
[Decisions instructions](decisions-prompts.md) are user-editable. Their supplied
defaults are suggested natural-language preferences. They can favor awaiting
Agent's substantive answer, or giving questions and commands Agent attention,
while allowing continued conversation and appropriate ignore/wait/queue/steer/
interrupt choices. Explicit overrides, including blank preferences, remain
overrides. A different configured personality or interpretation is not by
itself evidence of a framework defect.

There is no universal numerical cutoff, shared confidence calibration or hidden
logit bias that can make Jev, LFM3 d1, Laya and compatible text models behave
identically. Existing explicit adapter refusal settings are local declarations,
not interchangeable probabilities or evidence of a universal routing quality.
Defaults can suggest behavior; actual quality needs separately scoped model
evaluation, including its first failures.

After a valid model selection, trusted native code still owns the deterministic
contract: authenticated source/task/session/epoch/revision identity, retained
transcripts and retrieval, queue/wait/steer/interrupt semantics, permissions,
disclosure, stale-event rejection, idempotency, custody and uncertain cleanup.
Malformed model output refuses. A generated acknowledgment proves no Agent
acceptance or tool effect. Task state, partial evidence, durable publication,
actual program execution and semantic success remain distinct observations;
native outcome gates cannot be replaced by a model's assertion.

The existing [retained-window/two-channel design](adr/0019-streaming-window-control.md),
[source authority](adr/0020-streaming-source-authority.md),
[independent model selection](adr/0098-independent-decisions-model-selection.md),
[transcript retrieval](agent-transcript-retrieval.md), and
[bounded native audio](adr/0112-buffered-native-audio.md) continue to define their
own contracts and qualification limits. This documents their responsibility
boundary, without reopening the completed implementation in
[#49](https://github.com/rgoza1996/Kinaiya/issues/49),
[#53](https://github.com/rgoza1996/Kinaiya/issues/53),
[#180](https://github.com/rgoza1996/Kinaiya/issues/180),
[#255](https://github.com/rgoza1996/Kinaiya/issues/255), and
[#517](https://github.com/rgoza1996/Kinaiya/issues/517).

The observed native handoff failure in
[#483](https://github.com/rgoza1996/Kinaiya/issues/483), informational routing in
[#485](https://github.com/rgoza1996/Kinaiya/issues/485), and unsupported work
claims in [#486](https://github.com/rgoza1996/Kinaiya/issues/486) retain their
separate evidence and acceptance. Neither this policy nor a passing scripted
transport test proves speech quality, routing accuracy, or that generated
statements are truthful.
