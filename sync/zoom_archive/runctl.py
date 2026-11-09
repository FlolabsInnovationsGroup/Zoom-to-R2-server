"""Start / stop / status of a sync run, shared by the CLI and the admin page (D15).

Three files next to the manifest:
  sync.alive  JSON {pid, mode, started_at}; the running CLI rewrites it at least every HEARTBEAT seconds
  sync.stop   exists = the running CLI stops: queued files are cancelled, in-flight downloads aborted (left pending)
  sync.log    output of runs started from the admin page
A run counts as running while sync.alive is younger than STALE seconds, so a crashed run never blocks the next.
"""
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

HEARTBEAT = 5
STALE = 30
MODES = ("backfill", "scan")  # what the admin page may start


def _p(manifest_path: str, name: str) -> Path:
    return Path(manifest_path).with_name(name)


def running(manifest_path: str) -> dict | None:
    """{pid, mode, started_at, stopping} of the live run, or None."""
    f = _p(manifest_path, "sync.alive")
    for attempt in range(5):  # on Windows the file is briefly unreadable while heartbeat() replaces it
        try:
            if time.time() - f.stat().st_mtime > STALE:
                return None
            info = json.loads(f.read_text(encoding="utf-8"))
            return {**info, "stopping": stop_requested(manifest_path)}
        except FileNotFoundError:
            if attempt == 4:
                return None
        except (OSError, ValueError):
            if attempt == 4:
                return None
        time.sleep(0.05)
    return None


def heartbeat(manifest_path: str, info: dict) -> None:
    f = _p(manifest_path, "sync.alive")
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(info), encoding="utf-8")
    os.replace(tmp, f)


@contextmanager
def _lock(manifest_path: str):
    """Short exclusive lock (O_EXCL file) around "is a run going? / become the run", so two starters cannot both pass."""
    f = _p(manifest_path, "sync.lock")
    f.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        try:
            os.close(os.open(f, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                if time.time() - f.stat().st_mtime > 10:  # left behind by a crashed starter
                    f.unlink(missing_ok=True)
            except OSError:
                pass
            time.sleep(0.1)
    else:
        raise OSError("sync.lock is busy")
    try:
        yield
    finally:
        f.unlink(missing_ok=True)


def claim(manifest_path: str, info: dict) -> bool:
    """The CLI becomes THE run: False if another process is running. A run that start() already registered
    for this pid counts as ours. A stop request left over from an earlier run is dropped."""
    with _lock(manifest_path):
        run = running(manifest_path)
        mine = run and (run.get("pid") == os.getpid() or
                        (run.get("token") and run.get("token") == os.environ.get("ZOOM_ARCHIVE_RUN_TOKEN")))
        if run and not mine:
            return False
        _p(manifest_path, "sync.stop").unlink(missing_ok=True)  # a stop request is for an earlier run
        heartbeat(manifest_path, info)
        return True


def finished(manifest_path: str) -> None:
    """Remove this run's files; another run's sync.alive (other pid) is left alone."""
    f = _p(manifest_path, "sync.alive")
    try:
        if json.loads(f.read_text(encoding="utf-8")).get("pid") != os.getpid():
            return
    except (OSError, ValueError):
        pass
    for name in ("sync.alive", "sync.stop"):
        _p(manifest_path, name).unlink(missing_ok=True)


def stop_requested(manifest_path: str) -> bool:
    return _p(manifest_path, "sync.stop").exists()


def request_stop(manifest_path: str) -> None:
    if running(manifest_path):
        _p(manifest_path, "sync.stop").write_text(str(time.time()), encoding="utf-8")


def default_concurrency() -> int:
    """Parallel files for a run started from the admin page: SYNC_CONCURRENCY (1-8), else 6."""
    try:
        return max(1, min(8, int(os.environ.get("SYNC_CONCURRENCY") or 6)))
    except ValueError:
        return 6


def start(manifest_path: str, mode: str, concurrency: int | None = None) -> bool:
    """Start `python -m zoom_archive --mode <mode>` in the background, output appended to sync.log.
    False if a run is already going. The child gets this process's environment (Zoom + R2 settings)."""
    if mode not in MODES:
        return False
    concurrency = concurrency or default_concurrency()
    with _lock(manifest_path):
        return _start_locked(manifest_path, mode, concurrency)


def _start_locked(manifest_path: str, mode: str, concurrency: int) -> bool:
    if running(manifest_path):
        return False
    _p(manifest_path, "sync.stop").unlink(missing_ok=True)
    log = _p(manifest_path, "sync.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, "-m", "zoom_archive", "--mode", mode]
    if mode == "backfill":
        args += ["--concurrency", str(concurrency)]
    token = os.urandom(8).hex()  # the child proves it is THIS run (on Windows a venv python.exe re-spawns, so pids differ)
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)  # Windows: Ctrl-C in the web console stays there
    with open(log, "ab") as out:
        out.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())} start {mode} =====\n".encode())
        out.flush()
        child = subprocess.Popen(args, cwd=str(Path(__file__).resolve().parents[1]), stdout=out, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, creationflags=flags, env={**os.environ, "PYTHONIOENCODING": "utf-8", "ZOOM_ARCHIVE_RUN_TOKEN": token})
    # Register the child at once (still under the lock), so a second Start sees a run before the child has booted.
    heartbeat(manifest_path, {"pid": child.pid, "token": token, "mode": mode, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    # Wait briefly for the child's first heartbeat so the page shows "running" right away.
    for _ in range(20):
        if running(manifest_path):
            break
        time.sleep(0.25)
    return True


def log_tail(manifest_path: str, lines: int = 15) -> list[str]:
    f = _p(manifest_path, "sync.log")
    try:
        with open(f, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 16384))
            return fh.read().decode("utf-8", "replace").splitlines()[-lines:]
    except OSError:
        return []
