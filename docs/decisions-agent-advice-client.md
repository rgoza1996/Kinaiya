# Agent-side Decisions advice client (#566)

`DecisionsAdviceClient` connects the Agent service credential to a fixed
literal loopback Decisions origin. The link disables redirects and environment
proxies. The launcher creates the client when both Agent and Decisions are
selected, the protected next-launch policy or its legacy host source has a
valid declaration, and `decisions_url` names a literal IPv4 loopback origin.
Agent can start and work independently
when the policy or Decisions service is unavailable.

An accepted ordinary Agent worker can request the `decisions_advice` purpose of
`consult_model` when its current native capability and permissions allow it.
The request includes the exact current objective, complete retained role-bearing
history, question, source revisions, retained context references, and effective
disclosure scope. The native worker and Session check those references and the
objective revision before admitting a provider upload. An accepted steer takes
effect at the next stable objective revision. The worker checks retained skill
and plugin disclosure through the native pin guards at the handoff and while it
observes the original. See [the revocation regression](https://github.com/rgoza1996/Kinaiya/issues/708).

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

The worker only returns a completed, measured observation after local and
remote cleanup settles and native task and disclosure checks pass again.
The next Agent model call receives the raw text as an untrusted tool observation.
The native evidence record retains its digest, size, and actual model pin.
Worker interruption requests cancellation of the same original. Unresolved
cleanup remains visible to Agent workspace and Resources lifecycle controls.
Standing tasks require their own admission boundary.

## Verification

The focused eight-file suite passed 327 tests each on Python 3.13.18 and
3.14.10. It covers the actual Decisions and Session routes, the measured
LM Studio protocol with synthetic frames, a lost submission acknowledgment,
late arrival after a null observation, withheld model and response cleanup,
wrong identity echoes, revoked dispatch, and the surrounding topology and
auxiliary regressions. Ruff check and formatting passed. The tests leave
the existing wire schemas unchanged.

The ordinary worker and retained-context integration passed 1,250 tests
per interpreter on those same versions. The final tool advertisement,
catalog and provider wire subset passed 1,039 tests per interpreter.
The normal worker control verifies complete native history and source
revisions, raw advice delivery, actual interruption cleanup, unavailable
Decisions behavior, and selected service launch. The regressions cover
accepted steer during measured preparation, retained skill revocation
during preflight, and accurate advertisement of the optional route.
