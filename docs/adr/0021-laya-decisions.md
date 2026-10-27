# ADR 0021 — Typed local Laya Decisions

Status: experimental, opt-in. Tracks [#53](https://github.com/rgoza1996/Kinaiya/issues/53),
[Roadmap §6](../../Roadmap.md#6-asynchronous-decisions) and
[clarification §10](../../clarification.md#10-decisions-real-time-converse-control-and-agent-routing).
The verified alpha.8 reference keeps its existing Decisions provider. This adapter
and synthetic evaluation do not complete M3 or establish natural voice interaction.

## Interface and authority

Laya is a discriminative typed-decision model. Its Jev `POST /v1/systemone`
interface takes `state`, `model` and choice questions with `instructions` and
`criteria`. It is not an OpenAI chat-completions or dialogue endpoint. The host
adapter imports only the normal HTTP dependencies; Torch, Transformers and Laya
run in a separate environment/process.

The adapter accepts the existing native window/finalized classifier input, then
asks separate conversation, Agent-route and optional target questions. Short
choice aliases map to the existing four-way native labels. Target aliases map
by position to the supplied task ID/revision; task objectives are excluded.
Only bounded kind/text context fields are forwarded. Native consumers still
bind source identity, revision, epoch, disclosure scope and permissions. Neither
model choice nor confidence commits unfinished speech or authorizes a tool.

Missing/malformed answers, inconsistent probabilities, abstention, unexpected
model routing and any reported truncation/dropped state refuse classification.
The response has a 128 KiB raw-byte bound before decoding. Redirects, compression
and ambient proxies are disabled. HTTP timeouts include a total request deadline;
cancellation closes the response. There is no heuristic or cloud fallback.

Finalized-source refusals persist `routing.classification_refused` with source
identity/revision and a bounded native reason. They produce no `routing.decided`
or Agent dispatch. The consumer advances after persistence; replay finds the
receipt even beyond one journal page. A fresh corrected/resubmitted instruction
can route normally. Transient service failures retain existing retry behavior.
Window refusals use the existing durable failed-window status. Raw backend
errors, credentials and transcript text are not copied into error receipts.

Both classifiers retain at most twelve earlier entries (2,000 characters each)
and eight task references. `decisions_window_chars` configures the native JSON
context bound, from 256 to 36,000 characters, default 36,000. When over budget,
native code removes earlier context and reduces task references; it preserves
the complete current text or refuses. A small checkpoint should use a smaller
explicit bound; characters are not tokens. The adapter still checks the actual
server truncation report. Long inputs or large target questions may therefore
be refused even within the native character bound.

## Opt-in configuration

Configure only the Decisions role, alongside independently configured Agent and
Converse profiles:

```json
{
  "decisions_window_chars": 4000,
  "providers": {
    "decisions": {
      "kind": "laya",
      "base_url": "http://127.0.0.1:8871",
      "model": "typed-decisions",
      "api_key_env": "KINAIYA_LAYA_API_KEY",
      "allow_cloud": false,
      "timeout": 5
    }
  }
}
```

Use a separate Laya service secret; never reuse a Kinaiya owner/service token.
The key reference is optional for a separately secured service. Remote endpoints
require explicit `allow_cloud=true`, HTTPS and the existing per-source disclosure
policy. Agent and Converse reject `kind=laya` at startup.

The endpoint is the server root, not `/v1` or `/chat/completions`. Choose a
registered alias of letters/digits/underscores/hyphens. Leave `max_len` and
`head_max_len` unset to use the checkpoint's trained defaults; explicit overrides
are bounded and must leave space for state. Use the extended Jev response
(`LAYA_JEV_STRICT=0`), which includes routing and truncation evidence.

The measured runtime uses Laya source
[`b9fe351ce81e2d974bca2bf219c1e4030c0a05ea`](https://github.com/NandhaKishorM/laya/tree/b9fe351ce81e2d974bca2bf219c1e4030c0a05ea)
(0.4.16), installed into a separate environment, with
[`convaiinnovations/laya`](https://huggingface.co/convaiinnovations/laya/tree/7b928d828b7b0e022f929d9bd2e44165aa270148)
revision `7b928d828b7b0e022f929d9bd2e44165aa270148`.
Set `LAYA_REVISION` to that commit, register/preload only the chosen checkpoint,
disable auto task detection, bind loopback and cap concurrency/threads. The study
used four threads, one resident model and two admitted requests. Verify downloaded
artifact hashes before loading; do not execute Hub Python code.

The upstream [HTTP API](https://nandhakishorm.github.io/laya/http-api/) and
[fine-tuning guide](https://nandhakishorm.github.io/laya/finetune/) document the
separate worker. GPU/CPU dependencies belong to that environment, not Kinaiya's
normal development lock. No automatic installer or model download was added.
The pinned Hub-cache startup in this study still required public Hub metadata;
it is not evidence of an offline cold start. Local fine-tune loading was offline.

Decisions `/health` exposes optional `provider_readiness`. A bare healthy HTTP
server is insufficient: the selected alias must be resident, with a reported
revision and an actual device rather than a preference. Unversioned local paths
report not ready without an explicitly pinned artifact, even if inference works.
The [verified separate worker](../laya-worker.md) copies five manifest-pinned
inference files into a private snapshot, rejects path/size/digest violations,
preloads only that snapshot and detects load-time metadata changes. Its authenticated
responses identify the manifest. The host's optional `expected_artifact_sha256`
must match on every health/inference response before body decoding. Readiness
still requires actual residency/device; local artifacts report `revision: null`
and separate `artifact_sha256`. This is an authenticated worker receipt, not
hardware attestation. It does not invent a Hub revision. The coordinator can enforce local
disclosure policy but does not own or unload this external Laya process.

## Verification and research limits

The Muse Contributor CLI worker supplied an initial adapter/test basis in an
isolated worktree. Its four-step cap ended the run without a completion verdict.
The lead corrected the actual Jev field names and health shape, bounded streaming
before decode, and added native integration and refusal/replay regressions.
Mock HTTP tests establish protocol behavior, not model quality.

Before valid model predictions, the lead froze separate 24-case development and
24-case held-out public synthetic references. Labels are lead judgments, not
independent human judgments. Training uses new synthetic templates; development
cases are validation only. The held-out answers remain ignored and unavailable
to implementation workers. Development-set adaptations are recorded separately.
Upstream training reports call an explicit validation file “held_out”; that name
does not make the repeatedly consulted development split independent evaluation.

The base typed checkpoint scored 13/24 Agent routes, 8/16 conversation labels
and 9/11 relevant targets. Three ignored cases incorrectly routed work. The AMD
ROCm GPU produced the same 24 classifications as the CPU, with 34 ms median and
51 ms p95 classifier HTTP latency versus about 1.67/1.95 seconds on CPU. These
are local classifier measurements, not audible or end-to-end conversation latency.

The first supervised candidate improved to 22/24 routes, 16/16 conversation
labels and 9/11 targets, with no false work routes. It still missed a mandatory
cancellation and ambiguous targets, so it was rejected for promotion. Its training
used 330 synthetic rows, 724 optimizer-training items, 80 internal calibration
items, eight epochs and approximately 280 seconds on the AMD GPU. Training used
no Meta API calls. Further results and exact hashes belong in the linked evidence.

The second continuation fixed the mandatory examples and ambiguous targets but
regressed a finalized speech-only stop. The third continuation scored all 24
development routes, all 16 conversation labels and all 11 relevant targets
correctly. These are consulted development references, not held-out accuracy.

The actual native probe with real UUID task identities passed a minimal-context
positive control: two no-work cases, revision-2 steering with a discarded stale
action, cancellation before a file write, and a queued follow-up released only
after native completion. Partial-speech writes required exact owner approval;
file contents/evidence and replay behavior were checked. Agent and Converse
inference were explicit fixtures; Laya inference, the journal, native runtimes,
approval broker and file tools were real.

The broader probe adds a preceding Converse explanation and submits cancellation
during speech. That same third candidate passed only the unfinished/backchannel
cases and failed steer/interrupt/queue classification. No file effects occurred
in those failed cases. An earlier second-candidate probe also failed native
target validation. These failures block promotion despite the perfect small
development set. The frozen 24-case held-out split was not queried for v3.

Candidate v4 added consistent UUID substitutions, varied earlier conversation
and speaking hints independent of semantic labels. It passed the development
set and three broader native runs before freezing. One evaluation of the reserved
24-case split then scored 23/24 Agent routes, 16/16 conversation labels and 11/11
relevant targets, with no false work or interruptions. One steering request was
missed as ignore. These small lead-authored synthetic references are not human
voice acceptance. The held-out split is consumed and must not inform further
tuning/training. [V4 evidence](../../evaluation/evidence/laya-2026-10-27/report-v4.json)
records the frozen artifact, settings, results and pinned-worker probes. Default
promotion remains off; resource ownership and broader voice gates remain open.

Run the committed probe with a configured local candidate and its separate key
in `KINAIYA_LAYA_API_KEY`:

```bash
python scripts/probe_laya_native.py --url http://127.0.0.1:8875 --model kinaiya-v3 \
  --output .dev-run/laya-probe-full --context-profile preceding_converse
python scripts/probe_laya_native.py --url http://127.0.0.1:8875 --model kinaiya-v3 \
  --output .dev-run/laya-probe-minimal --context-profile minimal
```

Each output directory must be new. The probe writes fixed public synthetic
fixtures in its own workspace, uses no paid inference, records failed cases and
exits nonzero unless all five pass. Minimal is a plumbing control; it cannot
replace the broader-context gate. [Evidence and reproducible synthetic training
data](../../evaluation/evidence/laya-2026-10-27/README.md) retain the comparison,
training/runtime hashes, resource samples and limits.

Laya normalizes `tokenizer_config.json` on load, replacing `TokenizersBackend`
metadata with `PreTrainedTokenizerFast`. The study retained original and normalized
hashes; model weights and tokenizer vocabulary bytes did not change. A failed
continuation integrity preflight was retained, then retried against a verified
pristine copy. Runtime artifact identity must include this normalization rather
than silently treating changed bytes as the original frozen artifact.

The five specification examples, independent held-out route/target gates, no
false work/interruptions, native permissions and broader voice latency/echo gates
remain required. No fast classifier result alone enables the intended default.
