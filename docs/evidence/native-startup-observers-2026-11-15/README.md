# Native startup failure observers

Refs [#639](https://github.com/rgoza1996/Kinaiya/issues/639) and
[#620](https://github.com/rgoza1996/Kinaiya/issues/620). Baseline:
`548f85d84874b2b0d3366ffdb36f8c74aa880ae8`.

The first full local Python 3.13 qualification failed on an initial standing
dispatch GET returning 503. A hosted 3.14 run previously failed at the same
setup boundary. The hosted 3.13 qualification instead failed a positive media
slot-reuse case with `pre_command_mismatch`. Their causes remain unproven.
These test-only observers retain bounded context for a future original failure;
they repair neither native failure and close neither issue.

## Observation boundaries

`observe_standing_read` temporarily tees the existing owner's `run` for the
single initial GET. It calls the original once, preserves its result or raised
exception and restores the exact original instance-method shape on success,
failure and cancellation. The fixture still requires the original 200 response
before any grant or write; a 503 fails without retry. Only owner-closed state,
active-operation count, elapsed time and up to eight exception types with twenty
static code locations each are retained. Error text, arguments, locals, full
paths, SQL and payloads are excluded, including suppressed exception context.

`observe_media_commands` tees the existing `_command` and `_read_observation`
methods through the test fixture's monkeypatch owner. It performs no extra proc
read or descriptor operation, returns the exact original values and logs only a
false command result. The observation distinguishes unavailable, empty and
present bytes, reporting only a byte count. It retains at most 64 failures and
no argv, nonce, command bytes, hash or PID. Native process identity, pidfd,
grant, cleanup, permission, retry and deadline behavior is unchanged. Actual
runtime source is byte-identical to the baseline.

## Verification

The controlled real-ASGI closed-authority case first gives eight passes and one
failure: the original single GET still returns its native 503, but the fixture
does not retain owner context. After wiring the observer, the same refusal and
single-read assertion pass with safe context. Synthetic controls check exact
return identity and call count, restoration of an existing override, cancellation,
cyclic/bounded exception chains, privacy and single-read media predicates. These
synthetic checks do not establish native process identity or authority.

- Python 3.13.18: **150 passed**, zero failures/skips, 71.18s, across the observer
  unit/setup controls and the existing media process, retirement, actual decoder,
  standing execution, resource-binding Agent and physical-timeout modules.
- Python 3.12.3: **13 observer controls passed**, 1.10s.
- Python 3.14.10: **31 passed**, 21.68s: observer controls, the actual 18-original
  media slot-reuse case, the full resource-binding Agent module and the original
  physical-timeout case that failed during 3.13 setup.
- Ruff check, format (1,043 files) and diff checks pass.

These overlapping runs are reported separately. Passing targeted original cases
does not reproduce or resolve the intermittent full-suite failures. Exact-head
hosted CI remains a separate result.

The earlier development attempts are retained: the absent-helper collection
errors; a pytest assertion-message representation mistake in the new oracle;
and an extended synthetic-chain constructor mistake (119 passed, one failed,
29 decoder skips in the 3.12 environment). The oracle mistakes were corrected
without changing native behavior. The final 3.13 run exercises actual codecs
with no skips. Source/log hashes and commands are in
[verification.json](verification.json); raw logs remain in local `output/`.
