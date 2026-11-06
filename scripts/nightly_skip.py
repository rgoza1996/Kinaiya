"""Skip a scheduled nightly only when its exact source was already published.

Use the production nightly resolver so a draft, failed upload, or malformed release
cannot be mistaken for a qualified published nightly. Manual dispatch is intentionally
not gated by this module.
"""

import argparse
import re
import subprocess
from pathlib import Path

from packages.distribution.releases import API, ReleaseError, fetch_release_json, resolve_release
from scripts.release_gate import _api

_SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")
_RELEASE_PAGE = re.compile(re.escape(API) + r"\?per_page=100&page=[1-5]\Z")


def _fetch_release_json(url: str):
    """Authenticate release-index reads without loosening asset URL validation."""
    if _RELEASE_PAGE.fullmatch(url):
        return _api(url.removeprefix("https://api.github.com/"))
    return fetch_release_json(url)


def skip_unchanged_nightly(source_sha: str, *, resolver=resolve_release, fetch_json=None) -> bool:
    """True only if the latest *published and validated* nightly matches source_sha."""
    if not isinstance(source_sha, str) or _SOURCE_SHA.fullmatch(source_sha) is None:
        raise ReleaseError("Invalid scheduled nightly source SHA")
    try:
        manifest = resolver("nightly", fetch_json=fetch_json or _fetch_release_json)
    except ReleaseError as exc:
        if exc.code == "release_absent":
            return False  # First nightly: don't skip a build that has never published.
        raise  # API, validation and transport failures must not mean 'unchanged'.
    if not isinstance(manifest, dict) or not _SOURCE_SHA.fullmatch(str(manifest.get("source_sha", ""))):
        raise ReleaseError("Nightly resolver returned an invalid source identity")
    return manifest["source_sha"] == source_sha


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        skip = skip_unchanged_nightly(args.source_sha)
        with args.output.open("a", encoding="utf-8") as output:
            output.write(f"skip_unchanged={'true' if skip else 'false'}\n")
    except (ReleaseError, OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"nightly comparison failed; no release attempted: {exc}\n")
    if skip:
        print(f"Skipping scheduled nightly: {args.source_sha} already has a published nightly.")
    else:
        print(f"Scheduled nightly needed: {args.source_sha} is not the latest published nightly.")


if __name__ == "__main__":
    main()
