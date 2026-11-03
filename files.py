"""Locked, atomic updates of extension-managed configuration files."""

import fcntl
import os
import stat
import tempfile
from collections.abc import Callable


def edit_file(path: str, transform: Callable[[str], str], mode: int = 0o600) -> bool:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    # Lock a stable sidecar: locking the replaced inode does not serialize writers.
    with open(path + ".lock", "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = None
        content = ""
        if os.path.exists(path):
            previous = os.stat(path)
            with open(path, encoding="utf-8") as existing:
                content = existing.read()
        updated = transform(content)
        if previous and updated == content:
            return False
        fd, temporary = tempfile.mkstemp(prefix=".reverse_proxy-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                if previous:
                    current = os.fstat(output.fileno())
                    if (current.st_uid, current.st_gid) != (
                        previous.st_uid,
                        previous.st_gid,
                    ):
                        os.fchown(output.fileno(), previous.st_uid, previous.st_gid)
                os.fchmod(
                    output.fileno(),
                    stat.S_IMODE(previous.st_mode) if previous else mode,
                )
                output.write(updated)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return True
