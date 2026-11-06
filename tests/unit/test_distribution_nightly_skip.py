"""Offline regression tests for scheduled-nightly deduplication."""

from pathlib import Path

import pytest
import yaml

from packages.distribution.releases import API, ReleaseError
from scripts import nightly_skip

SHA = "a" * 40
DIFFERENT = "b" * 40
WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/distribution.yml"


def _resolver(source_sha):
    def resolve(channel, *, fetch_json):
        assert channel == "nightly"
        assert callable(fetch_json)
        return {"source_sha": source_sha}

    return resolve


def test_skip_when_last_published_nightly_has_exact_same_source():
    assert nightly_skip.skip_unchanged_nightly(SHA, resolver=_resolver(SHA)) is True


def test_build_when_last_published_nightly_source_differs():
    assert nightly_skip.skip_unchanged_nightly(SHA, resolver=_resolver(DIFFERENT)) is False


def test_build_first_nightly_when_no_published_nightly():
    def absent(_channel, *, fetch_json):
        raise ReleaseError("No qualified nightly release is published", "release_absent")

    assert nightly_skip.skip_unchanged_nightly(SHA, resolver=absent) is False


def test_feed_or_invalid_release_errors_block_instead_of_skipping():
    def refused(_channel, *, fetch_json):
        raise ReleaseError("Release feed unavailable", "feed_unavailable")

    with pytest.raises(ReleaseError, match="unavailable"):
        nightly_skip.skip_unchanged_nightly(SHA, resolver=refused)
    with pytest.raises(ReleaseError, match="invalid source identity"):
        nightly_skip.skip_unchanged_nightly(SHA, resolver=_resolver("invalid"))


def test_invalid_source_is_refused_without_querying_releases():
    def unexpected(_channel, *, fetch_json):
        pytest.fail("Invalid source must not query the release feed")

    with pytest.raises(ReleaseError, match="Invalid scheduled"):
        nightly_skip.skip_unchanged_nightly("HEAD", resolver=unexpected)


def test_release_index_uses_authenticated_gh_and_assets_keep_verified_transport(monkeypatch):
    requests = []
    monkeypatch.setattr(nightly_skip, "_api", lambda endpoint: requests.append(endpoint) or [])
    monkeypatch.setattr(nightly_skip, "fetch_release_json", lambda url: requests.append(url) or {})
    url = f"{API}?per_page=100&page=1"
    assert nightly_skip._fetch_release_json(url) == []
    assert requests == ["repos/rgoza1996/Kinaiya/releases?per_page=100&page=1"]
    asset = "https://github.com/rgoza1996/Kinaiya/releases/download/nightly-1-aaaaaaaaaaaa/release-manifest.json"
    assert nightly_skip._fetch_release_json(asset) == {}
    assert requests[-1] == asset


def test_github_output_records_skip_and_first_build(monkeypatch, tmp_path, capsys):
    output = tmp_path / "output"
    for skip in (True, False):
        monkeypatch.setattr(nightly_skip, "skip_unchanged_nightly", lambda sha: skip)
        nightly_skip.main(["--source-sha", SHA, "--output", str(output)])
    assert output.read_text() == "skip_unchanged=true\nskip_unchanged=false\n"
    assert "Skipping scheduled nightly" in capsys.readouterr().out


def test_workflow_only_skips_scheduled_nightlies_before_expensive_jobs():
    config = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    jobs = config["jobs"]
    prepare = jobs["prepare"]
    assert prepare["outputs"]["skip_unchanged"] == ("$" + "{{ steps.unchanged.outputs.skip_unchanged }}")
    guard = next(step for step in prepare["steps"] if step.get("id") == "unchanged")
    gate = next(step for step in prepare["steps"] if step.get("id") == "gate")
    assert guard["if"] == "github.event_name == 'schedule'"
    assert "python -m scripts.nightly_skip" in guard["run"]
    assert '--source-sha "$GITHUB_SHA"' in guard["run"]
    assert "GH_TOKEN" in guard["env"]
    assert prepare["steps"].index(guard) < prepare["steps"].index(gate)
    assert "steps.unchanged.outputs.skip_unchanged != 'true'" in gate["if"]
    for name in ("build", "mobile-android", "mobile-ios", "publish"):
        assert "needs.prepare.outputs.skip_unchanged != 'true'" in jobs[name]["if"]
    assert "needs.prepare.outputs.dry_run == 'false'" in jobs["publish"]["if"]
    assert "github.ref == 'refs/heads/main'" in jobs["publish"]["if"]
