"""Process kilidi (pid_file bazlı) (orta katman)."""

import os
import time

__all__ = ["acquire_lock", "release_lock"]


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
