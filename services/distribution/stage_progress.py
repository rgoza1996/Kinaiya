"""Human-readable stage progress, quiet redacted logs, and deterministic JSON output (#384)."""

from __future__ import annotations

import contextvars
import json
import os
import re
import shutil
import sys
import textwrap
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

STAGES: tuple[tuple[str, str], ...] = (
    ("preflight", "Checking host & sandbox prerequisites"),
    ("dependencies", "Verifying system dependencies"),
    ("download", "Downloading & verifying release artifact"),
    ("staging", "Staging runtime & isolated virtual environment"),
    ("setup_handoff", "Handing off to Kinaiya setup"),
)

STAGE_LABELS: dict[str, str] = dict(STAGES)
STAGE_INDEX: dict[str, int] = {key: idx + 1 for idx, (key, _) in enumerate(STAGES)}

STAGE_REMEDIATION: dict[str, str] = {
    "preflight": (
        "Verify Ubuntu 22.04/24.04/26.04 host prerequisites, Python 3.11-3.14, >=1 GiB free disk, "
        "and bubblewrap user namespaces."
    ),
    "dependencies": (
        "Install missing packages manually with `sudo apt-get install --no-upgrade -y <pkgs>` "
        "or refresh your local apt index."
    ),
    "download": (
        "Check HTTPS connectivity to github.com / release-assets.githubusercontent.com "
        "and retry the installation."
    ),
    "staging": (
        "Check Python venv/ensurepip availability, network access to pypi.org, "
        "and writable space in the install root."
    ),
    "setup_handoff": "Run `kinaiya setup` explicitly to complete provider configuration.",
}

_ACTIVE_REPORTER: contextvars.ContextVar[StageReporter | None] = contextvars.ContextVar(
    "kinaiya_active_stage_reporter", default=None
)

_AUTH_HEADER_RE = re.compile(
    r"(?i)(\bAuthorization\s*:\s*)(?:Bearer\s+|Basic\s+|Token\s+)?[^\s\"'\\,;]+"
)
_BEARER_RE = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]{4,}")
_URL_USERINFO_RE = re.compile(r"\b((?:https?|wss?)://)[^\s/@\"']+@(?=[^\s/\"']+)")
_JSON_SECRET_RE = re.compile(
    r'(?i)("(?:[a-z0-9_]*?(?:api_key|secret|token|password|credential)(?!_env)[a-z0-9_]*)"\s*:\s*)"[^"]*"'
)
_ASSIGN_SECRET_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_]*?(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)(?!_ENV)[A-Za-z0-9_]*\s*[=:]\s*)"
    r"([^\s,;\"']+|\"[^\"]*\"|'[^']*')"
)
_CLI_FLAG_SECRET_RE = re.compile(
    r"(?i)(--(?:[a-z0-9-]*?(?:key|token|secret|password|credential))(?!-env)\s+)([^\s\"']+|\"[^\"]*\"|'[^']*')"
)
_TOKEN_PREFIX_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,})\b"
)


def redact_install_log(text: str) -> str:
    """Strip credentials, Authorization headers, Bearer tokens, and URL userinfo."""
    if not text:
        return ""
    redacted = _AUTH_HEADER_RE.sub(r"\1[REDACTED]", text)
    redacted = _BEARER_RE.sub(r"\1 [REDACTED]", redacted)
    redacted = _URL_USERINFO_RE.sub(r"\1[REDACTED]@", redacted)
    redacted = _JSON_SECRET_RE.sub(r'\1"[REDACTED]"', redacted)
    redacted = _CLI_FLAG_SECRET_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _ASSIGN_SECRET_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _TOKEN_PREFIX_RE.sub("[REDACTED]", redacted)
    return redacted


def ensure_private_log(log_path: Path) -> Path:
    """Create or open a private 0600 diagnostic log file, refusing symlink aliases."""
    if log_path.is_symlink():
        raise RuntimeError("Diagnostic log path cannot be a symlink alias")
    parent = log_path.parent
    if parent.is_symlink() or (
        parent.exists() and parent.resolve() != Path(os.path.abspath(parent))
    ):
        raise RuntimeError("Diagnostic log directory cannot use symlink aliases")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent.resolve() != Path(os.path.abspath(parent)):
        raise RuntimeError("Diagnostic log directory cannot use symlink aliases")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(log_path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    return log_path


def append_private_log(log_path: Path, message: str) -> Path:
    """Append redacted diagnostic output to the user-private 0600 installer log."""
    ensure_private_log(log_path)
    safe_text = redact_install_log(message)
    if not safe_text.endswith("\n"):
        safe_text += "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(log_path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(safe_text)
            handle.flush()
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return log_path


def current_reporter() -> StageReporter | None:
    return _ACTIVE_REPORTER.get()


@contextmanager
def activate_reporter(reporter: StageReporter | None):
    token = _ACTIVE_REPORTER.set(reporter)
    try:
        yield reporter
    finally:
        _ACTIVE_REPORTER.reset(token)


class StageReporter:
    """Render stage-based installer progress on TTYs and deterministic JSON when piped."""

    def __init__(
        self,
        *,
        log_path: Path | None = None,
        json_output: bool = False,
        stream: Any = None,
        err_stream: Any = None,
        width: int | None = None,
        no_color: bool | None = None,
        env: Mapping[str, str] | None = None,
        defer_log_creation: bool = False,
    ) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.err_stream = err_stream if err_stream is not None else sys.stderr
        self.log_path = log_path or (Path.home() / ".cache/kinaiya/install.log")
        self._defer_log = bool(defer_log_creation)
        self._buffered_logs: list[str] = []
        env_map = os.environ if env is None else env
        is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.json_mode = bool(json_output or not is_tty)
        env_no_color = bool(env_map.get("NO_COLOR")) or env_map.get("TERM", "") == "dumb"
        self.no_color = bool(
            no_color if no_color is not None else (self.json_mode or not is_tty or env_no_color)
        )
        detected_width = width if width is not None else shutil.get_terminal_size((80, 24)).columns
        self.width = max(20, int(detected_width))
        self.stages: dict[str, dict[str, Any]] = {
            key: {
                "index": idx + 1,
                "label": label,
                "status": "pending",
                "detail": None,
            }
            for idx, (key, label) in enumerate(STAGES)
        }
        self.download_state: dict[str, Any] = {
            "downloaded_bytes": 0,
            "total_bytes": None,
            "sha256_verified": False,
        }
        self.failed_stage: str | None = None
        self.cancelled: bool = False

    def _style(self, text: str, ansi_code: str) -> str:
        if self.no_color or self.json_mode:
            return text
        return f"\x1b[{ansi_code}m{text}\x1b[0m"

    def _wrap_lines(self, text: str) -> list[str]:
        out: list[str] = []
        for raw_line in text.splitlines() or [""]:
            if len(raw_line) <= self.width:
                out.append(raw_line)
            else:
                wrapped = textwrap.wrap(
                    raw_line,
                    width=self.width,
                    break_long_words=True,
                    break_on_hyphens=False,
                )
                out.extend(wrapped or [""])
        return out

    def _write_tty(self, text: str, *, error: bool = False) -> None:
        target = self.err_stream if error else self.stream
        safe = redact_install_log(text)
        for line in self._wrap_lines(safe):
            target.write(line + "\n")
        if hasattr(target, "flush"):
            target.flush()

    def enable_log_persistence(self) -> Path:
        self._defer_log = False
        ensure_private_log(self.log_path)
        if self._buffered_logs:
            combined = "\n".join(self._buffered_logs)
            self._buffered_logs.clear()
            append_private_log(self.log_path, combined)
        return self.log_path

    def log(self, message: str, *, stage: str | None = None) -> Path:
        prefix = f"[{stage}] " if stage else ""
        entry = redact_install_log(prefix + message)
        if self._defer_log:
            self._buffered_logs.append(entry)
            return self.log_path
        return append_private_log(self.log_path, entry)

    def log_subprocess(
        self,
        stage: str,
        command: list[str] | tuple[str, ...],
        returncode: int,
        stdout: str | bytes | None = None,
        stderr: str | bytes | None = None,
    ) -> Path:
        if isinstance(stdout, bytes):
            stdout_str = stdout.decode("utf-8", errors="replace")
        else:
            stdout_str = stdout or ""
        if isinstance(stderr, bytes):
            stderr_str = stderr.decode("utf-8", errors="replace")
        else:
            stderr_str = stderr or ""
        cmd_str = " ".join(str(part) for part in command)
        parts = [f"command={cmd_str} exit_code={returncode}"]
        if stdout_str.strip():
            parts.append(f"stdout:\n{stdout_str.strip()}")
        if stderr_str.strip():
            parts.append(f"stderr:\n{stderr_str.strip()}")
        return self.log("\n".join(parts), stage=stage)

    def render_plan(self, payload: dict[str, Any], *, indent: int | None = 2) -> str:
        """Render the installation plan as deterministic JSON or human-readable TTY lines."""
        if self.json_mode:
            return redact_install_log(json.dumps(payload, indent=indent))
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else payload
        channel = plan.get("channel", payload.get("channel", "stable"))
        version = plan.get("version")
        source_sha = str(plan.get("source_sha", payload.get("source_sha", "")))
        short_sha = source_sha[:12] if source_sha else "unknown"
        install_dir = payload.get("install", plan.get("install", ""))
        download_bytes = payload.get("download_bytes")
        if download_bytes is None and isinstance(plan.get("release"), dict):
            artifacts = plan["release"].get("artifacts", [])
            if artifacts and isinstance(artifacts[0], dict):
                download_bytes = artifacts[0].get("size")
        size_str = (
            f"{download_bytes} B"
            if isinstance(download_bytes, int) and download_bytes > 0
            else "size unknown"
        )
        missing = payload.get(
            "missing_dependencies", payload.get("missing_system_dependencies", [])
        )
        dep_cmd = payload.get("dependency_command", payload.get("system_dependency_command", ""))
        lines = [
            "Kinaiya Installation Plan",
            f"  Channel:      {channel}" + (f" (v{version})" if version else ""),
            f"  Source SHA:   {short_sha}",
            f"  Artifact:     {size_str} (SHA-256 verified)",
            f"  Install root: {install_dir}",
            f"  Dependencies: {', '.join(missing) if missing else 'all present'}",
        ]
        if dep_cmd:
            lines.append(f"  Apt command:  {dep_cmd}")
        lines.append("  Stages:       1/5 preflight .. 5/5 setup_handoff")
        lines.append(f"  Private log:  {self.log_path} (0600)")
        wrapped: list[str] = []
        for line in lines:
            wrapped.extend(self._wrap_lines(redact_install_log(line)))
        return "\n".join(wrapped)

    def start_stage(self, stage: str, detail: str | None = None) -> None:
        if stage not in STAGE_LABELS:
            raise ValueError(f"Unknown installer stage: {stage!r}")
        idx = STAGE_INDEX[stage]
        label = STAGE_LABELS[stage]
        self.stages[stage]["status"] = "running"
        self.stages[stage]["detail"] = detail
        suffix = f" ({detail})" if detail else ""
        self.log(f"[STAGE {idx}/{len(STAGES)} START] {label}{suffix}", stage=stage)
        if not self.json_mode:
            self._write_tty(f"[{idx}/{len(STAGES)}] {label}{suffix} ...")

    def update_download(self, downloaded_bytes: int, total_bytes: int | None = None) -> None:
        if downloaded_bytes < 0:
            raise ValueError("downloaded_bytes must be non-negative")
        if self.stages["download"]["status"] == "pending":
            self.start_stage("download")
        self.download_state["downloaded_bytes"] = int(downloaded_bytes)
        valid_total = int(total_bytes) if type(total_bytes) is int and total_bytes > 0 else None
        self.download_state["total_bytes"] = valid_total
        self.download_state["sha256_verified"] = False
        idx = STAGE_INDEX["download"]
        if valid_total is not None:
            pct = min(100, (int(downloaded_bytes) * 100) // valid_total)
            msg = (
                f"[{idx}/{len(STAGES)}] Download progress: "
                f"{downloaded_bytes}/{valid_total} B ({pct}%) - awaiting SHA-256 verification"
            )
        else:
            # Never invent a percentage when artifact total bytes or duration are unknown.
            msg = (
                f"[{idx}/{len(STAGES)}] Download progress: "
                f"{downloaded_bytes} B (total size unknown) - awaiting SHA-256 verification"
            )
        self.log(msg, stage="download")
        if not self.json_mode:
            self._write_tty(msg)

    def mark_sha256_verified(self, sha256_hex: str | None = None) -> None:
        self.download_state["sha256_verified"] = True
        digest_note = f" ({sha256_hex[:12]}...)" if sha256_hex else ""
        self.log(f"SHA-256 verification succeeded{digest_note}", stage="download")

    def complete_stage(
        self,
        stage: str,
        detail: str | None = None,
        *,
        sha256_verified: bool = False,
    ) -> None:
        if stage not in STAGE_LABELS:
            raise ValueError(f"Unknown installer stage: {stage!r}")
        if stage == "download":
            if sha256_verified:
                self.download_state["sha256_verified"] = True
            if not self.download_state["sha256_verified"]:
                raise RuntimeError(
                    "Download stage cannot be marked complete before SHA-256 verification succeeds"
                )
        idx = STAGE_INDEX[stage]
        label = STAGE_LABELS[stage]
        self.stages[stage]["status"] = "passed"
        self.stages[stage]["detail"] = detail
        suffix = f" ({detail})" if detail else ""
        self.log(f"[STAGE {idx}/{len(STAGES)} PASS] {label}{suffix}", stage=stage)
        if not self.json_mode:
            badge = self._style("PASS", "32")
            self._write_tty(f"[{idx}/{len(STAGES)}] {label}{suffix} ... {badge}")

    def skip_stage(self, stage: str, reason: str = "not required") -> None:
        if stage not in STAGE_LABELS:
            raise ValueError(f"Unknown installer stage: {stage!r}")
        idx = STAGE_INDEX[stage]
        label = STAGE_LABELS[stage]
        self.stages[stage]["status"] = "skipped"
        self.stages[stage]["detail"] = reason
        self.log(f"[STAGE {idx}/{len(STAGES)} SKIP] {label} ({reason})", stage=stage)
        if not self.json_mode:
            self._write_tty(f"[{idx}/{len(STAGES)}] {label} ... SKIP ({reason})")

    def fail_stage(
        self,
        stage: str,
        reason: str,
        *,
        remediation: str | None = None,
    ) -> str:
        if stage not in STAGE_LABELS:
            raise ValueError(f"Unknown installer stage: {stage!r}")
        idx = STAGE_INDEX[stage]
        label = STAGE_LABELS[stage]
        clean_reason = redact_install_log(str(reason).strip())
        action = redact_install_log(remediation or STAGE_REMEDIATION[stage])
        self.stages[stage]["status"] = "failed"
        self.stages[stage]["detail"] = clean_reason
        self.failed_stage = stage
        self.log(
            f"[STAGE {idx}/{len(STAGES)} FAIL] {label}: {clean_reason} | Remediation: {action}",
            stage=stage,
        )
        badge = self._style("FAIL", "31")
        summary_lines = [
            f"[{idx}/{len(STAGES)}] {label} ({stage}) ... {badge}",
            f"  Reason:         {clean_reason}",
            f"  Diagnostic log: {self.log_path} (mode 0600)",
            f"  Remediation:    {action}",
        ]
        formatted_lines: list[str] = []
        for line in summary_lines:
            formatted_lines.extend(self._wrap_lines(line))
        formatted = "\n".join(formatted_lines)
        if not self.json_mode:
            self._write_tty(formatted, error=True)
        return formatted

    def cancel(self, reason: str = "Installation cancelled; no files changed") -> str:
        self.cancelled = True
        clean_reason = redact_install_log(str(reason).strip())
        self.log(f"[CANCELLED] {clean_reason}")
        msg = f"Cancelled: {clean_reason}"
        if not self.json_mode:
            self._write_tty(msg, error=True)
        return msg
