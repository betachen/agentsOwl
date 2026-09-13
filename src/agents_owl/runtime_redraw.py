"""Refresh cached TUI screens after a dtach client attaches."""

from __future__ import annotations

import fcntl
import socket
import struct
import subprocess
import termios
import time


def _window_size() -> bytes:
    return fcntl.ioctl(0, termios.TIOCGWINSZ, bytes(8))


def _send_size(control: socket.socket, size: bytes) -> None:
    # dtach 0.9 protocol: MSG_WINCH, unused length, native struct winsize.
    # https://github.com/crigler/dtach/blob/master/dtach.h
    control.sendall(bytes((3, 0)) + size)


def refresh_attached_screen(client: subprocess.Popen, runtime_socket: str) -> None:
    """Pulse the runtime size after attachment without sending keyboard input.

    Some TUI views ignore Ctrl+L and ignore SIGWINCH when dimensions are
    unchanged. Give their renderer time to observe a different size before
    restoring the current terminal dimensions. The control connection never
    subscribes to output or forwards input.
    """

    deadline = time.monotonic() + 1.0
    while client.poll() is None:
        if not termios.tcgetattr(0)[3] & termios.ICANON:
            break
        if time.monotonic() >= deadline:
            return
        time.sleep(0.01)
    # dtach sets raw mode immediately before registering the attached client.
    time.sleep(0.1)
    if client.poll() is not None:
        return
    original = _window_size()
    rows, columns, xpixels, ypixels = struct.unpack("@4H", original)
    if rows < 2 or columns < 2:
        return
    temporary = struct.pack("@4H", rows - 1, columns, xpixels, ypixels)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
        control.settimeout(0.2)
        control.connect(runtime_socket)
        try:
            _send_size(control, temporary)
            time.sleep(0.15)
        finally:
            # A real resize during the pulse takes precedence over the old size.
            try:
                restored = _window_size()
            except OSError:
                restored = original
            _send_size(control, restored)


def attach_with_redraw(argv: list[str], runtime_socket: str) -> None:
    """Keep dtach responsible for input, detach keys and terminal restoration."""

    with subprocess.Popen(argv) as client:
        try:
            try:
                refresh_attached_screen(client, runtime_socket)
            except (OSError, termios.error):
                # Redraw is best-effort; a failed refresh must not end a session.
                pass
            code = client.wait()
        except BaseException:
            # This is only the attaching client, never the runtime/agent daemon.
            if client.poll() is None:
                client.terminate()
            raise
    if code:
        raise SystemExit(code if code > 0 else 128 - code)
