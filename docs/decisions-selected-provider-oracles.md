# Selected-provider currentness observations (#614)

The OpenRouter catalog/selection integration oracle must establish a successful
selected-model classification, its original task/revision target, exact request
bytes, raw response and completed inspection capture. A permitted 500 ms native
currentness refusal cannot establish that success. It also does not establish
that the selected provider implementation is broken.

The first CI failure is retained: PR #605, head `b06fee90`, run `43696731664`,
Python 3.11 job `132758828677`, one failure with 14,876 passes. A controlled
genuine Session latest-window response held after its handler returns 200
reproduces that strict refusal before any provider request. The original
unmodified positive oracle fails, with one error diagnostic, zero provider
requests and zero native reservations. This establishes the controlled mechanism;
the CI job's historical inner scheduling cause remains unproved.

The corrected **test oracle** permits at most three explicitly fresh diagnostic
observations, using the same original model lease, source and classifier. It
continues only for the exact bounded-currentness refusal with a TimeoutError
cause, zero observed provider transport entries, a matching retained error
capture, no raw/validated output, no native reservation and no routing event.
Every refused capture remains recorded with its source, model pin and observation
ordinal. Missing or uncertain capture evidence fails these checks. A fresh
classification must still pass all production currentness and disclosure guards.

The final positive assertions still require exactly one provider request, the
expected classification and task revision, matching selected model pin, exact
request and raw response bytes, and a completed capture. All preceding capture
request bytes must match that same provider request. Three refusals exhaust the
oracle and fail; a dispatched provider opportunity cannot be reobserved, even
if an exception has the same text and cause. Controlled tests cover all three
cases without contacting an external service or using real inference.

This manual fixture uses `decision_attempt(..., admission=None)`: its inspection
captures are observational. They do not create or refund finalized native
attempts, and their count must not be treated as a native allowance or billing
claim. Live-window and finalized production accounting retain their existing
separate contracts. No production source, currentness deadline, provider retry,
confidence policy, native limit or runtime recovery behavior changes.

## Unconfirmed inspection finish (#643)

A bounded capture finish is best effort. If its write is unconfirmed, the native
row may remain `started`, which the inspection contract calls unknown. The
positive selection oracle now raises the specific test-only
`UnconfirmedSelectionCapture` failure after checking the original call, source,
model pin, observation ordinal, empty output, zero provider requests, zero native
reservations and no routing event. It neither fabricates an error completion nor
permits another diagnostic observation. All other invalid evidence still fails
the original assertions.

The exhaustion negative control proves either three confirmed pre-dispatch
refusals or an earlier conservative stop on unconfirmed inspection. It checks the
exact distinct failure and capture states in each case. It does not accept an
arbitrary AssertionError. This does not relax the positive oracle: catalog,
request/output binding and successful classification still require the original
completed capture and exactly one provider request.

The first hosted 3.12 failure in PR #636/run 43765825209 is retained: the bounded
native currentness refusal was followed by a `started` capture, and the old
negative control failed its terminal-error assertion (14,988 other cases passed).
Its particular finish failure cause remains unproven. Controlled actual
Session/SQLite/JournalClient cases hold the finish before commit or only its
acknowledgement after commit. Before commit, unknown inspection stops the
positive oracle without a provider request or fresh observation; after commit,
the retained error proof permits the existing single successful provider request.
The first three controlled cases give two failures and one pass with the old
oracle. Separate evidence records final checks and source/log hashes. No
production deadline, retry, native admission or model behavior changes.
