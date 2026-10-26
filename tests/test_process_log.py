"""Real child processes exercise buffering, native writes, timeout and incremental IO."""

import importlib.util
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "process_log_under_test", Path(__file__).resolve().parents[1] / "utils/process_log.py")
process_log = importlib.util.module_from_spec(spec)
spec.loader.exec_module(process_log)


class ProcessLogTests(unittest.TestCase):
    def test_split_unicode_budget_no_replay_and_unterminated_tail(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "worker.log"
            relay = process_log.FileLogRelay(path)
            self.assertEqual(relay.read(), "")
            data = "前处理🎉\n最后一行".encode("utf-8")
            chunks = []
            for byte in data:
                with path.open("ab") as output:
                    output.write(bytes([byte]))
                chunks.append(relay.read(budget=1))
                self.assertEqual(relay.read(), "")
            chunks.append(relay.read(final=True))
            self.assertEqual("".join(chunks), data.decode("utf-8"))
            self.assertEqual(relay.read(final=True), "")

    def test_final_drains_more_than_poll_budget_and_flushes_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "worker.log"
            path.write_bytes(b"x" * 300000 + b"\xe4")
            relay = process_log.FileLogRelay(path)
            first = relay.read()
            self.assertEqual(len(first), 256 * 1024)
            self.assertEqual(first + relay.read(final=True), "x" * 300000 + "\ufffd")

    def test_truncated_legacy_log_resets_reader(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "worker.log"
            path.write_bytes(b"old content")
            relay = process_log.FileLogRelay(path)
            self.assertEqual(relay.read(), "old content")
            path.write_bytes(b"new")
            self.assertEqual(relay.read(), "new")

    def test_child_forwards_stdout_stderr_native_bytes_before_exit(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "worker.log")
            marker = Path(root) / "finished"
            # PYTHONUNBUFFERED must make even print(end='') observable before exit.
            code = ("import os,sys,time;from pathlib import Path;"
                    "print('前处理开始',end='');"
                    "time.sleep(.5);"
                    "os.write(1,'原生引擎输出\\n'.encode('utf-8'));"
                    "sys.stderr.write('Traceback: 报错\\n');"
                    f"Path({str(marker)!r}).write_text('done');"
                    "sys.exit(3)")
            seen = []
            arrived_before_exit = []

            def emit(text):
                seen.append(text)
                if "前处理开始" in text:
                    arrived_before_exit.append(not marker.exists())

            result = process_log.run_logged_process([sys.executable, "-c", code], path, 5, emit)
            self.assertEqual(result.returncode, 3)
            self.assertEqual(arrived_before_exit, [True])
            self.assertEqual("".join(seen), Path(path).read_bytes().decode("utf-8"))
            self.assertIn("原生引擎输出", "".join(seen))
            self.assertIn("Traceback: 报错", "".join(seen))

    def test_timeout_reaps_child_and_keeps_last_output(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "worker.log")
            seen = []
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                process_log.run_logged_process(
                    [sys.executable, "-c", "import time;print('before timeout');time.sleep(30)"],
                    path, .5, seen.append)
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual("".join(seen), Path(path).read_bytes().decode("utf-8"))
            self.assertIn("before timeout", "".join(seen))

    def test_display_failure_does_not_kill_export(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "worker.log")

            def broken_console(_text):
                raise OSError("console closed")

            result = process_log.run_logged_process(
                [sys.executable, "-c", "print('retained')"], path, 5, broken_console)
            self.assertEqual(result.returncode, 0)
            self.assertIn("retained", Path(path).read_bytes().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
