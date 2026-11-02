"""Full gzip-envelope integrity and expansion limits, independent of tar parsing."""

import gzip

import pytest

from packages.distribution.releases import ReleaseError, verify_gzip_archive


def test_gzip_limit_counts_padding_and_concatenated_members(tmp_path):
    path = tmp_path / "payload.gz"
    path.write_bytes(gzip.compress(b"x" * 32) + gzip.compress(b"y" * 33))
    with pytest.raises(ReleaseError, match="bound"):
        verify_gzip_archive(path, 64)
    verify_gzip_archive(path, 65)


def test_gzip_rejects_invalid_compressed_body(tmp_path):
    path = tmp_path / "payload.gz"
    path.write_bytes(b"\x1f\x8b\x08\x00" + b"\x00" * 6 + b"\x07" + b"\x00" * 8)
    with pytest.raises(ReleaseError, match="Invalid|truncated"):
        verify_gzip_archive(path, 64)
