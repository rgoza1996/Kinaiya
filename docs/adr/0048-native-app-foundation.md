# Android and iOS development applications

Status: implementation contract for #115, a foundation for M8 (#11). Native
compilation is verified only by actual platform build results. This change does
not implement native audio, WebRTC, Telecom/CallKit, push or locked-screen calling.

Generate a fresh scratch application with the existing official Flutter 3.47.8
SDK, commit 737ba8f885dc774b53b442759999a312c9461e4e. Use `--template app
--platforms android,ios --android-language kotlin --project-name kinaiya_flutter
--org dev.kinaiya --no-pub --no-overwrite`, suppress analytics/version checks and
copy only deliberate Android/iOS source into the established Flutter app. Preserve
its pubspec, lockfile, Dart, web, tests and assets. Never run template repair over
the established project. Do not install a native SDK or touch personal devices
or the running host services on this Linux workstation.

The generated development application/bundle identifiers are development choices,
not a production domain, store registration or signing claim. Use the launcher
name `Kinaiya Dev`; generated template launcher icons remain development
placeholders. The existing owner (`lib/main.dart`) and scoped foreground companion
(`lib/companion_main.dart`) are alternative builds of the same identity and replace
each other on installation. Keep their CI artifacts separately named and copy the
first build before the second can overwrite it. No keystore, signing team,
provisioning profile, owner credential or generated local build configuration is
committed.

Android requires ordinary INTERNET access in the main manifest. Add no microphone,
notification, foreground-service, full-screen or Telecom permissions. iOS gets a
truthful local-network purpose string for connecting to an explicitly entered
Kinaiya host; it does not advertise/discover Bonjour services. Add no microphone,
VoIP background mode, PushKit entitlement or CallKit code. Keep normal certificate
verification and platform transport policy: no trust-all code, Android cleartext
override or broad App Transport Security bypass. A physical phone's loopback is
the phone, not the host PC. Reachable trusted HTTPS host configuration is a separate
owner deployment choice; generating applications exposes no service ports.

Native audio adapters still report unavailable. Owner credential-file import and
native presentation persistence/import/export remain unimplemented; existing
manual owner setup is in memory. Prefer the separately scoped companion for phone
control; it never requests an owner credential. Its grant is in memory, and process
death requires pairing again. Existing foreground lifecycle stops future polls
when inactive; an already-dispatched finite request may settle. Preserve uncertain
action reconciliation and `accepted_waiting_media` as waiting for media, never
connected. There is no background service or system incoming-call UI.

Add finite Android and iOS simulator CI jobs while retaining all existing Python
and web jobs. Reuse the official Flutter tag/commit verification and enforced pub
lockfile. The pinned Android template uses JDK 17, SDK 36, NDK 28.2.13676358,
Gradle 9.3.1, AGP 9.1.0 and Kotlin 2.4.0. Use the hosted Ubuntu 24.04 SDK and explicit
official SDK-manager installation of that NDK if absent. Do not automatically
accept new license terms: use only the runner's existing accepted license state,
with closed stdin so an unexpected prompt fails rather than hanging. Record the
actual tool versions. Maven/Gradle/Flutter engine acquisition in these native
builds is intentional; it is not an offline build claim.

Use hosted macOS 15 with its preinstalled Xcode 26.3 and simulator SDK, recording
their actual versions. Selection changes only that ephemeral CI runner. The
generated deployment floor is iOS 15. Build both entrypoints
with `flutter build ios --simulator --debug --no-codesign --no-pub`; do not boot a
simulator, contact a phone, sign, archive for distribution or upload to a store.
Android builds are `flutter build apk --debug --no-pub` for each entrypoint.
Unsigned simulator applications and debug APKs establish compilation, not phone
network, physical audio, platform lifecycle or production calling acceptance.

Keep native checks focused on these builds and artifacts; existing shared tests
already cover Dart behavior. Verify project metadata, transport permissions,
ignored generated files and artifact separation in review. Full M8 remains open.

Sources: the installed pinned Flutter templates and tool constants;
[hosted Ubuntu image](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md),
[hosted macOS image](https://github.com/actions/runner-images/blob/main/images/macos/macos-15-Readme.md),
and Apple's [local-network purpose description](https://developer.apple.com/documentation/bundleresources/information-property-list/nslocalnetworkusagedescription).
Hosted inventories can change; actual CI logs establish the tools used.
