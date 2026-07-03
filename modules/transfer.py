"""Yedek transferi: rsync / scp (üst katman).

Katman kuralı (spec §9.2): `run_scp` transfer hızını hesaplamak için uzak dizin boyutuna
ihtiyaç duyar (`get_dir_size_gb`, space katmanında). Üst→üst import yerine `get_dir_size_fn`
parametresiyle (dependency injection) verilir.

Transferler UZUN komuttur: duvar-saati timeout yerine canlılık bazlı watchdog (spec §11.4).
Başarısız denemeler arasında üstel backoff + WARNING log (spec §11.1 kural 3, §11.2).
"""

import time

from .connection import run_long_command

__all__ = ["run_rsync", "run_scp"]

_BACKOFF_BASE = 5      # saniye
_BACKOFF_CAP = 300     # saniye


def _backoff_sleep(logger, attempt, max_retries, label, err):
    """Başarısız denemeyi loglar ve son deneme değilse üstel backoff uygular."""
    logger.warning(f"{label} attempt {attempt}/{max_retries} failed. "
                   f"Err: {(err or '').strip()[:200]}")
    if attempt < max_retries:
        wait = min(_BACKOFF_BASE * (2 ** (attempt - 1)), _BACKOFF_CAP)
        logger.info(f"{label}: retrying in {wait}s (backoff)...")
        time.sleep(wait)


def run_rsync(logger, ssh_client, source_dir, remote_dest, max_retries=3, watchdog=None):
    cmd = f"rsync -avz --progress --stats --partial {source_dir} {remote_dest}"
    logger.info(f"rsync starting: {source_dir} --> {remote_dest}")

    overall_start = time.time()
    last_err = ""
    for attempt in range(1, max_retries + 1):
        status, out, err = run_long_command(ssh_client, cmd, logger, watchdog=watchdog)
        total_elapsed = time.time() - overall_start

        if status == 0:
            def parse_rsync_bytes(line_str):
                parts = line_str.split(":")
                if len(parts) < 2:
                    return 0
                val = parts[1].strip().split()[0].replace(",", "")
                suffixes = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
                if val and val[-1].upper() in suffixes:
                    try:
                        return float(val[:-1]) * suffixes[val[-1].upper()]
                    except ValueError:
                        return 0
                try:
                    return float(val)
                except ValueError:
                    return 0

            total_bytes = 0
            for line in out.splitlines():
                if "Total file size" in line:
                    total_bytes = parse_rsync_bytes(line)
                    break
            avg_speed_mbps = (total_bytes / (1024 ** 2)) / total_elapsed if total_elapsed > 0 else 0
            return total_elapsed, avg_speed_mbps, attempt, out

        last_err = err
        _backoff_sleep(logger, attempt, max_retries, "rsync", err)

    raise RuntimeError(f"rsync failed after {max_retries} attempts. Last error: {last_err.strip()}")


def run_scp(logger, ssh_client, source_dir, remote_dest, max_retries=3, watchdog=None, get_dir_size_fn=None):
    cmd = f"scp -r {source_dir} {remote_dest}"
    logger.info(f"scp starting: {source_dir} --> {remote_dest}")

    overall_start = time.time()
    last_err = ""
    for attempt in range(1, max_retries + 1):
        status, out, err = run_long_command(ssh_client, cmd, logger, watchdog=watchdog)
        total_elapsed = time.time() - overall_start

        if status == 0:
            total_bytes = get_dir_size_fn(ssh_client, source_dir) * (1024 ** 3)
            avg_speed_mbps = (total_bytes / (1024 ** 2)) / total_elapsed if total_elapsed > 0 else 0
            return total_elapsed, avg_speed_mbps, attempt, out

        last_err = err
        _backoff_sleep(logger, attempt, max_retries, "scp", err)

    raise RuntimeError(f"scp failed after {max_retries} attempts. Last error: {last_err.strip()}")
