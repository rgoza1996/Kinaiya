# Converse Capture Mode preference (#623)

This increment persists and inspects the next Call's capture choice. Adaptive
Live remains unavailable: its streaming scheduler/backchannel implementation and
rollout are tracked in [the complete plan](adaptive-live-capture.md). It changes
neither a model/provider selection nor an execution permission.

## Native owner and contract

Converse owns the protected `converse-capture-mode.sqlite` in the host data
directory. Core retains shared provider/security/storage ownership. The
`converse_capture.v1` contract has three values: `adaptive_live`,
`rolling_windows`, and `legacy_silence`. Selection is either an explicit saved
value or a pointer to the host default. A saved value survives restart and a
default change, even when that value is currently unsupported.

| Owner-only endpoint | Result |
| --- | --- |
| `GET /v1/converse/options/capture-mode` | Current preference, default, native identity and CAS observation |
| `POST /v1/converse/options/capture-mode` | Save one exact original change for the next capture |
| `POST /v1/converse/options/capture-mode-changes/lookup` | Read the exact original's immutable receipt or native absence/fence |

The five `CaptureMode*.json` schemas in `packages/contracts/schemas` define the
closed wire shapes. Requests use bounded integer-only JSON, at most 4,096 bytes
and five seconds for body acquisition. Responses are at most 16,384 bytes and
uncached. Missing/unavailable native authority returns 503, malformed input 422,
an oversized body 413, and a conflicting original or stale CAS 409. Owner
authentication remains required while Converse inference is off; Agent tokens
cannot change this preference.

A change binds backend identity, controller generation/epoch, revision, state
token, observed default, selection and idempotency key. The transaction stores
the exact original and receipt before acknowledgment. An exact replay returns
that receipt; the same key cannot identify a different original. Revision is
bounded to 1,000 changes; the existing preference remains inspectable at capacity.
The file has a 64 MiB logical bound and protected identity/schema/history checks.
An existing empty/corrupt file, workspace alias, symlink, foreign authority or
unconfirmed rollback cannot create a fresh writable authority silently.

Replacement controllers fence earlier writes. Lookup never retries a mutation.
Absence alone, matching current values, a timeout or a host-default change does
not prove that an original failed or cannot still commit. A positive receipt
must match every original binding. The browser retains at most 16 owner/backend
draft/recovery scopes in RAM and refuses further admission when it cannot retain
the original. Its independent transport retains the existing ten-second result
and one-second cleanup bounds.

`capture_state: not_observed` and `change_boundary: next_capture` are explicit:
these preference receipts establish no physical microphone/playback state and
start no audio process, provider request, model lease or Agent work. Runtime
shutdown retires this database even if another dependency's cleanup fails.

## Resolution and owner UI

An explicit host declaration is `converse_capture: {mode: <one of the three>}`.
Until Adaptive Live is qualified, implicit host configurations preserve existing
behavior: configured modular windows resolve to Rolling Windows; native buffered
audio and the old non-window path resolve to Legacy Silence. Invalid or unknown
defaults remain unknown. An explicit saved choice never selects another mode on
capability loss.

Converse → Settings → Voice shows Capture Mode independently of optional audio
output controls. It separates the selection for the next Call from the actual
current Call, its pending start and an unconfirmed microphone stop. Legacy
Silence is under Advanced/Compatibility; its end-silence control is shown only
for that inspected/current mode. Generic Call/Tell help remains available before
inspection. Inspecting or saving a preference does not arm the microphone.

Each new Call inspects the native preference before capture, checks retained
save uncertainty and rechecks the connection/audio-input owner. Its capture
choice remains fixed for that Call. Unsupported, unknown, unavailable or
unconfirmed settings produce a visible refusal. A late inspection cannot arm a
different input owner. Saving during an existing Call affects a subsequent Call.
The old native/window producer contracts and one-message Tell operation remain
separate compatibility paths; this preference is not capture authorization.

Adaptive Live is disabled in both the native snapshot and this client. A saved
Adaptive selection remains visible and unavailable. Enabling the completed path
and making it the default for new configurations requires the qualification and
rollout recorded in #623; this increment does neither.
