# ADR0072: Typed Meta action wire

Status: proposed; opt-in profile, default and production preview unchanged

## Context

Issue [#169](https://github.com/rgoza1996/Kinaiya/issues/169) retained a fixed
six-task real-provider run at `dcd3eea`: three native successes and three
failures. An inner JSON object followed by prose and mixed publication/revision
wire fields passed the broad outer response schema but were correctly rejected
by native decoding. Another response exhausted its budget. Two separate create
proposals occurred despite intact receipts; the cause is not established.

## Decision

Add an explicit `action_format: "typed-v2"` to the existing Meta adapter. Its
plain root contains `action`, a nested alternative of closed per-operation
objects. All fourteen native protocol operations have typed projections, gated
by the existing immutable per-inference snapshot. Publication create/update
arguments and all nine native component shapes are direct JSON data. All strict
wire fields are required; only declared optional properties accept null for
omission. Meaningful null table cells, false, zero, empty data and numeric types
survive conversion. Existing Pydantic supplies both schema and strict branch
validation, with a bounded adapter cache and fresh emitted schemas.

The [Meta contract](https://dev.meta.ai/docs/structured-output) documents nested
`anyOf`, closed/fully-required objects, local nonrecursive references and limits.
Offline schema conformance is not service admission or model-quality evidence.
The compatibility default stays `flat-v1`; neither format silently falls back.
The launcher can select the new format in an Agent provider profile using the
existing configuration path. No ordinary Converse/Decisions profile changes.

Bounded JSON parsing still rejects malformed text, duplicate keys, invalid
Unicode and non-finite outer values. Typed failures retain only constant
`typed_wire` diagnostics. Native UI byte/semantic, permission, source/revision,
storage and finish checks remain independently required. Foreign gateway JSON
strings retain existing conversion and native broker semantics. A known
unavailable operation reaches only the existing name-only membership refusal.
Unknown historical gateway keys cannot disappear during history conversion;
malformed history remains opaque rather than becoming an invented valid action.
Native UI history continues to contain real bounded receipts, not reconstructed
assistant actions. Request format/tools are captured before async transport;
format and complete schema participate in exact streaming-prompt count binding.

## Verification and limitations

The narrow combined suite passed 521 checks, including 181 pure-codec/native
store checks, 71 actual adapter/native-runtime integration checks and existing
provider/advertisement/count/history regressions. All 32 native selections and
all nine UI types are exercised. Maximum emitted schema measurements are depth8,
135 properties, 12 enum entries, 1610 aggregate naming characters and 1512
expanded JSON-value nodes. Full exact-head CI, independent review and actual
provider/task qualification must be recorded with the final PR/evidence; no
service acceptance or general reliability is inferred from these local checks.

The initial Muse CLI candidate is retained but rejected before import or
integration: it modeled unsupported UI types and a different publication API.
Investigation confirmed the empty preset exposed no read tools, despite a task
referencing source files. This delegation setup did not supply the requested
context; it does not establish coding ability with adequate context. The lead
implemented the native contract. Subsequent restricted review supplies all
needed public source inline. That actual Contributor xhigh CLI review completed
without static findings; it did not execute tests or contact the provider. Retain
both attempts: the rejected candidate cost an estimated $0.013619658, and the
corrected review cost $0.009509. These are guard estimates, not billing.

Initial test observations are retained: collection failed before the module
existed; one negative pure fixture mistakenly used a valid text string; one new
integration fixture attempted nonstream generation after an exact streaming
count. Corrected the test inputs/path, with no validation or binding relaxation.
Independent adapter review also caught unconditional disabled-UI hints,
historical gateway key dropping and a new serializer mismatch; those changes
were corrected with explicit regressions.

This is a provider grammar, not the universal selected-operation schema/catalog
or permission diagnostics still required by #171. It installs no tool, adds no
new capability grant, repairs no rejected output and makes no claim of harness
improvement, human voice quality, browser rendering or complete roadmap gates.
