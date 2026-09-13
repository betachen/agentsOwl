from __future__ import annotations

import fcntl
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agents_owl.runtime_redraw import attach_with_redraw, refresh_attached_screen


# Model a cached TUI detail view: Ctrl+L is ignored; only a real size change
# repaints. Report the input buffer on repaint to detect accidental keystrokes.
CACHED_TUI = """
import os, select, signal, tty
tty.setraw(0)
signal.signal(signal.SIGWINCH, lambda *args: None)
previous = None
pending = b''
while True:
    size = os.get_terminal_size(0)
    if size != previous:
        previous = size
        os.write(1, f'FRAME {size.lines} {size.columns} {pending.hex()}\\n'.encode())
    if select.select([0], [], [], 0.01)[0]:
        data = os.read(0, 1024)
        if not data:
            break
        pending += data.replace(b'\\x0c', b'')
"""


class RedrawTests(unittest.TestCase):
    def test_refresh_failure_keeps_client_attached(self) -> None:
        client = Mock()
        client.wait.return_value = 0
        with patch("agents_owl.runtime_redraw.subprocess.Popen") as spawn, patch(
            "agents_owl.runtime_redraw.refresh_attached_screen", side_effect=OSError("unavailable")
        ):
            spawn.return_value.__enter__.return_value = client
            attach_with_redraw(["dtach", "-a", "/tmp/selected.sock"], "/tmp/selected.sock")
        client.wait.assert_called_once()
        client.terminate.assert_not_called()

    def test_finished_client_does_not_resize_runtime(self) -> None:
        client = Mock()
        client.poll.return_value = 0
        with patch("agents_owl.runtime_redraw.time.sleep"), patch(
            "agents_owl.runtime_redraw.socket.socket"
        ) as socket_factory:
            refresh_attached_screen(client, "/tmp/selected.sock")
        socket_factory.assert_not_called()

    def test_restores_latest_size_even_when_pulse_is_interrupted(self) -> None:
        original = struct.pack("@4H", 28, 127, 0, 0)
        resized = struct.pack("@4H", 35, 100, 0, 0)
        control = Mock()
        client = Mock()
        client.poll.return_value = None
        with patch("agents_owl.runtime_redraw.termios.tcgetattr", return_value=[0] * 7), patch(
            "agents_owl.runtime_redraw._window_size", side_effect=[original, resized]
        ), patch("agents_owl.runtime_redraw.socket.socket") as socket_factory, patch(
            "agents_owl.runtime_redraw.time.sleep", side_effect=[None, KeyboardInterrupt]
        ):
            socket_factory.return_value.__enter__.return_value = control
            with self.assertRaises(KeyboardInterrupt):
                refresh_attached_screen(client, "/tmp/selected.sock")
        control.connect.assert_called_once_with("/tmp/selected.sock")
        packets = [call.args[0] for call in control.sendall.call_args_list]
        self.assertEqual(packets, [bytes((3, 0)) + struct.pack("@4H", 27, 127, 0, 0), bytes((3, 0)) + resized])

    @unittest.skipUnless(shutil.which("dtach"), "dtach is required for PTY integration")
    def test_reconnect_repaints_cached_view_and_preserves_input_and_runtime(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owl-redraw-") as directory:
            socket_path = str(Path(directory) / "runtime.sock")
            server = subprocess.Popen(
                ["dtach", "-N", socket_path, sys.executable, "-u", "-c", CACHED_TUI],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 3
                while not Path(socket_path).exists():
                    self.assertIsNone(server.poll(), "dtach daemon exited during startup")
                    self.assertLess(time.monotonic(), deadline, "runtime did not start")
                    time.sleep(0.01)
                old = ["dtach", "-a", socket_path, "-z", "-r", "ctrl_l"]
                self.run_client(old, expect_frame=True, draft=b"draft")
                # This is the reported failure: reattach succeeds but produces
                # only terminal escape codes, since Ctrl+L does not repaint.
                self.run_client(old, expect_frame=False)
                fixed = [
                    sys.executable, "-c",
                    "from agents_owl.session_manager import attach_runtime; "
                    "import sys; attach_runtime(sys.argv[1])",
                    socket_path,
                ]
                for disconnect in (False, True, False):
                    self.run_client(
                        fixed, expect_frame=True, expected_draft=b"draft", disconnect=disconnect,
                    )
                    self.assertIsNone(server.poll(), "detach terminated the runtime")
            finally:
                server.terminate()
                server.wait(timeout=3)

    def run_client(
        self, command: list[str], *, expect_frame: bool,
        draft: bytes = b"", expected_draft: bytes = b"", disconnect: bool = False,
    ) -> None:
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("@4H", 28, 127, 0, 0))
        original_term = termios.tcgetattr(slave)
        client = subprocess.Popen(
            command, stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        )
        try:
            output = b""
            expected = b"FRAME 28 127 " + expected_draft.hex().encode()
            deadline = time.monotonic() + (3 if expect_frame else 0.5)
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    output += os.read(master, 65536)
                if expect_frame and expected in output:
                    break
            if expect_frame:
                self.assertIn(expected, output)
            else:
                self.assertNotIn(b"FRAME", output)
            if draft:
                os.write(master, draft)
                time.sleep(0.1)
            if disconnect:
                # SSH terminal loss sends SIGHUP to the foreground process group.
                os.killpg(client.pid, signal.SIGHUP)
                self.assertEqual(client.wait(timeout=3), -signal.SIGHUP)
            else:
                os.write(master, b"\x1c")  # Ctrl+\ detaches only this client.
                self.assertEqual(client.wait(timeout=3), 0)
                self.assertEqual(termios.tcgetattr(slave), original_term)
        finally:
            if client.poll() is None:
                client.terminate()
                client.wait(timeout=3)
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
