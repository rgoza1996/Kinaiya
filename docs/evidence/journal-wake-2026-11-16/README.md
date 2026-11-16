# Native journal wake qualification (#623)

This passive metadata increment supplies immediate transaction-change wake hints
for a future completion-driven consumer. It starts no inference, capture, playback
or Agent work. Adaptive Live remains off. See the [contract and limits](../../journal-wake.md)
and [source-pinned receipt](verification.json).

The final **175-case matrix passes on Python 3.13.18 in 93.74s and Python 3.14.10
in 94.10s**, with all 855 Python inputs unchanged in each interval. It includes the
48 new wake/HTTP/contract vectors, existing media lifecycle/standing-owner fault
controls and the original legacy schema-family guards. These counts are not added
to an invented full-suite total.

The earlier 169-file direct-Store/SQLite regression produced **17 failed, 4,864
passed, 1 deselected, one existing warning in 1973.16s**. All 17 failures stopped in
the media fixture's obsolete constructor assumption that no connection factory
was supplied; those lifecycle test bodies never ran. The fixture now composes its
original real-SQLite fault observer with the supplied WakeConnection. The standing
commit-boundary observer likewise preserves both behaviors. All 41 original test
bodies (14 media and 27 standing) are AST-identical; the original assertions and
fault injection run against the actual composed connection. Extra checks require
both observer/wake types and the identical Store wake owner. The initial failure
log remains preserved. Production Python inputs are byte-identical to that broader
run; only these two test constructors and the legacy schema exclusion list changed.
No full-green 169-file rerun is claimed.

The [first script control](first-script-control.py.txt) fails after a script's valid
prefix committed but its later statement raised: the old in-progress wrapper
missed the wake until a subsequent read. Finally observation now preserves the
original SQLite outcome and reports its changed state at exit. The [first scheduling
control](first-scheduling-control.py.txt) fails when callback scheduling removes a
slot although its actual Task is still pending. The slot now remains owned until
the actual wait's finally path executes. Both failures were tracked before repair.

A preliminary fixture omitted final:true on its synthetic conversation and used
the wrong delete helper name. That original error is preserved separately and is
not notification-mechanism evidence.

Actual SQLite/ASGI/client controls cover commit, rollback, script prefix/error,
cross-thread wakeup, change-before-subscription, no read/idle self-wake, reset and
deletion, 64 actual retained waits, callback failure, actual cancellation, Store
close/replacement, authenticated role parity, credential rotation, strict types,
ambiguous JSON/byte bounds and the three frozen additive schemas. Caller
cancellation retains the original HTTP wait and sole close through the existing
client registry. A cursor/wake never proves publication, freshness or permissions.

All 540 pre-existing JSON schema files remain byte-identical. The three additions
have their independent frozen pin; the original 184-file aggregate retains its
count and 21351c70... hash. Static/export/diff checks are recorded in the receipt.
No SDK/dependency, host policy, installed owner, model/provider choice, quality gate
or runtime default changed. Full exact-head hosted CI is required before integration.

Notifications cover this original connection, not an unsupported external writer.
Unknown or replaced owners refuse. The one-second maximum idle wait is a heartbeat;
commits wake the original event immediately. The new scheduler/source partition,
backchannel/echo/mute ownership, decoder rollover and rollout remain #623 work.
