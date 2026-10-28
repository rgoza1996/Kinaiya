"""Synthetic unit tests for packages.resources.gpu (issue #53, Roadmap 14.3).

All fixtures are tiny synthetic files under tmp_path; SYSFS_PCI/PROC_ROOT are
monkeypatched. No real /sys, /proc, GPU, subprocesses, or network access.
"""

import os
from pathlib import Path

import pytest

import packages.resources.gpu as gpu_mod
from packages.resources.gpu import read_amd_memory, read_amd_process_vram, valid_pci_bus_id

BDF = "0000:03:00.0"
OTHER = "0000:04:00.0"
MAXB = 2**50

STAT_TAIL = "R 1 1 1 0 -1 4194304 0 0 0 0 0 0 0 0 20 0 1 0"


def _sysfs(monkeypatch, tmp_path):
    root = tmp_path / "sys"
    root.mkdir()
    monkeypatch.setattr(gpu_mod, "SYSFS_PCI", root)
    return root


def _pci(root, bdf=BDF, vendor="0x1002", driver="amdgpu", total=None, used=None):
    dev = root / bdf
    dev.mkdir(parents=True, exist_ok=True)
    (dev / "vendor").write_text(vendor + "\n", encoding="ascii")
    (dev / "driver").unlink(missing_ok=True)
    if driver is not None:
        os.symlink(driver, dev / "driver")
    if total is not None:
        (dev / "mem_info_vram_total").write_text(str(total) + "\n", encoding="ascii")
    if used is not None:
        (dev / "mem_info_vram_used").write_text(str(used) + "\n", encoding="ascii")
    return dev


def _proc(monkeypatch, tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    monkeypatch.setattr(gpu_mod, "PROC_ROOT", root)
    return root


def _drm(bdf=BDF, cid=7, kib=1024):
    return (
        "pos:\t0\nflags:\t0100002\nmnt_id:\t15\n"
        f"drm-pdev:\t{bdf}\ndrm-client-id:\t{cid}\ndrm-memory-vram:\t{kib} KiB\n"
    )


def _pid(root, pid, start, fds):
    pdir = root / str(pid)
    fdir = pdir / "fdinfo"
    fdir.mkdir(parents=True)
    (pdir / "stat").write_text(f"{pid} (testproc) {STAT_TAIL} {start} 0 0 0 0 0 0 0\n")
    for name, content in fds.items():
        if isinstance(content, bytes):
            (fdir / name).write_bytes(content)
        else:
            (fdir / name).write_text(content, encoding="ascii")
    return pdir


def test_valid_pci_bus_id():
    for good in ("0000:03:00.0", "ffff:ff:1f.7", "1234:ab:cd.0", "0000:00:1a.3"):
        assert valid_pci_bus_id(good) is True
    for bad in (
        "",
        None,
        123,
        "0000:03:00.8",
        "0000:03:00.",
        "0:03:00.0",
        "0000:3:00.0",
        "0000:AB:CD.1",
        "0000:03:00.0\n",
        " 0000:03:00.0",
        "0000:03:00.0 ",
        "0000-03-00.0",
        "00000:03:00.0",
        "0000:03:00.0:0",
    ):
        assert valid_pci_bus_id(bad) is False


def test_sysfs_proc_constants():
    assert gpu_mod.SYSFS_PCI == Path("/sys/bus/pci/devices")
    assert gpu_mod.PROC_ROOT == Path("/proc")


def test_read_amd_memory_success(monkeypatch, tmp_path):
    root = _sysfs(monkeypatch, tmp_path)
    _pci(root, total=8589934592, used=1073741824)
    assert read_amd_memory(BDF) == {
        "pci_bus_id": BDF,
        "total_bytes": 8589934592,
        "used_bytes": 1073741824,
        "free_bytes": 7516192768,
    }


def test_read_amd_memory_boundaries(monkeypatch, tmp_path):
    root = _sysfs(monkeypatch, tmp_path)
    _pci(root, total=MAXB, used=MAXB, driver=str(tmp_path / "drivers" / "amdgpu"))
    assert read_amd_memory(BDF)["free_bytes"] == 0


@pytest.mark.parametrize("bad", ["", "bogus", "0000:03:00.8", "0000:AB:CD.1", None, 123])
def test_read_amd_memory_invalid_bdf_raises(monkeypatch, tmp_path, bad):
    _sysfs(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        read_amd_memory(bad)


def test_read_amd_memory_unavailable(monkeypatch, tmp_path):
    root = _sysfs(monkeypatch, tmp_path)
    assert read_amd_memory(BDF) is None  # no device dir
    dev = _pci(root, total=100, used=10)
    (dev / "vendor").unlink()
    assert read_amd_memory(BDF) is None
    _pci(root, total=100, used=10, driver=None)
    assert read_amd_memory(BDF) is None  # driver link missing
    dev2 = _pci(root, total=100, used=10)
    (dev2 / "mem_info_vram_used").unlink()
    assert read_amd_memory(BDF) is None


def test_read_amd_memory_wrong_vendor_or_driver(monkeypatch, tmp_path):
    root = _sysfs(monkeypatch, tmp_path)
    _pci(root, vendor="0x8086", total=100, used=10)
    assert read_amd_memory(BDF) is None
    _pci(root, vendor="0x1002", driver="i915", total=100, used=10)
    assert read_amd_memory(BDF) is None


@pytest.mark.parametrize(
    ("total", "used"),
    [
        ("abc", "0"),
        ("", "0"),
        ("  ", "0"),
        ("0x10", "0"),
        ("1.5", "0"),
        ("-5", "0"),
        ("12 KiB", "0"),
        ("1_000", "0"),
        ("0", "0"),
        (str(MAXB + 1), "0"),
        ("100", "101"),
        ("100", "-1"),
        ("100", "xyz"),
    ],
)
def test_read_amd_memory_bad_counters(monkeypatch, tmp_path, total, used):
    root = _sysfs(monkeypatch, tmp_path)
    _pci(root, total=total, used=used)
    assert read_amd_memory(BDF) is None


def test_read_amd_memory_oversized(monkeypatch, tmp_path):
    root = _sysfs(monkeypatch, tmp_path)
    _pci(root, total="8" + "0" * 1000, used="0")
    assert read_amd_memory(BDF) is None


def test_process_vram_success_bytes(monkeypatch, tmp_path):
    proot = _proc(monkeypatch, tmp_path)
    _pid(proot, 1001, 555, {"0": _drm(kib=2048)})
    assert read_amd_process_vram(1001, BDF) == 2048 * 1024
    _pid(proot, 1002, 556, {"0": _drm(kib=1)})
    assert read_amd_process_vram(1002, BDF) == 1024
    _pid(proot, 1003, 557, {"0": _drm(kib=2**40)})
    assert read_amd_process_vram(1003, BDF) == MAXB


def test_process_vram_dedup_and_multi_client(monkeypatch, tmp_path):
    proot = _proc(monkeypatch, tmp_path)
    _pid(
        proot,
        2001,
        1,
        {
            "0": _drm(cid=1, kib=100),
            "1": _drm(cid=1, kib=300),  # repeated descriptor: take max
            "2": _drm(cid=2, kib=50),
        },
    )
    assert read_amd_process_vram(2001, BDF) == (300 + 50) * 1024


def test_process_vram_zero_vs_absent(monkeypatch, tmp_path):
    proot = _proc(monkeypatch, tmp_path)
    _pid(proot, 3001, 9, {"0": _drm(kib=0)})
    result = read_amd_process_vram(3001, BDF)
    assert result is not None and result == 0
    _pid(proot, 3002, 9, {"0": "pos:\t0\nflags:\t0100002\n"})
    assert read_amd_process_vram(3002, BDF) is None
    _pid(proot, 3003, 9, {"0": _drm(bdf=OTHER, kib=0)})
    assert read_amd_process_vram(3003, BDF) is None  # only other-device zero


def test_process_vram_other_device(monkeypatch, tmp_path):
    proot = _proc(monkeypatch, tmp_path)
    _pid(proot, 4001, 3, {"0": _drm(bdf=OTHER, kib=16)})
    assert read_amd_process_vram(4001, BDF) is None
    _pid(proot, 4002, 3, {"0": _drm(kib=64), "1": _drm(bdf=OTHER, kib=1)})
    assert read_amd_process_vram(4002, BDF) is None  # refuse mixed use
    _pid(proot, 4003, 3, {"0": _drm(kib=64), "1": _drm(bdf=OTHER, kib=0)})
    assert read_amd_process_vram(4003, BDF) == 64 * 1024  # other zero ignored


def test_process_vram_ordinary_and_nonnumeric_ignored(monkeypatch, tmp_path):
    proot = _proc(monkeypatch, tmp_path)
    _pid(
        proot,
        5001,
        7,
        {
            "0": "pos:\t0\nflags:\t0100002\n",
            "1": _drm(kib=32),
            "info": "garbage{{{",
        },
    )
    assert read_amd_process_vram(5001, BDF) == 32 * 1024


@pytest.mark.parametrize("bad", [True, False, 0, -1, 2**31, "42", None, 4.0])
def test_process_vram_invalid_pid_raises(monkeypatch, tmp_path, bad):
    _proc(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        read_amd_process_vram(bad, BDF)


@pytest.mark.parametrize("bad", ["", "bogus", "0000:03:00.8", "0000:AB:CD.1", None, 123])
def test_process_vram_invalid_bdf_raises(monkeypatch, tmp_path, bad):
    _proc(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        read_amd_process_vram(42, bad)


def _bad_record(content):
    return {"0": content}


def test_process_vram_malformed(monkeypatch, tmp_path):
    proot = _proc(monkeypatch, tmp_path)
    base = "pos:\t0\n"
    cases = [
        base + f"drm-pdev:\t{BDF}\ndrm-client-id:\t7\ndrm-memory-vram:\t10 MB\n",
        base + f"drm-pdev:\t{BDF}\ndrm-client-id:\t7\ndrm-memory-vram:\t-3 KiB\n",
        base + f"drm-pdev:\t{BDF}\ndrm-client-id:\t7\ndrm-memory-vram:\t1.5 KiB\n",
        base + f"drm-pdev:\t{BDF}\ndrm-client-id:\t7\ndrm-memory-vram:\t1024\n",
        base + f"drm-pdev:\t{BDF}\ndrm-client-id:\t7\ndrm-memory-vram:\tabc KiB\n",
        base + f"drm-pdev:\t{BDF}\ndrm-client-id:\t7\ndrm-memory-vram:\t{2**40 + 1} KiB\n",
        base + "drm-client-id:\t7\ndrm-memory-vram:\t8 KiB\n",  # missing pdev
        base + f"drm-pdev:\t{BDF}\ndrm-memory-vram:\t8 KiB\n",  # missing client-id
        base + "drm-pdev:\tnotabdf\ndrm-client-id:\t7\ndrm-memory-vram:\t8 KiB\n",
        base + f"drm-pdev:\t{BDF}\ndrm-pdev:\t{OTHER}\n"
        "drm-client-id:\t7\ndrm-memory-vram:\t8 KiB\n",
        base + f"drm-pdev:\t{BDF}\ndrm-client-id:\t7\ndrm-client-id:\t8\ndrm-memory-vram:\t8 KiB\n",
        base + f"drm-pdev:\t{BDF}\ndrm-client-id:\t7\n",  # no counter for our device
        base + f"drm-pdev:\t{BDF}\ndrm-client-id:\t-2\ndrm-memory-vram:\t8 KiB\n",
    ]
    for i, content in enumerate(cases):
        _pid(proot, 6000 + i, 11, _bad_record(content))
        assert read_amd_process_vram(6000 + i, BDF) is None


def test_process_vram_identity(monkeypatch, tmp_path):
    proot = _proc(monkeypatch, tmp_path)
    assert read_amd_process_vram(7001, BDF) is None  # unknown pid
    pdir = _pid(proot, 7002, 5, {"0": _drm(kib=8)})
    (pdir / "stat").write_text("garbage\n", encoding="ascii")
    assert read_amd_process_vram(7002, BDF) is None
    pdir = _pid(proot, 7003, 5, {"0": _drm(kib=8)})
    (pdir / "stat").write_text("7003 (testproc) R 1\n", encoding="ascii")
    assert read_amd_process_vram(7003, BDF) is None
    # comm with spaces/parens still parses starttime (field 22)
    pdir = _pid(proot, 7004, 77, {"0": _drm(kib=8)})
    (pdir / "stat").write_text(
        f"7004 (my worker (1)) {STAT_TAIL} 77 0 0 0 0 0 0 0\n", encoding="ascii"
    )
    assert read_amd_process_vram(7004, BDF) == 8 * 1024


def test_process_vram_replaced_identity(monkeypatch, tmp_path):
    proot = _proc(monkeypatch, tmp_path)
    pdir = _pid(proot, 8001, 111, {"0": _drm(kib=64)})
    real_read = gpu_mod._read_bounded

    def sneaky(path, limit):
        raw = real_read(path, limit)
        if path.parent.name == "fdinfo":
            (pdir / "stat").write_text(
                f"8001 (testproc) {STAT_TAIL} 222 0 0 0 0 0 0 0\n", encoding="ascii"
            )
        return raw

    monkeypatch.setattr(gpu_mod, "_read_bounded", sneaky)
    assert read_amd_process_vram(8001, BDF) is None


def test_process_vram_bounds(monkeypatch, tmp_path):
    proot = _proc(monkeypatch, tmp_path)
    many = {str(i): "pos:\t0\n" for i in range(1025)}
    _pid(proot, 9001, 1, many)
    assert read_amd_process_vram(9001, BDF) is None
    at_limit = {str(i): "pos:\t0\n" for i in range(1023)}
    at_limit["1023"] = _drm(kib=16)
    _pid(proot, 9002, 1, at_limit)  # exactly 1024 entries still measured
    assert read_amd_process_vram(9002, BDF) == 16 * 1024
    _pid(proot, 9003, 1, {"0": _drm(kib=8) + "x" * 17000})
    assert read_amd_process_vram(9003, BDF) is None  # fdinfo over 16KiB
    _pid(proot, 9004, 1, {"0": b"x" * (16 * 1024 + 1)})
    assert read_amd_process_vram(9004, BDF) is None


def test_process_vram_disappearing_fdinfo(monkeypatch, tmp_path):
    proot = _proc(monkeypatch, tmp_path)
    pdir = _pid(proot, 9101, 4, {"0": _drm(kib=64), "1": "pos:\t0\n"})
    real_read = gpu_mod._read_bounded

    def sneaky(path, limit):
        if path == pdir / "fdinfo" / "0":
            path.unlink()
        return real_read(path, limit)

    monkeypatch.setattr(gpu_mod, "_read_bounded", sneaky)
    assert read_amd_process_vram(9101, BDF) is None


@pytest.mark.parametrize("field", ["drm-client-id", "drm-memory-vram"])
def test_oversized_decimal_refuses_without_integer_conversion_error(monkeypatch, tmp_path, field):
    proot = _proc(monkeypatch, tmp_path)
    content = _drm().replace(
        "drm-client-id:\t7" if field == "drm-client-id" else "drm-memory-vram:\t1024 KiB",
        field + ":\t" + "9" * 5000 + (" KiB" if field.endswith("vram") else ""),
    )
    _pid(proot, 9201, 1, {"0": content})
    assert read_amd_process_vram(9201, BDF) is None


def test_foreign_unknown_vram_and_impossible_total_refused(monkeypatch, tmp_path):
    proot = _proc(monkeypatch, tmp_path)
    _pid(proot, 9301, 1, {"0": _drm(), "1": f"drm-pdev:\t{OTHER}\ndrm-client-id:\t8\n"})
    assert read_amd_process_vram(9301, BDF) is None
    _pid(proot, 9302, 1, {"0": _drm(cid=1, kib=2**40), "1": _drm(cid=2, kib=2**40)})
    assert read_amd_process_vram(9302, BDF) is None
