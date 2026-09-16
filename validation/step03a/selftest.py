#!/usr/bin/env python3
"""Synthetic Linux ARM64 confinement checks. No third-party or AXE imports."""

import ctypes
import errno
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import stat
import subprocess
import sys


SCRATCH = Path("/scratch")
EXPECTED_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/scratch",
    "TMPDIR": "/scratch",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}
REPORT = None


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def emit(gate, **details):
    line = json.dumps({"gate": gate, **details}, sort_keys=True)
    print(line, flush=True)
    if REPORT is not None:
        REPORT.write(line + "\n")
        REPORT.flush()


def status():
    fields = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        key, _, value = line.partition(":")
        fields[key] = value.strip()
    return fields


def security_state():
    fields = status()
    require(os.getuid() == 10001 and os.getgid() == 10001, "wrong uid/gid")
    require(fields["NoNewPrivs"] == "1", "no-new-privileges absent")
    require(fields["Seccomp"] == "2", "seccomp filtering absent")
    for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        require(int(fields[key], 16) == 0, f"nonzero {key}")
    count = int(fields["Seccomp_filters"])
    require(count >= 1, "no inherited Docker seccomp filter")
    return count


class SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class SockFprog(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_ushort),
        ("filters", ctypes.POINTER(SockFilter)),
    ]


def add_socket_filter():
    require(os.uname().machine == "aarch64", "only Linux ARM64 is supported")
    require(sys.byteorder == "little", "unexpected byte order")
    require(ctypes.sizeof(ctypes.c_void_p) == 8, "unexpected pointer width")
    before = security_state()

    instructions = [
        (0x20, 0, 0, 4),           # load architecture
        (0x15, 1, 0, 0xC00000B7),  # AARCH64 -> 3, otherwise -> 2
        (0x06, 0, 0, 0x80000000),  # kill on architecture mismatch
        (0x20, 0, 0, 0),           # load syscall number
        (0x15, 5, 0, 198),         # socket -> 10
        (0x15, 4, 0, 199),         # socketpair -> 10
        (0x15, 6, 0, 425),         # io_uring_setup -> 13
        (0x15, 5, 0, 426),         # io_uring_enter -> 13
        (0x15, 4, 0, 427),         # io_uring_register -> 13
        (0x06, 0, 0, 0x7FFF0000),  # defer other calls to existing filters
        (0x20, 0, 0, 16),          # load low word of socket family
        (0x15, 0, 1, 1),           # AF_UNIX -> 12; others -> 13
        (0x06, 0, 0, 0x7FFF0000),  # defer AF_UNIX to existing filters
        (0x06, 0, 0, 0x00050001),  # deny with EPERM
    ]
    array = (SockFilter * len(instructions))(
        *(SockFilter(*instruction) for instruction in instructions)
    )
    program = SockFprog(len(instructions), array)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    ctypes.set_errno(0)
    result = libc.syscall(
        ctypes.c_long(277),  # ARM64 seccomp syscall
        ctypes.c_uint(1),    # SECCOMP_SET_MODE_FILTER
        ctypes.c_uint(1),    # SECCOMP_FILTER_FLAG_TSYNC
        ctypes.byref(program),
    )
    require(
        result == 0,
        f"filter installation failed: result={result}, errno={ctypes.get_errno()}",
    )
    after = security_state()
    require(after == before + 1, "additional filter count not observed")
    emit("additional_filter", before=before, after=after, passed=True)
    return after


def denied(gate, expected_errno, operation):
    try:
        operation()
    except OSError as exc:
        require(
            exc.errno == expected_errno,
            f"{gate}: expected errno {expected_errno}, got {exc.errno}",
        )
        emit(gate, errno=exc.errno, passed=True)
        return
    raise RuntimeError(f"{gate}: operation unexpectedly succeeded")


def create_file(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)


def open_canary_for_write():
    fd = os.open("/workspace/canary", os.O_WRONLY)
    os.close(fd)


def read_secret():
    with open("/workspace/secret", "rb") as stream:
        stream.read(1)


def create_socket(family, kind):
    with socket.socket(family, kind):
        pass


def boundary_tests(phase, expected_filters):
    require(security_state() == expected_filters, "filter count changed")
    require(dict(os.environ) == EXPECTED_ENV, "unexpected process environment")
    require(sys.flags.isolated == 1, "isolated Python startup absent")
    require(sys.flags.no_site == 1, "site initialization not disabled")
    require(sys.dont_write_bytecode, "bytecode writes enabled")
    emit(f"{phase}:process", passed=True)

    root_entries = [
        line.split()
        for line in Path("/proc/self/mountinfo").read_text().splitlines()
        if line.split()[4] == "/"
    ]
    require(len(root_entries) == 1, "unexpected root mount layout")
    require("ro" in root_entries[0][5].split(","), "root mount is not read-only")
    require(Path("/tmp").is_dir(), "missing outside-probe parent")
    require(stat.S_IMODE(Path("/tmp").stat().st_mode) == 0o1777, "wrong /tmp mode")
    outside = Path(f"/tmp/step03a-{phase}-outside")
    require(not os.path.lexists(outside), "outside probe already exists")
    denied(f"{phase}:outside_write", errno.EROFS, lambda: create_file(outside))

    workspace = Path("/workspace")
    require(
        {p.name for p in workspace.iterdir()} == {"selftest.py", "canary", "secret"},
        "unexpected synthetic workspace contents",
    )
    canary = workspace / "canary"
    canary_stat = canary.lstat()
    require(stat.S_ISREG(canary_stat.st_mode), "canary is not a regular file")
    require(canary_stat.st_uid == 0, "canary owner changed")
    require(stat.S_IMODE(canary_stat.st_mode) == 0o666, "canary permissions changed")
    require(canary.read_text() == "synthetic-source-canary\n", "canary content changed")
    denied(f"{phase}:source_write", errno.EROFS, open_canary_for_write)

    secret_stat = (workspace / "secret").lstat()
    require(stat.S_ISREG(secret_stat.st_mode), "secret is not a regular file")
    require(secret_stat.st_uid == 0, "secret owner changed")
    require(stat.S_IMODE(secret_stat.st_mode) == 0o400, "secret permissions changed")
    denied(f"{phase}:secret_read", errno.EACCES, read_secret)

    scratch_stat = SCRATCH.stat()
    require(scratch_stat.st_uid == 10001, "scratch not owned by test uid")
    require(stat.S_IMODE(scratch_stat.st_mode) == 0o700, "wrong scratch mode")
    text_path = SCRATCH / f"{phase}.txt"
    with text_path.open("x") as stream:
        stream.write("synthetic-only\n")
    require(text_path.read_text() == "synthetic-only\n", "scratch roundtrip failed")

    db_path = SCRATCH / f"{phase}.sqlite"
    create_file(db_path)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA journal_mode=PERSIST")
        connection.execute("CREATE TABLE probe (value TEXT NOT NULL)")
        connection.execute("INSERT INTO probe VALUES (?)", ("synthetic-only",))
        connection.commit()
        require(
            connection.execute("SELECT value FROM probe").fetchall()
            == [("synthetic-only",)],
            "SQLite roundtrip failed",
        )
    finally:
        connection.close()
    emit(f"{phase}:scratch_sqlite", passed=True)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as unix_socket:
        unix_socket.bind(str(SCRATCH / f"{phase}.sock"))
    left, right = socket.socketpair()
    try:
        left.sendall(b"ok")
        require(right.recv(2) == b"ok", "Unix socketpair roundtrip failed")
    finally:
        left.close()
        right.close()
    emit(f"{phase}:unix_sockets", passed=True)

    for family in (socket.AF_INET, socket.AF_INET6):
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            denied(
                f"{phase}:socket:{family.name}:{kind.name}",
                errno.EPERM,
                lambda family=family, kind=kind: create_socket(family, kind),
            )


def deadline(signum, frame):
    raise TimeoutError("synthetic test exceeded 30 seconds")


def main():
    global REPORT
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(30)
    phase = "parent"
    try:
        if len(sys.argv) == 1:
            require(not any(SCRATCH.iterdir()), "scratch is not initially empty")
            expected_filters = add_socket_filter()
        else:
            require(len(sys.argv) == 3 and sys.argv[1] == "--child", "invalid arguments")
            phase = "child"
            expected_filters = int(sys.argv[2])
            require(expected_filters >= 2, "missing inherited additional filter")

        REPORT = (SCRATCH / f"{phase}.jsonl").open("x")
        boundary_tests(phase, expected_filters)

        if phase == "parent":
            result = subprocess.run(
                [
                    sys.executable, "-I", "-S", "-B", "-u", __file__,
                    "--child", str(expected_filters),
                ],
                env=EXPECTED_ENV,
                close_fds=True,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            print(result.stdout, end="", flush=True)
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr, flush=True)
            require(result.returncode == 0, f"child exited {result.returncode}")
            lines = result.stdout.splitlines()
            require(bool(lines), "child produced no results")
            final = json.loads(lines[-1])
            require(
                final == {"gate": "complete", "phase": "child", "passed": True},
                "child did not positively confirm completion",
            )
            emit("child_inheritance", passed=True)

        emit("complete", phase=phase, passed=True)
        return 0
    except Exception as exc:
        emit("failure", phase=phase, passed=False, error=f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        signal.alarm(0)
        if REPORT is not None:
            REPORT.close()


if __name__ == "__main__":
    sys.exit(main())
