"""Process kilidi: pid_file (instance) + fcntl host lock (orta katman).

- `acquire_lock`/`release_lock`: instance bazlı pid_file kilidi (cron üst üste binme koruması).
- `acquire_host_lock`/`release_host_lock`: aynı TARGET_SERVER.host'a yedek alan instance'ları
  serileştirir (spec §8.2). fcntl.flock tabanlı — process ölürse kernel kilidi otomatik bırakır
  (stale-PID problemi yok).
"""

import fcntl
import os
import time

from .config import sanitize_instance_id

__all__ = ["acquire_lock", "release_lock", "acquire_host_lock", "release_host_lock"]


def acquire_lock(pid_file, retries=3, wait=30):
    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                old_pid = int(f.read().strip())

            for attempt in range(1, retries + 1):
                if os.path.exists(f"/proc/{old_pid}"):
                    print(f"[INFO] Another process (PID {old_pid}) is running. Waiting {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"[INFO] Stale PID file found. Removing lock.")
                    os.remove(pid_file)
                    break
            else:
                return False, old_pid
        except (ValueError, OSError):
            try:
                os.remove(pid_file)
            except OSError:
                pass

    try:
        with open(pid_file, "w") as f:
            f.write(str(os.getpid()))
        return True, os.getpid()
    except OSError as e:
        print(f"[WARNING] Could not write PID file: {e}")
        return True, os.getpid()


def release_lock(pid_file):
    try:
        if os.path.exists(pid_file):
            os.remove(pid_file)
    except OSError:
        pass


def acquire_host_lock(temp_dir, host, logger, timeout_min=120, poll_sec=30):
    """Aynı host'a yedek alan instance'ları serileştirir (spec §8.2).

    Kilit dosyası: {temp_dir}/rman_hostlock_{sanitize(host)}.lock
    Doluysa poll ederek bekler; timeout_min aşılırsa None döner (çağıran FAILED ile çıkar).
    Başarılıysa açık dosya handle döner — kilit bu handle açık kaldığı sürece tutulur.
    """
    key = sanitize_instance_id(host or "local")
    lock_path = os.path.join(temp_dir, f"rman_hostlock_{key}.lock")
    lf = open(lock_path, "w")
    deadline = time.time() + timeout_min * 60
    waited = False
    acquired = False
    while not acquired:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (BlockingIOError, OSError):
            if time.time() >= deadline:
                lf.close()
                logger.error(f"Host lock timeout ({timeout_min}m) for host '{host}'. "
                             f"Another instance holds the lock. Exiting FAILED.")
                return None
            if not waited:
                logger.info(f"Waiting for host lock on '{host}' "
                            f"(another instance is backing up the same host)...")
                waited = True
            time.sleep(poll_sec)
    if waited:
        logger.info(f"Host lock acquired for host '{host}'.")
    return lf


def release_host_lock(lock_handle):
    if lock_handle:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
            lock_handle.close()
        except OSError:
            pass
