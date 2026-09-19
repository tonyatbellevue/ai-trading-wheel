"""Process-wide safety rails: no network, no sensitive logging, no stray temp files.

Three independent guarantees are implemented here:

1. ``enable_network_lockdown()`` monkey-patches the standard library socket layer
   so every outbound connect/send/DNS lookup raises :class:`NetworkBlockedError`.
   This is defence in depth: the app never calls out, and if a dependency tried,
   it would crash rather than transmit a document.
2. ``SecureTempDir`` redirects ``tempfile`` into a private directory and
   best-effort shreds every file in it on exit (also registered with ``atexit``
   so a crash still cleans up).
3. ``get_logger()`` returns a logger wired to a filter that refuses to emit any
   record carrying detected text. Only categories, counts and page numbers are
   ever logged, and nothing is written to disk unless the user opts in.
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from typing import Optional


class NetworkBlockedError(RuntimeError):
    """Raised whenever any code in this process attempts network access."""


_LOCKDOWN_ACTIVE = False


def _blocked(*_args, **_kwargs):
    raise NetworkBlockedError(
        "Network access is permanently disabled in LocalRedact. "
        "This application processes documents entirely on this machine."
    )


def enable_network_lockdown() -> None:
    """Disable every outbound network primitive in this process. Idempotent."""
    global _LOCKDOWN_ACTIVE
    if _LOCKDOWN_ACTIVE:
        return

    real_socket_cls = socket.socket

    class _GuardedSocket(real_socket_cls):  # type: ignore[misc,valid-type]
        def connect(self, *args, **kwargs):
            _blocked()

        def connect_ex(self, *args, **kwargs):
            _blocked()

        def sendto(self, *args, **kwargs):
            _blocked()

        def sendall(self, *args, **kwargs):
            _blocked()

        def send(self, *args, **kwargs):
            _blocked()

    socket.socket = _GuardedSocket  # type: ignore[assignment]
    socket.create_connection = _blocked  # type: ignore[assignment]
    socket.getaddrinfo = _blocked  # type: ignore[assignment]
    socket.gethostbyname = _blocked  # type: ignore[assignment]
    socket.gethostbyname_ex = _blocked  # type: ignore[assignment]
    if hasattr(socket, "create_server"):
        socket.create_server = _blocked  # type: ignore[assignment]

    # urllib/http/ftp import socket lazily, so the patch above already covers
    # them; patching urlopen as well makes the failure message clearer.
    try:
        import urllib.request

        urllib.request.urlopen = _blocked  # type: ignore[assignment]
    except Exception:  # pragma: no cover - urllib is always importable
        pass

    _LOCKDOWN_ACTIVE = True


def network_lockdown_active() -> bool:
    return _LOCKDOWN_ACTIVE


def _shred_file(path: Path, passes: int = 1) -> None:
    """Overwrite a file with random bytes before unlinking it.

    On SSDs with wear levelling this is not a forensic guarantee, but it removes
    the plaintext from the file the OS hands to the next allocator, which is the
    realistic threat for a desktop tool.
    """
    try:
        size = path.stat().st_size
        if size:
            with open(path, "r+b", buffering=0) as fh:
                for _ in range(passes):
                    fh.seek(0)
                    fh.write(os.urandom(size))
                    fh.flush()
                    os.fsync(fh.fileno())
    except (OSError, ValueError):
        pass
    try:
        path.unlink()
    except OSError:
        pass


def shred_tree(root: Path) -> None:
    """Shred every file under ``root`` and remove the directory."""
    if not root.exists():
        return
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            _shred_file(Path(dirpath) / name)
    shutil.rmtree(root, ignore_errors=True)


class SecureTempDir:
    """Private scratch directory that is shredded on exit.

    While active, ``tempfile.tempdir`` points here, so any third-party library
    (for example an OCR engine writing an intermediate image) lands inside the
    directory we control and shred.
    """

    def __init__(self, prefix: str = "localredact-") -> None:
        self._prefix = prefix
        self.path: Optional[Path] = None
        self._previous_tempdir: Optional[str] = None
        self._atexit_registered = False

    def __enter__(self) -> "SecureTempDir":
        self.path = Path(tempfile.mkdtemp(prefix=self._prefix))
        try:
            os.chmod(self.path, 0o700)
        except OSError:
            pass
        self._previous_tempdir = tempfile.tempdir
        tempfile.tempdir = str(self.path)
        atexit.register(self.cleanup)
        self._atexit_registered = True
        return self

    def __exit__(self, *_exc) -> None:
        self.cleanup()

    def new_file(self, suffix: str = "") -> Path:
        assert self.path is not None, "SecureTempDir used outside its context"
        fd, name = tempfile.mkstemp(dir=str(self.path), suffix=suffix)
        os.close(fd)
        return Path(name)

    def cleanup(self) -> None:
        if self.path is None:
            return
        tempfile.tempdir = self._previous_tempdir
        shred_tree(self.path)
        self.path = None
        if self._atexit_registered:
            try:
                atexit.unregister(self.cleanup)
            except Exception:
                pass
            self._atexit_registered = False


class _NoSensitiveDataFilter(logging.Filter):
    """Drop any log record explicitly marked as carrying document content."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "sensitive", False):
            return False
        return True


_LOGGER_CONFIGURED = False


def get_logger(name: str = "localredact") -> logging.Logger:
    """Return the app logger. Console only, no file handler, no document text."""
    global _LOGGER_CONFIGURED
    logger = logging.getLogger(name)
    if not _LOGGER_CONFIGURED:
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        handler.addFilter(_NoSensitiveDataFilter())
        logger.addHandler(handler)
        logger.propagate = False
        _LOGGER_CONFIGURED = True
    return logger


def mask(text: str, keep: int = 2) -> str:
    """Return a partially masked rendering of ``text`` for status messages.

    Used for anything that could end up outside the in-memory review table.
    """
    if not text:
        return ""
    stripped = text.strip()
    if len(stripped) <= keep:
        return "*" * len(stripped)
    if len(stripped) <= keep * 2:
        return stripped[0] + "*" * (len(stripped) - 1)
    return f"{stripped[:keep]}{'*' * (len(stripped) - keep * 2)}{stripped[-keep:]}"
