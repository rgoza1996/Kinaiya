# ADR 0038: separate local dialogue evidence from Agent success

Tracks [M2](https://github.com/rgoza1996/Kinaiya/issues/4),
[#49](https://github.com/rgoza1996/Kinaiya/issues/49) and the dialogue defect
[#97](https://github.com/rgoza1996/Kinaiya/issues/97).

## Actual integration observation

One frozen rehearsal used merged `a750998f` with native service processes,
managed local Whistle STT, Qwen3-0.6B dialogue, Laya Decisions and VibeVoice
Realtime 0.5B synthesis. Only the independent Agent used authorized
public/synthetic Contributor inference. Development Muse CLI sessions are
separate from Converse's local dialogue provider.

A 4.7-second synthetic recording asked for a file containing seven. Actual
Whistle recognition supplied the unchanged request. Decisions asynchronously
queued native Agent work. Converse emitted text before the owner resolved
the action-specific approval. Interrupting a pending PCM read stopped the
native synthesis producer; stale speech returned 409, while Agent remained
waiting for its approval. The approved write and verification produced native
completion evidence. A deterministic grounded notice then produced actual
PCM, without another dialogue-model request.

After durable Converse-off, all four managed model groups were released.
Direct Agent separately wrote and read a second file containing eight. The
Converse model lifecycle remained stopped through the final drained journal
cut. All five service groups and the runner were reaped. Independent retained
evidence review passed 185 checks over 1,849 observations and 23 journal events.

## Quality and performance limits

Qwen's actual first reply was:

> I can't create a file named "proof" with only the number seven.
> Let me know if you'd like help with something else!

This is a dialogue-quality failure even though Agent independently succeeded.
The original model output, raw SSE and observations are retained unchanged.
Protocol success does not satisfy Stage B's natural-response criterion.
Follow-up #97 clarifies conversational acknowledgment while routing and work
status remain unknown; an acknowledgment cannot establish task acceptance,
execution, permission or completion.

The CPU profiles remain unsuitable for realtime. Decoded PCM is generated
audio, with no physical playback, hearing, audible-onset or p95 claim. This
one finalized batch-input run does not establish retained incremental-window
integration, the broader interruption/steering matrix or full M2/#49 acceptance.
No default model, live reference or release promotion follows from this run.

## Reproducibility and accounting

The [measured evidence](../../evaluation/evidence/local-converse-vertical-2026-10-28.json)
records exact source/tree, model profiles, output, native ordering, file hashes,
sampled process metrics, cleanup and original artifact digests. A read-only
allowlist bundle retains the frozen harness and public raw artifacts locally;
private auth/state and service tails are excluded. This is an actual experiment,
not a portable CI job. The separate PR96 CI record covers its software baseline.

Two native Agent tasks made five Contributor calls. Their catalog estimate was
$0.001493072; combined development usage at that checkpoint was estimated at
$1.056765402. The conservative budget guard including retained uncertainty was
$3.669340854, within the $22 admission ceiling and $25 authorization. Provider
billing was unavailable; these values are estimates, not invoices. Subsequent
development calls are accounted separately.
