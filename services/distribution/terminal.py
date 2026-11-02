"""Line-oriented controlling-terminal I/O without seek-requiring buffering."""

import io
from contextlib import contextmanager


@contextmanager
def open_terminal():
    # BufferedRandom (open(..., 'r+')) requires seekability; a TTY has none.
    # Use raw bidirectional bytes plus text decoding/line-buffered writes instead.
    with open("/dev/tty", "r+b", buffering=0) as raw:
        with io.TextIOWrapper(
            raw, encoding="utf-8", line_buffering=True, write_through=True
        ) as terminal:
            yield terminal
