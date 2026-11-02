"""Mobile distribution identity, packaging and manifest contracts (#263/#264).

These tests need no Flutter/Android/Xcode toolchain; the real builds run in the
release workflow and the native install smoke jobs.
"""

import hashlib
import json
import os
import zipfile

import pytest

from scripts.mobile_build import (
    MobileBuildError,
    dart_defines,
    flutter_command,
    identity,
    merge,
    package_ipa,
    xcconfig_text,
)

SHA = "0123456789abcdef0123456789abcdef01234567"


def ident(**changes):
    values = {
        "app": "kinaiya",
        "platform": "android",
        "channel": "nightly",
        "version": "0.1.0",
        "build_number": 42,
        "source_sha": SHA,
        **changes,
    }
    return identity(**values)


def test_identities_match_gradle_and_dart_contracts():
    owner = ident()
    assert owner["application_id"] == "dev.kinaiya.kinaiya_flutter.nightly"
    assert owner["asset"] == "kinaiya-nightly-42-0123456789ab-android.apk"
    assert owner["version"] == "0.1.0-nightly.42+0123456789ab"
    assert owner["display_name"] == "Kinaiya Nightly"
    stable = ident(channel="stable")
    assert stable["application_id"] == "dev.kinaiya.kinaiya_flutter"
    assert stable["tag"] == "v0.1.0" and stable["version"] == "0.1.0"
    companion_ios = ident(app="companion", platform="ios")
    assert companion_ios["application_id"] == "dev.kinaiya.kinaiyaFlutter.companion.nightly"
    assert companion_ios["asset"] == (
        "kinaiya-companion-nightly-42-0123456789ab-ios-sideload-unsigned.ipa"
    )
    assert companion_ios["target"] == "lib/companion_main.dart"


@pytest.mark.parametrize(
    "changes",
    [
        {"channel": "development"},
        {"channel": "beta"},
        {"version": "0.1.0-alpha.1"},
        {"version": "01.0.0"},
        {"build_number": 0},
        {"build_number": True},
        {"build_number": 2_100_000_001},
        {"source_sha": "ABC"},
        {"app": "owner"},
        {"platform": "windows"},
    ],
)
def test_invalid_inputs_are_refused(changes):
    with pytest.raises(MobileBuildError):
        ident(**changes)


def test_flutter_command_compiles_matching_release_identity():
    data = ident(platform="ios", app="companion")
    command = flutter_command(data)
    assert command[:5] == ["flutter", "build", "ios", "--release", "--no-codesign"]
    assert command[command.index("-t") + 1] == "lib/companion_main.dart"
    # Numeric CFBundleShortVersionString; full channel version is a define.
    assert command[command.index("--build-name") + 1] == "0.1.0"
    assert command[command.index("--build-number") + 1] == "42"
    assert set(dart_defines(data)) <= set(command)
    assert "--dart-define=KINAIYA_CHANNEL=nightly" in command
    android = flutter_command(ident())
    assert android[:4] == ["flutter", "build", "apk", "--release"]
    assert "--no-codesign" not in android


def test_xcconfig_contains_only_validated_identity():
    text = xcconfig_text(ident(platform="ios"))
    assert "KINAIYA_BUNDLE_ID = dev.kinaiya.kinaiyaFlutter.nightly\n" in text
    assert "KINAIYA_DISPLAY_NAME = Kinaiya Nightly\n" in text
    bad = ident(platform="ios")
    bad["display_name"] = "Kinaiya\nOTHER = 1"
    with pytest.raises(MobileBuildError):
        xcconfig_text(bad)


def test_ipa_packaging_is_payload_rooted_and_reproducible(tmp_path):
    app = tmp_path / "Runner.app"
    (app / "Frameworks").mkdir(parents=True)
    (app / "Runner").write_bytes(b"binary")
    os.chmod(app / "Runner", 0o755)
    (app / "Info.plist").write_bytes(b"<plist/>")
    first = package_ipa(app, tmp_path / "a.ipa")
    second = package_ipa(app, tmp_path / "b.ipa")
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        names = archive.namelist()
        assert all(name.startswith("Payload/Runner.app/") for name in names)
        assert archive.read("Payload/Runner.app/Runner") == b"binary"
    with pytest.raises(MobileBuildError):
        package_ipa(tmp_path / "missing.app", tmp_path / "c.ipa")
    with pytest.raises(FileExistsError):
        package_ipa(app, first)


def _sidecar(directory, data, payload, signing):
    artifact = directory / data["asset"]
    artifact.write_bytes(payload)
    entry = {
        "name": data["asset"],
        "app": data["app"],
        "platform": data["platform"],
        "channel": data["channel"],
        "version": data["version"],
        "build_number": data["build_number"],
        "source_sha": data["source_sha"],
        "application_id": data["application_id"],
        "signing": signing,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "url": f"https://github.com/rgoza1996/Kinaiya/releases/download/{data['tag']}/{data['asset']}",
    }
    (directory / f"{data['asset']}.json").write_text(json.dumps(entry))
    return entry


ANDROID_SIGNING = {"kind": "android-distribution", "certificate_sha256": "ab" * 32}
IOS_SIGNING = {"kind": "unsigned-sideload", "certificate_sha256": None}


def test_merge_writes_manifest_after_verifying_files(tmp_path):
    _sidecar(tmp_path, ident(), b"apk", ANDROID_SIGNING)
    _sidecar(tmp_path, ident(platform="ios", app="companion"), b"ipa", IOS_SIGNING)
    manifest = merge(tmp_path, channel="nightly", build_number=42, source_sha=SHA)
    assert [a["name"] for a in manifest["artifacts"]] == sorted(
        a["name"] for a in manifest["artifacts"]
    )
    written = json.loads((tmp_path / "mobile-manifest.json").read_text())
    assert written == manifest
    assert not list(tmp_path.glob("*.apk.json")) and not list(tmp_path.glob("*.ipa.json"))


@pytest.mark.parametrize("tamper", ["digest", "size", "identity", "signing", "missing"])
def test_merge_rejects_inconsistent_sidecars(tmp_path, tamper):
    data = ident()
    entry = _sidecar(tmp_path, data, b"apk", ANDROID_SIGNING)
    if tamper == "digest":
        (tmp_path / data["asset"]).write_bytes(b"apX")
    elif tamper == "size":
        (tmp_path / data["asset"]).write_bytes(b"apk-longer")
    elif tamper == "identity":
        entry["application_id"] = "dev.kinaiya.kinaiya_flutter"
    elif tamper == "signing":
        entry["signing"] = IOS_SIGNING
    elif tamper == "missing":
        (tmp_path / data["asset"]).unlink()
    if tamper in ("identity", "signing"):
        (tmp_path / f"{data['asset']}.json").write_text(json.dumps(entry))
    with pytest.raises(MobileBuildError):
        merge(tmp_path, channel="nightly", build_number=42, source_sha=SHA)


def test_merge_rejects_wrong_release_identity(tmp_path):
    _sidecar(tmp_path, ident(), b"apk", ANDROID_SIGNING)
    with pytest.raises(MobileBuildError):
        merge(tmp_path, channel="nightly", build_number=43, source_sha=SHA)
    with pytest.raises(MobileBuildError):
        merge(tmp_path / "empty", channel="nightly", build_number=42, source_sha=SHA)
