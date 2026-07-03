"""Disk alanı yönetimi (üst katman).

Katman kuralı (spec §9.2): üst katman modülleri birbirini import etmez. `ensure_free_space`
RMAN katalog temizliği için `run_rman`'e ihtiyaç duyar; bu, üst→üst import yerine
`run_rman_fn` parametresiyle (dependency injection) verilir — koordinasyonu backup.py yapar.
"""

import os
import json
from datetime import datetime, timedelta

from .connection import run_command_wrapper
from .history import get_history_file, mark_history_deleted

__all__ = [
    "get_free_gb",
    "get_dir_size_gb",
    "list_daily_dirs",
    "get_required_gb",
    "ensure_free_space",
]


def get_free_gb(ssh_client, path, logger=None):
    status, out, err = run_command_wrapper(ssh_client, f"df -k {path} | awk 'NR==2 {{print $4}}'", None, quiet=True)
    try:
        kb = int(out.strip())
        return kb / (1024 ** 2)
    except Exception as e:
        if logger:
            logger.warning(f"Could not determine free space for '{path}': {e}. Returning 0 GB.")
        return 0


def get_dir_size_gb(ssh_client, path, logger=None):
    status, out, err = run_command_wrapper(ssh_client, f"du -sk {path} | awk '{{print $1}}'", None, quiet=True)
    try:
        kb = int(out.strip())
        return kb / (1024 ** 2)
    except Exception as e:
        if logger:
            logger.warning(f"Could not determine dir size for '{path}': {e}. Returning 0 GB.")
        return 0


def list_daily_dirs(ssh_client, backup_root, oracle_sid):
    cmd = f"find {backup_root} -mindepth 1 -maxdepth 3 -type d -not -path '*/logs*' -printf '%p|%C@\\n'"
    status, out, err = run_command_wrapper(ssh_client, cmd, None, quiet=True)
    dirs = []
    for line in out.splitlines():
        if "|" in line:
            parts = line.split("|")
            dir_path = parts[0].strip()

            # Determine relative path depth
            try:
                rel_path = os.path.relpath(dir_path, backup_root)
                path_parts = rel_path.replace("\\", "/").split("/")

                is_valid = False
                # Old format matches: '27JUN2026' (depth 1)
                if len(path_parts) == 1 and "202" in path_parts[0]:
                    is_valid = True
                # Intermediate format matches: 'JUN/27/6010832131274' (depth 3, not starting with SID)
                elif len(path_parts) == 3 and path_parts[0] != oracle_sid:
                    is_valid = True
                # New format matches: 'ORCL/JUL/300626' (depth 3, starting with SID)
                elif len(path_parts) == 3 and path_parts[0] == oracle_sid:
                    is_valid = True

                if is_valid:
                    dirs.append((dir_path, float(parts[1])))
            except Exception:
                pass
    dirs.sort(key=lambda x: x[1])
    return [d[0] for d in dirs]


def get_required_gb(logger, backup_config, history_dir):
    # history_dir artık config'ten değil, resolved path'ten parametreyle gelir (spec §3.1) —
    # namespacing'de config'te boş olabildiği için buradan okumak None -> TypeError riskiydi.
    fallback_gb = backup_config["fallback_size_gb"]
    buffer_pct  = backup_config["space_buffer_pct"]

    files_to_check = [
        get_history_file(history_dir),
        get_history_file(history_dir, datetime.now() - timedelta(days=31))
    ]

    for h_file in files_to_check:
        if os.path.exists(h_file):
            try:
                with open(h_file, "r") as f:
                    data = json.load(f)
                for record in reversed(data):
                    if record.get("operation") == "Backup" and record.get("status") == "SUCCESS":
                        size = float(record.get("size_gb", 0))
                        if size > 1.0:
                            logger.info(f"Using required size from history ({h_file}): {size:.1f} GB")
                            return size * (1 + buffer_pct)
            except Exception as e:
                logger.warning(f"Could not read history file {h_file}: {e}")
                continue

    logger.info(f"No valid history found. Using fallback size: {fallback_gb:.1f} GB")
    return fallback_gb * (1 + buffer_pct)


def ensure_free_space(logger, ssh_client, env, backup_config, oracle_sid, history_dir, db_creds=None, run_rman_fn=None):
    backup_root = backup_config["backup_root"]
    required_gb = get_required_gb(logger, backup_config, history_dir)
    free_gb     = get_free_gb(ssh_client, backup_root, logger)

    logger.info(f"Free disk space on target : {free_gb:.1f} GB  |  Required : {required_gb:.1f} GB")

    if free_gb >= required_gb:
        return True, free_gb, required_gb

    logger.warning("Insufficient disk space! Removing oldest backup dirs from target...")

    # Run RMAN catalog cleanup once before removing directories
    rman_clean = "CROSSCHECK BACKUP; DELETE NOPROMPT EXPIRED BACKUP; DELETE NOPROMPT OBSOLETE; QUIT;"
    try:
        run_rman_fn(logger, env, ssh_client, rman_clean, label="cleanup", db_creds=db_creds)
    except RuntimeError:
        logger.warning("RMAN catalog cleanup failed during space reclamation. Continuing with directory removal.")

    daily_dirs = list_daily_dirs(ssh_client, backup_root, oracle_sid)
    for old_dir in daily_dirs:
        if free_gb >= required_gb:
            break
        run_command_wrapper(ssh_client, f"rm -rf {old_dir}", logger)
        logger.info(f"Removed directory for space: {old_dir}")
        mark_history_deleted(history_dir, old_dir)
        free_gb = get_free_gb(ssh_client, backup_root, logger)

    # Crosscheck again after physical removal to sync RMAN catalog
    try:
        run_rman_fn(logger, env, ssh_client, "CROSSCHECK BACKUP; CROSSCHECK ARCHIVELOG ALL; QUIT;", label="post-cleanup-crosscheck", db_creds=db_creds)
    except RuntimeError:
        logger.warning("Post-cleanup crosscheck failed.")

    if free_gb < required_gb:
        logger.error("Could not free enough space. Backup aborted.")
        return False, free_gb, required_gb

    return True, free_gb, required_gb
