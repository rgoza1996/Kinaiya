# Converse model setup declarations (contract version 1)

Issue #138 adds an owner setup dialog to the revised Home and optional Converse
presentations. `GET /v1/converse/setup` supplies the current host declarations for
that dialog. It is a read-only inspection endpoint, with no installer, model
discovery, provider call, configuration write, service restart, or model switch.
The supplied visual design does not establish new runtime capabilities.

## Authority and persistence

The endpoint requires the existing owner bearer credential and a permitted browser
origin. Agent, Converse and Resources credentials cannot inspect it. Successful
responses and unavailable-declaration errors carry `Cache-Control: no-store`.
Authentication failures use the existing service authentication errors.

The launcher projects only explicit model identities from its startup
configuration. It discards endpoint URLs, artifact paths, secrets, credential
references and unrelated settings before handing the declaration to Converse.
There is no setup POST. A host model change still requires configuring and
restarting the appropriate service. Inspecting setup does not change owner intent,
resource policy, disclosure consent, permissions, sessions, memory, captures,
playback, or independent Agent/Decisions operation. Client presentation preferences
have a separate owner and persistence mechanism.

The display projection is optional and cannot prevent Converse startup. A valid
provider may use an opaque model alias, such as an absolute local identifier, that
the narrower display contract refuses to publish. Expected projection validation
failures produce a distinct in-process unavailable marker, retaining neither the
configuration nor the exception. Setup inspection then returns the generic
no-store 503; ordinary health, owner controls and the configured runtime keep
their existing owners. This differs from absent embedded metadata (`None`), which
continues to report unknown declarations. Only projection `ValueError` failures
(including Unicode and response-model validation) are converted; unrelated
programming, I/O and runtime exceptions are not swallowed. Provider validation
and runtime construction remain outside this fallback.

The 64-character lowercase hexadecimal `revision` hashes the visible role/input/
reasoning declarations. It changes when those declarations change, not when a
private credential, transient operation count, session or transcript changes. It
is a declaration identity, not an authority token or a hot-swap precondition.

## Wire document

The exported schema is
[`ConverseSetup.json`](../packages/contracts/schemas/ConverseSetup.json); strict
Python response models live in
[`converse_setup.py`](../packages/contracts/converse_setup.py).

The root has `contract_version: 1`, `revision`, `roles`, `inputs`, `reasoning` and
`operation_state`. No event `schema_version` field is added to this owner document.

`roles` always contains exactly `conversation`, `stt`, `tts` and `decisions`. Each
role has:

| Field | Meaning |
| --- | --- |
| `selected_id` | Bounded `provider:model` declaration, or null. |
| `label` | Bounded display name, or null. |
| `configured` | A declaration exists; this does **not** mean loaded, ready or usable. |
| `source` | `runtime` for the active Converse provider object; `host_configuration` for other services. |
| `options` | The one configured `{id,label}` choice, or an empty list. |
| `mutable` | Always false in version 1. |
| `reason` | Explains host configuration, unavailable metadata, and restart requirements. |

The conversation declaration comes from `runtime.provider.info`, not a request or
model output. The other roles describe startup configuration in the host that
launched Converse; their separate processes may be stopped, unloaded, changed or
unavailable. No readiness or GPU placement claim is made. An embedded runtime
without host metadata reports those roles unconfigured and reasoning unavailable.
An unmanaged speech endpoint does not reveal its STT model, so the STT selection
remains unknown. Managed Whisper uses its configured `stt_model` identity, such as
`tiny.en`, without publishing its checkpoint path. Whistle requires its explicit
managed selection. TTS lists only the configured speech profile, never the retained
candidate catalog as though every candidate were installed.

Example of a declared role using synthetic identifiers:

```json
{
  "selected_id": "whisper:tiny.en",
  "label": "Whisper tiny.en",
  "configured": true,
  "source": "host_configuration",
  "options": [{"id": "whisper:tiny.en", "label": "Whisper tiny.en"}],
  "mutable": false,
  "reason": "Configured on host; loaded state is not checked. Changing this model requires a service restart."
}
```

Names use at most 128 UTF-8 bytes before forming an ID; IDs and labels use at most
256 characters. Empty names, control/format characters, absolute paths, parent
traversal and endpoint-shaped identities are refused. Unavailable or malformed
runtime declarations produce a generic 503 with no offending value. The response
models forbid extra fields, bound counters to JavaScript-safe integers and limit
each option list to its single declared selection.

`inputs` describes the implemented conversation transport:

```json
{"supported":["text"],"selected":["text"],"direct_audio":false,"stt_required":true}
```

Microphone input still uses the existing STT bridge before dialogue generation.
Loading a model with an image projector does not establish image/video/audio
transport support. Direct model audio and disabling STT are unavailable in this
contract. These declarations neither enable capture nor bypass disclosure checks.

`reasoning` contains `selected`, its sole `options` value, `mutable:false` and a
`reason`. It reports the actual host request setting: the current native LM Studio
adapter requires `none`; compatible providers without an explicit effort report
`auto`; Meta's default is `xhigh`. Unsupported or future declared effort labels do
not acquire new semantics or UI controls. An embedded runtime without a host
declaration reports `unavailable`. Changing reasoning is not a client-local
cosmetic preference.

`operation_state.text` is the current runtime `active`, `cleanup_pending`,
`cleanup_failed`, `admitting`, and `capacity` count snapshot.
`operation_state.speech` supplies `active`, `cancellation_pending` and
`cleanup_failed`. These counts are observations, not a lock, a native model-load
receipt, or proof that a future operation can start.

## Verification

`tests/contract/test_converse_setup.py` exercises owner and origin authority,
read-only method behavior, absent embedded/unmanaged service metadata, independent
role declarations, reasoning identity, revision stability during activity,
malformed runtime names, secret/path exclusion and exported-schema consistency.
All fixtures are synthetic; no external inference, model load, service restart or
private transcript is needed.
The launcher regression also constructs the accepted opaque provider alias,
captures the real Converse application without binding a server, and verifies
health plus persistent owner off/on controls while setup remains unavailable. A
network transport trap verifies that none of those paths contacts a model.
