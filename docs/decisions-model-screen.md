# Decisions model screen — October 31, 2026

This fixed textual screen compares seven hosted Decisions endpoints, the existing local Laya checkpoint and a later downloaded D1 Q8_0 trial. Liquid d1-ultra had the highest observed joint count, 45/48; Laya had 38/48. These are results on one small consumed synthetic set, not a replacement/default decision. No production adapter, setting, model default or quality promotion changed.

The [machine-readable evidence](../evaluation/evidence/decisions-models-2026-10-31.json) includes literal probabilities/confidence, source/provider identities, request/raw-response hashes and independently recomputable sanitized response hashes. The [frozen fixture](../evaluation/decisions/2026-10-31/README.md) contains the exact pre-inference inputs, references and manifest. Its pending/no-inference creation metadata remains unchanged; current completion/consumption is recorded in the evidence.

## Results and denominators

Each model received the same 48 cases once: 32 streaming-window and 16 finalized turns. Every route has 12 reference cases. Cloud endpoints each received one additional separate BLUE/GREEN protocol probe, giving 343 cloud requests (7 probes + 336 cases); Laya made 48 case requests plus 2 readiness reads. All 343 cloud responses and 48 Laya case responses passed the declared typed contracts with zero protocol/transport errors, observed EOF and confirmed close. No retry, fallback, output repair, reference change or answer chaining occurred.

| Requested model | Agent intent | Window timing | Target choice | Joint | False work | False interrupt | Case p50/p95 ms | Reported USD incl. probe |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| typesafe/jev-1.14 | 44/48 | 30/32 | 36/36 | 42/48 | 1/12 | 0/36 | 265.4/337.2 | 0.001374114 |
| cloudflare/clef-flash | 40/48 | 23/32 | 30/36 | 30/48 | 5/12 | 3/36 | 337.3/572.0 | 0.000475741 |
| microsoft/microsoft-decision-1 | 45/48 | 29/32 | 34/36 | 40/48 | 0/12 | 0/36 | 363.6/453.8 | 0.000667716 |
| liquid/d1 | 42/48 | 29/32 | 35/36 | 38/48 | 4/12 | 2/36 | 337.5/598.5 | 0.00031972 |
| baseten/joey-1 | 45/48 | 29/32 | 36/36 | 44/48 | 2/12 | 1/36 | 298.9/406.2 | 0.00020467 |
| cloudflare/clef | 43/48 | 31/32 | 36/36 | 42/48 | 2/12 | 1/36 | 340.6/621.6 | 0.004807488 |
| liquid/d1-ultra | 46/48 | 31/32 | 36/36 | 45/48 | 1/12 | 0/36 | 373.0/512.6 | 0.000671412 |
| Laya kinaiya-v4 (local) | 43/48 | 30/32 | 30/36 | 38/48 | 3/12 | 2/36 | 81.062/87.328 | not measured |
| LiquidAI/d1-Omni-2B Q8_0 (local, attempt 06) | 42/48 | 26/32 | 33/36 | 34/48 | 6/12 | 2/36 | 100.447251/121.687 | not measured |

The completed local D1 trial had **48 valid fixed cases, zero errors**, startup
**1223.723169ms**, a separate first protocol inference of **73.668757ms**, and a
first fixed-case decision of **99.837706ms**. Case timing includes response
capture, owned HTTP close and complete typed validation, following native warmup
and the probe; it is not cold inference or the same boundary as Laya. Its one
protocol POST plus 48 case POSTs are separate from the original cloud/Laya
counts above. The [completed-run detail](#attempt-06-completed-local-protocol-and-fixed-case-screen)
preserves all five earlier pre-inference startup failures and measurement limits.

Target choice scores 36 cases and requires the offered choice **and exact native task ID/revision**, including NONE/no-task null. Ignore normalizes target/revision to null. Joint requires all applicable intent, timing and target conditions. False work uses 12 reference-ignore negatives; false interrupt uses 36 non-cancellation negatives. Error/unattempted cases would remain incorrect in fixed denominators; the observed and pessimistic harm numerators coincide here because there were none. Per-mode confusion matrices and every mismatch are retained in the evidence.

W21/W23/W28/W29/F11/F15/F16 separate typed correction/cancellation intent from ambiguous/NONE targets. The generic finalized classifier policy can require ignore instead. Agreement with those typed references does not establish an effective executor route. **Target choice grants no permission and does not imply task admission, dispatch, interruption or success** ([Decisions authority](../clarification.md#10-decisions-real-time-converse-control-and-agent-routing)).

Cloud latency covers request/client creation through observed response/client close; protocol probes are excluded from case percentiles. Laya's displayed adapter latency includes pre/post identity checks; its transport-through-close p50/p95 was **54.225/62.582ms**, compared with **81.062/87.328ms** at the adapter boundary. Different boundaries and uncontrolled hardware/network conditions prevent a controlled model-speed conclusion. Confidence is a literal provider field, separate from the selected-option probability; no calibration or native confidence policy is qualified.

## Cost, identity and review

The provider-reported cloud usage.cost sum is **USD 0.008520861**, with 343 known reports. **Confirmed billing remains null.** The irreversible reservation stays **USD 2.772142464**, below the USD 9 admission/USD 10 owner limits; low reported cost never releases reservations. All cloud requests used the exact frozen provider tag, no fallbacks, and the documented prompt/completion/request price governor. Nonzero serialized output tokens were allowed at the frozen zero completion rate. No online pricing refresh or confirmed invoice is claimed.

Every actual model/provider matched its frozen profile. Jev resolved to typesafe/jev-1.14-20261014/TypeSafe; Microsoft to microsoft/microsoft-decision-1-20261009/Azure; Joey to baseten/joey-1-20261029/BaseTen; d1/d1-ultra to liquid/d1-20260930 and liquid/d1-ultra-20261015/Liquid. Clef and Clef Flash retained their undated cloudflare identities through DeepInfra tag deepinfra/fp8. These are hosted measurements; closed cloud models are not thereby local-capable. The downloaded **LFM3 LiquidAI/d1-Omni-2B Q8_0 is a different identity from hosted liquid/d1**.

The cloud fixture/source baseline is `b199a93`; the actual existing Laya adapter source was `a4f22c9`. Exact shared state/question projections were verified. Laya used kinaiya-v4 (native body alias laya-rl-agent). Full source, artifact-manifest and report hashes are in the evidence; disk/commit hashes do not attest imports at worker start.

Independent review blocked the initial runner before paid use: JSON-escaped credential echoes could reach parsed persistence, and a crash after a fatal result but before a halt marker could resume admission. The repaired runner scans bounded strict-decoded strings/keys before capture persistence and derives halt from every durable fatal result before key access/reservation. All 18 added regressions first failed, then passed; the complete 109 offline tests passed, followed by an unchanged lead repeat and a second independent MockTransport review. No real credential or endpoint was used in those checks.

The completed-run audit independently checked 16,581 consistency predicates across all 343 cloud records and all 48 Laya cases plus 2 readiness captures. Scores, confusion matrices, costs, probabilities, hashes and latency percentiles matched; no runner scoring function was called. Two provisional audit-only failures (pin scope and canonicalized probe ordering) were corrected and retained in audit provenance. This is consistency evidence, not formal assurance. The new public fixture/projection/evidence checks passed 152 tests; with existing Laya unit/native integration regressions, 217 tests passed in 3.49s using synthetic transports only. Scoped Ruff and formatting checks passed. Full raw captures remain private; the published sanitized hashes differ explicitly from original-body hashes. Selected endpoint file pins were verified, but the unretained original full catalog HTTP body cannot be independently rehashed. No credentials, auth headers, local paths, raw request IDs or private conversations are published.

## Downloaded D1: completed screen and retained startup failures

The [local D1 record](../evaluation/evidence/decisions-local-d1-2026-10-31.json)
preserves five startup failures and one completed sixth attempt. The original
phase-one JSON and four files containing the frozen 48-case corpus are unchanged.
Attempts 01–05 all stopped before **any SystemOne or fixture POST**; their D1
quality and decision latencies remain null. Attempt 06 completed one protocol
check and all 48 fixed cases. Its actual results are reported separately below;
health GET timings and earlier startup observations are not decision latency.

### Attempt 01: original startup refusal

One owned text-only Q8_0 load used official revision c53ae0b1f92b17065e9158d34019c9ca1ea5bcf8, model SHA aedf1a7c341b4281979423dbc39e9c3d3ad1da8834156c8e75af5399ca3b0861, and the installed LM Studio ROCm llama.cpp backend 2.67.0 (0.8.0-dev build1, 0893ec9). Fixed settings included all GPU layers, context 8192 and batch/ubatch 1024; 8192 was an owner fallback, not a measured SystemOne token count. Native empty startup warmup was allowed.

Startup was refused **before any SystemOne protocol or fixture inference request**. The last owned-process sample was 2,129,195,008 bytes against a conservative 2,046,820,352-byte observer cap; global free 387,710,976 bytes remained above the 128MiB floor. Source order identifies the observer predicate; NativeWorkerManager reports readiness-failed. Per-PID DRM use includes runtime/allocator costs and differs from projected model allocation. This is a guard refusal, not evidence of OOM, a backend-estimator defect, general incompatibility or inability to fit.

The owned child was reaped with an empty group, and HTTP cleanup was confirmed. During attempt 01, no other-model resource control call, download, unload, retuning or rerun occurred; recorded pre-existing process identities remained present. Zero protocol/fixture posts means **no D1 quality score, successful full-GPU placement or memory-fit claim**.

### Attempts 02–05: controlled startup and diagnostic results

The later attempts retained the same model and installed backend hashes. They
used a declared 4GiB owned-process observer cap, an 8GiB candidate profile and a
512MiB global margin on the existing physical 32GB development GPU. These are
guard settings, not an 8GB whole-stack hardware test or a hard VRAM quota. The
fixed workload remained context 8192, batch/ubatch 1024, one parallel slot and all
GPU layers requested, with normal empty startup warmup allowed. A separate
earlier owner-authorized Qwen unload preceded these controlled attempts; these
attempts issued no other-worker unload/reload or controller action and retained
the recorded peer/owner-policy state.

| Attempt | Recorded stopping condition | Maximum sampled owned VRAM, bytes | Inference POSTs |
| --- | --- | ---: | ---: |
| 02 | Terminal `cancelled`; observer recorded `owned_vram_measurement_unavailable`, and the initial 503 health response was refused. No stronger causal conclusion. | 40,960 | 0 |
| 03 | Primary observer refusal `owned_vram_measurement_unavailable`; terminal cancellation recorded. | 2,132,480,000 | 0 |
| 04 | Startup diagnostic: native operation failed; retained numeric projection records 25/25 layers and a CPU model-weight refusal. CPU buffer amount is unknown. | 1,986,539,520 | 0 |
| 05 | Primary observer refusal `owned_vram_measurement_unavailable`; terminal cancellation recorded. | 2,128,109,568 | 0 |

Attempt 03 observed 58 HTTP-200 health replies after one loading response, but
did not complete the full native readiness/placement gate. A healthy HTTP
endpoint alone is not SystemOne protocol validation. Attempt 04 was diagnostic
only: no model-list request or inference was allowed. Its 25/25 layer line and
CPU-weight refusal do not establish a CPU buffer size or successful all-GPU
operation.

Attempt 05 explicitly declared the verified `ROCm0` tensor-buffer override. Its
bounded numeric log projection records 25/25 layers, context 8192 and a positive
ROCm0 model buffer displayed as 1752.84MiB; that rounded display is not an exact
byte count. Startup then lost an owned-VRAM reading. A transient descriptor-read
race is possible from the pinned GPU reader, but is not a proven cause or proof
of self-exit. In every attempt, a later explicit cleanup receipt's
`term_sent=false` is not a complete signal history: cancellation of
`manager.start` may already have signalled the child.

All five attempts confirmed owned-child reaping, an empty group and HTTP
cleanup. Controlled attempts also confirmed removal of their temporary owned
API-key file. The public record retains source/runner/model/runtime digests,
literal non-sensitive health replies and numeric telemetry without private host
paths, process identities, headers, raw logs or user messages. Its sampled
maxima can be recomputed from the published numeric columns, but those columns
do not establish a continuous true peak or exact model-weight allocation.
Unattempted-case `ERROR` placeholders from the private runner are not model
predictions and are not published as a measured 0/48 D1 accuracy result.

### Attempt 06: completed local protocol and fixed-case screen

Attempt 06 completed once with the same pinned model/backend, context 8192,
batch/ubatch 1024, all 25 layers and explicit `ROCm0` tensor-buffer override used
in attempt 05. Its declared change corrected startup telemetry handling. Unknown
owned-memory readings before readiness used a conservative 4GiB profile
reservation; missing readings after readiness remained fatal. This does not
establish the cause of attempt 05's refusal. The record retains all three
pre-ready gaps and the separate first observation with no owned process, for
four null readings among 273 samples; null is never an observed zero.

Native readiness retained a fresh owned-memory reading of **2,132,508,672 bytes**,
a numeric **25/25** layer result, context/batch/ubatch **8192/1024/1024** and a
positive ROCm0 model buffer displayed as **1752.84MiB**. The display is rounded,
not exact bytes. This passed the native layer/buffer readiness gate; it does not
attest every runtime kernel as GPU-only. Full original native log bytes were not
retained, so the selected readiness projection cannot reconstruct the full log.
The maximum sampled owned memory was **2,171,473,920 bytes** (2.171GB decimal,
approximately 2.022GiB), not a continuous true peak, model-weight size or full
stack allocation. The owned child exited with native return code 0; reaping,
empty group, HTTP cleanup, temporary key-file removal and unchanged recorded
peer/owner-policy state were confirmed. No other resource-controller action,
model unload or Qwen reload occurred during the attempt.

There were **49 SystemOne POSTs**: one literal public marker protocol check,
then 48 fixed cases, all valid with no error or unattempted case. Two health
GETs and one model-catalog GET are separate. The [public local D1
record](../evaluation/evidence/decisions-local-d1-2026-10-31.json) includes each
canonical request, all 49 original public-synthetic response bodies, parsed
answers, raw-body and canonicalized-response hashes, relative interval endpoints
and per-case nanosecond durations. Inference body fields and bytes are unchanged;
canonical JSON serialization has a distinct, explicitly labelled hash. The
model-catalog body is omitted, so its retained original hash cannot be recomputed
from this capsule. No headers, private paths, process identities, absolute
performance-clock readings or private conversations are published.

| Measurement | Observed milliseconds | Boundary |
| --- | ---: | --- |
| Startup to native readiness | 1223.723169 | Native start wrapper; includes allowed empty native warmup. Manager's separate duration was 1.223699629s; result did not retain the wrapper interval endpoints. |
| First protocol inference | 73.668757 | Protocol wrapper through complete validation; begins before request preparation and follows native warmup. Reported input tokens: 61. |
| First fixed case | 99.837706 | Actual HTTP request start through complete typed validation; follows native warmup and protocol check, so is not cold inference. |
| All 48 fixed cases, p50 / p95 / maximum | 100.447251 / 121.687 / 123.683116 | Nearest rank, including the first case; bounded body/capture, actual owned HTTP close and local typed validation included. |
| Remaining 47 fixed cases, p50 / p95 | 100.752524 / 121.687 | Same boundary, excluding only the first case. |

The distinct request-attempt endpoint, after validation settlement, gives all-48
p50/p95 **100.450001/121.6902ms**. Individual intervals support independent
recomputation rather than substitution of startup, health or probe timing for
case latency. Earlier Laya **81.062/87.328ms** adapter/identity and
**54.225/62.582ms** transport-through-close p50/p95 have different boundaries;
these uncontrolled measurements do not rank model speed.

| Same consumed synthetic fixture | Agent intent | Conversation timing | Target | Joint | False work | False interrupt |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Local D1 Q8_0, attempt 06 | 42/48 | 26/32 | 33/36 | 34/48 | 6/12 | 2/36 |
| Earlier local Laya kinaiya-v4 | 43/48 | 30/32 | 30/36 | 38/48 | 3/12 | 2/36 |

Scores and per-mode confusion matrices recompute from the literal answers and
frozen author references. This small consumed screen does not show D1 improving
all measured criteria over Laya and does not justify a default change. The
references are synthetic author judgments, not human ground truth or a new
holdout. The seven typed-intent versus generic finalized-policy cases listed
above remain policy mismatches; raw intent scores do not establish native task
admission or effective routing.

The actual closed response shape is model/answers/usage, with each choice answer
containing type, choice, probabilities and confidence. All **120 confidence
values** are retained literally. In these responses each happens to equal its
selected probability; this observation is not a general protocol rule or a
calibration result, and Laya's `answer_confidence` semantics are not assumed.
The nearby upstream source examined separately is commit `8ca4060`, distinct
from installed backend `0893ec9`. SystemOne usage can account for multiple
question/variant prompts, so its summed `input_tokens` is not one head's context
capacity. Neither context 8192 nor absent truncation metadata proves complete
input delivery or zero truncation.

An independent stdlib audit, without runner/scorer imports or any new inference,
recomputed all 49 request bindings, all 52 response/capture hashes, aggregate
scores, percentiles and telemetry extrema; it also checked native readiness
and cleanup receipts. Its frozen receipt SHA is
`62b896deb6d750181e00ddace437452213e27a21bf432f069efec8f328a13ea0`.
The first audit-only check used an incorrect finalized alias mapping; its
corrected result and original failure were retained. This was not a trial failure
or evidence mutation. These checks establish artifact consistency, not a new
hardware observation or formal assurance. The 12 local-D1 public consistency
tests additionally recompute exact normalized targets/revisions, all per-mode
confusion matrices and fixed metrics from the literal published answers. With
the 152 original phase-one checks, **164 tests passed**; scoped Ruff and format
checks passed.

The whole local GPU stack at **8GB first, or 12GB if needed, remains unqualified**.
Physical 32GB host/coexistence observations are not 8GB/12GB results; no 16GB or
32GB profile tests were performed. Native M3 admission/dispatch, permissions,
Agent task success, actual audio/STT/overlap and browser behavior remain separate
unqualified gates. Native SystemOne adapters and any production setting/default
promotion remain pending. These measurements add evidence; they do not add or
relax a qualification gate.

### Separate single-request physical-batch refusal

A separately frozen boundary diagnostic issued exactly **one** additional public
synthetic SystemOne POST, without replaying the protocol probe or 48-case set.
The unchanged model/backend and context/batch/ubatch **8192/1024/1024** reached
native readiness. Its 4508-byte request received the literal **HTTP 500** error:
`input (2130 tokens) is too large to process. increase the physical batch size (current batch size: 1024)`.
The capture retains `model_http_status_refused`, observed EOF and confirmed owned
close; the native refusal is not relabelled as HTTP success. The
[separate capsule](../evaluation/evidence/decisions-d1-overflow-2026-10-31.json)
publishes the exact synthetic request and error bytes, hashes and relative wire
intervals. The error wrapper, starting before payload construction and HTTP
preparation, took **56.313016ms** through validation; actual HTTP request start
through error validation was **38.416791ms**. Both are error-path measurements,
**not decision latency**. Startup including empty warmup was separately
**2306.539096ms**.

The 91 retained telemetry samples include four pre-ready gaps and one initial
missing owned-process reading. Maximum sampled owned VRAM was **2,132,496,384
bytes**; missing readings remain null with conservative reservation, not zero.
Cleanup, native return code 0, owned-child reaping, HTTP close, temporary key-file
removal and recorded peer/owner-policy preservation were confirmed. Independent
raw-artifact review passed; prior attempt 06 and the frozen fixture remain
unchanged. This establishes the reported physical-batch refusal for this one
request/profile/install. It does not independently verify the native token
count, effective 8192-token head capacity, universal absence of truncation,
quality, native dispatch, a default change or an 8GB/12GB full stack.
