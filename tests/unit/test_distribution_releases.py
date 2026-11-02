"""Offline release contract tests for #262; fixtures are not published releases."""

import copy
import hashlib

import pytest

from packages.distribution import releases

SHA = "a" * 40
API = "https://api.github.com/repos/rgoza1996/Kinaiya/releases"
ROOT = "https://github.com/rgoza1996/Kinaiya/releases"


def manifest(channel="stable", build=123):
    version = "0.2.0" if channel == "stable" else f"0.2.0-nightly.{build}+{SHA[:12]}"
    tag = f"v{version}" if channel == "stable" else f"nightly-{build}-{SHA[:12]}"
    name = f"kinaiya-{tag}-source.tar.gz"
    return {
        "schema_version": 1,
        "channel": channel,
        "version": version,
        "build_number": build,
        "source_sha": SHA,
        "protocol_min": 1,
        "protocol_max": 1,
        "state_schema": 1,
        "min_updater": 1,
        "artifacts": [
            {
                "name": name,
                "platform": "linux",
                "architecture": "any",
                "url": f"{ROOT}/download/{tag}/{name}",
                "sha256": hashlib.sha256(b"source").hexdigest(),
                "size": 6,
            }
        ],
    }


def release(data):
    tag = data["artifacts"][0]["url"].split("/")[-2]
    return {
        "tag_name": tag,
        "target_commitish": SHA,
        "draft": False,
        "prerelease": data["channel"] == "nightly",
        "html_url": f"{ROOT}/tag/{tag}",
        "assets": [
            {
                "name": "release-manifest.json",
                "browser_download_url": f"{ROOT}/download/{tag}/release-manifest.json",
                "size": 1024,
                "state": "uploaded",
            },
            *[
                {
                    "name": a["name"],
                    "browser_download_url": a["url"],
                    "size": a["size"],
                    "state": "uploaded",
                }
                for a in data["artifacts"]
            ],
        ],
    }


def test_stable_resolves_only_the_official_latest_endpoint():
    expected = manifest()
    metadata = release(expected)
    calls = []

    def fetch(url):
        calls.append(url)
        return metadata if url == f"{API}/latest" else expected

    assert releases.resolve_release("stable", fetch_json=fetch) == expected
    assert calls == [f"{API}/latest", metadata["assets"][0]["browser_download_url"]]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("channel", "dev"),
        ("version", "main"),
        ("version", "0.2.0-alpha.1"),
        ("build_number", True),
        ("build_number", 0),
        ("build_number", 2**63),
        ("source_sha", "A" * 40),
        ("protocol_min", 0),
        ("protocol_max", False),
        ("state_schema", -1),
        ("min_updater", "1"),
        ("artifacts", []),
        ("artifacts", [None]),
    ],
)
def test_manifest_refuses_invalid_contract_fields(field, value):
    data = manifest()
    data[field] = value
    with pytest.raises(releases.ReleaseError):
        releases.validate_manifest(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "../source.tar.gz"),
        ("name", "secret\\\\source.tar.gz"),
        ("platform", "unknown"),
        ("architecture", "unknown"),
        ("sha256", "f" * 63),
        ("sha256", "F" * 64),
        ("size", True),
        ("size", 0),
        ("size", 2**40),
        ("url", "https://[invalid"),
        ("url", "https://evil.invalid/source.tar.gz"),
        ("url", "http://github.com/rgoza1996/Kinaiya/releases/download/v0.2.0/source"),
        ("url", "https://github.com/other/Kinaiya/releases/download/v0.2.0/source"),
        ("url", "https://github.com/rgoza1996/Kinaiya/releases/download/nightly/source"),
    ],
)
def test_manifest_refuses_invalid_artifact_fields(field, value):
    data = manifest()
    data["artifacts"][0][field] = value
    with pytest.raises(releases.ReleaseError):
        releases.validate_manifest(data)


def test_manifest_cross_field_identity_and_required_source():
    data = manifest()
    assert releases.validate_manifest(data) == data
    for mutation in (
        {"protocol_min": 2},
        {"channel": "nightly"},
        {"artifacts": data["artifacts"] * 2},
        {"artifacts": [{**data["artifacts"][0], "platform": "android"}]},
    ):
        with pytest.raises(releases.ReleaseError):
            releases.validate_manifest({**copy.deepcopy(data), **mutation})


def test_nightly_scans_bounded_pages_and_chooses_highest_immutable_build():
    expected = manifest("nightly", 124)
    old = manifest("nightly", 123)
    unrelated = {"tag_name": "v0.1.0-alpha.19", "prerelease": True, "draft": False}
    first = [release(old), *[unrelated] * 99]
    calls = []

    def fetch(url):
        calls.append(url)
        if url == f"{API}?per_page=100&page=1":
            return first
        if url == f"{API}?per_page=100&page=2":
            return [release(expected), release(manifest())]
        return expected

    assert releases.resolve_release("nightly", fetch) == expected
    assert calls[:2] == [f"{API}?per_page=100&page=1", f"{API}?per_page=100&page=2"]


@pytest.mark.parametrize(
    "mutation",
    [
        {"draft": True},
        {"prerelease": True},
        {"tag_name": "v0.2.0-alpha.1"},
        {"target_commitish": "main"},
        {"html_url": "https://github.com/evil/Kinaiya/releases/tag/v0.2.0"},
        {"assets": []},
    ],
)
def test_stable_refuses_wrong_release_or_partial_publication(mutation):
    data = manifest()
    metadata = {**release(data), **mutation}
    with pytest.raises(releases.ReleaseError):
        releases.resolve_release("stable", lambda url: metadata if url == f"{API}/latest" else data)


def test_release_asset_metadata_must_match_validated_manifest():
    data = manifest()
    metadata = release(data)
    metadata["assets"][1]["size"] += 1
    with pytest.raises(releases.ReleaseError):
        releases.resolve_release("stable", lambda url: metadata if url == f"{API}/latest" else data)


def test_nightly_ambiguity_or_incomplete_scan_fails_closed():
    metadata = release(manifest("nightly"))
    with pytest.raises(releases.ReleaseError, match="[Aa]mbiguous"):
        releases.resolve_release("nightly", lambda url: [metadata, metadata])
    calls = []

    def fetch(url):
        calls.append(url)
        return [{"tag_name": "alpha", "draft": False, "prerelease": True}] * 100

    with pytest.raises(releases.ReleaseError, match="[Bb]ound"):
        releases.resolve_release("nightly", fetch)
    assert len(calls) == releases.MAX_PAGES


def test_no_release_and_unavailable_feed_are_distinct_actionable_errors():
    from urllib.error import HTTPError

    for channel in ("stable", "nightly"):

        def absent(url, channel=channel):
            if channel == "stable":
                raise HTTPError(url, 404, "Not Found", {}, None)
            return []

        with pytest.raises(releases.ReleaseError) as error:
            releases.resolve_release(channel, absent)
        assert error.value.code == "release_absent"

    def unavailable(url):
        raise OSError("offline")

    with pytest.raises(releases.ReleaseError) as error:
        releases.resolve_release("stable", unavailable)
    assert error.value.code == "feed_unavailable"


def test_unknown_channel_does_not_fetch():
    with pytest.raises(releases.ReleaseError):
        releases.resolve_release("dev", lambda url: pytest.fail("Unexpected fetch"))


def test_default_transport_is_used_and_keeps_404_absent(monkeypatch):
    from urllib.error import HTTPError

    def missing(url):
        raise HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(releases, "fetch_release_json", missing, raising=False)
    with pytest.raises(releases.ReleaseError) as error:
        releases.resolve_release("stable")
    assert error.value.code == "release_absent"


def test_json_transport_rejects_oversize_duplicate_keys_and_malformed_json(monkeypatch):
    import io

    class Response(io.BytesIO):
        def geturl(self):
            return f"{API}/latest"

    for body in (b"x" * (releases.MAX_JSON_BYTES + 1), b'{"a":1,"a":2}', b"not json"):
        monkeypatch.setattr(
            releases, "open_release_url", lambda url, body=body: Response(body), raising=False
        )
        with pytest.raises(releases.ReleaseError):
            releases.fetch_release_json(f"{API}/latest")
    monkeypatch.setattr(releases, "open_release_url", lambda url: Response(b'{"ok":1}'))
    assert releases.fetch_release_json(f"{API}/latest") == {"ok": 1}


@pytest.mark.parametrize(
    "url",
    [
        "http://api.github.com/repos/rgoza1996/Kinaiya/releases/latest",
        "https://api.github.com/repos/evil/Kinaiya/releases/latest",
        "https://github.com/rgoza1996/Kinaiya/releases/latest/download/source.tar.gz",
        "https://github.com/rgoza1996/Kinaiya/releases/download/nightly/source.tar.gz",
        "https://github.com@evil.invalid/source",
    ],
)
def test_transport_refuses_unofficial_or_mutable_start_url(url):
    with pytest.raises(releases.ReleaseError):
        releases.open_release_url(url)


def test_redirect_policy_allows_only_github_asset_cdn():
    source = manifest()["artifacts"][0]["url"]
    assert releases.validate_redirect(
        source,
        "https://release-assets.githubusercontent.com/github-production-release-asset/1/2?sig=example",
    )
    for target in (
        "http://release-assets.githubusercontent.com/a",
        "https://evil.invalid/a",
        "https://github.com/evil/a",
        "https://release-assets.githubusercontent.com.evil.invalid/a",
        "https://user@release-assets.githubusercontent.com/a",
    ):
        with pytest.raises(releases.ReleaseError):
            releases.validate_redirect(source, target)
    with pytest.raises(releases.ReleaseError):
        releases.validate_redirect(
            f"{API}/latest", "https://release-assets.githubusercontent.com/a"
        )


def test_artifact_selection_digest_and_freshness_are_explicit(tmp_path):
    data = manifest()
    artifact = releases.select_artifact(data, "linux", "x86_64")
    assert artifact == data["artifacts"][0]
    with pytest.raises(releases.ReleaseError) as error:
        releases.select_artifact(data, "windows", "x86_64")
    assert error.value.code == "unsupported_platform"
    path = tmp_path / "source.tar.gz"
    path.write_bytes(b"source")
    releases.verify_artifact(path, artifact)
    path.write_bytes(b"tamper")
    with pytest.raises(releases.ReleaseError, match="digest"):
        releases.verify_artifact(path, artifact)
    path.write_bytes(b"oversized")
    with pytest.raises(releases.ReleaseError, match="size"):
        releases.verify_artifact(path, artifact)
    assert releases.update_status(data, 122) == "available"
    assert releases.update_status(data, 123) == "current"
    with pytest.raises(releases.ReleaseError) as error:
        releases.update_status(data, 124)
    assert error.value.code == "stale_release"


def test_artifact_verification_bounds_reads_if_the_file_changes(tmp_path, monkeypatch):
    import io
    from pathlib import Path

    artifact = manifest()["artifacts"][0]
    path = tmp_path / "artifact"
    path.write_bytes(b"source")
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: io.BytesIO(b"source-extra"))
    with pytest.raises(releases.ReleaseError, match="size"):
        releases.verify_artifact(path, artifact)
