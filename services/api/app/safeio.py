"""Parent-side access to untrusted parser artifacts.

Walk directories without following links, open only singly linked regular files,
and publish through a new file in the destination directory. Checking a pathname
and reopening it later is deliberately avoided.
"""
from contextlib import contextmanager, ExitStack
import errno
import json
import os
from pathlib import Path
import secrets
import shutil
import stat

from starlette.responses import FileResponse, JSONResponse, Response

JSON_LIMIT = 64 * 1024 * 1024
ARTIFACT_LIMIT = 512 * 1024 * 1024


@contextmanager
def parent_fd(path: Path):
    path = path.absolute()
    if ".." in path.parts or path.name in ("", ".", ".."):
        raise ValueError("invalid artifact path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, path.name
    finally:
        os.close(fd)


@contextmanager
def open_regular(path: Path, *, append: bool = False):
    with parent_fd(path) as (directory, name):
        flags = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        flags |= (os.O_WRONLY | os.O_APPEND | os.O_CREAT) if append else os.O_RDONLY
        fd = os.open(name, flags, 0o600, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError(errno.EINVAL, "artifact is not a singly linked regular file")
        with os.fdopen(fd, "ab" if append else "rb") as stream:
            fd = -1
            yield stream
    finally:
        if fd >= 0:
            os.close(fd)


@contextmanager
def atomic_file(path: Path):
    with parent_fd(path) as (directory, name):
        temporary = f".{name}.{secrets.token_hex(12)}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                yield stream
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def read_bytes(path: Path, limit: int = ARTIFACT_LIMIT) -> bytes:
    with open_regular(path) as stream:
        if os.fstat(stream.fileno()).st_size > limit:
            raise OSError(errno.EFBIG, "artifact exceeds its size limit")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise OSError(errno.EFBIG, "artifact exceeds its size limit")
        return data


def read_json(path: Path, limit: int = JSON_LIMIT):
    return json.loads(read_bytes(path, limit))


def write_json(path: Path, value) -> None:
    with atomic_file(path) as stream:
        stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def copy_file(source: Path, destination: Path, limit: int = ARTIFACT_LIMIT) -> None:
    with open_regular(source) as src, atomic_file(destination) as dst:
        size = 0
        while chunk := src.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise OSError(errno.EFBIG, "artifact exceeds its size limit")
            dst.write(chunk)


def remove(path: Path) -> None:
    """Unlink a link itself; rmtree's fd implementation never follows replaced directories."""
    with parent_fd(path) as (directory, name):
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(name, dir_fd=directory)
            else:
                os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass


class ArtifactResponse(Response):
    """Keep a validated descriptor open throughout range/HEAD/streaming responses."""

    def __init__(self, path: Path, media_type: str):
        super().__init__(media_type=media_type)
        self.path = path

    async def __call__(self, scope, receive, send):
        with ExitStack() as stack:
            try:
                stream = stack.enter_context(open_regular(self.path))
            except OSError:
                await JSONResponse({"detail": {"code": "not_found", "message": "File unavailable."}},
                                   status_code=404)(scope, receive, send)
                return
            # FileResponse reopens this held descriptor, never the original untrusted path.
            response = FileResponse(f"/proc/self/fd/{stream.fileno()}", media_type=self.media_type,
                                    stat_result=os.fstat(stream.fileno()),
                                    headers={"Cache-Control": "private, no-store"})
            # A deferred pathsend could outlive the descriptor; stream while it is held instead.
            scope = {**scope, "extensions": {k: v for k, v in scope.get("extensions", {}).items()
                                              if k != "http.response.pathsend"}}
            await response(scope, receive, send)
