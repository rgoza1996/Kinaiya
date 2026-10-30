# ADR0061: Explicit Meta Converse speech and exact prompt measurement

Status: implemented opt-in integration for #142; no live-call or billing qualification.

Converse can explicitly select Meta Responses for dialogue and a separate buffered
Meta STT client, while retaining the existing local `SpeechClient` for TTS. The
local speech client's origin/token policy is unchanged. Neither cloud adapter
asserts local worker, GPU residency, readiness or release evidence. Decisions and
Agent remain independently selected. Persona, output text and speech chunking
are unchanged; there is no retry, cloud fallback or output repair.

An owner profile may select:

```json
{
  "providers": {
    "converse": {
      "kind": "meta",
      "base_url": "http://127.0.0.1:9790/v1",
      "secret_ref": "muse",
      "allow_cloud": true,
      "model": "muse-spark-1.3-contributor",
      "reasoning_effort": "minimal",
      "max_tokens": 2048,
      "context_tokens": 1007997
    }
  },
  "voice": {
    "url": "http://127.0.0.1:8765",
    "profile": "Kokoro 82M",
    "stt": {
      "provider": "meta",
      "base_url": "http://127.0.0.1:9790/v1",
      "secret_ref": "muse",
      "allow_cloud": true,
      "model": "muse-voice-transcribe-1.0",
      "timeout_s": 120
    }
  }
}
```

This is a fragment, not a runnable complete service/resource configuration. The
context limit must be explicitly verified by the owner against the selected
model's current catalog. `/v1/models` returning only IDs is insufficient evidence
for a context limit. On 2026-10-30, the authenticated `/muse-code/models` catalog reported a
1,007,997-token context for `muse-spark-1.3-contributor` and a `minimal` variant.
The live test profile uses that verified context limit. `minimal` is the lowest supported
Muse Spark reasoning effort; `none` is unsupported. These are host declarations,
not mutable app settings or a change to other profiles' defaults.

`measure_prompt` posts a detached copy of the **same complete generation body**
to `/v1/responses/input_tokens`: model, exact rendered messages, reasoning,
structured text/tools when present, output limit and stream field. A strict
positive safe-integer count is accepted only after bounded UTF-8 JSON parsing,
real EOF/response cleanup and unchanged input/configuration. ContextBudgetBuilder
then chooses whole history entries and revalidates the exact whole-prompt budget;
it does not replace token counts with byte lengths or fixed turn counts. A
single-use measured request ticket is revalidated synchronously before generation,
including after context assembly's native awaits. Changed configuration, transport,
provider identity or request text requires another exact count. Only
explicit measurement-capable cloud adapters pass the Converse resource wrapper;
local-only resource policy and local-only DAG recall remain enforced. Native
disclosure checks precede private count requests as well as generation.

The independent STT client sends `request` JSON plus actual `audio` WAV to
`/v1/asr/transcribe`. Browser WebM/Opus and local WAV are decoded in memory using
lazy PyAV, with nested file/network protocols denied, maximum compressed upload
10 MB and both source/decoded duration at most 60 seconds. Output is mono PCM16
16 kHz WAV. Conversion does not load a speech model or retain audio. The real
decoder worker remains owned through cancellation. This optional adapter requires PyAV 17.1.0 and NumPy 2.3.3 for sample conversion
(`meta-speech` extra; hash-pinned `requirements-meta-speech.lock`). No package is
installed on demand. A reusable host was qualified with real generated PCM WAV
and Opus WebM decoding without loading a speech model.

Meta STT uses its own Meta credential and **omits** the Contributor header because
voice is Standard-only. The local TTS bearer never leaves loopback. The native
multipart and window routes authorize captured `content_scope` before cloud
decoding/upload; the current Meta policy permits only `public_synthetic`. Missing
scope remains `local_only`. A private recording must not be relabelled to use this
development Contributor pipeline. Window session/thread/epoch/revision and runtime
availability are rechecked before upload and accepting a result. Owner off/on,
replacement or source change cannot publish a late transcript. An already
admitted remote upload may still be processed/billed after cancellation; local
cleanup is not a remote revocation or provider deletion receipt.

The canonical development guard adds only exact count and buffered STT routes.
Count calls have no generation reservation and a durable bounded audit counter;
they retain the exact body. Audio has a separate bounded subledger under the same
lock/atomic save as text: text admission≤$22, audio≤$3 and combined settled estimates,
unknown reservations and each new admission≤$25 (or a lower existing ledger cap).
Existing text charges, billing floor and uncertainty are preserved. Each audio
request reserves the full 60-second admissible duration at $0.18/hour. A valid full
response settles a conservative upward whole-second estimate, at least the full
uploaded duration and never less because a provider reports a smaller number.
HTTP/transport/partial/malformed/close uncertainty retains the entire reservation.
The guard accepts only the normalizer's canonical 44-byte PCM WAV layout:
one 16-byte `fmt ` chunk, one `data` chunk, exact RIFF/data/file sizes, matching
sample/byte rates and block alignment. Extra chunks, additional audio data or
ambiguous sizes are refused before reservation or forwarding. Counting just a
general WAV parser's first data chunk is insufficient to bound the upload.
Auxiliary responses require unambiguous bounded HTTP framing, actual entity EOF
and successful close before any audio reservation can settle. Duplicate, invalid
or oversized Content-Length, conflicting transfer framing and premature EOF
retain the full reservation even when the received prefix is valid JSON.
Overlapping windows are separate processed requests and separately charged.
This is conservative admission/accounting, not a provider billing receipt.
No transcripts, audio, credentials or prompt text enter the ledger. Auxiliary
count/audio records are each capped at 10000; admission refuses at capacity.

The canonical guard change does not modify or restart an active guard or migrate
a live ledger. The owner must independently review the active-version delta,
preserve its state/uncertainty, validate source/policy and perform any migration.
Paid verification and live promotion are separate gates.

Primary contracts: [reasoning](https://dev.meta.ai/docs/reasoning),
[token counting](https://dev.meta.ai/docs/token-counting),
[Responses schemas](https://dev.meta.ai/docs/api-reference/responses/schemas),
[voice transcribe](https://dev.meta.ai/docs/api-reference/voice/transcribe),
[voice schemas](https://dev.meta.ai/docs/api-reference/voice/schemas),
[speech-to-text](https://dev.meta.ai/docs/speech-to-text),
[pricing](https://dev.meta.ai/docs/pricing-rate-limits). Documentation wording
about injected hidden tokens differs between counting and pricing pages; no
guessed adjustment is added and no measured count is represented as billed usage.
