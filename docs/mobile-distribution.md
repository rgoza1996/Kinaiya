# Mobile distribution: Android APKs and iOS sideload IPAs (#263, #264)

**Status:** experimental distribution path for the existing Flutter clients. It
installs and updates the apps; it does **not** add native audio, calling, push,
background operation or locked-screen calls (those stay under #11). Phones are
clients of a Linux host you configure; they never install or update the host.

Owner decisions this follows: mobile files are hosted on **GitHub Releases**
(Play Store/App Store/TestFlight are optional future routes, not prerequisites),
Android gets signed APKs, and iOS uses eligible sideloading.

## What is distributed

Every nightly/stable release built by `.github/workflows/distribution.yml` can
contain these files, described with SHA-256 digests in `mobile-manifest.json`:

| File | App | Installed identity (stable / nightly) | Signing |
| --- | --- | --- | --- |
| `kinaiya-<tag>-android.apk` | Owner client (`lib/main.dart`) | `dev.kinaiya.kinaiya_flutter` / `…kinaiya_flutter.nightly` | Kinaiya distribution key |
| `kinaiya-companion-<tag>-android.apk` | Scoped companion (`lib/companion_main.dart`) | `…kinaiya_flutter.companion` / `….companion.nightly` | Kinaiya distribution key |
| `kinaiya-<tag>-ios-sideload-unsigned.ipa` | Owner client | `dev.kinaiya.kinaiyaFlutter` / `…kinaiyaFlutter.nightly` | **Unsigned** |
| `kinaiya-companion-<tag>-ios-sideload-unsigned.ipa` | Scoped companion | `…kinaiyaFlutter.companion` / `….companion.nightly` | **Unsigned** |

Local development builds (no `KINAIYA_CHANNEL` define) use `.dev` identities,
labelled **Kinaiya Dev** / **Kinaiya Companion Dev**, so they never collide with
or replace an installed release. Stable, nightly, development, owner and
companion builds coexist and do not share app-private storage or pairing state.

The Android application ID is derived in Gradle from the compiled entry point
and the same `KINAIYA_CHANNEL` define the Dart code reads, so the installed
identity cannot disagree with the in-app channel. iOS receives the matching
bundle ID from a generated, git-ignored `ios/Flutter/Distribution.xcconfig`
written only by `scripts/mobile_build.py`.

Which app to install: the **companion** pairs with a one-time code and never asks
for the host owner credential, so it is the safer choice for a phone. The owner
client offers Converse and Agent text, and needs a manually entered owner
credential that stays in memory only.

## Android

1. Open the release page (or use **Settings → About → Check for updates**) and
   download the APK for the app you want.
2. Optionally compare its SHA-256 with `mobile-manifest.json`/the release notes.
3. Open it and confirm Android's installation prompt. Android may ask you to
   allow your browser to install unknown apps.

**Updates:** Android installs an update only when it is signed with the *same*
certificate as the installed app, so a tampered or foreign APK is rejected by the
OS (`INSTALL_FAILED_UPDATE_INCOMPATIBLE`). The release notes list the signing
certificate SHA-256. In-place updates keep app data. Moving between nightly and
stable means installing the other app; they coexist, and nothing migrates.

## iOS

Kinaiya does not yet have Apple-signed builds. The IPA is **unsigned on purpose**
for tools that re-sign it with *your* Apple ID on your device, such as AltStore,
SideStore or Sideloadly. Apple's rules apply: a free Apple ID re-signs apps for
7 days with a small app limit; a paid developer account extends that. These tools
are third-party software you choose; Kinaiya does not endorse or bundle them.

Apple's EU alternative distribution (Web Distribution or alternative app
marketplaces) requires an authorized developer account, notarization and, for web
distribution, a registered domain. That route is **not provided**; it is blocked
on the owner's Apple Developer enrolment and signing material. See
<https://support.apple.com/en-us/117767> and
<https://developer.apple.com/support/web-distribution-eu/>.

**Updates:** download the newer IPA and install it with the same tool; it keeps
data when the bundle ID matches.

## In-app update check

**Settings → About** (owner) and **About this build** (companion) show version,
channel, build number and source SHA. On Android/iOS release builds, **Check for
updates** makes one anonymous request to the GitHub Releases API, *only when
pressed*. It reports exactly one of: current, update available, newer release
without a file for this app/platform, no release on this channel, feed
unavailable/rate-limited, invalid response, or published release older than
this build. Failures are never reported as "up to date", and older releases are
never offered.

**Open release page** hands an official, validated
`https://github.com/rgoza1996/Kinaiya/releases/tag/<tag>` URL to the browser.
Native code re-validates that URL. The app never downloads, replaces or executes
its own update. Development builds do not check. Host updates remain a separate
owner operation on the Linux host (`kinaiya update`).

## Host connection

Off-loopback connections require HTTPS with normal certificate verification;
there is no trust-all or cleartext override. The companion distinguishes an
untrusted certificate, an unreachable host, and revoked/expired access, and never
reflects raw server text. Grants and owner credentials remain in memory: a
process restart requires pairing again. Persistent, platform-backed credential
storage is not implemented (it needs its own threat model and tests).

## Release owner setup (one time)

Android signing is skipped, with a warning and no APK, until configured. Create a
dedicated key for GitHub-distributed APKs; do **not** reuse a Play upload key and
never commit it:

```sh
keytool -genkeypair -keystore kinaiya-distribution.jks -storetype PKCS12 \
  -alias kinaiya -keyalg RSA -keysize 4096 -validity 10000 \
  -dname "CN=Kinaiya, O=Kinaiya"
keytool -list -v -keystore kinaiya-distribution.jks -alias kinaiya | grep SHA256
base64 -w0 kinaiya-distribution.jks   # value for the secret below
```

In each of the `release-nightly` and `release-stable` environments, add secrets
`KINAIYA_ANDROID_KEYSTORE_BASE64`, `KINAIYA_ANDROID_KEYSTORE_PASSWORD`,
`KINAIYA_ANDROID_KEY_ALIAS`, `KINAIYA_ANDROID_KEY_PASSWORD`, and the variable
`KINAIYA_ANDROID_CERT_SHA256` (colon-free lowercase hex of the certificate
SHA-256). Keep an offline backup: losing the key means users must uninstall to
move to a new one. Gradle refuses to package a nightly/stable APK without the
key or with an Android debug certificate, and `scripts/mobile_build.py`
verifies the package ID, versionCode, v2 signature and pinned fingerprint.

## Verification and limits

`.github/workflows/mobile-distribution.yml` runs on relevant pull requests:

- Android: release builds through `scripts/mobile_build.py` with throwaway CI
  keys, refusal without signing material, then on an Android emulator: install,
  launch, in-place upgrade (versionCode 1 → 2, first-install time kept),
  rejection of an APK signed with a different key, and owner/companion nightly
  coexistence.
- iOS: unsigned IPAs with the expected bundle ID, display name and build number,
  plus a simulator install/launch of the nightly identity.

Unit tests cover identity rules (`tests/unit/test_mobile_build.py`) and the update
checker/launcher/panel (`apps/kinaiya_flutter/test/distribution_test.dart`).

**Not yet verified:** installation on physical Android or iOS devices,
installation through a specific sideloading tool, real host pairing from a phone
over the network, Play Store/App Store/TestFlight routes, and any signed iOS
build. Record those with device model, OS version, app build and host revision
before claiming them.
