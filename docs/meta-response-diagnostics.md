# Meta terminal response diagnostics (#453)

Meta `failed` and `incomplete` responses remain terminal failures with no
automatic retry, output repair, fallback or success inference. `failed` now
reports `Meta response failed`; `incomplete` reports `Meta response did not
complete`. Only the exact native `incomplete_details.reason` value
`max_output_tokens` adds `output token budget exhausted`. Missing, malformed,
unknown or filtered output is not evidence of token exhaustion. Streaming
errors likewise make no unsupported budget claim.

An authenticated failed `agent.outcome` can include the optional
`provider_response_diagnostic` projection in its existing journal envelope:

```json
{
  "schema_version": "1.0",
  "provider": "meta",
  "status": "incomplete",
  "reason": "max_output_tokens",
  "error_code": "missing",
  "request_sha256": "<native canonical frozen request digest>",
  "task_id": "<native task UUID>",
  "objective_revision": 1,
  "generation_id": "<native context observation operation UUID>",
  "usage": {"input_tokens": 23, "output_tokens": 7, "total_tokens": 30}
}
```

The example UUID/digest placeholders are explanatory, not accepted values.
The supervisor supplies the task/revision and per-inference generation ID.
The provider's digest must match the final consumed prepared request's exact
measurement. Each generation receives its own native operation ID, including
when two generations submit identical bodies. This digest is an existing
native request binding, not an upstream request or response identifier.
The journal already supplies session/thread/epoch, task/model pins and
source references. Diagnostics grant no permission or completion authority.

The closed encoded projection is at most **1 KiB**. Native reason labels are
`max_output_tokens` and `content_filter`; failed status uses `not_applicable`.
Allowlisted error codes are `server_error`, `rate_limit_exceeded`,
`invalid_prompt`, `invalid_request_error`, `context_length_exceeded`,
`content_filter` and `model_error`. These labels report supplied metadata,
not an independently established cause. Other values become `unknown`;
absent/null fields become `missing`; invalid types, strings exceeding 64
characters, or metadata containers exceeding 16 keys become `malformed`.

Optional usage retains only canonical `input_tokens`, `output_tokens` and,
when present, consistent `total_tokens`. Input/output must both be present,
strict non-boolean integers between zero and `2^53-1`; their sum and supplied
total must fit that bound and agree. Invalid or oversized usage is omitted.
Other fields, nested details and legacy usage aliases are not retained.
This observation does not change billing, accounting or unresolved spend.

Arbitrary server messages, IDs, output, raw bodies, prompts, credentials,
headers, private input, parser details and exception causes never enter this
projection. It does not enable the Decisions raw-capture facility for Agent.
The supervisor rebuilds the closed projection before changing task state to
failed; invalid optional metadata cannot block the ordinary failure outcome.
Steering, cancellation, disclosure and original transport cleanup fences
remain authoritative. A close failure or cancellation is not wrapped as a
Meta status error. Old-epoch journal writes remain rejected.

An earlier verified file or UI artifact remains usable if a later provider
call fails; it does not make the task successful. The prior November 4 tasks
listed in [#453](https://github.com/rgoza1996/Kinaiya/issues/453) lack these
retained diagnostics. Their actual causes remain unknown; this change does
not backfill a status, reason, usage or token-exhaustion claim.

Implementation: `packages/providers/response_diagnostics.py`,
`packages/providers/text.py` and `services/agent/runtime.py`. Deterministic
synthetic coverage is in `tests/unit/test_meta_response_diagnostics.py` and
`tests/integration/test_agent_meta_response_diagnostics.py`. Existing
[UI decode diagnostics](ui-decode-diagnostics.md), provider admission,
disclosure and cleanup checks remain separate. No exported contract, schema
pin or legacy diagnostic shape changes.
