from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

if os.name == "posix":
    import termios

logger = logging.getLogger(__name__)

TERMINAL_RESET = (
    b"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1005l"
    b"\x1b[?1006l\x1b[?1015l\x1b[?1016l\x1b[?1004l"
    b"\x1b[?2004l\x1b[?2026l\x1b[?1049l\x1b[?25h"
)


@contextmanager
def restored_terminal() -> Iterator[None]:
    descriptor = _terminal_descriptor()
    attributes: list[Any] | None = None
    try:
        if descriptor is not None and os.name == "posix":
            try:
                attributes = termios.tcgetattr(descriptor)
            except termios.error:
                logger.debug("Unable to capture terminal attributes", exc_info=True)
        yield
    finally:
        if descriptor is not None:
            try:
                os.write(descriptor, TERMINAL_RESET)
            except OSError:
                logger.debug("Unable to reset terminal modes", exc_info=True)
            try:
                if attributes is not None:
                    termios.tcsetattr(descriptor, termios.TCSANOW, attributes)
            except (OSError, termios.error):
                logger.debug("Unable to restore terminal attributes", exc_info=True)
            finally:
                os.close(descriptor)


def _terminal_descriptor() -> int | None:
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            descriptor = stream.fileno()
            if not os.isatty(descriptor):
                continue
            if os.name == "posix":
                return os.open(os.ttyname(descriptor), os.O_RDWR | os.O_NOCTTY)
            if stream is not sys.stdin:
                return os.dup(descriptor)
        except (AttributeError, OSError, ValueError):
            continue
    return None


def run_process(argv: Sequence[str], *, cwd: Path, env: dict[str, str]) -> int:
    child: subprocess.Popen[bytes] | None = None
    pending: list[int] = []
    handlers: dict[int, Any] = {}

    def forward_signal(signum: int, frame: object) -> None:
        del frame
        if signum == signal.SIGINT:
            return
        if child is None:
            pending.append(signum)
            return
        if child.poll() is not None:
            return
        try:
            if os.name == "posix" and os.getpgrp() == os.getpid():
                signal.signal(signum, signal.SIG_IGN)
                try:
                    os.killpg(os.getpgrp(), signum)
                finally:
                    signal.signal(signum, forward_signal)
            else:
                child.send_signal(signum)
        except ProcessLookupError:
            pass

    try:
        with restored_terminal():
            if os.name == "posix" and threading.current_thread() is threading.main_thread():
                for watched_signal in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                    handlers[watched_signal] = signal.signal(watched_signal, forward_signal)
            child = subprocess.Popen(list(argv), cwd=cwd, env=env)
            try:
                for pending_signal in pending:
                    forward_signal(pending_signal, None)
                return child.wait()
            except BaseException:
                child.kill()
                child.wait()
                raise
    finally:
        for watched_number, handler in handlers.items():
            signal.signal(watched_number, handler)
