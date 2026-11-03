"""Unit and snapshot tests for Issue #399: optional terminal intro animation and activity indicator."""

from __future__ import annotations

import io

import pytest

from services.distribution.terminal_animation import (
    CLEAR_LINE,
    CURSOR_HIDE,
    CURSOR_SHOW,
    MASCOT,
    MOON_FRAMES,
    BrandedActivityIndicator,
    render_intro_sequence,
    should_animate,
)


class FakeTTYStream(io.StringIO):
    """Controllable in-memory stream with configurable TTY capability."""

    def __init__(self, *, is_tty: bool = True) -> None:
        super().__init__()
        self._is_tty = is_tty
        self.flushes = 0

    def isatty(self) -> bool:
        return self._is_tty

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


class FakeClock:
    """Deterministic monotonic clock for animation timing bounds."""

    def __init__(self, initial: float = 10.0) -> None:
        self.now = initial
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.now += duration


def test_rich_tty_displays_brief_intro_and_ends_on_clean_static_frame() -> None:
    stream = FakeTTYStream(is_tty=True)
    clock = FakeClock()
    env = {"TERM": "xterm-256color"}

    assert should_animate(stream, env=env, width=80) is True
    final = render_intro_sequence(
        stream,
        width=80,
        env=env,
        sleep_fn=clock.sleep,
        clock_fn=clock,
        max_duration_s=0.16,
    )

    output = stream.getvalue()
    assert final == MASCOT
    assert output.startswith(CURSOR_HIDE)
    assert output.endswith(MASCOT + "\n" + CURSOR_SHOW)
    assert sum(clock.sleeps) <= 0.16 + 1e-9
    assert len(clock.sleeps) == 2
    assert stream.flushes >= 3


@pytest.mark.parametrize(
    ("is_tty", "env"),
    [
        (False, {"TERM": "xterm-256color"}),
        (True, {"TERM": "xterm-256color", "NO_COLOR": "1"}),
        (True, {"TERM": "xterm-256color", "NO_COLOR": ""}),
        (True, {"TERM": "xterm-256color", "REDUCED_MOTION": "1"}),
        (True, {"TERM": "xterm-256color", "KINAIYA_NO_ANIMATION": "1"}),
        (True, {"TERM": "xterm-256color", "CI": "true"}),
        (True, {"TERM": "dumb"}),
        (True, {"TERM": ""}),
    ],
)
def test_disabled_motion_and_non_tty_emit_static_text_with_zero_escape_codes(
    is_tty: bool, env: dict[str, str]
) -> None:
    stream = FakeTTYStream(is_tty=is_tty)
    clock = FakeClock()

    assert should_animate(stream, env=env, width=80) is False
    final = render_intro_sequence(
        stream,
        width=80,
        env=env,
        sleep_fn=clock.sleep,
        clock_fn=clock,
    )

    output = stream.getvalue()
    assert "\x1b" not in output
    assert clock.sleeps == []
    if is_tty:
        assert final == MASCOT
        assert output == MASCOT + "\n"
    else:
        assert final == "Kinaiya"
        assert output == "Kinaiya\n"

    # Activity indicator in static mode emits zero escape sequences and never implies completion early
    ind_stream = FakeTTYStream(is_tty=is_tty)
    with BrandedActivityIndicator(
        ind_stream, "Verifying release bundle", width=80, env=env, clock_fn=clock
    ) as indicator:
        assert "[...] Verifying release bundle\n" == ind_stream.getvalue()
        indicator.tick("checking SHA-256")
        assert "\x1b" not in ind_stream.getvalue()
        assert "[PASS]" not in ind_stream.getvalue()
        indicator.finish(status="pass", message="verified")

    assert "\x1b" not in ind_stream.getvalue()
    assert ind_stream.getvalue().splitlines() == [
        "[...] Verifying release bundle",
        "[PASS] Verifying release bundle: verified",
    ]


def test_simulated_keyboard_interrupt_mid_intro_restores_cursor_and_clean_static_frame() -> None:
    stream = FakeTTYStream(is_tty=True)
    clock = FakeClock()

    def interrupt_sleep(duration: float) -> None:
        clock.sleep(duration)
        raise KeyboardInterrupt("user pressed Ctrl-C during intro")

    with pytest.raises(KeyboardInterrupt, match="Ctrl-C"):
        render_intro_sequence(
            stream,
            width=80,
            env={"TERM": "xterm-256color"},
            sleep_fn=interrupt_sleep,
            clock_fn=clock,
        )

    output = stream.getvalue()
    assert output.endswith(MASCOT + "\n" + CURSOR_SHOW)


def test_branded_activity_indicator_rich_tty_and_interrupt_cleanup() -> None:
    stream = FakeTTYStream(is_tty=True)
    clock = FakeClock()
    env = {"TERM": "xterm-256color"}

    with BrandedActivityIndicator(
        stream, "Starting local host", width=80, env=env, clock_fn=clock
    ) as indicator:
        assert indicator.animated is True
        assert MOON_FRAMES[0] in stream.getvalue()
        assert "[PASS]" not in stream.getvalue()
        indicator.tick("session healthy")
        assert MOON_FRAMES[1] in stream.getvalue()
        indicator.tick("agent healthy")
        assert MOON_FRAMES[2] in stream.getvalue()
        indicator.finish(status="pass", message="all sockets ready")

    output = stream.getvalue()
    assert clock.sleeps == []
    assert "[PASS] Starting local host: all sockets ready\n" in output
    assert output.endswith(CURSOR_SHOW)

    # Interrupt before finish() must not report PASS and must restore cursor visibility
    err_stream = FakeTTYStream(is_tty=True)
    with pytest.raises(KeyboardInterrupt):
        with BrandedActivityIndicator(
            err_stream, "Checking loopback ports", width=80, env=env, clock_fn=clock
        ) as indicator2:
            indicator2.tick("probing 8760")
            raise KeyboardInterrupt

    err_out = err_stream.getvalue()
    assert "[PASS]" not in err_out
    assert f"{CLEAR_LINE}[INTERRUPTED] Checking loopback ports\n" in err_out
    assert err_out.endswith(CURSOR_SHOW)


@pytest.mark.parametrize("narrow_width", [59, 40, 20, 5])
def test_narrow_terminal_uses_compact_static_fallback_without_wrapping_or_escapes(
    narrow_width: int,
) -> None:
    stream = FakeTTYStream(is_tty=True)
    clock = FakeClock()
    env = {"TERM": "xterm-256color"}

    assert should_animate(stream, env=env, width=narrow_width) is False
    final = render_intro_sequence(
        stream,
        width=narrow_width,
        env=env,
        sleep_fn=clock.sleep,
        clock_fn=clock,
    )

    output = stream.getvalue()
    assert "\x1b" not in output
    assert clock.sleeps == []
    lines = output.splitlines()
    assert len(lines) == 1
    assert len(lines[0]) <= narrow_width
    assert final == "Kinaiya"[:narrow_width]
