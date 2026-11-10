from __future__ import annotations

import errno
import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO


class NotRegularFileError(OSError):
    pass


def _replace(
    path: Path,
    write: Callable[[BinaryIO], object],
    mode: int,
    owner: tuple[int, int] | None,
    keep_mode: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        existing = None
    if existing is not None and stat.S_ISREG(existing.st_mode):
        if keep_mode:
            mode = existing.st_mode & 0o777
        if owner is None:
            owner = (existing.st_uid, existing.st_gid)
    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode
    )
    with os.fdopen(descriptor, "wb") as handle:
        write(handle)
        if owner is not None and os.geteuid() == 0:
            os.fchown(handle.fileno(), *owner)
    os.replace(temporary, path)


def write_atomic(
    path: Path,
    data: str | bytes,
    mode: int = 0o600,
    owner: tuple[int, int] | None = None,
    keep_mode: bool = True,
) -> None:
    content = data.encode() if isinstance(data, str) else data
    _replace(path, lambda handle: handle.write(content), mode, owner, keep_mode)


def copy_regular_file(
    source: Path, destination: Path, owner: tuple[int, int] | None = None
) -> bool:
    try:
        descriptor = os.open(
            source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
    except FileNotFoundError:
        return False
    except OSError as exc:
        if exc.errno != errno.ELOOP:
            raise
        raise NotRegularFileError(f"{source} isn't a regular file") from exc
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise NotRegularFileError(
                f"{source} isn't a regular file or has other hard links"
            )
        _replace(
            destination,
            lambda target: shutil.copyfileobj(handle, target),
            0o600,
            owner,
        )
    return True
