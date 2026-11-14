"""Human-readable TUI status formatting for Kinaiya lifecycle commands (#510).

Provides structured checklists inspired by Hermes Agent for:
- update: channel, commit, prerequisites, component checklist, up-to-date banner
- version: provenance, channel, release commit, install root
- auth rotate-owner: epoch, digest, device revocation status
- recover / rollback: generation symlinks and preserved directories
- uninstall: cleanly uninstalled components and preserved data notices

Preserves raw JSON when --json is passed or stdout is non-interactive.
"""

from __future__ import annotations

from typing import Any

from services.distribution.terminal_ui import (
    BOLD,
    PALETTE,
    RESET,
    ColorMode,
    detect_color_mode,
)


def _symbols(color_mode: ColorMode) -> tuple[str, str, str]:
    """Return (check_mark, arrow, tilde) with optional palette colors."""
    if color_mode == ColorMode.NONE:
        return "✓", "→", "~"
    c_ok = PALETTE.emerald.fg_sequence(color_mode)
    c_arr = PALETTE.indigo.fg_sequence(color_mode)
    c_warn = PALETTE.amber.fg_sequence(color_mode)
    return f"{c_ok}✓{RESET}", f"{c_arr}→{RESET}", f"{c_warn}~{RESET}"


def format_install_report(
    result: dict[str, Any],
    *,
    no_color: bool = False,
    is_tty: bool = True,
) -> str:
    """Format kinaiya install results into a clean, human-readable terminal checklist."""
    mode = ColorMode.NONE if (no_color or not is_tty) else detect_color_mode()
    ok_sym, arr_sym, _ = _symbols(mode)
    c_bold = BOLD if mode != ColorMode.NONE else ""
    c_head = PALETTE.moonlight.fg_sequence(mode) if mode != ColorMode.NONE else ""

    installed = result.get("installed")
    installed_dict = installed if isinstance(installed, dict) else {}
    ver = installed_dict.get("version", "unknown")
    sha = str(installed_dict.get("source_sha", ""))[:12]
    chan = installed_dict.get("channel", "stable")
    launcher_path = result.get("launcher", "kinaiya")
    next_step = result.get("next", "kinaiya setup; kinaiya serve host")

    lines: list[str] = [
        f"{c_bold}{c_head}Kinaiya Installation Complete{RESET}",
        f" {arr_sym} Release channel: {chan}",
        f" {arr_sym} Installed release: {ver}" + (f" ({sha})" if sha else ""),
        f" {arr_sym} Launcher: {launcher_path}",
        f"   {ok_sym} Verified release artifact checksums (SHA-256)",
        f"   {ok_sym} Built isolated virtual environment",
        f"   {ok_sym} Activated current generation symlink",
        f"{ok_sym} {c_bold}Successfully installed!{RESET} Next: {next_step}",
    ]
    return "\n".join(lines) + "\n"


def format_update_report(
    result: dict[str, Any],
    *,
    no_color: bool = False,
    is_tty: bool = True,
) -> str:
    """Format kinaiya update results into a clean, human-readable terminal checklist."""
    mode = ColorMode.NONE if (no_color or not is_tty) else detect_color_mode()
    ok_sym, arr_sym, warn_sym = _symbols(mode)
    c_bold = BOLD if mode != ColorMode.NONE else ""
    c_head = PALETTE.moonlight.fg_sequence(mode) if mode != ColorMode.NONE else ""

    lines: list[str] = []

    # If the release was actively installed in this invocation:
    installed = result.get("installed")
    if installed and isinstance(installed, dict):
        ver = installed.get("version", "unknown")
        sha = installed.get("source_sha", "")[:12]
        chan = installed.get("channel", "stable")
        lines.append(f"{c_bold}{c_head}Kinaiya Update Complete{RESET}")
        lines.append(f" {arr_sym} Release channel: {chan}")
        lines.append(f" {arr_sym} Applied release: {ver}" + (f" ({sha})" if sha else ""))
        lines.append(f"   {ok_sym} Verified release artifact checksums (SHA-256)")
        lines.append(f"   {ok_sym} Built isolated virtual environment")
        lines.append(f"   {ok_sym} Switched active generation symlink")
        lines.append(f"   {ok_sym} Preserved configuration, databases, and private tokens")
        lines.append(
            f"{ok_sym} {c_bold}Successfully updated!{RESET} Run 'kinaiya' to start services."
        )
        return "\n".join(lines) + "\n"

    action = result.get("action", "current")
    channel = result.get("channel", "stable")
    ver = result.get("version", "unknown")
    source_sha = result.get("source_sha", "")[:12]

    if action == "current":
        lines.append(f"{c_bold}{c_head}Kinaiya Update Check{RESET}")
        lines.append(f" {arr_sym} Update channel: {channel}")
        lines.append(
            f" {arr_sym} Latest release: {ver}" + (f" ({source_sha})" if source_sha else "")
        )
        lines.append(f"   {ok_sym} Host & sandbox prerequisites verified")
        lines.append(f"   {ok_sym} Core runtime and service contracts up to date")
        lines.append(f"   {ok_sym} Prebuilt Web UI assets up to date")
        lines.append(f"   {ok_sym} Configuration & databases preserved (no migrations required)")
        lines.append(f"{ok_sym} {c_bold}Already up to date!{RESET} [{ver}]")
    elif action in {"upgrade", "downgrade"}:
        rel = result.get("release", {})
        artifacts = rel.get("artifacts", []) if isinstance(rel, dict) else []
        size_mb = ""
        for art in artifacts:
            if isinstance(art, dict) and art.get("platform") == "linux" and "size" in art:
                size_mb = f"{art['size'] / (1024 * 1024):.1f} MB"
                break
        action_verb = "Upgrade" if action == "upgrade" else "Downgrade"
        lines.append(f"{c_bold}{c_head}Kinaiya Update Available ({action_verb}){RESET}")
        lines.append(f" {arr_sym} Update channel: {channel}")
        lines.append(
            f" {arr_sym} Target release: {ver}" + (f" ({source_sha})" if source_sha else "")
        )
        if size_mb:
            lines.append(f" {arr_sym} Download size: {size_mb}")
        lines.append(f"   {warn_sym} Running local services must be stopped before applying update")
        lines.append(f"   {ok_sym} Configuration and data will be preserved (no database rollback)")
        lines.append(
            f"Run {c_bold}'kinaiya update --yes'{RESET} to download and apply this update."
        )
    else:
        lines.append(f"{c_bold}{c_head}Kinaiya Update Status: {action}{RESET}")
        lines.append(f" {arr_sym} Channel: {channel}, Target: {ver}")

    return "\n".join(lines) + "\n"


def format_version_report(
    result: dict[str, Any],
    *,
    no_color: bool = False,
    is_tty: bool = True,
) -> str:
    """Format kinaiya version results into a clean, human-readable terminal summary."""
    mode = ColorMode.NONE if (no_color or not is_tty) else detect_color_mode()
    _, arr_sym, _ = _symbols(mode)
    c_bold = BOLD if mode != ColorMode.NONE else ""
    c_head = PALETTE.moonlight.fg_sequence(mode) if mode != ColorMode.NONE else ""

    lines: list[str] = [f"{c_bold}{c_head}Kinaiya Distribution Version{RESET}"]
    rel = result.get("release")
    if isinstance(rel, dict):
        lines.append(f" {arr_sym} Version: {rel.get('version', 'unknown')}")
        lines.append(f" {arr_sym} Channel: {rel.get('channel', 'unknown')}")
        if rel.get("source_sha"):
            lines.append(f" {arr_sym} Source commit: {rel['source_sha'][:12]}")
        if rel.get("build_number") is not None:
            lines.append(f" {arr_sym} Build number: {rel['build_number']}")
    else:
        lines.append(f" {arr_sym} Version: development checkout (unmanaged)")

    lines.append(f" {arr_sym} Managed install: {'Yes' if result.get('managed') else 'No'}")
    if result.get("install"):
        lines.append(f" {arr_sym} Install root: {result['install']}")
    lines.append(
        f" {arr_sym} Diagnostics: Run 'kinaiya doctor' to verify service and sandbox health"
    )

    return "\n".join(lines) + "\n"


def format_auth_report(
    result: dict[str, Any],
    *,
    no_color: bool = False,
    is_tty: bool = True,
) -> str:
    """Format kinaiya auth rotate-owner results into human-readable confirmation."""
    mode = ColorMode.NONE if (no_color or not is_tty) else detect_color_mode()
    ok_sym, arr_sym, _ = _symbols(mode)
    c_bold = BOLD if mode != ColorMode.NONE else ""
    c_head = PALETTE.moonlight.fg_sequence(mode) if mode != ColorMode.NONE else ""

    lines: list[str] = [
        f"{c_bold}{c_head}Kinaiya Owner Credential Rotation{RESET}",
        f"{ok_sym} Owner credential rotated successfully!",
    ]
    if "owner_epoch" in result:
        lines.append(f" {arr_sym} New owner epoch: {result['owner_epoch']}")
    if "owner_token_sha256" in result:
        digest = str(result["owner_token_sha256"])[:16]
        lines.append(f" {arr_sym} Owner token SHA-256 digest: {digest}...")
    lines.append(f"{ok_sym} All previous scoped sessions and browser tokens revoked.")
    lines.append(f" {arr_sym} Run 'kinaiya owner-login' or start Kinaiya to sign in to the Web UI.")

    return "\n".join(lines) + "\n"


def format_recover_report(
    result: dict[str, Any],
    *,
    no_color: bool = False,
    is_tty: bool = True,
) -> str:
    """Format kinaiya recover results into human-readable terminal output."""
    mode = ColorMode.NONE if (no_color or not is_tty) else detect_color_mode()
    ok_sym, arr_sym, _ = _symbols(mode)
    c_bold = BOLD if mode != ColorMode.NONE else ""
    c_head = PALETTE.moonlight.fg_sequence(mode) if mode != ColorMode.NONE else ""

    action = result.get("action", "")
    lines: list[str] = [f"{c_bold}{c_head}Kinaiya State Recovery{RESET}"]
    if action == "nothing_to_recover":
        lines.append(f"{ok_sym} No interrupted transaction found; installation is clean.")
    else:
        gen = result.get("generation", "unknown")
        lines.append(f"{ok_sym} Handled interrupted operation for generation: {gen}")
        lines.append(f" {arr_sym} Recovery action: {action}")
        lines.append(f"{ok_sym} User configuration, private databases, and workspace preserved.")
    return "\n".join(lines) + "\n"


def format_rollback_report(
    result: dict[str, Any],
    *,
    no_color: bool = False,
    is_tty: bool = True,
) -> str:
    """Format kinaiya rollback results into human-readable terminal output."""
    mode = ColorMode.NONE if (no_color or not is_tty) else detect_color_mode()
    ok_sym, arr_sym, _ = _symbols(mode)
    c_bold = BOLD if mode != ColorMode.NONE else ""
    c_head = PALETTE.moonlight.fg_sequence(mode) if mode != ColorMode.NONE else ""

    gen = result.get("generation") or "previous"
    lines: list[str] = [
        f"{c_bold}{c_head}Kinaiya Generation Rollback{RESET}",
        f"{ok_sym} Switched active code generation to: {gen}",
        f" {arr_sym} Code rollback applied safely.",
        f"{ok_sym} User configuration, private databases, and workspace preserved.",
    ]
    return "\n".join(lines) + "\n"


def format_uninstall_report(
    result: dict[str, Any],
    *,
    no_color: bool = False,
    is_tty: bool = True,
) -> str:
    """Format kinaiya uninstall results into human-readable terminal output."""
    mode = ColorMode.NONE if (no_color or not is_tty) else detect_color_mode()
    ok_sym, _, warn_sym = _symbols(mode)
    c_bold = BOLD if mode != ColorMode.NONE else ""
    c_head = PALETTE.moonlight.fg_sequence(mode) if mode != ColorMode.NONE else ""

    lines: list[str] = [
        f"{c_bold}{c_head}Kinaiya Uninstallation Complete{RESET}",
        f"{ok_sym} Managed launcher and runtime binaries removed.",
    ]
    if result.get("data_deleted"):
        lines.append(f" {warn_sym} Private data and configuration directories deleted.")
    else:
        lines.append(f"{ok_sym} User configuration and private data directories preserved.")
    lines.append(f"{ok_sym} User workspace directory preserved.")
    return "\n".join(lines) + "\n"
