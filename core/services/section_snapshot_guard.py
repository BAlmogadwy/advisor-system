from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from django.conf import settings

_LOCK_PATH = Path(settings.BASE_DIR) / "runtime" / "section_snapshot_operation.lock"
_PROCESS_LOCK = threading.Lock()


def _lock_file(handle: BinaryIO, *, blocking: bool) -> bool:
    """Acquire one cross-process byte lock without leaving stale lock state."""
    handle.seek(0, 2)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)

    try:
        if sys.platform == "win32":
            import msvcrt

            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            msvcrt.locking(handle.fileno(), mode, 1)
        else:  # pragma: no cover - exercised on the deployment platform
            import fcntl

            mode = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), mode)
    except OSError:
        return False
    return True


def _unlock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - exercised on the deployment platform
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def section_snapshot_operation_guard(*, blocking: bool = False) -> Iterator[bool]:
    """Serialize scraper startup and current-section snapshot mutations.

    The OS releases the byte lock automatically if a process exits, so a crash
    cannot leave a stale maintenance marker that an administrator must remove.
    The in-process lock covers threads; the byte lock covers scraper, import,
    and snapshot-clear processes.
    """
    if not _PROCESS_LOCK.acquire(blocking=blocking):
        yield False
        return

    handle: BinaryIO | None = None
    file_locked = False
    try:
        _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        handle = _LOCK_PATH.open("a+b")
        file_locked = _lock_file(handle, blocking=blocking)
        if not file_locked:
            yield False
            return
        yield True
    finally:
        if handle is not None:
            if file_locked:
                _unlock_file(handle)
            handle.close()
        _PROCESS_LOCK.release()
