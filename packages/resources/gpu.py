"""Bounded stdlib-only AMD GPU measurement helpers (issue #53, Roadmap 14.3).

Diagnostic-only sampling of driver-reported counters for ONE configured GPU.

Scope and limitations:

* The PID argument to :func:`read_amd_process_vram` is a diagnostic
  measurement key only. It confers no ownership, and these helpers never
  signal processes nor grant authority over them; native process
  ownership/signalling remains solely with ``WorkerManager``.
* ``mem_info_vram_*`` sysfs counters are driver-reported global totals, not
  reservations and not causal proof that any particular PID caused the usage.
* Counter samples race other workloads on the machine, so per-PID
  attribution is approximate and values may change between reads.
* No hard reservation is performed or implied. Zero is reported only when a
  matching record explicitly reports zero; absent data is ``None``, never a
  guessed zero.
* Stdlib only; no subprocesses and no heavy dependencies. All reads are
  bounded and scoped to the configured BDF (under ``SYSFS_PCI``) and PID
  (under ``PROC_ROOT``). ``SYSFS_PCI``/``PROC_ROOT`` are fixed production
  constants, monkeypatchable only in tests.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

__all__ = [
    "SYSFS_PCI",
    "PROC_ROOT",
    "valid_pci_bus_id",
    "read_amd_memory",
    "read_amd_process_vram",
]

SYSFS_PCI = Path("/sys/bus/pci/devices")
PROC_ROOT = Path("/proc")

_BDF_RE = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]")
_UINT_RE = re.compile(r"[0-9]+")
_VRAM_RE = re.compile(r"([0-9]+)\s+KiB")

_MAX_SYSFS_BYTES = 256
_STAT_MAX_BYTES = 65536
_FDINFO_MAX_FILES = 1024
_FDINFO_MAX_BYTES = 16 * 1024
_MAX_COUNTER_BYTES = 2**50
_MAX_PID = 2**31 - 1


class _Malformed(Exception):
    """Internal signal for malformed/inconsistent DRM metadata (maps to None)."""


def valid_pci_bus_id(value: object) -> bool:
    """True for exact lowercase Linux BDF ``dddd:bb:dd.f`` with ``f`` in 0..7."""
    return isinstance(value, str) and _BDF_RE.fullmatch(value) is not None


def _read_bounded(path: Path, limit: int) -> bytes | None:
    """Read at most ``limit`` bytes; None when missing/unreadable/oversized."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        return None
    if len(data) > limit:
        return None
    return data


def _parse_counter(text: str) -> int | None:
    """Strictly unsigned decimal integer bounded to 2**50, else None."""
    text = text.strip()
    if len(text) > 16 or _UINT_RE.fullmatch(text) is None:
        return None
    value = int(text, 10)
    if value > _MAX_COUNTER_BYTES:
        return None
    return value


def read_amd_memory(pci_bus_id: str) -> dict | None:
    """Driver-reported VRAM totals for the configured AMD GPU, else None.

    Returns ``{'pci_bus_id', 'total_bytes', 'used_bytes', 'free_bytes'}``.
    Reads only the configured BDF under ``SYSFS_PCI``; requires AMD vendor
    ``0x1002`` and an ``amdgpu`` driver link. Counters must satisfy
    ``0 < total <= 2**50`` and ``0 <= used <= total``. Invalid BDF raises
    ``ValueError``; unavailable/malformed data returns None. These are
    driver-reported global counters, not reservations or causal proof.
    """
    if not valid_pci_bus_id(pci_bus_id):
        raise ValueError(f"invalid PCI bus id: {pci_bus_id!r}")
    dev = SYSFS_PCI / pci_bus_id
    try:
        if not dev.is_dir():
            return None
    except OSError:
        return None
    vendor = _read_bounded(dev / "vendor", _MAX_SYSFS_BYTES)
    if vendor is None:
        return None
    try:
        vendor_text = vendor.decode("ascii").strip().lower()
    except UnicodeDecodeError:
        return None
    if vendor_text != "0x1002":
        return None
    try:
        driver_target = os.readlink(dev / "driver")
    except OSError:
        return None
    if os.path.basename(driver_target.rstrip("/")) != "amdgpu":
        return None
    total_raw = _read_bounded(dev / "mem_info_vram_total", _MAX_SYSFS_BYTES)
    used_raw = _read_bounded(dev / "mem_info_vram_used", _MAX_SYSFS_BYTES)
    if total_raw is None or used_raw is None:
        return None
    try:
        total = _parse_counter(total_raw.decode("ascii"))
        used = _parse_counter(used_raw.decode("ascii"))
    except UnicodeDecodeError:
        return None
    if total is None or used is None:
        return None
    if total <= 0 or used > total:
        return None
    return {
        "pci_bus_id": pci_bus_id,
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": total - used,
    }


def _proc_starttime(pid: int) -> str | None:
    """Kernel starttime (field 22) of ``/proc/<pid>/stat``, else None."""
    data = _read_bounded(PROC_ROOT / str(pid) / "stat", _STAT_MAX_BYTES)
    if data is None:
        return None
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return None
    closing = text.rfind(")")
    if closing < 0:
        return None
    fields = text[closing + 1 :].split()
    if len(fields) < 20:
        return None
    starttime = fields[19]
    if len(starttime) > 20 or _UINT_RE.fullmatch(starttime) is None or int(starttime) > 2**64 - 1:
        return None
    return starttime


def _parse_fdinfo_record(text: str) -> tuple[str, int, int | None] | None:
    """Parse one fdinfo file.

    Returns None for ordinary files without DRM keys, else
    ``(pdev, client_id, peak_vram_bytes)`` where the peak is None when no
    ``drm-memory-vram`` line is present. Raises :class:`_Malformed` on any
    malformed/inconsistent DRM metadata.
    """
    pdevs: set[str] = set()
    client_ids: set[int] = set()
    vram: list[int] = []
    seen_drm = False
    for line in text.splitlines():
        key, sep, val = line.partition(":")
        if not sep:
            continue
        key = key.strip()
        val = val.strip()
        if key == "drm-pdev":
            seen_drm = True
            if not valid_pci_bus_id(val):
                raise _Malformed(f"bad drm-pdev: {val!r}")
            pdevs.add(val)
        elif key == "drm-client-id":
            seen_drm = True
            if len(val) > 20 or _UINT_RE.fullmatch(val) is None or int(val) > 2**64 - 1:
                raise _Malformed(f"bad drm-client-id: {val!r}")
            client_ids.add(int(val, 10))
        elif key == "drm-memory-vram":
            seen_drm = True
            match = _VRAM_RE.fullmatch(val)
            if match is None or len(match.group(1)) > 13:
                raise _Malformed(f"bad drm-memory-vram: {val!r}")
            nbytes = int(match.group(1), 10) * 1024
            if nbytes > _MAX_COUNTER_BYTES:
                raise _Malformed("drm-memory-vram exceeds bound")
            vram.append(nbytes)
    if not seen_drm:
        return None
    if len(pdevs) != 1 or len(client_ids) != 1:
        raise _Malformed("inconsistent drm device/client identity")
    peak = max(vram) if vram else None
    return (next(iter(pdevs)), next(iter(client_ids)), peak)


def read_amd_process_vram(pid: int, pci_bus_id: str) -> int | None:
    """Sample VRAM bytes attributed to ``pid`` on the configured GPU.

    Diagnostic only: ``pid`` is a measurement key, never ownership. This
    function never signals processes and never grants authority (that stays
    solely with ``WorkerManager``); it performs no reservation, and counter
    samples race other workloads so attribution is approximate.

    ``/proc/<pid>/stat`` starttime is read before and after the measurement;
    a missing/changed identity yields None. At most 1024 numeric fdinfo
    entries are enumerated (excess refused), each bounded to 16KiB.
    ``drm-memory-vram`` values are unsigned decimals followed by ``KiB``,
    converted to bytes bounded to 2**50. Only records for the configured
    device count; repeated descriptors deduplicate by ``drm-client-id``
    (max counter per client, then summed). Missing/invalid metadata,
    inconsistent device/client identity, or VRAM on an unexpected device
    returns None, never a guessed zero. Zero is valid only when a matching
    record explicitly reports zero.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or not 1 <= pid <= _MAX_PID:
        raise ValueError(f"invalid pid: {pid!r}")
    if not valid_pci_bus_id(pci_bus_id):
        raise ValueError(f"invalid PCI bus id: {pci_bus_id!r}")
    start_before = _proc_starttime(pid)
    if start_before is None:
        return None
    fdinfo_dir = PROC_ROOT / str(pid) / "fdinfo"
    try:
        numeric = []
        with os.scandir(fdinfo_dir) as entries:
            for entry in entries:
                if _UINT_RE.fullmatch(entry.name):
                    numeric.append(entry.name)
                    if len(numeric) > _FDINFO_MAX_FILES:
                        return None
    except OSError:
        return None
    per_client: dict[int, int] = {}
    try:
        for name in sorted(numeric):
            raw = _read_bounded(fdinfo_dir / name, _FDINFO_MAX_BYTES)
            if raw is None:
                return None
            try:
                text = raw.decode("ascii")
            except UnicodeDecodeError:
                if b"drm-" in raw:
                    raise _Malformed("non-text drm data") from None
                continue
            record = _parse_fdinfo_record(text)
            if record is None:
                continue
            pdev, client_id, peak = record
            if pdev != pci_bus_id:
                if peak is None or peak:
                    raise _Malformed("VRAM used on unexpected device")
                continue
            if peak is None:
                raise _Malformed("matching record without drm-memory-vram")
            per_client[client_id] = max(peak, per_client.get(client_id, 0))
    except _Malformed:
        return None
    start_after = _proc_starttime(pid)
    if start_after is None or start_after != start_before:
        return None
    if not per_client:
        return None
    total = sum(per_client.values())
    return total if total <= _MAX_COUNTER_BYTES else None
