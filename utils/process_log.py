"""Stream subprocess output through files, without blocking on a full pipe."""

import codecs
import os
import subprocess
import sys
import threading
import time


REDIRECTED_LOG_ENV = "HERTA_WORKER_LOG_REDIRECTED"
_EMIT_LOCK = threading.Lock()


def configure_worker_output():
    # Embedded Python hosts may ignore PYTHONUNBUFFERED; configure their streams too.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True, write_through=True)
        except (AttributeError, OSError, ValueError):
            pass


def worker_log_environment():
    environment = os.environ.copy()
    environment.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8",
                       HERTA_WORKER_LOG_REDIRECTED="1")
    return environment


class FileLogRelay:
    """Read only appended bytes; preserve UTF-8 split across polling boundaries."""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def read(self, *, final=False, budget=256 * 1024):
        chunks = []
        try:
            with open(self.path, "rb") as stream:
                if os.fstat(stream.fileno()).st_size < self.offset:
                    self.offset = 0
                    self.decoder.reset()
                stream.seek(self.offset)
                remaining = budget
                while final or remaining > 0:
                    data = stream.read(64 * 1024 if final else min(64 * 1024, remaining))
                    if not data:
                        break
                    self.offset += len(data)
                    remaining -= len(data)
                    chunks.append(self.decoder.decode(data))
        except OSError:
            return ""
        if final:
            chunks.append(self.decoder.decode(b"", final=True))
            self.decoder.reset()
        return "".join(chunks)


def run_logged_process(command, log_path, timeout, emit):
    """Keep a complete job log and relay it while the child is still running.

    Called by parallel job threads: no Blender API calls belong in ``emit``.
    The timeout kills and reaps the child before the final log drain.
    """
    relay = FileLogRelay(log_path)

    def forward(final=False):
        text = relay.read(final=final)
        if text:
            with _EMIT_LOCK:
                try:
                    emit(text)
                except Exception:
                    # A detached/unavailable foreground console must not kill a job.
                    # Its complete output remains in log_path.
                    pass

    with open(log_path, "wb") as output:
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, env=worker_log_environment())
        started = time.monotonic()
        try:
            while process.poll() is None:
                forward()
                if time.monotonic() - started >= timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
                time.sleep(0.1)
            return subprocess.CompletedProcess(command, process.returncode)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            output.flush()
            forward(final=True)
