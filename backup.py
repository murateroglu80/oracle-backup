#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Oracle RMAN Backup Script - v6.1.0 (Hybrid Jump/Local Server Edition)
Advanced Refactoring:
  1. Persistent Backup History with Monthly Rotation (JSON database).
  2. Centralized Jump Server Execution (Paramiko SSH) or Local Execution (Subprocess).
  3. Dynamic Log Generation & Safe SFTP Transfer.
  4. RMAN Exit Code visibility (Fail-Fast mechanism) + Regex ORA-/RMAN- scans.
  5. Custom `.rman` script parsing capability.
  6. Device Type (SBT/DISK) and Parallelism Configuration.
  7. Auto-creation of configurable log/history directories (Defaults to ~/huaris/).
  8. HashiCorp Vault Integration for SMTP Password.
"""

import os
import sys
import argparse
import subprocess
import shutil
import time
import logging
import smtplib
import json
import yaml
import requests
import re
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header

import hvac
import paramiko


def load_config(config_path="config.yaml"):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    full_path = os.path.join(script_dir, config_path)
    if not os.path.exists(full_path):
        full_path = config_path
        
    if not os.path.exists(full_path):
        print(f"[ERROR] Configuration file '{full_path}' not found!")
        sys.exit(1)
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
            
        vault_config_path = os.path.join(script_dir, "vault_config.yaml")
        if os.path.exists(vault_config_path):
            with open(vault_config_path, "r", encoding="utf-8") as vf:
                vault_cfg = yaml.safe_load(vf)
                if vault_cfg and "VAULT_CONFIG" in vault_cfg:
                    config["VAULT_CONFIG"] = vault_cfg["VAULT_CONFIG"]
        
        if "VAULT_CONFIG" not in config:
            config["VAULT_CONFIG"] = {"enabled": False}
            
        return config
    except Exception as e:
        print(f"[ERROR] Failed to parse config file: {e}")
        sys.exit(1)
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"[ERROR] Failed to parse config file: {e}")
        sys.exit(1)

# ============================================================
# 1. COMMAND EXECUTION (SSH & LOCAL)
# ============================================================

def get_ssh_client(ssh_config, logger):
    logger.info(f"Connecting to target server {ssh_config['host']} via SSH...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        if ssh_config.get("key_file"):
            key_path = os.path.expanduser(ssh_config["key_file"])
            client.connect(hostname=ssh_config["host"], port=ssh_config.get("port", 22),
                           username=ssh_config["user"], key_filename=key_path)
        else:
            client.connect(hostname=ssh_config["host"], port=ssh_config.get("port", 22),
                           username=ssh_config["user"], password=ssh_config.get("password"))
        return client
    except Exception as e:
        logger.error(f"SSH connection failed: {e}")
        sys.exit(1)

def run_command_wrapper(ssh_client, cmd, logger, env_dict=None, timeout=None, quiet=False):
    env_prefix = ""
    if env_dict:
        for k, v in env_dict.items():
            env_prefix += f'export {k}="{v}"; '
    full_cmd = env_prefix + cmd
    
    if not quiet and logger:
        logger.debug(f"[CMD] {cmd}")
        
    if ssh_client:
        stdin, stdout, stderr = ssh_client.exec_command(full_cmd, timeout=timeout)
        out = stdout.read().decode('utf-8', errors='ignore')
        err = stderr.read().decode('utf-8', errors='ignore')
        status = stdout.channel.recv_exit_status()
    else:
        proc = subprocess.run(full_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, executable='/bin/bash')
        out = proc.stdout.decode('utf-8', errors='ignore')
        err = proc.stderr.decode('utf-8', errors='ignore')
        status = proc.returncode
    
    if not quiet and logger:
        for line in out.splitlines():
            logger.debug(f"  [STDOUT] {line}")
        for line in err.splitlines():
            logger.debug(f"  [STDERR] {line}")
            
    return status, out, err

# ============================================================
# 2. LOGGING
# ============================================================

def setup_logging(log_file):
    logger = logging.getLogger("rman_backup")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger

# ============================================================
# 3. VAULT INTEGRATION
# ============================================================

def get_vault_secret(vault_config, logger):
    logger.info("Connecting to HashiCorp Vault to fetch SMTP credentials...")
    try:
        client = hvac.Client(url=vault_config.get("url"), token=vault_config.get("token"))
        if not client.is_authenticated():
            raise Exception("Vault authentication failed.")
        
        secret_path = vault_config.get("secret_path")
        if secret_path.startswith("secret/data/"):
            secret_path = secret_path.replace("secret/data/", "")
        elif secret_path.startswith("secret/"):
            secret_path = secret_path.replace("secret/", "")
            
        read_response = client.secrets.kv.v2.read_secret_version(path=secret_path)
        password = read_response['data']['data'].get('smtp_password')
        if not password:
            password = read_response['data']['data'].get('password')
            
        if not password:
            raise Exception("SMTP password key not found in Vault secret.")
        logger.info("SMTP credentials retrieved successfully.")
        return password
    except Exception as e:
        logger.error(f"Vault connection or secret retrieval failed: {e}")
        sys.exit(1)


def get_vault_db_credentials(vault_config, logger):
    if not vault_config.get("enabled", False) or not vault_config.get("db_secret_path"):
        return None
    logger.info("Connecting to HashiCorp Vault to fetch DB credentials...")
    try:
        client = hvac.Client(url=vault_config.get("url"), token=vault_config.get("token"))
        if not client.is_authenticated():
            raise Exception("Vault authentication failed.")
        
        secret_path = vault_config.get("db_secret_path")
        if secret_path.startswith("secret/data/"):
            secret_path = secret_path.replace("secret/data/", "")
        elif secret_path.startswith("secret/"):
            secret_path = secret_path.replace("secret/", "")
            
        read_response = client.secrets.kv.v2.read_secret_version(path=secret_path)
        data = read_response['data']['data']
        logger.info("DB credentials retrieved successfully.")
        return data
    except Exception as e:
        logger.error(f"Vault DB credentials retrieval failed: {e}")
        return None

# ============================================================
# 4. PROCESS LOCK
# ============================================================

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

# ============================================================
# 5. DISK SPACE MANAGEMENT
# ============================================================

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

def list_daily_dirs(ssh_client, backup_root):
    status, out, err = run_command_wrapper(ssh_client, f"find {backup_root} -mindepth 1 -maxdepth 1 -type d -not -name 'logs' -printf '%p|%C@\n'", None, quiet=True)
    dirs = []
    for line in out.splitlines():
        if "|" in line:
            parts = line.split("|")
            try:
                dirs.append((parts[0], float(parts[1])))
            except Exception:
                pass
    dirs.sort(key=lambda x: x[1])
    return [d[0] for d in dirs]

def get_required_gb(logger, backup_config):
    history_dir = backup_config.get("history_dir")
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

def ensure_free_space(logger, ssh_client, env, backup_config):
    backup_root = backup_config["backup_root"]
    history_dir = backup_config.get("history_dir")
    required_gb = get_required_gb(logger, backup_config)
    free_gb     = get_free_gb(ssh_client, backup_root, logger)

    logger.info(f"Free disk space on target : {free_gb:.1f} GB  |  Required : {required_gb:.1f} GB")

    if free_gb >= required_gb:
        return True, free_gb, required_gb

    logger.warning("Insufficient disk space! Removing oldest backup dirs from target...")

    # Run RMAN catalog cleanup once before removing directories
    rman_clean = "CROSSCHECK BACKUP; DELETE NOPROMPT EXPIRED BACKUP; DELETE NOPROMPT OBSOLETE; QUIT;"
    try:
        run_rman(logger, env, ssh_client, rman_clean, label="cleanup")
    except RuntimeError:
        logger.warning("RMAN catalog cleanup failed during space reclamation. Continuing with directory removal.")

    daily_dirs = list_daily_dirs(ssh_client, backup_root)
    for old_dir in daily_dirs:
        if free_gb >= required_gb:
            break
        run_command_wrapper(ssh_client, f"rm -rf {old_dir}", logger)
        logger.info(f"Removed directory for space: {old_dir}")
        mark_history_deleted(history_dir, old_dir)
        free_gb = get_free_gb(ssh_client, backup_root, logger)

    # Crosscheck again after physical removal to sync RMAN catalog
    try:
        run_rman(logger, env, ssh_client, "CROSSCHECK BACKUP; CROSSCHECK ARCHIVELOG ALL; QUIT;", label="post-cleanup-crosscheck")
    except RuntimeError:
        logger.warning("Post-cleanup crosscheck failed.")

    if free_gb < required_gb:
        logger.error("Could not free enough space. Backup aborted.")
        return False, free_gb, required_gb

    return True, free_gb, required_gb

# ============================================================
# 6. RMAN, RSYNC & ORACLE UTILS
# ============================================================

def format_duration(seconds):
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    s = int(seconds) % 60
    if h > 0: return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"

def check_standby_exists(logger, env, ssh_client, db_creds=None):
    logger.info("Checking for Data Guard Standby existence via sqlplus...")
    
    if db_creds and db_creds.get("username") and db_creds.get("password"):
        user = db_creds["username"]
        pwd = db_creds["password"]
        host = db_creds.get("hostname") or db_creds.get("ip")
        db = db_creds.get("db", "")
        conn_str = f"{user}/\"{pwd}\"@{host}/{db} as sysdba"
    else:
        conn_str = "/ as sysdba"
        
    sql = "SET HEADING OFF FEEDBACK OFF PAGESIZE 0\nSELECT COUNT(*) FROM v$archive_dest WHERE target='STANDBY' AND destination IS NOT NULL;\nEXIT;\n"
    cmd = f"echo \"{sql}\" | sqlplus -s '{conn_str}'"
    status, out, err = run_command_wrapper(ssh_client, cmd, logger, env_dict=env, timeout=30, quiet=True)
    if status == 0:
        try:
            count = int(out.strip())
            if count > 0:
                logger.info(f"Standby database detected ({count} destinations).")
                return True
        except ValueError:
            pass
    return False

def run_rman(logger, env, ssh_client, rman_script, label="rman"):
    start = time.time()
    
    logger.info(f"Executing RMAN Script ({label}):\n{rman_script}")
    
    # Fail-Fast wrapper: Preserve RC
    # Use heredoc with 'EOF' (single-quoted) so shell does NO variable expansion
    cmd = f"""RMAN_TMP=$(mktemp /tmp/rman_script_XXXXXX.rman)
cat << 'EOF' > $RMAN_TMP
{rman_script}
EOF
rman target / @$RMAN_TMP
RC=$?
rm -f $RMAN_TMP
exit $RC"""
    
    status, out, err = run_command_wrapper(ssh_client, cmd, logger, env_dict=env, timeout=7200)
    elapsed = time.time() - start
    
    # Check explicitly for RMAN/ORA errors in output even if RC=0
    error_pattern = re.compile(r'(RMAN-\d+|ORA-\d+)')
    found_error = False
    
    for line in (out + "\n" + err).splitlines():
        if error_pattern.search(line):
            if any(ignore in line for ignore in ["RMAN-00571", "RMAN-00569", "Recovery Manager complete", "WARNING:"]):
                continue
            found_error = True
            break
            
    if found_error or status != 0:
        full_out = out + "\n" + err
        if "immutable" in full_out.lower() and "ORA-19509" in full_out:
            if logger:
                logger.warning(f"RMAN {label} reported an error, but it appears to be due to immutable backups preventing deletion. Ignoring error and treating as SUCCESS.")
        else:
            raise RuntimeError(f"RMAN {label} failed (rc={status}). See logs for ORA-/RMAN- errors.")

    return elapsed, out

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
                if len(parts) < 2: return 0
                val = parts[1].strip().split()[0].replace(",", "")
                suffixes = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
                if val and val[-1].upper() in suffixes:
                    try: return float(val[:-1]) * suffixes[val[-1].upper()]
                    except ValueError: return 0
                try: return float(val)
                except ValueError: return 0

            total_bytes = 0
            for line in out.splitlines():
                if "Total file size" in line:
                    total_bytes = parse_rsync_bytes(line)
                    break
            avg_speed_mbps = (total_bytes / (1024 ** 2)) / total_elapsed if total_elapsed > 0 else 0
            return total_elapsed, avg_speed_mbps, attempt, out
            
    raise RuntimeError(f"rsync failed after {max_retries} attempts.")

def run_scp(logger, ssh_client, source_dir, remote_dest, max_retries=3, timeout=28800):
    cmd = f"scp -r {source_dir} {remote_dest}"
    logger.info(f"scp starting: {source_dir} --> {remote_dest}")
    
    overall_start = time.time()
    for attempt in range(1, max_retries + 1):
        start = time.time()
        status, out, err = run_command_wrapper(ssh_client, cmd, logger, timeout=timeout)
        total_elapsed = time.time() - overall_start

        if status == 0:
            total_bytes = get_dir_size_gb(ssh_client, source_dir) * (1024 ** 3)
            avg_speed_mbps = (total_bytes / (1024 ** 2)) / total_elapsed if total_elapsed > 0 else 0
            return total_elapsed, avg_speed_mbps, attempt, out
            
    raise RuntimeError(f"scp failed after {max_retries} attempts. Output: {err}")

# ============================================================
# 7. METRICS & MONITORING
# ============================================================

def push_metrics(logger, monitoring_config, oracle_sid, elapsed, free_gb, required_gb, success):
    if not monitoring_config.get("enabled", False):
        logger.info("Monitoring is disabled. Skipping metric push.")
        return

    monitor_type = monitoring_config.get("type", "").lower()
    
    if monitor_type == "prometheus":
        url = monitoring_config.get("pushgateway_url")
        if not url: return
        data = (
            f"backup_status{{db=\"{oracle_sid}\"}} {1 if success else 0}\n"
            f"backup_duration_seconds{{db=\"{oracle_sid}\"}} {elapsed}\n"
            f"backup_free_space_gb{{db=\"{oracle_sid}\"}} {free_gb}\n"
            f"backup_required_space_gb{{db=\"{oracle_sid}\"}} {required_gb}\n"
        )
        try:
            requests.post(url, data=data, timeout=10)
            logger.info("Pushed metrics to Prometheus Pushgateway.")
        except Exception as e:
            logger.warning(f"Failed to push metrics to Prometheus: {e}")

    elif monitor_type == "zabbix":
        zabbix_server = monitoring_config.get("zabbix_server")
        zabbix_host = monitoring_config.get("zabbix_host")
        if not zabbix_server or not zabbix_host: return
        metrics = [
            (zabbix_host, "backup.status", 1 if success else 0),
            (zabbix_host, "backup.duration", elapsed),
            (zabbix_host, "backup.free_gb", free_gb),
            (zabbix_host, "backup.required_gb", required_gb)
        ]
        try:
            for host, key, val in metrics:
                cmd = ["zabbix_sender", "-z", zabbix_server, "-s", host, "-k", key, "-o", str(val)]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            logger.info("Pushed metrics to Zabbix Server.")
        except Exception as e:
            logger.warning(f"Failed to push metrics to Zabbix: {e}")

# ============================================================
# 8. PERSISTENT HISTORY MANAGEMENT
# ============================================================

def get_history_file(history_dir, date_obj=None):
    if date_obj is None:
        date_obj = datetime.now()
    filename = f"backup_history_{date_obj.strftime('%Y_%m')}.json"
    return os.path.join(history_dir, filename)

def get_history_file_for_dir(history_dir, dir_path):
    try:
        dir_name = os.path.basename(dir_path)
        dt = datetime.strptime(dir_name, "%d%b%Y")
        return get_history_file(history_dir, dt)
    except Exception:
        return get_history_file(history_dir)

def append_history(history_dir, record):
    h_file = get_history_file(history_dir)
    data = []
    if os.path.exists(h_file):
        try:
            with open(h_file, "r") as f:
                data = json.load(f)
        except Exception:
            pass
    data.append(record)
    with open(h_file, "w") as f:
        json.dump(data, f, indent=4)

def mark_history_deleted(history_dir, deleted_dir_path):
    h_file = get_history_file_for_dir(history_dir, deleted_dir_path)
    if not os.path.exists(h_file):
        return
    try:
        with open(h_file, "r") as f:
            data = json.load(f)
        
        updated = False
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for record in data:
            if record.get("directory", "").startswith(deleted_dir_path):
                if not record.get("is_deleted"):
                    record["is_deleted"] = True
                    record["deleted_at"] = now_str
                    updated = True
                    
        if updated:
            with open(h_file, "w") as f:
                json.dump(data, f, indent=4)
    except Exception:
        pass

def send_daily_summary(history_dir, mail_config, smtp_password, logger, target_date=None, target_server=None, oracle_config=None, backup_config=None, rman_report_html=""):
    h_file = get_history_file(history_dir)
    if not os.path.exists(h_file):
        return

    try:
        with open(h_file, "r") as f:
            runs = json.load(f)
    except Exception:
        return

    if not target_date:
        target_date = datetime.now().strftime("%Y-%m-%d")

    day_runs = [r for r in runs if r.get("run_time", "").startswith(target_date)]
    if not day_runs:
        return

    severity_map = {"INFO": 1, "WARNING": 2, "ERROR": 3}
    notification_level = mail_config.get("notification_level", "INFO").upper()
    min_severity_score = severity_map.get(notification_level, 1)

    max_day_severity = 1
    html_rows = ""
    
    for run in day_runs:
        run_status = run.get('status', 'FAILED').upper()
        run_severity = run.get('severity', 'INFO').upper()
        if "FAILED" in run_status:
            run_severity = "ERROR"
            
        run_score = severity_map.get(run_severity, 1)
        if run_score > max_day_severity:
            max_day_severity = run_score

        color = "#005500"
        if run_severity == "WARNING": color = "#856404"
        if run_severity == "ERROR": color = "#cc0000"
        row_color = "#f8f9fa" if run_status == "SUCCESS" else "#f8d7da"
        
        details = run.get('errors_warnings', '-')
        if run.get("remote_backup"):
            remote_status = "OK" if run.get("remote_complete") else "FAIL"
            details = f"Remote: {remote_status} | {run.get('remote_fail_desc', details)}"
            if run.get("transfer_speed_mbps"):
                details += f" ({run.get('transfer_speed_mbps')} MB/s)"

        html_rows += f"""
        <tr style="background-color: {row_color}; border-bottom: 1px solid #eee; font-size: 11px;">
            <td style="padding: 8px; text-align: left;">{run.get('operation', 'Backup')}</td>
            <td style="padding: 8px; text-align: left;">{run.get('start_time', run.get('run_time', '-'))} - {run.get('end_time', '-')}</td>
            <td style="padding: 8px; text-align: right;">{run.get('duration', '-')}</td>
            <td style="padding: 8px; text-align: right;">{run.get('size_gb', '0')} GB</td>
            <td style="padding: 8px; text-align: center; font-weight: bold; color: {color};">{run_status}</td>
            <td style="padding: 8px; text-align: left; color: #666;">{details}</td>
        </tr>
        """

    if max_day_severity < min_severity_score:
        logger.info(f"Day max severity ({max_day_severity}) below notification level ({min_severity_score}). Skipping mail.")
        return

    final_severity_label = "INFO"
    overall_status = "ALL OK"
    status_color = "#28a745" # Green
    
    if max_day_severity == 2: 
        final_severity_label = "WARNING"
        overall_status = "WARNING / PARTIAL"
        status_color = "#ffc107" # Yellow
    elif max_day_severity == 3: 
        final_severity_label = "ERROR"
        overall_status = "ERROR / FAILED"
        status_color = "#dc3545" # Red

    subject = f"{mail_config['subject_prefix']} [{final_severity_label}] Daily Summary | {target_date}"
    
    # Extract info safely
    oracle_sid = oracle_config.get("ORACLE_SID", "N/A") if oracle_config else "N/A"
    
    # Db host is either ORACLE_HOSTNAME or TARGET_SERVER host
    db_host = "Unknown"
    if oracle_config and oracle_config.get("ORACLE_HOSTNAME"):
        db_host = oracle_config.get("ORACLE_HOSTNAME")
    elif target_server and target_server.get("host"):
        db_host = target_server.get("host")
    else:
        import socket
        db_host = socket.gethostname()

    # Transfer target is remote_dest in BACKUP_CONFIG
    transfer_target = backup_config.get("remote_dest", "None") if backup_config else "None"

    success_count = sum(1 for r in day_runs if r.get('status', '').upper() == 'SUCCESS')
    total_count = len(day_runs)

    html_body = f"""
    <html>
    <body style="font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #333; line-height: 1.6;">
        <div style="max-width: 950px; margin: 0 auto; border: 1px solid #ddd; border-radius: 8px; overflow: hidden;">
            <div style="background-color: {status_color}; color: white; padding: 20px; text-align: center;">
                <h2 style="margin: 0;">Oracle RMAN Backup Summary</h2>
                <p style="margin: 5px 0 0 0;">Status: {overall_status} | Server: {db_host} | DB: {oracle_sid}</p>
            </div>
            
            <div style="padding: 20px;">
                <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
                    <tr>
                        <td style="width: 50%; padding: 10px; background: #f4f4f4;"><strong>Date:</strong> {target_date}</td>
                        <td style="width: 50%; padding: 10px; background: #f4f4f4;"><strong>DB Hostname:</strong> {db_host}</td>
                    </tr>
                    <tr>
                        <td style="padding: 10px;"><strong>Oracle SID:</strong> {oracle_sid}</td>
                        <td style="padding: 10px;"><strong>Transfer Target:</strong> {transfer_target}</td>
                    </tr>
                </table>

                <h3 style="border-bottom: 2px solid #eee; padding-bottom: 10px; color: #555;">Execution Details</h3>
                <table style="width: 100%; border-collapse: collapse;">
                    <thead>
                        <tr style="background-color: #343a40; color: white;">
                            <th style="padding: 12px; text-align: left;">Operation</th>
                            <th style="padding: 12px; text-align: left;">Time (Start - End)</th>
                            <th style="padding: 12px; text-align: right;">Duration</th>
                            <th style="padding: 12px; text-align: right;">Size</th>
                            <th style="padding: 12px; text-align: center;">Status</th>
                            <th style="padding: 12px; text-align: left;">Details / Message</th>
                        </tr>
                    </thead>
                    <tbody>
                        {html_rows}
                    </tbody>
                </table>
                
                <h3 style="border-bottom: 2px solid #eee; padding-bottom: 10px; color: #555; margin-top: 30px;">Latest RMAN Jobs (from DB)</h3>
                <div style="font-size: 11px; overflow-x: auto;">
                    {rman_report_html}
                </div>
                
                <div style="margin-top: 20px; font-size: 0.9em; color: #777; border-top: 1px solid #eee; padding-top: 10px;">
                    Daily Overview: {success_count} Success / {total_count} Total runs today.<br>
                    Notification Level: {notification_level}
                </div>
            </div>
            <div style="background-color: #f4f4f4; padding: 10px; text-align: center; font-size: 0.8em; color: #999;">
                This is an automated RMAN report generated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}.
            </div>
        </div>
    </body>
    </html>
    """

    to_addrs_raw = mail_config.get("to_addrs", [])
    if isinstance(to_addrs_raw, str):
        # Handle string like "a@b.com; c@d.com" or "a@b.com,c@d.com"
        to_addrs_list = [addr.strip() for addr in to_addrs_raw.replace(';', ',').split(',') if addr.strip()]
    else:
        to_addrs_list = to_addrs_raw

    if not to_addrs_list:
        logger.warning("No valid recipient addresses found. Skipping email.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"]    = mail_config["from_addr"]
    msg["To"]      = ", ".join(to_addrs_list)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(mail_config["smtp_host"], mail_config["smtp_port"], timeout=30) as srv:
            srv.ehlo()
            if mail_config.get("use_tls"):
                srv.starttls()
                srv.ehlo()
            if mail_config.get("use_auth", True):
                srv.login(mail_config["smtp_user"], smtp_password)
            srv.sendmail(mail_config["from_addr"], to_addrs_list, msg.as_string())
        logger.info(f"Daily summary email sent successfully ([{final_severity_label}]).")
    except Exception as e:
        logger.error(f"Failed to send daily email: {e}")

# ============================================================
# 9. MAIN
# ============================================================

def main(config_file="config.yaml", dry_run=False, test_mail=False, test_transfer=False):
    config = load_config("config.yaml")
    TARGET_SERVER = config.get("TARGET_SERVER", {})
    ORACLE_CONFIG = config.get("ORACLE_CONFIG", {})
    BACKUP_CONFIG = config.get("BACKUP_CONFIG", {})
    MAIL_CONFIG = config.get("MAIL_CONFIG", {})
    VAULT_CONFIG = config.get("VAULT_CONFIG", {})
    MONITORING_CONFIG = config.get("MONITORING_CONFIG", {})

    # Auto-resolve ~ and setup local dirs
    log_dir = os.path.expanduser(BACKUP_CONFIG.get("log_dir", "~/huaris/logs"))
    history_dir = os.path.expanduser(BACKUP_CONFIG.get("history_dir", "~/huaris/history"))
    pid_file = os.path.expanduser(BACKUP_CONFIG.get("pid_file", "/tmp/rman_backup.pid"))
    
    os.makedirs(log_dir, exist_ok=True)
    os.makedirs(history_dir, exist_ok=True)

    now  = datetime.now()
    hour = now.hour
    day_name  = now.strftime("%d%b%Y").upper()
    file_name = now.strftime("%d%b%y%H").upper()
    
    log_file = os.path.join(log_dir, f"backup_{file_name}.log")

    if dry_run or test_transfer:
        logger = setup_logging(os.path.join(log_dir, "backup_test.log"))
    else:
        logger = setup_logging(log_file)
        latest_link = os.path.join(log_dir, "backup_latest.log")
        try:
            if os.path.exists(latest_link) or os.path.islink(latest_link):
                os.remove(latest_link)
            os.symlink(log_file, latest_link)
        except Exception:
            pass
    
    if dry_run: logger.info("=== STARTING IN DRY-RUN MODE ===")
    
    db_creds = None
    if VAULT_CONFIG.get("enabled"):
        db_creds = get_vault_db_credentials(VAULT_CONFIG, logger)
    if test_transfer: logger.info("=== STARTING TEST TRANSFER MODE ===")
    
    if test_mail:
        logger.info("=== STARTING TEST MAIL ===")
        if MAIL_CONFIG.get("enabled"):
            smtp_password = None
            if MAIL_CONFIG.get("use_auth", True):
                if VAULT_CONFIG.get("enabled", True):
                    smtp_password = get_vault_secret(VAULT_CONFIG, logger)
                else:
                    smtp_password = MAIL_CONFIG.get("smtp_password")
            try:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = Header(f"{MAIL_CONFIG['subject_prefix']} [TEST] Mail Configuration", "utf-8")
                msg["From"]    = MAIL_CONFIG["from_addr"]
                msg["To"]      = ", ".join(MAIL_CONFIG["to_addrs"])
                msg.attach(MIMEText("<html><body><h3>SMTP Test Successful</h3><p>If you see this, your SMTP and Vault settings are correct.</p></body></html>", "html", "utf-8"))
                with smtplib.SMTP(MAIL_CONFIG["smtp_host"], MAIL_CONFIG["smtp_port"], timeout=30) as srv:
                    srv.ehlo()
                    if MAIL_CONFIG.get("use_tls"):
                        srv.starttls()
                        srv.ehlo()
                    if MAIL_CONFIG.get("use_auth", True):
                        srv.login(MAIL_CONFIG["smtp_user"], smtp_password)
                    srv.sendmail(MAIL_CONFIG["from_addr"], MAIL_CONFIG["to_addrs"], msg.as_string())
                logger.info("Test email sent successfully.")
            except Exception as e:
                logger.error(f"Failed to send test email: {e}")
        else:
            logger.info("Mail is disabled in config.")
        return

    locked, pid = acquire_lock(pid_file)
    if not locked:
        logger.error("Another backup process is running.")
        sys.exit(2)

    ssh_client = None
    try:
        target_enabled = TARGET_SERVER.get("enabled", False)
        if target_enabled:
            ssh_client = get_ssh_client(TARGET_SERVER, logger)
        else:
            logger.info("TARGET_SERVER is disabled. Running all commands LOCALLY.")

        daily_dir = os.path.join(BACKUP_CONFIG["backup_root"], day_name)
        full_path = os.path.join(daily_dir, f"{hour:02d}")

        if not dry_run:
            run_command_wrapper(ssh_client, f"mkdir -p {full_path}", logger)

        env = {}
        for key, val in ORACLE_CONFIG.items():
            env[key] = str(val)
        oh = ORACLE_CONFIG.get("ORACLE_HOME", "")
        env["PATH"] = f"/usr/sbin:{oh}/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
        env["LD_LIBRARY_PATH"] = f"{oh}/lib:/lib:/usr/lib"
        env["CLASSPATH"] = f"{oh}/JRE:{oh}/jlib:{oh}/rdbms/jlib"
        env["TMP"] = "/tmp"
        env["TMPDIR"] = "/tmp"
        oracle_sid = ORACLE_CONFIG.get("ORACLE_SID", "")

        error_msg = None
        backup_start = datetime.now()
        overall_start = time.time()
        
        free_gb, required_gb = 0, 0

        try:
            # Space Check
            space_ok, free_gb, required_gb = ensure_free_space(logger, ssh_client, env, BACKUP_CONFIG)
            if not space_ok:
                raise RuntimeError("Insufficient disk space on target server.")

            # RMAN Backup
            parallelism = BACKUP_CONFIG.get("parallelism", 1)
            device_type = BACKUP_CONFIG.get("device_type", "DISK").upper()
            rman_script_file = BACKUP_CONFIG.get("rman_script_file", "")
            RMAN_TEMPLATE = config.get("RMAN_TEMPLATE", {})
            
            rman_script = None
            # Priority 1: Custom .rman file
            if rman_script_file:
                if not os.path.isabs(rman_script_file):
                    rman_script_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), rman_script_file)
                if os.path.exists(rman_script_file):
                    logger.info(f"Using custom RMAN script from: {rman_script_file}")
                    with open(rman_script_file, "r") as f:
                        rman_script = f.read()
                else:
                    logger.warning(f"Custom RMAN script file '{rman_script_file}' not found. Falling back to RMAN_TEMPLATE.")

            # Priority 2: RMAN Template from config.yaml
            if not rman_script:
                if test_transfer:
                    # test_transfer also uses RUN block to avoid SBT_TAPE fallback
                    rman_script = f"""
RUN {{
  ALLOCATE CHANNEL c1 TYPE {device_type};
  BACKUP AS COMPRESSED BACKUPSET CURRENT CONTROLFILE
    FORMAT '{full_path}/controlfile_test_{file_name}';
  RELEASE CHANNEL c1;
}}
QUIT;
"""
                else:
                    has_standby = check_standby_exists(logger, env, ssh_client, db_creds)
                    cleanup = RMAN_TEMPLATE.get("cleanup", {})
                    ret_days = cleanup.get("archive_retention_days", 2)
                    recovery_window = cleanup.get("recovery_window_days", 1)

                    def is_true(val):
                        if isinstance(val, str):
                            return val.lower() in ('true', 'yes', '1', 'on')
                        return bool(val)

                    if has_standby:
                        archivelog_deletion_cmd = f"DELETE NOPROMPT ARCHIVELOG ALL COMPLETED BEFORE 'SYSDATE-{ret_days}' BACKED UP 1 TIMES TO DISK AND APPLIED ON ALL STANDBY;"
                    else:
                        archivelog_deletion_cmd = f"DELETE NOPROMPT ARCHIVELOG ALL COMPLETED BEFORE 'SYSDATE-{ret_days}' BACKED UP 1 TIMES TO DISK;"

                    # If neither database nor archivelogs are being backed up, fallback to parallelism 1
                    if not is_true(RMAN_TEMPLATE.get("full_backup", True)) and not is_true(RMAN_TEMPLATE.get("archive_backup", True)):
                        logger.info("Only controlfile/SPFILE backup requested. Forcing parallelism to 1.")
                        parallelism = 1

                    # Build channel allocation
                    allocate_cmds = ""
                    release_cmds = ""
                    for i in range(1, parallelism + 1):
                        allocate_cmds += f"  ALLOCATE CHANNEL c{i} TYPE {device_type};\n"
                        release_cmds += f"  RELEASE CHANNEL c{i};\n"

                    # Build backup commands from template
                    backup_cmds = ""
                    if is_true(RMAN_TEMPLATE.get("full_backup", True)):
                        backup_cmds += f"""
  BACKUP AS COMPRESSED BACKUPSET FULL DATABASE 
    TAG 'DATABASE_{file_name}' 
    FORMAT '{full_path}/Data_%d_%I_%s_%T_%U.rman';
"""

                    if is_true(RMAN_TEMPLATE.get("archive_backup", True)):
                        backup_cmds += f"""
  SQL 'ALTER SYSTEM ARCHIVE LOG CURRENT';
  BACKUP AS COMPRESSED BACKUPSET 
    TAG 'ARCHIVELOG_{file_name}' 
    FORMAT '{full_path}/ARCH_%d_%I_%s_%T_%U.arch' 
    ARCHIVELOG ALL;
"""
                    if is_true(RMAN_TEMPLATE.get("controlfile_backup", True)):
                        backup_cmds += f"""
  BACKUP AS COMPRESSED BACKUPSET CURRENT CONTROLFILE 
    TAG 'CONTROLFILE_{file_name}' 
    FORMAT '{full_path}/CTL_%d_%T_%s_%p_ctlb';
"""

                    # Build cleanup commands from template
                    cleanup_cmds = ""
                    if is_true(cleanup.get("delete_obsolete", True)):
                        cleanup_cmds += f"\nDELETE NOPROMPT OBSOLETE RECOVERY WINDOW OF {recovery_window} DAYS;"
                    if is_true(cleanup.get("crosscheck_archivelog", True)):
                        cleanup_cmds += "\nCROSSCHECK ARCHIVELOG ALL;"
                    if is_true(cleanup.get("crosscheck_backup", True)):
                        cleanup_cmds += "\nCROSSCHECK BACKUP OF ARCHIVELOG ALL;"
                    if is_true(cleanup.get("report_obsolete", True)):
                        cleanup_cmds += "\nREPORT OBSOLETE;"
                    if is_true(cleanup.get("delete_expired_archivelog", True)):
                        cleanup_cmds += "\nDELETE NOPROMPT EXPIRED ARCHIVELOG ALL;"
                    if is_true(cleanup.get("delete_expired_controlfile", True)):
                        cleanup_cmds += "\nDELETE NOPROMPT EXPIRED BACKUP OF CONTROLFILE;"
                    if is_true(cleanup.get("delete_obsolete_orphan", True)):
                        cleanup_cmds += "\nDELETE FORCE NOPROMPT OBSOLETE ORPHAN;"
                        cleanup_cmds += "\nDELETE FORCE NOPROMPT OBSOLETE;"
                    if archivelog_deletion_cmd and is_true(RMAN_TEMPLATE.get("archive_backup", True)):
                        cleanup_cmds += f"\n{archivelog_deletion_cmd}"

                    # SPFILE backup from template
                    spfile_cmd = ""
                    if is_true(RMAN_TEMPLATE.get("spfile_backup", True)):
                        spfile_cmd = f"""
  BACKUP SPFILE 
    TAG 'SPFILE_{file_name}' 
    FORMAT '{full_path}/Spfile_%d_%I_%s_%T_%U.rman';
"""

                    # Extra custom commands from template
                    extra_cmds = ""
                    for cmd in RMAN_TEMPLATE.get("extra_commands", []):
                        resolved_cmd = cmd.replace("{path}", full_path)
                        extra_cmds += f"\n  {resolved_cmd}"

                    rman_script = f"""
CONFIGURE CONTROLFILE AUTOBACKUP ON;
CONFIGURE CONTROLFILE AUTOBACKUP FORMAT FOR DEVICE TYPE {device_type} TO '{full_path}/%F';
CONFIGURE SNAPSHOT CONTROLFILE NAME TO '{full_path}/snapcf_%d_{file_name}.f';

RUN {{
{allocate_cmds}
{backup_cmds}
{spfile_cmd}
{extra_cmds}

{release_cmds}}}

{cleanup_cmds}

QUIT;
"""
            if dry_run:
                logger.info(f"[DRY-RUN] Would execute RMAN script on target:\n{rman_script}")
            else:
                run_rman(logger, env, ssh_client, rman_script, label="test_backup" if test_transfer else "full_backup")

        except Exception as exc:
            error_msg = str(exc)
            logger.error(f"BACKUP FAILED: {error_msg}")

        backup_elapsed = time.time() - overall_start
        success_status = "FAILED" if error_msg else "SUCCESS"
        
        history_record = {
            "run_time": backup_start.strftime("%Y-%m-%d %H:%M:%S"),
            "start_time": backup_start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "operation": "Backup" if not dry_run else "Backup (Dry-Run)",
            "directory": full_path,
            "duration": format_duration(backup_elapsed),
            "size_gb": f"{get_dir_size_gb(ssh_client, full_path):.1f}" if not error_msg else "0",
            "status": success_status,
            "severity": "INFO" if not error_msg else "ERROR",
            "errors_warnings": error_msg or "None",
            "is_deleted": False,
            "deleted_at": None
        }
        
        if dry_run:
            logger.info(f"[DRY-RUN] Would append history locally: {history_record}")
        else:
            append_history(history_dir, history_record)

        # Transfer local log_file to remote DB server
        if not dry_run and not test_transfer and not error_msg:
            try:
                if ssh_client:
                    sftp = ssh_client.open_sftp()
                    sftp.put(log_file, f"{full_path}/backup_{file_name}.log")
                    sftp.close()
                else:
                    shutil.copy2(log_file, f"{full_path}/backup_{file_name}.log")
            except Exception as e:
                logger.warning(f"Failed to copy local log file to DB server: {e}")

        # Transfer Backup to final destination (remote_dest)
        transfer_triggered = False
        transfer_hours = BACKUP_CONFIG.get("transfer_hours", BACKUP_CONFIG.get("rsync_hours", []))
        transfer_method = BACKUP_CONFIG.get("transfer_method", "rsync").lower()

        is_transfer_hour = (transfer_hours == "all" or transfer_hours == ["all"] or (isinstance(transfer_hours, list) and hour in transfer_hours))
        
        # Only transfer if there was NO error
        if not error_msg and (is_transfer_hour or test_transfer):
            transfer_triggered = True
            transfer_start_time = datetime.now()
            transfer_overall_start = time.time()
            try:
                remote_base = BACKUP_CONFIG["remote_dest"].split(":")[0]
                remote_path = BACKUP_CONFIG["remote_dest"].split(":")[1]
                remote_full_dest = f"{BACKUP_CONFIG['remote_dest']}/{day_name}"

                if dry_run:
                    logger.info(f"[DRY-RUN] Would execute {transfer_method} to {remote_full_dest}")
                    transfer_elapsed, avg_speed, attempts = 0.5, 100.0, 1
                else:
                    # Depending on local vs ssh_client, ssh might need -o StrictHostKeyChecking=no
                    ssh_prefix = f"ssh -o StrictHostKeyChecking=no {remote_base} "
                    run_command_wrapper(ssh_client, f"{ssh_prefix} mkdir -p {remote_path}/{day_name}", logger, quiet=True)
                    if transfer_method == "scp":
                        run_command_wrapper(ssh_client, f"{ssh_prefix} cmd /c mkdir \"{remote_path}\\{day_name}\"", logger, quiet=True)
                        transfer_elapsed, avg_speed, attempts, _ = run_scp(logger, ssh_client, full_path, remote_full_dest)
                    else:
                        transfer_elapsed, avg_speed, attempts, _ = run_rsync(logger, ssh_client, full_path, remote_full_dest)
                
                transfer_record = {
                    "run_time": transfer_start_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "start_time": transfer_start_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "end_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "operation": transfer_method.capitalize() if not dry_run else f"{transfer_method.capitalize()} (Dry-Run)",
                    "directory": remote_full_dest,
                    "duration": format_duration(transfer_elapsed),
                    "transfer_speed_mbps": round(avg_speed, 2),
                    "total_attempts": attempts,
                    "size_gb": f"{get_dir_size_gb(ssh_client, full_path):.1f}",
                    "status": "SUCCESS",
                    "severity": "INFO",
                    "remote_backup": True,
                    "remote_complete": True,
                    "errors_warnings": "None",
                    "is_deleted": False,
                    "deleted_at": None
                }
                if dry_run:
                    logger.info(f"[DRY-RUN] Would append transfer history locally: {transfer_record}")
                else:
                    append_history(history_dir, transfer_record)
            except Exception as e:
                append_history(history_dir, {
                    "run_time": transfer_start_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "start_time": transfer_start_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "end_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "operation": transfer_method.capitalize(),
                    "directory": "N/A",
                    "duration": format_duration(time.time() - transfer_overall_start),
                    "status": "FAILED",
                    "severity": "ERROR",
                    "remote_backup": True,
                    "remote_complete": False,
                    "remote_fail_desc": str(e),
                    "errors_warnings": str(e),
                    "is_deleted": False,
                    "deleted_at": None
                })

        # Routine Cleanup
        keep_days = BACKUP_CONFIG.get("keep_days", 7)
        cutoff = time.time() - keep_days * 86400
        for bdir in list_daily_dirs(ssh_client, BACKUP_CONFIG["backup_root"]):
            if bdir == daily_dir:
                continue
            status, out, err = run_command_wrapper(ssh_client, f"stat -c %Y {bdir}", None, quiet=True)
            try:
                bdir_time = float(out.strip())
                if bdir_time < cutoff:
                    run_command_wrapper(ssh_client, f"rm -rf {bdir}", logger)
                    logger.info(f"Routine cleanup: Removed directory {bdir}")
                    mark_history_deleted(history_dir, bdir)
            except Exception:
                pass

        # Push Metrics
        if dry_run:
            logger.info("[DRY-RUN] Would push metrics.")
        else:
            push_metrics(logger, MONITORING_CONFIG, oracle_sid, backup_elapsed, free_gb, required_gb, not bool(error_msg))

        
        # RMAN Report Query
        rman_report_html = ""
        if not dry_run and not error_msg:
            if db_creds and db_creds.get("username") and db_creds.get("password"):
                user = db_creds["username"]
                pwd = db_creds["password"]
                host = db_creds.get("hostname") or db_creds.get("ip")
                db = db_creds.get("db", "")
                conn_str = f"{user}/\"{pwd}\"@{host}/{db} as sysdba"
            else:
                conn_str = "/ as sysdba"
            
            report_sql = """SET MARKUP HTML ON SPOOL ON ENTMAP OFF
SET PAGESIZE 100 LINESIZE 200 TRIMSPOOL ON HEADING ON FEEDBACK OFF
SELECT * FROM (
SELECT 
  rj.session_key,
  rj.input_type,
  rj.status,
  TO_CHAR(rj.start_time, 'DD.MM.YYYY HH24:MI') AS baslangic,
  rj.input_bytes_display    AS okunan,
  rj.output_bytes_display   AS yazilan,
  rj.time_taken_display     AS sure
FROM v$rman_backup_job_details rj
ORDER BY rj.start_time DESC
) WHERE rownum <= 10;
EXIT;"""
            cmd = f"echo \"{report_sql}\" | sqlplus -s '{conn_str}'"
            status, out, err = run_command_wrapper(ssh_client, cmd, logger, env_dict=env, quiet=True)
            if status == 0:
                # Clear out any unwanted lines before the HTML table
                start_idx = out.find("<table")
                if start_idx != -1:
                    rman_report_html = out[start_idx:]
                else:
                    rman_report_html = out

        # Send Daily Summary
        daily_mail_hour = MAIL_CONFIG.get("daily_mail_hour", 23)
        should_send_mail = (transfer_triggered or str(daily_mail_hour).lower() == "all" or hour == daily_mail_hour)
        
        if should_send_mail and MAIL_CONFIG.get("enabled"):
            smtp_password = None
            if MAIL_CONFIG.get("use_auth", True):
                if VAULT_CONFIG.get("enabled", True):
                    smtp_password = get_vault_secret(VAULT_CONFIG, logger)
                else:
                    smtp_password = MAIL_CONFIG.get("smtp_password")
            report_date = backup_start.strftime("%Y-%m-%d")
            send_daily_summary(history_dir, MAIL_CONFIG, smtp_password, logger, target_date=report_date, target_server=TARGET_SERVER, oracle_config=ORACLE_CONFIG, backup_config=BACKUP_CONFIG, rman_report_html=rman_report_html)

        if error_msg:
            sys.exit(1)

    finally:
        if ssh_client:
            ssh_client.close()
        release_lock(pid_file)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Oracle RMAN Backup Script (Hybrid Jump/Local Server Edition)")
    parser.add_argument("--config", default="config.yaml", help="Path to the main configuration file.")
    parser.add_argument("--dry-run", action="store_true", help="Run the script without executing RMAN, Rsync/SCP, or modifying history.")
    parser.add_argument("--test-mail", action="store_true", help="Send a test email using the configured SMTP settings and exit.")
    parser.add_argument("--test-transfer", action="store_true", help="Run a quick backup of only the control file and transfer it via SCP/Rsync to test the remote connection.")
    args = parser.parse_args()

    main(config_file=args.config, dry_run=args.dry_run, test_mail=args.test_mail, test_transfer=args.test_transfer)
