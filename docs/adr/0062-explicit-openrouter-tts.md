# ADR0062: Explicit OpenRouter Kokoro TTS for Converse

Status: opt-in integration for #146; physical playback and production qualification remain separate.

Converse may explicitly select OpenRouter's `hexgrad/kokoro-82m` for speech
output independently of its dialogue and transcription providers. The selected
voice is `af_heart`. Existing local voice configurations retain their one-argument
speech interface. A remote failure does not select a local speech model, retry
the request or alter dialogue output.

An owner profile may contain this fragment:

```json
{
  "voice": {
    "stt": {
      "provider": "meta",
      "allow_cloud": true,
      "secret_ref": "muse",
      "model": "muse-voice-transcribe-1.0"
    },
    "tts": {
      "provider": "openrouter",
      "allow_cloud": true,
      "secret_ref": "openrouter",
      "model": "hexgrad/kokoro-82m",
      "voice": "af_heart",
      "base_url": "https://openrouter.ai/api/v1",
      "timeout_s": 120,
      "budget_path": "/owner-private/openrouter-tts-budget.json",
      "budget_limit_usd": 1.0
    }
  }
}
```

This is not a complete service/resource configuration. `budget_path` is an
owner-only local accounting file outside the Agent workspace. The OpenRouter
key is read from the protected file selected by `KINAIYA_OPENROUTER_KEY_FILE`,
or the `kinaiya/openrouter.key` file under the owner's XDG configuration directory.
The host profile contains a fixed secret reference rather than a key. Meta's
credential, Contributor header and Kinaiya's internal service bearer never enter
the OpenRouter transport. The transport bypasses ambient proxies and refuses
redirects. Provider setup publishes the selected cloud model, without exposing
credential references, accounting paths or claiming that a local worker is ready.

The native speech route authorizes the **persisted reply's** `content_scope`
before opening a cloud stream. Missing historical scope stays `local_only`.
Reply scope already incorporates current input, the history actually consumed
and persona scope; a speech request cannot replace it with a broader grant.
OpenRouter is an ordinary cloud provider under the shared scope policy, allowing
`public_synthetic` or explicitly `private_cloud_authorized` replies. This does
not broaden the separate Contributor or Meta STT policy. A Contributor dialogue
profile continues to admit only public or synthetic conversation.

The producer calls the explicit `synthesize_scoped(text, content_scope, current)`
interface. Its native currentness callback checks operation cancellation, runtime
identity, owner/availability admission, playback generation and session/thread/
epoch identity around asynchronous validation. The route pins the selected TTS
object and metadata. Composition additionally checks the protected resource
profile, desired Converse state, ready phase and policy revision before and
after the native callback. Off/on transitions, replacement and newer policy
revisions cannot authorize an old reply. Local synthesis keeps its existing
interface and does not depend on task-context propagation.

The OpenRouter adapter requests real PCM audio through `/audio/speech` and emits
bounded mono PCM16 little-endian frames with actual sample counts. The native
route supplies event, generation and chunk identity; provider fields cannot
override them. HTTP response ownership remains retained through cancellation
and real cleanup, using the existing Converse cleanup registry. An unconfirmed
close continues to block new speech. Cancellation fences later local output;
it does not prove remote processing, billing or physical playback has stopped.

Each admitted request is conservatively charged to a protected persistent
ledger before export, using UTF-8 input bytes at $0.62 per million. Completion
marks the record complete without a refund; uncertain requests remain charged.
The explicit development profile has a $1 cap. These bounds are conservative
local admission accounting, not a provider invoice or reconciliation service.

Selecting both cloud STT and cloud TTS constructs no local speech client.
Selecting local STT with cloud TTS retains one local client only for that STT
path. Speech composition does not retire, restart or reconfigure an independently
managed local worker. Resource lifecycle changes require their existing owner
control path.

Synthetic native integration checks cover reply-scope floors, client relabeling,
actual PCM frame shape, selected-backend replacement, owner off/on, session and
generation changes, and protected-policy changes before export or before accepting
late audio. Existing speech cancellation tests cover blocked headers/audio,
backpressure and retained cleanup ownership. No private conversation replay or
paid call is required by those tests.

Primary sources verified on 2026-10-30:
[OpenRouter TTS guide](https://openrouter.ai/docs/guides/overview/multimodal/tts),
[Kokoro API](https://openrouter.ai/hexgrad/kokoro-82m/api), and
[speech model catalog](https://openrouter.ai/api/v1/models?output_modalities=speech).
