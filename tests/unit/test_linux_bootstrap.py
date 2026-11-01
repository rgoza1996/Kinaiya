"""Exercise the standalone downloadable shell bootstrap without remote requests."""

import io
from pathlib import Path
from types import ModuleType
from urllib.error import HTTPError

import pytest


@pytest.fixture
def bootstrap():
    script = Path(__file__).resolve().parents[2] / "install.sh"
    assert script.is_file(), "standalone Linux bootstrap is missing"
    text = script.read_text()
    assert "# BEGIN PYTHON BOOTSTRAP\n" in text
    source = text.split("# BEGIN PYTHON BOOTSTRAP\n", 1)[1].split("\n# END PYTHON BOOTSTRAP", 1)[0]
    module = ModuleType("bootstrap_test")
    exec(compile(source, str(script), "exec"), module.__dict__)
    return module


def test_stable_absent_is_explicit_and_does_not_fallback(bootstrap, monkeypatch):
    calls = []

    def absent(url, **kwargs):
        calls.append(url)
        raise HTTPError(url, 404, "not published", {}, None)

    monkeypatch.setattr(bootstrap, "read_json", absent)
    with pytest.raises(bootstrap.BootstrapError, match="stable.*published"):
        bootstrap.resolve("stable")
    assert calls == [bootstrap.API + "/latest"]


def test_bootstrap_cannot_extract_links_or_traversal(bootstrap, tmp_path):
    import tarfile

    for name in ("../../outside", "/absolute", "valid/link"):
        archive = tmp_path / "bad.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            member = tarfile.TarInfo(name)
            member.type = tarfile.SYMTYPE
            member.linkname = "../../outside"
            tar.addfile(member)
        with pytest.raises(bootstrap.BootstrapError, match="unsafe|Unsafe"):
            bootstrap.unpack(archive, tmp_path / name.replace("/", "_"))
    assert not (tmp_path / "outside").exists()


def test_bootstrap_refuses_bad_digest_before_loading_release_code(bootstrap, tmp_path, monkeypatch):
    artifact = {
        "url": "https://github.com/rgoza1996/Kinaiya/releases/download/v0.2.1/kinaiya-v0.2.1-source.tar.gz",
        "size": 3,
        "sha256": "0" * 64,
    }
    monkeypatch.setattr(bootstrap, "open_url", lambda url: io.BytesIO(b"abc"))
    with pytest.raises(bootstrap.BootstrapError, match="digest"):
        bootstrap.download(artifact, tmp_path / "archive")


def test_bootstrap_preflight_failure_happens_before_feed_or_cache(bootstrap, tmp_path, monkeypatch):
    def denied():
        raise bootstrap.BootstrapError("bubblewrap namespace probe denied; never disable isolation")

    monkeypatch.setattr(bootstrap, "preflight", denied)
    monkeypatch.setattr(bootstrap, "resolve", lambda *a: pytest.fail("must not fetch release"))
    with pytest.raises(bootstrap.BootstrapError, match="namespace"):
        bootstrap.run(["--yes", "--no-setup"])
