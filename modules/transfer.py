"""Yedek transferi: rsync / scp (üst katman).

Katman kuralı (spec §9.2): `run_scp` transfer hızını hesaplamak için uzak dizin boyutuna
ihtiyaç duyar (`get_dir_size_gb`, space katmanında). Üst→üst import yerine `get_dir_size_fn`
parametresiyle (dependency injection) verilir.
"""

import time

from .connection import run_command_wrapper

__all__ = ["run_rsync", "run_scp"]


def run_rsync(logger, ssh_client, source_dir, remote_dest, max_retries=3, timeout=28800):
    cmd = f"rsync -avz --progress --stats --partial {source_dir} {remote_dest}"
    logger.info(f"rsync starting: {source_dir} --> {remote_dest}")

    overall_start = time.time()
    for attempt in range(1, max_retries + 1):
        start = time.time()
        status, out, err = run_command_wrapper(ssh_client, cmd, logger, timeout=timeout)
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

    raise RuntimeError(f"rsync failed after {max_retries} attempts.")


def run_scp(logger, ssh_client, source_dir, remote_dest, max_retries=3, timeout=28800, get_dir_size_fn=None):
    cmd = f"scp -r {source_dir} {remote_dest}"
    logger.info(f"scp starting: {source_dir} --> {remote_dest}")

    overall_start = time.time()
    for attempt in range(1, max_retries + 1):
        start = time.time()
        status, out, err = run_command_wrapper(ssh_client, cmd, logger, timeout=timeout)
        total_elapsed = time.time() - overall_start

        if status == 0:
            total_bytes = get_dir_size_fn(ssh_client, source_dir) * (1024 ** 3)
            avg_speed_mbps = (total_bytes / (1024 ** 2)) / total_elapsed if total_elapsed > 0 else 0
            return total_elapsed, avg_speed_mbps, attempt, out

    raise RuntimeError(f"scp failed after {max_retries} attempts. Output: {err}")
