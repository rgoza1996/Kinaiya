# Live window contract 2.0 qualification (#623)

This increment explicitly selects five-outcome classification while keeping the
four-outcome default and Rolling Windows worker unchanged. Adaptive Live remains
off. See the [contract and remaining integration](../../live-window-contract-v2.md)
and [source-pinned receipt](verification.json).

**64 focused cases pass on Python 3.13.18 in 5.24s and Python 3.14.10 in 5.48s.**
The broader 39-file Decisions/window regression passes **1,221 cases in 213.30s**
with one existing Starlette deprecation warning. All 849 recorded Python inputs
remain unchanged during each final check. Exact commands, environment and log
hashes are in the receipt; raw local logs remain under output/issue-623-live-v2-*.

The independent vectors exercise all five conversation outcomes with all four
Agent routes through the actual compatible HTTP adapter. Actual Laya/native-d1/
OpenRouter HTTP adapters submit the frozen five-choice grammar; each selected
outcome is preserved and old four-way responses are refused. Native source fields
cannot be supplied by the model; exact session/thread/epoch/event/sequence/revision
bindings come from the authenticated original. Private source/prompt disclosure,
coerced versions, old-worker incompatibility and frozen schema exports are covered.
These are synthetic responses, not model-quality or physical-audio evidence.

The [frozen target control](first-target-control.py.txt) first fails twice because
the in-progress v2 implementation accepts an invented task or an unobserved task
revision; nine other malformed-output cases pass. This was tracked under #623
before correction. The repair validates the selected pair against the exact
submitted task references, refuses it without another call and preserves the
original model result. It never invents an IGNORE replacement or permission.

Two earlier test mistakes remain separately recorded:49 setup errors from a
missing synthetic Task idempotency_key, then 15 fixture/observation failures (deque
versus list comparison and missing required NativeD1 min_confidence argument).
They are not behavioral evidence. After correction, all 49 original controls pass;
additional negative/version/disclosure/export controls bring the final count to 64.
Provider thresholds and defaults remain unchanged.

All 538 pre-existing schema files are byte-identical to the parent. The two additive
exports have their independent frozen family pin. Existing finalized classifiers,
provider/model selection and owner instructions remain intact. Ruff, formatting
and diff checks pass. No SDK, dependency, host policy or installed owner changed.

Remaining #623 work includes completion/fresh-source scheduling, qualified
publication/consumer routing, same-capture decoder rollover, backchannel/echo/mute
ownership, timing evidence and rollout. No new worker, default or effect path is
enabled by this increment. Native resource recovery is separately under #657.
