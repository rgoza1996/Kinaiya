# Core uncertain-save fixture deadline (#652)

The full Flutter verification first failed before the intended Core save: the
initial inspector had no choice widget. The original fixture gave every request
20 ms, including synchronous client acquisition and initial inspection. Its
pre-target failure does not establish any production write outcome.

A retained control adds 50 ms of synchronous acquisition to that original
fixture. It deterministically fails at the same missing-choice boundary, before
the target POST. This demonstrates a fixture fault mechanism; it does not prove
the cause of every earlier coarse failure.

The test now uses the existing native ten-second result budget. It includes the
same 50 ms acquisition control, observes the actual original POST and its exact
body before advancing eleven seconds of fixture time, then checks the retained
unknown result, navigation/reconnect, exact read-only lookup, native fencing and
one mutation. Every original assertion is retained. No application source,
production deadline, cleanup rule or transport bound changes.

The five Core widget cases pass. The Dart analyzer AST audit retains all 29
original expectation expressions and adds two admission expectations; the
original first-failure/control logs, their hashes and exact commands are in
`verification.json` and local `output/issue-652-*`. Full-suite composition is
qualified separately before integration. Model quality is outside this check.
