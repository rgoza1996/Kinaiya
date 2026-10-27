# Laya Decisions research, 27 October 2026

**Experimental; default promotion remains off.** [Issue #53](https://github.com/rgoza1996/Kinaiya/issues/53)
and [ADR 0021](../../../docs/adr/0021-laya-decisions.md) track the adapter and
remaining gates. The reference alpha.8 configuration is unchanged.

[report-v4.json](report-v4.json) records the latest candidate and verified-worker
results. [report.json](report.json) preserves the earlier v1–v3 study, including
its failures and then-unqueried reserved split. Both contain source/model hashes, package
versions, per-profile aggregates, training settings, native probe results,
resource samples and budget provenance. All reference labels and training
templates were authored by the lead using public synthetic text. They are not
independent human judgments or real user recordings.

## What was measured

The 24-case development split was frozen before valid model predictions. It
contains sixteen retained-window cases and eight finalized-message cases,
including the five clarification examples, irrelevant speech, speech-only stops,
quoted instructions, assistant suggestions and ambiguous/multiple task targets.
Relevant target scoring covers eleven cases; unused targets on ignored speech
do not contribute to target accuracy. A separate 24-case held-out split remained
unqueried through v3. It was evaluated once after v4 passed its development and
native gates and was frozen; it is now consumed and must not inform further tuning.

| Candidate | Agent routes | Conversation labels | Relevant targets | False work on ignored speech |
| --- | ---: | ---: | ---: | ---: |
| Fixed ignore/ready/NONE comparison | 11/24 | 9/16 | 2/11 | 0 |
| Base typed checkpoint | 13/24 | 8/16 | 9/11 | 3 |
| Supervised v1 | 22/24 | 16/16 | 9/11 | 0 |
| Supervised v2 | 23/24 | 16/16 | 11/11 | 1 |
| Supervised v3 | 24/24 | 16/16 | 11/11 | 0 |
| Supervised v4 (development) | 24/24 | 16/16 | 11/11 | 0 |
| Frozen v4 (reserved split, one evaluation) | 23/24 | 16/16 | 11/11 | 0 |

V4 augmented the 970-row v3 data with consistent UUID substitutions, varied
earlier conversation and speaking hints independent of labels: 1,940 rows,
4,680 optimizer-training items and 128 internal calibration items. It continued
v3 for two epochs in 512.6 seconds, with peak allocated memory 8.56 GB. The
minimal native control and three broader-context runs with fresh UUIDs each
passed all five cases before the candidate was frozen. The subsequent reserved
split missed one steering request as ignore; all conversation/target references
were correct, with no false work/interruptions or reported truncation. Target
accuracy is scored separately and does not make the missed route correct.
Classifier HTTP median/p95 was 55/64 ms on AMD ROCm. These are small synthetic
measurements, not human or acoustic acceptance.

The [pinned worker](../../../docs/laya-worker.md) then verified a private snapshot
of that same artifact, reported truthful local identity with a null Hub revision,
and passed both native profiles with the pin checked on every response. All 24
development question hashes, request sizes and mapped outputs matched the
original worker. A real boundary probe first caught upstream fallback for an
unknown short model alias; a strict pre-inference alias guard fixed it. The
retained failure and successful single/batch/auth checks are in the v4 report.
Resource-coordinator ownership and the broader voice gates remain open.

These are consulted development results. V1 missed a mandatory cancellation;
v2 confused stopping speech with stopping work. V3's perfect development result
did not generalize to the broader native probe. No runtime heuristic uses the
fixed comparison baseline.

The base typed CPU/GPU runs used the same pinned checkpoint and request shape
(trained 1,024/256 token budgets). All 24 choice classifications agreed; maximum
raw probability difference was 0.0089, so numerical identity is not claimed.
CPU classifier HTTP median/p95 was approximately 1.67/1.95 seconds; AMD ROCm GPU
median/p95 was 34/51 ms. First requests after preload took about half a second
on GPU. These small-sample classifier timings are separate from audible and
end-to-end voice latency. The report includes first-request and warm-only timing
and defines the percentile calculation through the retained measurements.

The GPU was AMD Radeon Graphics (`gfx1201`), exposed through PyTorch's `cuda`
API using ROCm; it was not an NVIDIA GPU. Each server used four threads, one
resident model and concurrency two. Fresh local-candidate processes became
ready in about 12.3 seconds with a warm OS page cache. This excludes download
time and does not claim an empty-cache cold boot. Hub-backed startup required
public Hub metadata even with artifact files cached.

V1 trained for eight epochs from the pinned typed checkpoint. V2 continued for
four epochs with cancellation/target contrasts; v3 continued for three with
speech-only/work-stop contrasts. They used 330/730/970 rows, micro-batch four,
gradient accumulation four, soft cross entropy, 0.05 label smoothing, shuffled
choice options and gradient checkpointing. GPU time was about 280/317/321 seconds;
peak allocated memory was about 8.55 billion bytes per run, with a 65% process
memory limit. No Meta inference was used for training, inference or native probes.

## Native integration evidence

The committed [probe](../../../scripts/probe_laya_native.py) uses real Laya HTTP,
the persistent native journal, actual UUID task identities, independent native
Converse/Agent components, the approval broker and real file tools. Agent and
Converse model responses are explicit fixtures; text input is synthetic.

The `minimal` positive control passed all five examples. It verified no new work
or reply for unfinished/backchannel speech, discarded stale inference during
revision-2 steering, required owner approval before a partial-speech file write,
cancelled before another write, and released a queued follow-up only after
native completion. File hashes, source bindings and no-duplicate replay were
checked.

The `preceding_converse` profile retained an ordinary prior explanation and
submitted cancellation during speech. V3 passed two cases and failed steer,
interrupt and queue classification. The probe rejected those cases before
dispatch; no file effects occurred. V2 had also failed native target validation.
The positive control cannot replace these failing acceptance cases. Neither run
establishes ASR, audible playback, echo robustness, human turn-taking or full M3.

With an independently configured local candidate and a separate service key in
`KINAIYA_LAYA_API_KEY`, run from the repository root:

```bash
python scripts/probe_laya_native.py --url http://127.0.0.1:8875 --model kinaiya-v3 \
  --context-profile preceding_converse --output .dev-run/laya-native-full
python scripts/probe_laya_native.py --url http://127.0.0.1:8875 --model kinaiya-v3 \
  --context-profile minimal --output .dev-run/laya-native-minimal
```

Output directories must be new. Full native records include only fixed synthetic
fixtures and local diagnostics; keep them ignored and review before publishing.
The preceding-conversation command is expected to exit nonzero for this candidate.

## Reproducing training

The deterministic gzip files contain the exact UTF-8 JSONL training/validation
bytes used. The report records compressed and uncompressed hashes. Extract
copies into an ignored experiment directory; do not edit the frozen references.
`calibration-v1.json` is the public development reference. The held-out answers
are intentionally absent.

Use pinned Laya source `b9fe351ce81e2d974bca2bf219c1e4030c0a05ea` (0.4.16),
the report's runtime versions, and the local `typed-decisions` artifact from
`convaiinnovations/laya@7b928d828b7b0e022f929d9bd2e44165aa270148`.
Verify model bytes before loading; do not execute Hub Python code. The exact
GPU dependencies were verified in an existing separate environment; a clean GPU
installation procedure was not verified. CPU reproduction can use the separately
recorded CPU environment but is not expected to produce identical GPU weights.

The native study called the pinned Python API so it could set gradient
checkpointing and the memory limit, rather than relying on CLI defaults:

```python
import torch
from laya.train import TrainConfig, finetune

torch.set_num_threads(4)
torch.cuda.set_per_process_memory_fraction(0.65)
# cfg is training[N]["config"] from report.json, with eval_data resolved
# to the extracted calibration-as-validation-v1.jsonl file.
config = TrainConfig(**cfg)
finetune(
    training_jsonl, verified_base_directory, new_output_directory, config=config, device="cuda"
)
```

Run v1 from the pinned Hub artifact, v2 from v1, v3 from v2, then v4 from v3
using `train-v4.jsonl.gz` and the v4 report's config. The reports
records every base/candidate digest. Seeds and settings support repeatability;
they do not guarantee identical bytes across hardware, runtime builds or AMP.

The upstream report labels any explicitly supplied validation file “held_out”.
Here that file is the repeatedly consulted development split, not Kinaiya's
unseen split. Dynamic target definitions cause an upstream warning when it
writes a single `questions.json`; the HTTP adapter always sends current typed
questions and never relies on that saved first target schema.

Laya's loader normalizes tokenizer metadata after verifying file hashes.
Original and normalized digests were retained; subsequent candidates were
normalized before freezing their runtime manifests. Vector weights and tokenizer
vocabulary bytes were unchanged by this compatibility normalization. Unversioned
local paths do not become Hub revisions. The opt-in first-party worker now
supports explicit manifest identity; unpinned local paths still report not ready.

The implementation's deterministic suite passed 653 Python and 15 Node tests,
Ruff, unchanged contract export and dependency audit. Real Laya probe failures
remain failures; CI fixtures do not convert them into a successful promotion.
