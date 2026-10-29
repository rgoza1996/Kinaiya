# ADR 0043: validate Agent file-write arguments before approval

Tracks [#105](https://github.com/rgoza1996/Kinaiya/issues/105), supporting the integration work in [#49](https://github.com/rgoza1996/Kinaiya/issues/49).

Status: implementation under verification. No model, complete Stage B, voice latency or combined 8 GB profile is qualified by this change.

## Problem and contract

Agent previously requested file-write approval before checking argument shape. A nonstring content value could therefore reach approval, and dispatch coerced a numeric path into a filename under an existing write grant. Models and generated actions are untrusted; a generic ToolAction arguments object does not establish a valid file-write request.

The trusted write_file boundary requires path and content strings. The path is nonempty and contains no NUL; content may be empty, must encode as UTF-8, and retains the existing 256 KiB bound. Unknown fields, missing required fields and wrong types are rejected without coercion or stripping.

Existing positive runtime tests include an optional objective_revision inside write arguments and bind it into the exact approval digest. Preserve that compatibility shape. Its value must be an integer, excluding booleans, matching the current native task revision. The supervisor remains the source of revision authority; model metadata cannot change it. The original argument object remains part of digest binding.

Validate before digest construction or approval creation, and revalidate at direct dispatch after awaited scope/source/revision fencing and before approval consumption or filesystem effects. Invalid actions produce bounded unsuccessful observations through the existing task loop, with no fabricated evidence or success. Cancellation, stale-action handling, permissions, source authority, approval expiry and one-use behavior keep their existing checks. Other tool argument schemas remain outside this focused change.

## Verification and retained failures

Use actual AgentRuntime, policy and journal regressions for invalid types and unknown fields under approval, broad-write and YOLO modes, direct-dispatch entry, valid legacy digest binding, native revision checks, empty content, UTF-8/content bounds and recovery through a later valid action. Preserve existing positive approval and steering assertions. These are deterministic tests without paid inference.

An earlier owned voice rehearsal stopped on a test-runner mount-parser defect before speech or Agent requests. A separately reconciled fresh run completed real local speech-to-file work, interruption and a grounded outcome notice, then failed the runner's two-key approval assumption on a second write containing valid legacy revision metadata. Both original failed results remain fixed. Correcting a future runner's compatibility check cannot relabel either result or qualify the separate failed dialogue screen. Actual integration, model quality, physical audio and aggregate VRAM measurements remain distinct evidence.
