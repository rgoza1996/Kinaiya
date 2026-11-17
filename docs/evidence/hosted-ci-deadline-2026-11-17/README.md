# Hosted full-suite execution budget (#675)

PR #672 head `49f94b564d249d0cc8d26adec07489312bef996d` ran
[CI 43996872998](https://github.com/rgoza1996/Kinaiya/actions/runs/43996872998).
Seven standard jobs passed; hosted Python 3.14 was **cancelled**, not passed.
It started at 01:36:38 UTC and ended at 02:36:53 UTC on November 17, 2026.
The original log reached 72% of the complete non-live suite without printing a
test failure before cancellation. This timing is consistent with the configured
60-minute hosted job limit; the cancellation text alone does not prove its cause.

[original-python314.log.json](original-python314.log.json) retains the entire
original UTF-8 log. Decode `content` without trimming whitespace to reproduce
SHA-256 `9eaa93dc77619a8feee09caa46593e23d11f3fd60bbad4f2752b8dc6503bc72d`.
The condition was recorded in [#675](https://github.com/rgoza1996/Kinaiya/issues/675)
before changing the workflow.

The hosted Python job now has a bounded 120-minute budget, leaving room beyond
the observed cancellation point for the remaining tests, contract export and
dependency audit. Native Python keeps its existing 180-minute budget. Test
selection, assertions, pinned dependencies, runner routing and host admission
policy stay unchanged. The change triggers ordinary PR qualification; it does
not retry the held native #624 run, change Python support or qualify an earlier
head retrospectively. Current-head CI must still finish successfully.
