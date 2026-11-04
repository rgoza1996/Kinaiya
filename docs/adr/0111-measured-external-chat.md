# ADR 0111: Exact measured chat for owner-managed endpoints

Issue: #447, under #204 and #357. Roadmap: independent model/provider choice and measured context admission. This branch adds a Converse text connection; native Omni speech and media ingestion remain separate unfinished work.

## Decision

Add the explicit `measured_chat` profile for a fixed owner-selected literal-loopback HTTP(S) endpoint. The user manages the inference engine, model and server lifecycle. Kinaiya does not patch or select model-specific inference kernels, discover aliases, download artifacts or infer capabilities from a model name.

`MeasuredChatProvider` freezes the entire generation request, including messages, stream mode, output reserve and reasoning. It sends that same body to `chat/completions/input_tokens` before `chat/completions`. Only a bounded strict `response.input_tokens` result can produce an admission. The declared context must accommodate input plus the output reserve. Unsupported counting, malformed counts and overflow refuse generation; there is no estimate, clipping, retry or fallback.

Streaming uses a one-use ticket bound to original source/configuration/transport identity. Prepared completion uses the existing request-local `PreparedComplete` protocol. Request hooks cannot retarget counted requests before dispatch. Original response cleanup remains owned across cancellation, including error responses; failed cleanup makes the adapter unavailable. The count deadline is checked after actual close even when a transport suppresses timeout cancellation.

Bounds are 512 KiB encoded request, 4 KiB count response, and the existing compatible JSON/SSE response limits. Plain JSON validation precedes copying, with bounded strings/nodes and maximum depth512. Diagnostics do not reflect credentials or untrusted content.

## Native selection and compatibility

The additive catalog kind requires explicit context capacity, a fitting output reserve, non-null reasoning (default `none`), exact `allow_cloud=false`, and an optional protected credential generation. Saving a declaration neither contacts nor activates the endpoint. Existing configuration generations, selection leases, rotation, disclosure and resource wrapping remain authoritative.

This increment permits native selection only for Converse text. Agent/auxiliary/pre-generation-proof construction is explicitly refused, including a captured recipe presented under another role. The existing immutable Agent auxiliary contract is unchanged. The provider class's prepared-completion implementation is independently testable; its existence does not authorize native Agent selection.

Map the new profile to the existing `openai-compatible` public provider identity. Existing `compatible` profiles retain their previous normalization and remain unmeasured. The native profile is an additive `StrictSettings` alternative; no existing exported WireModel, JSON schema, canonical profile or digest is broadened. Flutter has a separate new-kind normalizer and an explicit local measured-chat editor option.

## Proof limits

The owner trusts the selected authenticated endpoint, model alias and declared capacity. Exact-body counting proves request measurement, not immutable model artifacts, server incarnation, absence of hot reload, model quality or execution permission. Operators must keep the backend configuration coherent during a request and qualify their actual server protocol independently. No `/props` response or alias string is presented as a managed-runtime custody receipt.

The profile advertises text input, unknown readiness and unverified qualification. It does not enable audio/video input, speech output, duplex conversation, native media source control, or a successful sign-language demonstration. Those paths must retain their own source, consent, native ownership and delivery contracts.

## Verification

Use controlled synthetic HTTP exchanges to verify exact bytes, fit/refusal, overlap/replay, post-await mutation, original held cleanup and cleanup failure. Independently authored profile tests verify native/Dart canonical configuration, role restrictions and lease capture. Existing compatible/context/provider-selection regressions and immutable schema pins remain required. Record actual test and exact-head CI results in the linked PR; a bare local endpoint probe is separate evidence and cannot substitute for the Kinaiya end-to-end rehearsal.
