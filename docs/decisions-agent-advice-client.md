# Agent-side Decisions advice client (#566)

`DecisionsAdviceClient` connects the Agent service credential to a fixed
literal loopback Decisions origin. The link disables redirects and environment
proxies. The client is available for the native Agent integration. Launched
processes still use the existing service selection and advice remains disabled
in the launcher.

Before creating a call, a caller can read the Decisions runtime instance and
policy digest through the [internal HTTP API](decisions-agent-advice-api.md).
One submission reserves an original key locally before sending. Its controller,
full request digest, native task identity, objective revision, policy digest
and minimum disclosure scope bind every subsequent observation. A trusted
Agent guard can withdraw authority immediately before the actual dispatch.
The client keeps that key reserved, returns a local dispatch refusal, and
releases the custody slot after its exchanges settle.

The default client holds up to eight active and 64 retained originals. An
exact duplicate uses lookup without sending another submission. A changed
digest under the same key conflicts. Response framing, lengths, media type,
encoding, strict JSON and identity echoes are checked before a result can
settle its original. Complete model text and its disclosure scope travel with
the observation. The observation carries `untrusted_model_data` and
`not_assessed` for model authority and task status.

## Recovery and cleanup

An observer deadline or cancellation leaves the actual HTTP exchange enrolled.
The exchange owns the raw response and its sole close. A matching null lookup
or a remote controller conflict leaves the original unresolved. It can still
arrive later or own an unfinished prediction.

When the caller has an actual Resources operation, the client retains the
original in it. A qualified terminal observation and settlement of every
local HTTP exchange for that key together release the slot. Failed response
close keeps the client's transport unavailable and its original unresolved.
A remote unconfirmed cleanup observation fails the Resources owner and keeps
the original enrolled. A terminal observation remains historical evidence
even while local cleanup is pending.

`cancel` requests cancellation of the exact original. Once that request is
observed, further cancellation calls check its status by lookup. `close`
stops new enrollment and observes exact pending originals under its own
deadline. It returns false while any required cleanup remains unresolved.
A later call can check the same originals. The transport closes once when
the originals and their local exchanges have settled.

Agent worker dispatch, complete qualified input construction, native task
invalidation and launcher wiring remain needed before normal work can use
this client. Standing-task authority remains a separate native boundary.

## Verification

The focused eight-file suite passed 327 tests each on Python 3.13.18 and
3.14.10. It covers the actual Decisions and Session routes, the measured
LM Studio protocol with synthetic frames, a lost submission acknowledgment,
late arrival after a null observation, withheld model and response cleanup,
wrong identity echoes, revoked dispatch, and the surrounding topology and
auxiliary regressions. Ruff check and formatting passed. The tests leave
the existing wire schemas unchanged.
