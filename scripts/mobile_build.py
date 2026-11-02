"""Build one distributable mobile client artifact (#263/#264).

Run from the repository root on a release runner:

    python -m scripts.mobile_build --platform android --app kinaiya --channel nightly \
        --version 0.1.0 --build-number 42 --source-sha <40-hex> --output dist-mobile

Identity rules are shared with ``apps/kinaiya_flutter/android/app/build.gradle.kts``
(which derives the Android application ID from the same ``KINAIYA_CHANNEL`` define)
and ``lib/distribution/*.dart`` (asset naming used by the in-app update check).

- Android: release APK signed with the distribution key from the job environment.
  Gradle refuses to package a nightly/stable APK without it; this script also
  verifies the produced signature, package name and versionCode with the SDK's
  ``apksigner``/``aapt2`` before publishing it.
- iOS: an **unsigned** IPA for sideloading tools that re-sign with the installing
  user's Apple ID. It is labelled as such and is never presented as an App Store,
  TestFlight or notarized build.

Each artifact gets a sidecar JSON entry; ``--merge`` validates the sidecars and
writes ``mobile-manifest.json``. Nothing here publishes, uploads or signs iOS code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP_DIR = REPO / "apps" / "kinaiya_flutter"
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
SHA = re.compile(r"[0-9a-f]{40}")
ROOT = "https://github.com/rgoza1996/Kinaiya/releases"
TARGETS = {"kinaiya": "lib/main.dart", "companion": "lib/companion_main.dart"}
ASSET_PREFIX = {"kinaiya": "kinaiya", "companion": "kinaiya-companion"}
ASSET_SUFFIX = {"android": "android.apk", "ios": "ios-sideload-unsigned.ipa"}
ANDROID_BASE_ID = "dev.kinaiya.kinaiya_flutter"
IOS_BASE_ID = "dev.kinaiya.kinaiyaFlutter"
MAX_VERSION_CODE = 2_100_000_000


class MobileBuildError(ValueError):
    """A refusal; never silently substitutes a weaker artifact."""


def _require(condition, message):
    if not condition:
        raise MobileBuildError(message)


def identity(*, app, platform, channel, version, build_number, source_sha):
    """Validate inputs and derive the installed identity shared with Gradle/Dart."""
    _require(app in TARGETS, "Unsupported app")
    _require(platform in ASSET_SUFFIX, "Unsupported platform")
    _require(channel in ("nightly", "stable"), "Mobile distribution requires nightly or stable")
    _require(isinstance(version, str) and VERSION.fullmatch(version), "Version must be X.Y.Z")
    _require(
        type(build_number) is int and 1 <= build_number <= MAX_VERSION_CODE,
        "Build number must fit an Android versionCode",
    )
    _require(isinstance(source_sha, str) and SHA.fullmatch(source_sha), "Invalid source SHA")
    full_version = (
        version if channel == "stable" else f"{version}-nightly.{build_number}+{source_sha[:12]}"
    )
    tag = f"v{version}" if channel == "stable" else f"nightly-{build_number}-{source_sha[:12]}"
    companion = app == "companion"
    android_id = ANDROID_BASE_ID + (".companion" if companion else "")
    ios_id = IOS_BASE_ID + (".companion" if companion else "")
    if channel == "nightly":
        android_id += ".nightly"
        ios_id += ".nightly"
    label = ("Kinaiya Companion" if companion else "Kinaiya") + (
        " Nightly" if channel == "nightly" else ""
    )
    return {
        "app": app,
        "platform": platform,
        "channel": channel,
        "version": full_version,
        "base_version": version,
        "build_number": build_number,
        "source_sha": source_sha,
        "tag": tag,
        "application_id": android_id if platform == "android" else ios_id,
        "display_name": label,
        "target": TARGETS[app],
        "asset": f"{ASSET_PREFIX[app]}-{tag}-{ASSET_SUFFIX[platform]}",
    }


def dart_defines(data):
    return [
        f"--dart-define=KINAIYA_CHANNEL={data['channel']}",
        f"--dart-define=KINAIYA_VERSION={data['version']}",
        f"--dart-define=KINAIYA_BUILD_NUMBER={data['build_number']}",
        f"--dart-define=KINAIYA_SOURCE_SHA={data['source_sha']}",
    ]


def flutter_command(data):
    platform_args = (
        ["apk", "--release"]
        if data["platform"] == "android"
        else ["ios", "--release", "--no-codesign"]
    )
    return [
        "flutter",
        "build",
        *platform_args,
        "--no-pub",
        "-t",
        data["target"],
        # iOS CFBundleShortVersionString must be numeric; the full channel version
        # is still compiled into the app through KINAIYA_VERSION.
        "--build-name",
        data["base_version"],
        "--build-number",
        str(data["build_number"]),
        *dart_defines(data),
    ]


def xcconfig_text(data):
    """Generated, git-ignored overrides included by Debug/Release.xcconfig."""
    for key in ("application_id", "display_name"):
        _require(re.fullmatch(r"[A-Za-z0-9. ]{1,100}", data[key]), f"Unsafe {key}")
    return (
        "// Generated by scripts/mobile_build.py; do not commit.\n"
        f"KINAIYA_BUNDLE_ID = {data['application_id']}\n"
        f"KINAIYA_DISPLAY_NAME = {data['display_name']}\n"
    )


def package_ipa(app_bundle, destination):
    """Zip Runner.app as Payload/Runner.app with fixed timestamps and order."""
    app_bundle, destination = Path(app_bundle), Path(destination)
    _require(app_bundle.is_dir() and not app_bundle.is_symlink(), "Missing Runner.app")
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(app_bundle.rglob("*")):
            relative = Path("Payload") / app_bundle.name / path.relative_to(app_bundle)
            info = zipfile.ZipInfo(relative.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            if path.is_symlink():
                # Frameworks can contain relative symlinks; preserve them as links.
                info.create_system = 3
                info.external_attr = 0o120777 << 16
                archive.writestr(info, os.readlink(path))
            elif path.is_dir():
                info = zipfile.ZipInfo(relative.as_posix() + "/", date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (0o40755 << 16) | 0x10
                archive.writestr(info, b"")
            else:
                info.create_system = 3
                mode = 0o755 if os.access(path, os.X_OK) else 0o644
                info.external_attr = (0o100000 | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
    return destination


def _sdk_tool(name):
    home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    _require(home, "ANDROID_HOME is required to verify the APK")
    candidates = sorted(Path(home, "build-tools").glob(f"*/{name}"))
    _require(candidates, f"Android build-tools {name} not found")
    return str(candidates[-1])


def verify_android(apk, data, expected_cert_sha256=None):
    """Check package identity and a non-debug v2+ signature on the built APK."""
    badging = subprocess.run(
        [_sdk_tool("aapt2"), "dump", "badging", str(apk)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    package = re.search(r"package: name='([^']+)' versionCode='([0-9]+)'", badging)
    _require(package, "Could not read APK package identity")
    _require(package[1] == data["application_id"], f"APK package mismatch: {package[1]}")
    _require(int(package[2]) == data["build_number"], f"APK versionCode mismatch: {package[2]}")
    certs = subprocess.run(
        [_sdk_tool("apksigner"), "verify", "--verbose", "--print-certs", str(apk)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    _require("Verified using v2 scheme (APK Signature Scheme v2): true" in certs, "No v2 signature")
    _require("CN=Android Debug" not in certs, "APK is signed with a debug certificate")
    digests = re.findall(r"certificate SHA-256 digest: ([0-9a-f]{64})", certs)
    _require(len(set(digests)) == 1, "APK must have exactly one signing certificate")
    if expected_cert_sha256:
        _require(
            digests[0] == expected_cert_sha256.lower().replace(":", ""),
            "APK signing certificate does not match the pinned distribution certificate",
        )
    return digests[0]


def _entry(path, data, signing):
    return {
        "name": data["asset"],
        "app": data["app"],
        "platform": data["platform"],
        "channel": data["channel"],
        "version": data["version"],
        "build_number": data["build_number"],
        "source_sha": data["source_sha"],
        "application_id": data["application_id"],
        "signing": signing,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size,
        "url": f"{ROOT}/download/{data['tag']}/{data['asset']}",
    }


def build(data, output, *, expected_cert_sha256=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    destination = output / data["asset"]
    _require(not destination.exists(), "Refusing to overwrite an existing artifact")
    generated = APP_DIR / "ios" / "Flutter" / "Distribution.xcconfig"
    try:
        if data["platform"] == "ios":
            _require(not generated.exists(), "Stale Distribution.xcconfig present")
            generated.write_text(xcconfig_text(data), encoding="utf-8")
        subprocess.run(flutter_command(data), cwd=APP_DIR, check=True)
    finally:
        generated.unlink(missing_ok=True)
    if data["platform"] == "android":
        built = APP_DIR / "build" / "app" / "outputs" / "flutter-apk" / "app-release.apk"
        shutil.copyfile(built, destination)
        certificate = verify_android(destination, data, expected_cert_sha256)
        signing = {"kind": "android-distribution", "certificate_sha256": certificate}
    else:
        package_ipa(APP_DIR / "build" / "ios" / "iphoneos" / "Runner.app", destination)
        signing = {"kind": "unsigned-sideload", "certificate_sha256": None}
    entry = _entry(destination, data, signing)
    (output / f"{data['asset']}.json").write_text(json.dumps(entry, indent=2) + "\n")
    return entry


def merge(directory, *, channel, build_number, source_sha):
    """Validate sidecars against the files present and write mobile-manifest.json."""
    directory = Path(directory)
    entries = []
    for sidecar in sorted(directory.glob("*.json")):
        if sidecar.name == "mobile-manifest.json":
            continue
        entry = json.loads(sidecar.read_text())
        expected = identity(
            app=entry["app"],
            platform=entry["platform"],
            channel=channel,
            version=entry["version"].split("-")[0],
            build_number=build_number,
            source_sha=source_sha,
        )
        artifact = directory / entry["name"]
        _require(entry["name"] == expected["asset"], f"Unexpected asset name {entry['name']}")
        _require(entry["application_id"] == expected["application_id"], "Identity mismatch")
        _require(entry["version"] == expected["version"], "Version mismatch")
        _require(artifact.is_file() and not artifact.is_symlink(), f"Missing {entry['name']}")
        _require(artifact.stat().st_size == entry["size"], f"Size mismatch {entry['name']}")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        _require(digest == entry["sha256"], f"Digest mismatch {entry['name']}")
        kind = entry["signing"]["kind"]
        _require(
            (entry["platform"], kind)
            in {("android", "android-distribution"), ("ios", "unsigned-sideload")},
            "Signing kind does not match platform policy",
        )
        entries.append(entry)
        sidecar.unlink()
    _require(entries, "No mobile artifacts to describe")
    _require(len({e["name"] for e in entries}) == len(entries), "Duplicate mobile artifacts")
    manifest = {
        "schema_version": 1,
        "channel": channel,
        "build_number": build_number,
        "source_sha": source_sha,
        "artifacts": sorted(entries, key=lambda e: e["name"]),
        "notes": (
            "Android APKs are signed with the Kinaiya distribution key; Android accepts "
            "updates only with the same certificate. iOS IPAs are unsigned, for sideloading "
            "tools that re-sign them with the installing user's Apple ID. Native audio, "
            "calling, push and background operation are not included (see #11)."
        ),
    }
    (directory / "mobile-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--platform", choices=sorted(ASSET_SUFFIX))
    parser.add_argument("--app", choices=sorted(TARGETS))
    parser.add_argument("--channel", choices=("nightly", "stable"), required=True)
    parser.add_argument("--version")
    parser.add_argument("--build-number", type=int, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--merge", action="store_true", help="Write mobile-manifest.json")
    args = parser.parse_args(argv)
    try:
        if args.merge:
            result = merge(
                args.output,
                channel=args.channel,
                build_number=args.build_number,
                source_sha=args.source_sha,
            )
            print(f"mobile manifest: {len(result['artifacts'])} artifact(s)")
            return 0
        if not (args.platform and args.app and args.version):
            parser.error("build requires --platform, --app and --version")
        data = identity(
            app=args.app,
            platform=args.platform,
            channel=args.channel,
            version=args.version,
            build_number=args.build_number,
            source_sha=args.source_sha,
        )
        entry = build(
            data,
            args.output,
            expected_cert_sha256=os.environ.get("KINAIYA_ANDROID_CERT_SHA256") or None,
        )
        print(json.dumps(entry, indent=2))
    except (MobileBuildError, subprocess.CalledProcessError, OSError, KeyError) as exc:
        parser.exit(1, f"mobile build refused: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
