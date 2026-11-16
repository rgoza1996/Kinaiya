# Gated live mic/STT lifecycle qualification (#623)

This record covers the [input/lifecycle increment](../../live-stream-capture.md),
based on `aaf2cfa49853232949493ea77e0a551e4fbd34ae`, branch
`codex/623-adaptive-host-integration`. The normal launcher, browser Call,
Converse/Agent v2 consumers and Adaptive Live default remain gated off.

The final focused run passed **96 cases on each Python 3.13.18 and 3.14.10**.
Each reported one existing Starlette deprecation warning. Their 896 Python inputs
stayed unchanged. The final broader regression passed **1,932 cases in 489.50s**,
also with one existing Starlette warning. All three runs match the final 896
Python inputs. Full Ruff, 888-file formatting and diff checks passed. These are
input/lifecycle architecture checks, not whole-feature or physical-audio
qualification.

[verification.json](verification.json) preserves actual commands, results, log
hashes and the issue comments recorded before repairs. The one shared
[Python input manifest](python-input-sha256.json) identifies the frozen source.
All 568 [prior schema files](prior-schema-sha256.json) retain their bytes. Eleven
additive readiness/stream schemas have independent aggregate pin
`e7453211778e17609023c0488655584ed8d2bf9581873cd7c125c0303e813f34`;
the original 184-schema aggregate check also passed. [source-audit.json](source-audit.json)
records the preserved v1 default hooks and the normal launcher gate.

| Requirement | Implementation | Actual controls |
| --- | --- | --- |
| Explicit live Task lifecycle and v2 readiness; legacy independence | Decisions API, `RequiredLiveDecisions`, Converse app lifespan | `test_live_decisions_readiness.py`, `test_live_stream_api.py`, existing required-readiness tests |
| Registration before STT/source; one immutable fence; retained metadata ownership | `LiveMicCapture` | Delayed/cancelled registration, native negative fence, lost publication/fence acknowledgment, competing fence and empty-lookup controls |
| One logical mic/STT lease across decoder segments | `LiveIncrementalStreams` | Real synthetic Whistle IPC and native lease/admission ledger; contiguous rollover, fixed STT pin, monotonic source slots/absolute sample offsets, retained prior-segment provider context |
| Native revocation before fresh STT; truthful cleanup | Native permission recheck, retained original stream/lease | Native fence/controller replacement, never-dispatched PCM settlement, held actual HTTP close and pending actual paired-write controls |
| Exact paired source and recovery | Atomic writer and live publication lookup | Lost-ACK lookup without audio/publication replay, scope substitution, detached request, late recognized input after owner-off/controller replacement |
| Closed versioned wire and independent old modes | Eleven exported schemas, shared v1 hooks with original defaults | Required-field/byte pins, strict counters/booleans, old schema aggregate, default gate, role/envelope bounds and existing v1 stream cases |

The focused HTTP composition starts the real live worker and readiness monitor,
submits synthetic PCM through real broker/worker IPC, publishes its native pair,
and observes the exact five-outcome native result. A synthetic request-bound
compatible provider supplies diagnostic output; this is not a model-quality or
audible-backchannel result. No model reroll, confidence/style override, silent
provider change, package installation, host-policy or installed-owner change is
part of this increment.

Eight original failure groups and their relevant source/control snapshots are
retained here. Fixture mistakes are identified separately from production
faults; corrected fixture checks still require the original behavior. Original
assertions for actual harness faults were not weakened. Counts across focused
and broader checks overlap and must not be added as unique coverage.
