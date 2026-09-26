"""Spool a request body to a temporary file, hashing it on the way (CP-ADR-0072 §2).

The body never sits in memory as a whole: each chunk is hashed and appended
to a file on disk, so memory stays flat whatever the file size. A body past
``limit`` stops the spool and removes the file.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path


class SpoolLimitExceeded(Exception):
    """The body is larger than the limit; nothing is left on disk."""


@dataclass(frozen=True)
class SpooledFile:
    path: Path
    sha256: str
    size: int

    def remove(self) -> None:
        _remove(self.path)


def _remove(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


async def spool(chunks: AsyncIterator[bytes], *, limit: int) -> SpooledFile:
    """Write ``chunks`` to a temporary file; the caller removes it when done."""
    fd, name = tempfile.mkstemp(prefix="cp-artifact-", suffix=".part")
    path = Path(name)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as handle:
            async for chunk in chunks:
                if not chunk:
                    continue
                size += len(chunk)
                if size > limit:
                    raise SpoolLimitExceeded
                digest.update(chunk)
                await asyncio.to_thread(handle.write, chunk)
    except BaseException:
        _remove(path)
        raise
    return SpooledFile(path=path, sha256=digest.hexdigest(), size=size)
