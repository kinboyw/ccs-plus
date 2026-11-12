from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest

from ccs_plus.terminal import TERMINAL_RESET, restored_terminal, run_process

if os.name == "posix":
    import pty
    import termios

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX terminal and signal tests")
TERMINATING_SIGNALS = [signal.SIGTERM]
if os.name == "posix":
    TERMINATING_SIGNALS.append(signal.SIGHUP)

SUPERVISOR = """
import os, sys
from pathlib import Path
from ccs_plus.terminal import run_process
if os.name == 'posix' and sys.stdin.isatty():
    import fcntl, termios
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
code = run_process([sys.executable, '-c', sys.argv[1]], cwd=Path.cwd(), env=dict(os.environ))
sys.exit(code if code >= 0 else 128 - code)
"""

WORKER_SETUP = """
import os, sys, termios
attributes = termios.tcgetattr(0)
attributes[3] &= ~(termios.ECHO | termios.ICANON)
termios.tcsetattr(0, termios.TCSANOW, attributes)
os.write(1, b'\\x1b[?1003h\\x1b[?1006hREADY\\n')
"""


@contextmanager
def terminal_process(
    worker: str,
) -> Iterator[tuple[subprocess.Popen[bytes], int, int, list[object]]]:
    master, slave = pty.openpty()
    attributes = termios.tcgetattr(slave)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    process = subprocess.Popen(
        [sys.executable, "-c", SUPERVISOR, worker],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=environment,
        start_new_session=True,
    )
    try:
        yield process, master, slave, attributes
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        os.close(master)
        os.close(slave)


def read_until(descriptor: int, text: bytes) -> bytes:
    output = b""
    deadline = time.monotonic() + 5
    while text not in output:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
            pytest.fail(f"Timed out waiting for {text!r}; received {output!r}")
        output += os.read(descriptor, 65536)
    return output


def test_restores_terminal_after_exception(monkeypatch) -> None:
    master, slave = pty.openpty()
    attributes = termios.tcgetattr(slave)
    monkeypatch.setattr("ccs_plus.terminal._terminal_descriptor", lambda: os.dup(slave))
    try:
        with pytest.raises(RuntimeError, match="launch failed"), restored_terminal():
            changed = termios.tcgetattr(slave)
            changed[3] &= ~termios.ECHO
            termios.tcsetattr(slave, termios.TCSANOW, changed)
            raise RuntimeError("launch failed")
        assert termios.tcgetattr(slave) == attributes
        assert TERMINAL_RESET in read_until(master, TERMINAL_RESET)
    finally:
        os.close(master)
        os.close(slave)


def test_restores_attributes_even_when_terminal_write_fails(monkeypatch) -> None:
    master, slave = pty.openpty()
    attributes = termios.tcgetattr(slave)
    monkeypatch.setattr("ccs_plus.terminal._terminal_descriptor", lambda: os.dup(slave))

    def failed_write(*args):
        raise OSError("terminal disconnected")

    try:
        with restored_terminal():
            changed = termios.tcgetattr(slave)
            changed[3] &= ~termios.ECHO
            termios.tcsetattr(slave, termios.TCSANOW, changed)
            monkeypatch.setattr("ccs_plus.terminal.os.write", failed_write)
        assert termios.tcgetattr(slave) == attributes
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize(
    ("exit_script", "exit_code"),
    [("sys.exit(7)", 7), ("import signal; os.kill(os.getpid(), signal.SIGKILL)", 137)],
)
def test_restores_terminal_after_exit(exit_script, exit_code) -> None:
    with terminal_process(WORKER_SETUP + exit_script) as (process, master, slave, attributes):
        assert process.wait(timeout=5) == exit_code
        assert termios.tcgetattr(slave) == attributes
        output = read_until(master, TERMINAL_RESET)
        assert b"\x1b[?1003h" in output
        assert b"\x1b[?1003l" in output


@pytest.mark.parametrize("signum", [signal.SIGINT, *TERMINATING_SIGNALS])
def test_restores_terminal_after_signal(signum) -> None:
    worker = WORKER_SETUP + "import time\ntime.sleep(60)"
    with terminal_process(worker) as (process, master, slave, attributes):
        read_until(master, b"READY")
        if signum == signal.SIGINT:
            os.killpg(process.pid, signum)
        else:
            os.kill(process.pid, signum)
        assert process.wait(timeout=5) == 128 + signum
        assert termios.tcgetattr(slave) == attributes
        assert TERMINAL_RESET in read_until(master, TERMINAL_RESET)


@pytest.mark.parametrize("signum", TERMINATING_SIGNALS)
def test_forwards_signal_to_agent_descendants(signum) -> None:
    grandchild = """
import os, signal, time
def stop(signum, frame):
    os.write(1, f'GRANDCHILD_STOPPED:{signum}\\n'.encode())
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGHUP, stop)
os.write(1, b'GRANDCHILD_READY\\n')
time.sleep(60)
"""
    worker = (
        WORKER_SETUP
        + f"""
import signal, subprocess, time
child = subprocess.Popen([sys.executable, '-c', {grandchild!r}])
def stop(signum, frame):
    child.wait(timeout=3)
    raise SystemExit(128 + signum)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGHUP, stop)
time.sleep(60)
"""
    )
    with terminal_process(worker) as (process, master, _slave, _attributes):
        read_until(master, b"GRANDCHILD_READY")
        os.kill(process.pid, signum)
        assert process.wait(timeout=5) == 128 + signum
        output = read_until(master, TERMINAL_RESET)
        assert f"GRANDCHILD_STOPPED:{signum}".encode() in output


def test_ctrl_c_reaches_agent_without_a_duplicate_interrupt() -> None:
    worker = (
        """
import os, signal, sys
def stop(signum, frame):
    os.write(1, b'INTERRUPTED\\n')
    raise SystemExit(0)
signal.signal(signal.SIGINT, stop)
"""
        + WORKER_SETUP
        + "import time\ntime.sleep(60)"
    )
    with terminal_process(worker) as (process, master, _slave, _attributes):
        read_until(master, b"READY")
        os.write(master, b"\x03")
        assert process.wait(timeout=5) == 0
        output = read_until(master, TERMINAL_RESET)
        assert output.count(b"INTERRUPTED") == 1


def test_agent_keeps_foreground_group_across_stop_and_continue() -> None:
    worker = (
        """
import os, signal
assert os.getpgrp() == os.getppid()
assert os.tcgetpgrp(0) == os.getpgrp()
def resume(signum, frame):
    raise SystemExit(0)
signal.signal(signal.SIGCONT, resume)
"""
        + WORKER_SETUP
        + "import time\ntime.sleep(60)"
    )
    with terminal_process(worker) as (process, master, slave, attributes):
        read_until(master, b"READY")
        os.killpg(process.pid, signal.SIGSTOP)
        deadline = time.monotonic() + 5
        while True:
            waited_pid, status = os.waitpid(process.pid, os.WNOHANG | os.WUNTRACED)
            if waited_pid:
                assert os.WIFSTOPPED(status)
                break
            if time.monotonic() >= deadline:
                pytest.fail("Supervisor did not stop with its foreground group")
            time.sleep(0.01)
        os.killpg(process.pid, signal.SIGCONT)
        assert process.wait(timeout=5) == 0
        assert termios.tcgetattr(slave) == attributes
        assert TERMINAL_RESET in read_until(master, TERMINAL_RESET)


def test_no_control_sequences_in_redirected_output(tmp_path) -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    completed = subprocess.run(
        [sys.executable, "-c", SUPERVISOR, "print('agent output')"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        cwd=tmp_path,
        env=environment,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout == b"agent output\n"
    assert completed.stderr == b""


def test_restores_signal_handlers_when_spawn_fails(monkeypatch, tmp_path) -> None:
    watched = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    handlers = {signum: signal.getsignal(signum) for signum in watched}
    monkeypatch.setattr("ccs_plus.terminal._terminal_descriptor", lambda: None)
    with pytest.raises(FileNotFoundError):
        run_process([str(tmp_path / "missing-agent")], cwd=tmp_path, env={})
    assert {signum: signal.getsignal(signum) for signum in watched} == handlers


@pytest.mark.parametrize("signum", TERMINATING_SIGNALS)
@pytest.mark.parametrize("during_spawn", [False, True])
def test_forwards_only_to_child_when_not_own_process_group(
    monkeypatch, tmp_path, signum, during_spawn
) -> None:
    forwarded = []
    previous = signal.getsignal(signum)

    class Child:
        def poll(self):
            return None

        def send_signal(self, received):
            forwarded.append(received)

        def wait(self):
            if not during_spawn:
                signal.getsignal(signum)(signum, None)
            return -signum

    def spawn(*args, **kwargs):
        assert kwargs == {"cwd": tmp_path, "env": {}}
        if during_spawn:
            signal.getsignal(signum)(signum, None)
        return Child()

    def forbidden_group_signal(*args):
        pytest.fail("Must not signal an unowned process group")

    monkeypatch.setattr("ccs_plus.terminal._terminal_descriptor", lambda: None)
    monkeypatch.setattr("ccs_plus.terminal.os.getpgrp", lambda: os.getpid() + 1)
    monkeypatch.setattr("ccs_plus.terminal.os.killpg", forbidden_group_signal)
    monkeypatch.setattr("ccs_plus.terminal.subprocess.Popen", spawn)
    assert run_process(["agent"], cwd=tmp_path, env={}) == -signum
    assert forwarded == [signum]
    assert signal.getsignal(signum) == previous
