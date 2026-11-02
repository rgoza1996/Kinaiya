# 0106: Native d1 Decisions protocol and explicit selection

Status: implementation under verification; model and media qualification remain
open in [#204](https://github.com/rgoza1996/Kinaiya/issues/204), part of
[#196](https://github.com/rgoza1996/Kinaiya/issues/196).

## Requirement and compatibility

Decisions must support the selected classifier through its actual protocol.
The native llama.cpp d1 endpoint differs from the existing Jev/Laya endpoint:
it returns a top-level model identity, choice probabilities and confidence,
and input/output token usage. It does not supply Laya's routing, truncation or
artifact receipts. Fabricating those fields would conceal a missing guarantee.

The separate `native_d1` provider preserves the existing typed window and
finalized-text classifier inputs and routes. It neither changes the Laya wire
parser nor turns either classifier into a dialogue model. This increment
implements text transport and explicit host/catalog selection. Direct audio,
shared media/ASR lineage, native Omni speech output and full-duplex qualification
remain separate requirements; they are not replaced by this text interface.

The reference protocol was inspected in unmodified llama.cpp
[`ddf48f8`](https://github.com/ggml-org/llama.cpp/tree/ddf48f85c933c1f438212a02eb21e7b001d2d37d).
The cached research build was CPU-only. Neither source inspection nor a model
label establishes GPU residency, exact weight identity, routing quality or
compatibility of an independently changed runtime.

## Declaration and model identity

A native d1 profile has only:

| Field | Meaning |
| --- | --- |
| `kind` | Explicit `native_d1`; never inferred from a model name or URL. |
| `base_url` | HTTP(S) loopback server root with an explicit port. |
| `model` | Exact configured canonical native model alias, at most 128 ASCII characters. |
| `allow_cloud` | Always false for this adapter. |
| `credential` | Optional exact stored credential generation. |
| `timeout_s` | Integer 1–120 seconds, default 5. |
| `min_confidence` | Required owner-selected finite number from 0 to 1. |

There are no reasoning, output-token, Laya token-window or artifact-proof
controls. The confidence cutoff is a refusal policy, not calibrated certainty,
a recommended threshold or a permission grant. The owner enters it explicitly;
the editor supplies no default. A declaration save constructs no adapter and
does not probe, load or select a model.

Only Decisions accepts this provider. The existing owner model selector captures
the exact catalog generation and configuration digest, then constructs a fresh
task-owned adapter at real classification admission. Host selection retains its
configured service lifetime. Existing credential, source, disclosure, revision,
resource and actual operation ownership checks remain in effect.

The request always includes the configured model and uses the fixed
`/v1/systemone?autoload=false` path. The audited single-model server ignores the
body's model selector, so a successful response must independently contain the
exact captured canonical alias. A different alias is refused rather than
adopted. This binds protocol identity; it is not model-file attestation.
Readiness remains unknown and qualification not checked. The adapter's health
projection performs no network request and makes no loaded-device claim.

## Request, response and refusal

The body contains exactly `model`, rendered text `state` and the fixed typed
choice `questions`. Existing bounded context and task-reference projection is
preserved, including the complete current text and exclusion of task objectives.
The complete compact UTF-8 request is bounded to 262,144 bytes before capture
or transmission. Capture and final transport checks bind the original body,
model, policy, client, endpoint and headers across awaited callbacks.

The raw, identity-encoded response is bounded to 128 KiB before decoding.
Redirects and ambient proxies are disabled. Duplicate keys, nonfinite values,
invalid UTF-8, unknown fields, inconsistent choices/probabilities/confidence,
a changed model, and nonzero generated output tokens refuse classification.
The successful schema is exactly `model`, `answers` and `usage`; each choice
uses native `confidence`, with no fabricated Laya metadata.

The confidence floor applies to the finalized route, both window outputs, and
the target when an Agent action is selected. Every target is structurally
validated; ignored Agent routing clears its binding. Task IDs and objective
revisions come only from the native captured task options. Low confidence
produces a refusal, never a synthetic Ignore or an unauthorized task.

HTTP capacity failures remain failures. The adapter does not retry with a
shortened state, another model, a smaller context or a cloud fallback. The
audited LFM3 d1 implementation rejects context/batch overflow; an arbitrary
different backend cannot inherit that qualification from its response shape.

## Trace and actual cleanup

Calls retain `native_d1_systemone` as their distinct request/protocol format.
Catalog model pins permit only exact provider/protocol pairs. A reused capture
requires its original call identity; Session retains that call's actual pin.
Historical records without a pin keep their original storage/digest behavior.

The existing Decisions model lease owns the original HTTP exchange and sole
response-close operation. Cancellation or a request deadline cannot turn a
pending close into retirement. Cleanup failure stays visible and blocks new
selection/admission. Closing a socket is not evidence that external model
inference stopped.

The adapter also fences the successful-return path after that cleanup: a
transport that suppresses cancellation cannot turn a cancelled or expired HTTP
exchange into a routing result. A new caller cancellation is re-raised; an
expired native request deadline produces a provider error, even if the
transport eventually returns valid bytes. When both occurred, the caller
cancellation takes precedence. An actual cleanup failure keeps its original
sticky failure instead. Historical cancellation counts from before the
exchange are not treated as a new cancellation.

Catalog confidence values retain their fractional JSON value on reread. This
permission applies only to the native profile's top-level `min_confidence`;
receipt counters, legacy profiles and nested fields retain their existing
integer-only stored-document rules. Catalog `timeout_s` maps to the adapter's
`timeout`; a host launcher declaration uses the existing `timeout` field.

## Verification boundary

Offline protocol and native selection checks must distinguish fixture transport,
actual service/source routing, cleanup ownership and model inference. Existing
Laya checks remain required. The dated [negative and mixed model evidence](../evidence/multimodal-2026-10-31/README.md)
is retained. No new model, paid inference, GPU, capture, device, service or live
configuration action is part of this implementation increment.

The [dated integration evidence](../../evaluation/evidence/native-d1-text-integration-2026-11-01.json)
records exact source/test hashes, retained failed baselines, the independently
authored native protocol/selection and deadline checks, scoped numeric controls,
legacy compatibility and the post-main-merge launcher/packaging run. It also
retains the intermittent unchanged bubble-drag test failure from the first
main-based Flutter run. Applicable exact-head CI is still required before
merging; these offline checks do not qualify a model or native media runtime.
