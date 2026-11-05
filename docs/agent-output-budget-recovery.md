# Agent recovery after settled Meta output exhaustion

Issue [#484](https://github.com/rgoza1996/Kinaiya/issues/484) preserves the
November 4 native failure at the configured 32,768 output-token ceiling. That
failed task remains failed. This behavior does not resume a historical task or
establish that its requested work was completed.

## Narrow recovery boundary

One fresh replan is available per current objective revision only when the
exact measured buffered Meta request proves all of the following:

- The original bounded response reached EOF, its actual owned HTTP close
  completed, its total deadline succeeded, and its HTTP status is 2xx.
- A strict full-object parser rejects duplicate fields, invalid UTF-8,
  unpaired Unicode surrogates and nonfinite values, including ignored fields.
- Status is exactly `incomplete`, details are exactly
  `{"reason":"max_output_tokens"}`, and no competing error is present.
- Returned usage is valid and positive, its optional total is consistent,
  and output tokens equal this request's declared output ceiling. Returned
  usage is provider metadata, not independent billing verification.
- An opaque, one-use native attempt binds the exact provider, caller Task and
  measured request digest. The native task/revision/generation, source,
  session/epoch, grants and disclosure checks still hold after close.

An exact native resource wrapper retains its own policy predicate. Foreign
providers, subclasses, unmeasured fixtures and ordinary diagnostic exceptions
cannot supply the settlement capability. Streamed responses are unchanged.

## What happens next

The runtime consumes the revision's allowance before awaiting or publishing a
notice. It records a bounded native `agent.state` observation with the original
redacted diagnostic and adds constant feedback asking for one small valid next
action. No partial assistant output, action or tool result enters task history.
The next ordinary loop iteration performs fresh exact counting and native
export admission. Existing monetary admission applies to that new request.

The model, effort, output/context limits, owner step limit, objective and grants
remain unchanged. Recovery creates no evidence, clears no failed effect or
publication fence, and never bypasses native executable/media completion
checks. A second exhaustion in the same revision is terminal. An authenticated
objective revision can receive its own one-use allowance.

Timeouts, cancellation, cleanup uncertainty, failed/unknown status, filtering,
malformed metadata and stale authority retain terminal or native control
behavior. There is no transport retry, provider fallback, partial-action
repair, automatic finish or budget refund.

## Verification status

Independent root and companion source reviews preceded execution. With the
installed environment, `ruff format` and `ruff check` passed for the five
changed Python files. The initial focused command
`python -m pytest -q tests/unit/test_meta_output_budget_diagnostic.py tests/integration/test_meta_output_budget_recovery.py`
passed **133 tests in 7.61 seconds**. A separate 19-module regression command
passed **1,207 tests in 156.96 seconds**, covering Agent runtime, prepared
count/admission, history budgeting, existing Meta status/timeout/action/UI
behavior, task programs/media, and standing execution. Neither run failed or
skipped a test; all 775 repository Python inputs remained unchanged during
each run. `git diff --check` also passed.

The new fixtures use public synthetic data and mock HTTP only. The actual
historical task failure remains preserved; these checks do not establish a
successful translation or model-quality improvement. No live rollout or
provider inference was performed for this correction.
