# Foreground companion client

Status: experimental foreground control client; follows #108, implements #109.

Use a separate Flutter entry point for the phone companion. It connects only to
the Session service's companion routes, holds only a scoped device bearer, and
does not reuse the owner AppState, Agent endpoints or Converse credentials. The
existing owner application retains its independent state and voice ownership.

Pairing consumes one high-entropy invitation over HTTPS (loopback HTTP is allowed
only for local development). Device credentials stay in memory, travel only in
Authorization headers, never enter URLs or persisted settings, and are cleared
on disconnect/disposal. Per-request HTTP clients reject redirects and close at
completion or timeout. Errors do not reflect host bodies or credential echoes.
Transport close precedes stream cancellation. A cancellation unconfirmed after
300 ms retires the grant and requires pairing again; polling cannot wait forever
or continue through that uncertain adapter cleanup.
Lost pairing receipts require a new invitation; the host cannot reissue the raw
device secret. Disconnect is local forgetting; revocation is the host operation.

The controller polls only while foregrounded, with one refresh in flight and a
two-second delay after completion. Each request has a five-second header/read
deadline and a 256 KiB response cap. At most five pages of twenty calls are read
per refresh, matching the host's hundred-call retention bound. Cursor, identity,
epoch, revision and UTF-8 note limits are checked before publication. The newest
call is displayed first; later pages cannot conceal a new invitation.

Every operation captures a pairing generation. Disconnect, disposal or re-pair
closes pending transports and retires old callbacks. Call revisions merge
monotonically so an older poll cannot overwrite an action receipt. An ambiguous
action retains the exact request and key for retry; a newer authoritative call
snapshot reconciles it without claiming which actor caused the transition.
HTTP409 clears that command and refreshes rather than submitting a fresh action.
Access denial clears the local grant and all displayed call data.
Replacing a home widget's controller disposes only the controller it created;
injected controllers remain the caller's responsibility.

Answer means accepted and awaiting media. There is no connected/active state or
media-ready callback in this slice. Host notes render as literal selectable text
and survive terminal call states; they cannot execute, auto-link or dispatch.
Android/iOS native projects, WebRTC audio, secure persistent native credentials,
push/system calling and real-device background/locked-screen verification remain
M8 work. Building the web target establishes only the shared foreground client.

Verification lives in `test/companion_test.dart` and the deterministic CI's
separate companion web build, with actual host/client integration recorded in the
PR. Synthetic HTTP fixtures establish protocol/lifetime behavior, not call media,
physical audibility or device delivery.
