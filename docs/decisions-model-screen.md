# Decisions model screen — October 31, 2026

This fixed textual screen compares seven hosted Decisions endpoints with the existing local Laya checkpoint. Liquid d1-ultra had the highest observed joint count, 45/48; Laya had 38/48. These are results on one small consumed synthetic set, not a replacement/default decision. No production adapter, setting, model default or quality promotion changed.

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
| Laya kinaiya-v4 (local) | 43/48 | 30/32 | 30/36 | 38/48 | 3/12 | 2/36 | 81.1/87.3 | not measured |

Target choice scores 36 cases and requires the offered choice **and exact native task ID/revision**, including NONE/no-task null. Ignore normalizes target/revision to null. Joint requires all applicable intent, timing and target conditions. False work uses 12 reference-ignore negatives; false interrupt uses 36 non-cancellation negatives. Error/unattempted cases would remain incorrect in fixed denominators; the observed and pessimistic harm numerators coincide here because there were none. Per-mode confusion matrices and every mismatch are retained in the evidence.

W21/W23/W28/W29/F11/F15/F16 separate typed correction/cancellation intent from ambiguous/NONE targets. The generic finalized classifier policy can require ignore instead. Agreement with those typed references does not establish an effective executor route. **Target choice grants no permission and does not imply task admission, dispatch, interruption or success** ([Decisions authority](../clarification.md#10-decisions-real-time-converse-control-and-agent-routing)).

Cloud latency covers request/client creation through observed response/client close; protocol probes are excluded from case percentiles. Laya's displayed adapter latency includes pre/post identity checks; its transport-through-close p50/p95 was 54.2/62.6ms. Different boundaries and uncontrolled hardware/network conditions prevent a controlled model-speed conclusion. Confidence is a literal provider field, separate from the selected-option probability; no calibration or native confidence policy is qualified.

## Cost, identity and review

The provider-reported cloud usage.cost sum is **USD 0.008520861**, with 343 known reports. **Confirmed billing remains null.** The irreversible reservation stays **USD 2.772142464**, below the USD 9 admission/USD 10 owner limits; low reported cost never releases reservations. All cloud requests used the exact frozen provider tag, no fallbacks, and the documented prompt/completion/request price governor. Nonzero serialized output tokens were allowed at the frozen zero completion rate. No online pricing refresh or confirmed invoice is claimed.

Every actual model/provider matched its frozen profile. Jev resolved to typesafe/jev-1.14-20261014/TypeSafe; Microsoft to microsoft/microsoft-decision-1-20261009/Azure; Joey to baseten/joey-1-20261029/BaseTen; d1/d1-ultra to liquid/d1-20260930 and liquid/d1-ultra-20261015/Liquid. Clef and Clef Flash retained their undated cloudflare identities through DeepInfra tag deepinfra/fp8. These are hosted measurements; closed cloud models are not thereby local-capable. The downloaded **LFM3 LiquidAI/d1-Omni-2B Q8_0 is a different identity from hosted liquid/d1**.

The cloud fixture/source baseline is `b199a93`; the actual existing Laya adapter source was `a4f22c9`. Exact shared state/question projections were verified. Laya used kinaiya-v4 (native body alias laya-rl-agent). Full source, artifact-manifest and report hashes are in the evidence; disk/commit hashes do not attest imports at worker start.

Independent review blocked the initial runner before paid use: JSON-escaped credential echoes could reach parsed persistence, and a crash after a fatal result but before a halt marker could resume admission. The repaired runner scans bounded strict-decoded strings/keys before capture persistence and derives halt from every durable fatal result before key access/reservation. All 18 added regressions first failed, then passed; the complete 109 offline tests passed, followed by an unchanged lead repeat and a second independent MockTransport review. No real credential or endpoint was used in those checks.

The completed-run audit independently checked 16,581 consistency predicates across all 343 cloud records and all 48 Laya cases plus 2 readiness captures. Scores, confusion matrices, costs, probabilities, hashes and latency percentiles matched; no runner scoring function was called. Two provisional audit-only failures (pin scope and canonicalized probe ordering) were corrected and retained in audit provenance. This is consistency evidence, not formal assurance. The new public fixture/projection/evidence checks passed 152 tests; with existing Laya unit/native integration regressions, 217 tests passed in 3.49s using synthetic transports only. Scoped Ruff and formatting checks passed. Full raw captures remain private; the published sanitized hashes differ explicitly from original-body hashes. Selected endpoint file pins were verified, but the unretained original full catalog HTTP body cannot be independently rehashed. No credentials, auth headers, local paths, raw request IDs or private conversations are published.

## Separate downloaded D1 startup trial

One owned text-only Q8_0 load used official revision c53ae0b1f92b17065e9158d34019c9ca1ea5bcf8, model SHA aedf1a7c341b4281979423dbc39e9c3d3ad1da8834156c8e75af5399ca3b0861, and the installed LM Studio ROCm llama.cpp backend 2.67.0 (0.8.0-dev build1, 0893ec9). Fixed settings included all GPU layers, context 8192 and batch/ubatch 1024; 8192 was an owner fallback, not a measured SystemOne token count. Native empty startup warmup was allowed.

Startup was refused **before any SystemOne protocol or fixture inference request**. The last owned-process sample was 2,129,195,008 bytes against a conservative 2,046,820,352-byte observer cap; global free 387,710,976 bytes remained above the 128MiB floor. Source order identifies the observer predicate; NativeWorkerManager reports readiness-failed. Per-PID DRM use includes runtime/allocator costs and differs from projected model allocation. This is a guard refusal, not evidence of OOM, a backend-estimator defect, general incompatibility or inability to fit.

The owned child was reaped with an empty group, and HTTP cleanup was confirmed. No other-model resource control call, download, unload, retuning or rerun occurred; recorded pre-existing process identities remained present. Zero protocol/fixture posts means **no D1 quality score, successful full-GPU placement or memory-fit claim**.

The whole local GPU stack at **8GB first, or 12GB if needed, remains unqualified**. The physical 32GB host/coexistence observations are not 8GB/12GB results; no 16GB or 32GB profile tests were performed. Native M3 admission/dispatch, permissions, Agent task success, actual audio/STT/overlap and browser behavior remain separate unqualified gates.
