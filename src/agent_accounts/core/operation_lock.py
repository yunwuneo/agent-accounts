"""跨进程平台操作锁；进程退出由操作系统释放，不用删除锁文件。"""

from contextlib import contextmanager

from agent_accounts.core import paths
from agent_accounts.core.config import ConfigError


@contextmanager
def platform_lock(platform: str):
    import os

    path = paths.ensure_dir(paths.home() / "locks") / f"{platform}.lock"
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ConfigError("同平台已有命令运行，请先结束它再切换后端") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
