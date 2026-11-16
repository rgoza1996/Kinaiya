# Runtime-settings fixture boundaries (#660, #624)

The retained native Flutter job at 72f2a9ac reports 1,463 passing cases and two
runtime-settings failures: expected uncertainty was false, and a positive fence
lookup returned null. The original test/library source is unchanged on the later
5dab6b0a baseline. Its seven original focused cases pass locally; that does not
explain the guarded native failure.

The [frozen first controls](first-controlled-boundaries.dart.txt) reproduce both
symptoms on unchanged production code, with independent actual observations:

- A synchronous acquisition exceeding the 15ms budget closes its original client
  once, sends zero requests and leaves zero transport owners. The old scope
  fixture's expected uncertainty fails because its actual-send precondition was
  never established.
- A lookup sends its exact original request once, but its planned native fence
  stays behind a held response through the result deadline. The original lookup
  owner remains retained. Expected refusal fails with null because that fence
  has not been observed. Teardown releases and joins the original response.

These are demonstrated fixture preconditions, not a measurement proving the
historical runner's exact scheduling cause. The production behavior is correct
at both controlled boundaries. The final negative controls explicitly retain
no-send and unobserved-fence behavior, including no late automatic settlement.

Only two existing positive fixtures now use their ordinary 1,000ms acquisition/
result budget, establish actual sends and join held originals in teardown even
after assertion failure. The independent public display deadlines remain 1–2ms.
Bounded actual-owner waits replace two blind 5ms sleeps. All 77 original expect
expressions remain unchanged, with two additional actual-send assertions.
Production libraries, default deadlines, global capacity, original retention,
scope/backend identity, ABA and native-fence rules are unchanged.

Qualification with the pinned Flutter 3.47.8 / Dart 3.13.7 and unchanged dependency
lock: 18 focused cases pass; the complete Flutter suite passes 1,525 cases with two
opt-in skips in 73.33s tool elapsed. Analysis reports no issues. All source and
dependency-metadata pins remain unchanged during each run, and after advancing
the branch to the already-qualified journal-wake main. The initial environment
preparation failed before any tests because package_graph.json was missing;
existing exact-lock metadata was reused with --no-pub, without package install
or resolution.

[verification.json](verification.json) retains exact commands, input/log hashes,
original failures and the assertion audit. This is local synthetic transport
evidence. The historical native result remains failed; #660 remains open for
original-platform confirmation after integration. No runner, host policy,
installed runtime or model setting was changed.
