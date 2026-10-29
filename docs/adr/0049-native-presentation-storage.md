# Private native presentation storage

Implementation contract for #117, following the native application foundation
in #116. Appearance preferences and the optional selected character package use
the existing presentation snapshot and validation. This adds persistence only;
native document pickers, credential storage, voice and system calling are separate.

## Shared interface

Android and iOS implement MethodChannel `dev.kinaiya/presentation_storage_v1`:

- `read`, with null arguments, returns the complete stored String or null when
  no snapshot exists.
- `write`, with exactly `{"snapshot": String}`, returns null only after atomic
  replacement succeeds. No caller-controlled filename, path, URL or identifier
  is accepted. Unknown methods are not implemented.
- A snapshot is strict Unicode represented by at most 13 * 1024 * 1024 UTF-8
  bytes. Reject unpaired UTF-16 surrogates before encoding, and reject malformed
  UTF-8 on read. Bound file reads even if the file grows after its size check.
  Native and Dart enforce these limits independently. Empty/invalid JSON still
  reaches the existing controller's validation and safe-default recovery.
- Failures use fixed `presentation_invalid`, `presentation_unavailable` or
  `presentation_busy` codes and fixed messages, without paths, raw OS errors,
  snapshot content or details. Never log snapshots or errors containing them.

The Dart implementation selects native storage only on Android/iOS; web keeps
IndexedDB and other platforms keep session storage. A missing plugin, unexpected
return type or failed call propagates to the controller's existing fixed notice
and safe default/session behavior. A ten-second caller timeout keeps restore
and save responses bounded; it does not prove a native write stopped or failed.
Native admission and ordering remain authoritative after a timeout.

## Native ownership and files

Each process has one serial storage worker, shared across engine/activity/plugin
recreation, with at most two accepted operations total (running plus queued).
Reject additional work immediately. Do not perform disk I/O or large UTF-8
encoding/decoding on the platform UI thread. Deliver results on that thread;
detach retires delivery for that engine but does not reorder or silently abandon
an accepted write. The process owns its worker until process exit. No unbounded
thread, queue, retry or background scheduling is introduced.

Android uses a single fixed file under Context.noBackupFilesDir, and Android's
AtomicFile startWrite/finishWrite/failWrite recovery. iOS uses one fixed file in
a private Application Support subdirectory explicitly excluded from backup,
with Foundation atomic replacement and ordinary platform file protection. Keep
the parent excluded so replacement cannot remove the exclusion policy. No file
picker, shared/external storage, remote path or additional permission is needed.
Failed replacement preserves the previous complete snapshot. Concurrent calls
and engine recreation cannot let an older accepted write finish after a newer
accepted write. Abrupt process/device loss durability is not established merely
by a successful API return; reopening revalidates the complete snapshot.

Only presentation preferences and optional artwork enter this file. Service
credentials, host URLs, personality, conversation, model settings and grants
remain outside the format. Existing controller validation, immutable asset
budgets, revision ownership and serialized writes remain unchanged. Corrupt
restores leave the default usable with a notice; failed saves keep the current
session's selection usable and explain that saving was unconfirmed.

## Verification and ownership

Root owns the Dart adapter, channel/controller tests and documentation. A bounded
visible Muse Contributor xhigh worker owns Android's plugin and registration;
a Codex worker owns iOS's plugin and registration. Both follow this exact channel
contract, use no new package, and leave pub/Dart/web/other-platform files to root.
Source review checks file and backup scope, strict bounds, atomic error recovery,
queue/lifecycle ownership and fixed errors. Dart tests cover real controller
restore/save behavior through a synthetic channel, malformed responses, invalid
Unicode/oversize snapshots, timeout and unsupported-platform behavior. Existing
validation/order tests remain required. Native CI compiles both owner/companion
targets for both platforms, alongside Python, Flutter and browser checks.

These checks do not establish physical-device process-restart persistence,
backup exclusion behavior, power-loss durability or filesystem failure recovery.
Those require device testing. No local SDK installation, license acceptance,
device/service contact, new package or model inference is part of this change.
