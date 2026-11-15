# Native d1 task-status matrix, November 15, 2026

Issue #485 remains **open**. The declared twelve-case comparison produces
**one accepted routing result matching the plan and eleven low-confidence
refusals** at the explicit 0.7 floor. This does not establish the requested
classification quality. All first raw answers, distributions and refusals are
retained; the floor, prompts and outputs were not adjusted after observation.

The [plan](../../../evaluation/plans/issue-485-native-d1-2026-11-15.json) was
committed in the clean candidate
`dfee04ed56c98f7c324b964ddec7cd1f123c7b0c` and
[declared before inference](https://github.com/rgoza1996/Kinaiya/issues/485#issuecomment-6891871542).
It covers progress/result/failure/cancellation status, thanks, active correction
and cancellation, explicit new work after failure, mixed status/new work,
unrelated conversation, publication status and follow-up after completion.

| Case | First result |
| --- | --- |
| Active progress | Refused below floor |
| Succeeded result | Refused below floor |
| Failed explanation | Refused below floor; raw Agent top choice is new work |
| Cancelled status | Refused below floor; raw Agent top choice is new work |
| Ordinary thanks | Refused below floor |
| Active correction | Refused below floor |
| Active cancellation | Refused below floor |
| New work after failure | Refused below floor |
| Status plus explicit new work | Refused below floor |
| Unrelated conversation | Accepted: companion responds without interrupting, Agent work unchanged |
| Active publication status | Refused below floor |
| Follow-up after completion | Refused below floor; raw correction choice has no active target |

Raw top choices are not usable routing decisions. Several also disagree with
the declared interpretations, so merely lowering the floor would not establish
the requested behavior. The 0.7 value is this comparison's explicit refusal
policy, not a shared model-independent confidence calibration.

The actual Store → WindowDecisionsWorker → WindowClassifier →
NativeD1RoutingProvider path used the already-installed **d1-2B BF16** asset and
the existing isolated engine overlay at source
`8ca4060b7e30e76baaa5670ee1ec1b760b7f8552`. It ran one slot, eight threads,
8192 context/batch and the existing five-second provider/classification limits.
The editable default interpretation preference remained in use, with the v4
native read-only history contract. Model, engine executable/library, plan,
helper and source hashes are in [observations.json](observations.json).

Each session's task state is an explicitly **controlled typed fixture**, not
the result of running a real Agent task. The native journal retains two public
context rows and the current authenticated speech-window fixture. The real
model receives the actual constructed request and returns typed native choices.
There is one original captured request and raw response per case, no retry or
reused classification. Terminal cases actually contain the new read-only block
and no typed target question; active cases retain their target question.

A separate retained-record verification checked all twelve original native
inspection request/response/source bindings and SHA-256s. The individual
`*-inspection.json` files are unchanged exports. Eleven refusal cases publish
no window decision; the accepted case's native source binding matches. A
read-only SQLite query confirms **zero Agent dispatches**. No Agent dispatcher
or tool execution ran. Engine exit was 0, with no recorded run or cleanup error.

This qualifies neither statistical routing accuracy nor the owner's distinct
Omni deployment, real speech recognition, dialogue generation, physical devices
or latency. There was no cloud or paid helper request, new model/dependency
download, microphone use, owner runtime/configuration change or installed-engine
replacement. These first failures remain separate from the synthetic contract
regressions that verify the input repair.
