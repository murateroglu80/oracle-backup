#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Oracle RMAN Backup Script - v5.1.0
Advanced Refactoring:
  1. Persistent Backup History with Monthly Rotation (JSON database).
  2. Smart Disk Space Calculation (O(1) via JSON history).
  3. Deletion Tracking (is_deleted: true in JSON).
  4. Dynamic Data Guard Applied-On-Standby checks via sqlplus.
  5. HashiCorp Vault Integration for SMTP Password.
  6. External config.yaml configuration.
  7. Monitoring (Zabbix/Prometheus Pushgateway) integration.
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
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header

try:
    import hvac
except ImportError:
    print("[ERROR] 'hvac' library is missing. Install it using 'pip install hvac'")
    sys.exit(1)


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
            return yaml.safe_load(f)
    except Exception as e:
        print(f"[ERROR] Failed to parse config file: {e}")
        sys.exit(1)

# ============================================================
# 1. ENVIRONMENT SETUP
# ============================================================

def setup_environment(oracle_config):
    env = os.environ.copy()
    for key, val in oracle_config.items():
        env[key] = str(val)
    oh = oracle_config["ORACLE_HOME"]
    env["PATH"]            = f"/usr/sbin:{oh}/bin:/usr/local/bin:/usr/bin:/bin"
    env["LD_LIBRARY_PATH"] = f"{oh}/lib:/lib:/usr/lib"
    env["CLASSPATH"]       = f"{oh}/JRE:{oh}/jlib:{oh}/rdbms/jlib"
    env["TMP"]             = "/tmp"
    env["TMPDIR"]          = "/tmp"
    return env

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
        # Clean path for hvac (removes mount point and data prefix if present)
        # hvac kv v2 automatically prepends secret/data/ to the path internally
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

def get_free_gb(path):
    return shutil.disk_usage(path).free / (1024 ** 3)

def get_dir_size_gb(path):
    total_bytes = 0
    if not os.path.exists(path):
        return 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fname in filenames:
            fp = os.path.join(dirpath, fname)
            try:
                total_bytes += os.path.getsize(fp)
            except OSError:
                pass
    return total_bytes / (1024 ** 3)

def list_daily_dirs(backup_root):
    dirs = []
    if not os.path.exists(backup_root):
        return []
    for entry in os.scandir(backup_root):
        if entry.is_dir() and entry.name != "logs":
            dirs.append((entry.path, entry.stat().st_ctime))
    dirs.sort(key=lambda x: x[1])
    return [d[0] for d in dirs]

def get_required_gb(logger, backup_config):
    history_dir = backup_config.get("history_dir")
    fallback_gb = backup_config["fallback_size_gb"]
    buffer_pct  = backup_config["space_buffer_pct"]
    
    # Check current month, then previous month for last successful backup size
    files_to_check = [
        get_history_file(history_dir), # Current month
        get_history_file(history_dir, datetime.now() - timedelta(days=31)) # Previous month
    ]
    
    for h_file in files_to_check:
        if os.path.exists(h_file):
            try:
                with open(h_file, "r") as f:
                    data = json.load(f)
                # Scan backwards for the last successful backup
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

def ensure_free_space(logger, env, backup_config):
    backup_root = backup_config["backup_root"]
    history_dir = backup_config.get("history_dir")
    required_gb = get_required_gb(logger, backup_config)
    free_gb     = get_free_gb(backup_root)

    logger.info(f"Free disk space : {free_gb:.1f} GB  |  Required : {required_gb:.1f} GB")

    if free_gb >= required_gb:
        return True, free_gb, required_gb

    logger.warning("Insufficient disk space! Removing oldest backup dirs...")

    daily_dirs = list_daily_dirs(backup_root)
    for old_dir in daily_dirs:
        if free_gb >= required_gb:
            break
        rman_clean = "CROSSCHECK BACKUP; DELETE NOPROMPT EXPIRED BACKUP; DELETE NOPROMPT OBSOLETE; QUIT;"
        try:
            run_rman(logger, env, rman_clean, label="cleanup")
        except RuntimeError:
            pass

        shutil.rmtree(old_dir, ignore_errors=True)
        logger.info(f"Removed directory for space: {old_dir}")
        mark_history_deleted(history_dir, old_dir)
        
        free_gb = get_free_gb(backup_root)

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

def check_standby_exists(logger, env):
    logger.info("Checking for Data Guard Standby existence via sqlplus...")
    sql = "SET HEADING OFF FEEDBACK OFF PAGESIZE 0\nSELECT COUNT(*) FROM v$archive_dest WHERE target='STANDBY' AND destination IS NOT NULL;\nEXIT;\n"
    try:
        proc = subprocess.Popen(
            ["sqlplus", "-s", "/ as sysdba"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, universal_newlines=True
        )
        stdout, stderr = proc.communicate(input=sql, timeout=30)
        if proc.returncode == 0:
            try:
                count = int(stdout.strip())
                if count > 0:
                    logger.info(f"Standby database detected ({count} destinations).")
                    return True
                return False
            except ValueError:
                return False
        return False
    except Exception:
        return False

def run_rman(logger, env, rman_script, label="rman"):
    start = time.time()
    proc = subprocess.Popen(
        ["rman", "target", "/"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, env=env, universal_newlines=True
    )
    stdout, _ = proc.communicate(input=rman_script, timeout=7200)
    elapsed = time.time() - start

    for line in stdout.splitlines():
        logger.debug(f"  [RMAN] {line}")

    if proc.returncode != 0:
        raise RuntimeError(f"RMAN {label} failed (rc={proc.returncode})")

    return elapsed, stdout

def run_rsync(logger, source_dir, remote_dest, max_retries=3, timeout=28800):
    cmd = ["rsync", "-avz", "--progress", "--stats", "--partial", source_dir, remote_dest]
    logger.info(f"rsync starting: {source_dir} --> {remote_dest}")
    
    overall_start = time.time()
    last_stdout   = ""
    attempts_made = 0

    for attempt in range(1, max_retries + 1):
        attempts_made = attempt
        start = time.time()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True
            )
            last_stdout, _ = proc.communicate(timeout=timeout)
            elapsed = time.time() - start

            if proc.returncode == 0:
                total_elapsed = time.time() - overall_start
                
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
                for line in last_stdout.splitlines():
                    if "Total file size" in line:
                        total_bytes = parse_rsync_bytes(line)
                        break
                avg_speed_mbps = (total_bytes / (1024 ** 2)) / total_elapsed if total_elapsed > 0 else 0
                return total_elapsed, avg_speed_mbps, attempts_made, last_stdout

        except subprocess.TimeoutExpired:
            proc.kill()
            
    raise RuntimeError(f"rsync failed after {max_retries} attempts.")

def run_scp(logger, source_dir, remote_dest, max_retries=3, timeout=28800):
    cmd = ["scp", "-r", source_dir, remote_dest]
    logger.info(f"scp starting: {source_dir} --> {remote_dest}")
    
    overall_start = time.time()
    last_stdout   = ""
    attempts_made = 0

    for attempt in range(1, max_retries + 1):
        attempts_made = attempt
        start = time.time()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True
            )
            last_stdout, _ = proc.communicate(timeout=timeout)
            elapsed = time.time() - start

            if proc.returncode == 0:
                total_elapsed = time.time() - overall_start
                # scp doesn't give a nice summary of bytes easily, so we estimate speed
                total_bytes = get_dir_size_gb(source_dir) * (1024 ** 3)
                avg_speed_mbps = (total_bytes / (1024 ** 2)) / total_elapsed if total_elapsed > 0 else 0
                return total_elapsed, avg_speed_mbps, attempts_made, last_stdout

        except subprocess.TimeoutExpired:
            proc.kill()
            
    raise RuntimeError(f"scp failed after {max_retries} attempts. Output: {last_stdout}")

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
        # Assuming format DDMMMYYYY like 11MAY2026
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

def send_daily_summary(history_dir, mail_config, smtp_password, logger, target_date=None):
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

    # Severity Mapping
    # SUCCESS = INFO, WARNING = WARNING, FAILED = ERROR
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

        color = "#005500" # Green
        if run_severity == "WARNING": color = "#856404" # Orange
        if run_severity == "ERROR": color = "#cc0000" # Red
        
        details = run.get('errors_warnings', '-')
        if run.get("remote_backup"):
            remote_status = "OK" if run.get("remote_complete") else "FAIL"
            details = f"Remote: {remote_status} | {run.get('remote_fail_desc', details)}"
            if run.get("transfer_speed_mbps"):
                details += f" ({run.get('transfer_speed_mbps')} MB/s)"

        html_rows += f"""<tr>
            <td>{run.get('operation', 'Backup')}</td>
            <td>{run.get('start_time', run.get('run_time', '-'))}</td>
            <td>{run.get('end_time', '-')}</td>
            <td>{run.get('duration', '-')}</td>
            <td>{run.get('size_gb', '0')}</td>
            <td style='color:{color}; font-weight:bold;'>{run_status}</td>
            <td>{details}</td>
        </tr>"""

    # Determine if we should send based on severity
    if max_day_severity < min_severity_score:
        logger.info(f"Day max severity ({max_day_severity}) below notification level ({min_severity_score}). Skipping mail.")
        return

    final_severity_label = "INFO"
    if max_day_severity == 2: final_severity_label = "WARNING"
    if max_day_severity == 3: final_severity_label = "ERROR"

    subject = f"{mail_config['subject_prefix']} [{final_severity_label}] Daily Summary | {target_date}"

    html_body = f"""<html>
    <body style="font-family:sans-serif; background:#f4f4f4; padding:20px;">
      <h2 style="color:{'#005500' if max_day_severity==1 else ('#856404' if max_day_severity==2 else '#cc0000')};">
        RMAN Daily Backup Summary - {final_severity_label}
      </h2>
      <table border="1" cellpadding="8" cellspacing="0" style="background:#fff; width:100%; border-collapse:collapse; font-size:13px;">
        <tr style="background:#f8f9fa;">
          <th>Operation</th><th>Start</th><th>End</th><th>Duration</th><th>Size (GB)</th><th>Status</th><th>Details</th>
        </tr>
        {html_rows}
      </table>
      <p style="font-size:11px; color:#666; margin-top:20px;">Notification Level: {notification_level} | Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    </body>
    </html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"]    = mail_config["from_addr"]
    msg["To"]      = ", ".join(mail_config["to_addrs"])
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(mail_config["smtp_host"], mail_config["smtp_port"], timeout=30) as srv:
            srv.ehlo()
            if mail_config.get("use_tls"):
                srv.starttls()
                srv.ehlo()
            if mail_config.get("use_auth", True):
                srv.login(mail_config["smtp_user"], smtp_password)
            srv.sendmail(mail_config["from_addr"], mail_config["to_addrs"], msg.as_string())
        logger.info(f"Daily summary email sent successfully ([{final_severity_label}]).")
    except Exception as e:
        logger.error(f"Failed to send daily email: {e}")

# ============================================================
# 9. MAIN
# ============================================================

def main(dry_run=False, test_mail=False):
    config = load_config("config.yaml")
    ORACLE_CONFIG = config["ORACLE_CONFIG"]
    BACKUP_CONFIG = config["BACKUP_CONFIG"]
    MAIL_CONFIG = config["MAIL_CONFIG"]
    VAULT_CONFIG = config["VAULT_CONFIG"]
    MONITORING_CONFIG = config.get("MONITORING_CONFIG", {})

    logger = setup_logging(BACKUP_CONFIG["log_dir"] + "/backup_test.log" if dry_run else BACKUP_CONFIG["log_dir"] + "/backup_latest.log")
    if dry_run:
        logger.info("=== STARTING IN DRY-RUN MODE ===")
    
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

    now  = datetime.now()
    hour = now.hour

    day_name  = now.strftime("%d%b%Y").upper()
    file_name = now.strftime("%d%b%y%H").upper()

    daily_dir = os.path.join(BACKUP_CONFIG["backup_root"], day_name)
    full_path = os.path.join(daily_dir, f"{hour:02d}")
    log_dir   = BACKUP_CONFIG["log_dir"]
    log_file  = os.path.join(log_dir, f"backup_{file_name}.log")
    pid_file  = BACKUP_CONFIG["pid_file"]
    history_dir = BACKUP_CONFIG.get("history_dir")

    os.makedirs(log_dir, exist_ok=True)
    if not dry_run:
        os.makedirs(full_path, exist_ok=True)
        os.makedirs(history_dir, exist_ok=True)

    # Re-setup logger with the proper log file
    logger = setup_logging(log_file)
    env    = setup_environment(ORACLE_CONFIG)
    oracle_sid  = ORACLE_CONFIG["ORACLE_SID"]

    locked, pid = acquire_lock(pid_file)
    if not locked:
        logger.error("Another backup process is running.")
        sys.exit(2)

    error_msg = None
    backup_start = datetime.now()
    overall_start = time.time()
    
    free_gb, required_gb = 0, 0

    try:
        # STEP 1: Space Check
        space_ok, free_gb, required_gb = ensure_free_space(logger, env, BACKUP_CONFIG)
        if not space_ok:
            raise RuntimeError("Insufficient disk space.")

        # STEP 2: RMAN Backup
        parallelism = BACKUP_CONFIG.get("parallelism", 1)
        has_standby = check_standby_exists(logger, env)
        if has_standby:
            archivelog_deletion_cmd = "DELETE NOPROMPT ARCHIVELOG ALL BACKED UP 1 TIMES TO DISK AND APPLIED ON ALL STANDBY;"
        else:
            archivelog_deletion_cmd = "DELETE NOPROMPT ARCHIVELOG ALL BACKED UP 1 TIMES TO DISK;"

        rman_script = f"""
CROSSCHECK BACKUP;
CROSSCHECK ARCHIVELOG ALL;
DELETE NOPROMPT EXPIRED ARCHIVELOG ALL;
SQL 'ALTER SYSTEM ARCHIVE LOG CURRENT';
CONFIGURE CONTROLFILE AUTOBACKUP ON;
CONFIGURE CONTROLFILE AUTOBACKUP FORMAT FOR DEVICE TYPE DISK TO '{full_path}/ora_cf%F';
CONFIGURE SNAPSHOT CONTROLFILE NAME TO '{full_path}/snapcf_{oracle_sid}_{file_name}.f';
CONFIGURE DEVICE TYPE DISK PARALLELISM {parallelism};
BACKUP AS COMPRESSED BACKUPSET FULL DATABASE
  TAG 'DATABASE_{file_name}'
  FORMAT '{full_path}/data_%d_%I_%s_%T_%U.rman'
  PLUS ARCHIVELOG
  TAG 'ARCHIVELOG_{file_name}'
  FORMAT '{full_path}/arch_%d_%I_%s_%T_%U.arch';
BACKUP AS COMPRESSED BACKUPSET CURRENT CONTROLFILE
  FORMAT '{full_path}/controlfile_{file_name}';
{archivelog_deletion_cmd}
QUIT;
"""
        if dry_run:
            logger.info(f"[DRY-RUN] Would execute RMAN script:\n{rman_script}")
        else:
            run_rman(logger, env, rman_script, label="full_backup")

    except Exception as exc:
        error_msg = str(exc)
        logger.error(f"BACKUP FAILED: {error_msg}")
    finally:
        release_lock(pid_file)

    backup_elapsed = time.time() - overall_start
    success_status = "FAILED" if error_msg else "SUCCESS"
    
    # Save Persistent History
    history_record = {
        "run_time": backup_start.strftime("%Y-%m-%d %H:%M:%S"),
        "start_time": backup_start.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "operation": "Backup" if not dry_run else "Backup (Dry-Run)",
        "directory": full_path,
        "duration": format_duration(backup_elapsed),
        "size_gb": f"{get_dir_size_gb(full_path):.1f}" if not error_msg else "0",
        "status": success_status,
        "severity": "INFO" if not error_msg else "ERROR",
        "errors_warnings": error_msg or "None",
        "is_deleted": False,
        "deleted_at": None
    }
    
    if dry_run:
        logger.info(f"[DRY-RUN] Would append history: {history_record}")
    else:
        append_history(history_dir, history_record)

    # STEP 3: Transfer (Rsync/SCP if configured)
    transfer_triggered = False
    transfer_hours = BACKUP_CONFIG.get("transfer_hours", BACKUP_CONFIG.get("rsync_hours", []))
    transfer_method = BACKUP_CONFIG.get("transfer_method", "rsync").lower()

    if not error_msg and hour in transfer_hours:
        transfer_triggered = True
        transfer_start_time = datetime.now()
        transfer_overall_start = time.time()
        try:
            shutil.copy2(log_file, full_path)
            
            remote_base = BACKUP_CONFIG["remote_dest"].split(":")[0]
            remote_path = BACKUP_CONFIG["remote_dest"].split(":")[1]
            remote_full_dest = f"{BACKUP_CONFIG['remote_dest']}/{day_name}"

            if dry_run:
                logger.info(f"[DRY-RUN] Would create directory {remote_path}/{day_name} and transfer to {remote_full_dest} via {transfer_method}")
                transfer_elapsed, avg_speed, attempts = 0.5, 100.0, 1
            else:
                if transfer_method == "scp":
                    # For Windows targets we try ssh mkdir, but ignore errors if it fails, and also try cmd /c mkdir
                    mkdir_cmd = f"mkdir -p {remote_path}/{day_name}"
                    subprocess.run(["ssh", remote_base, mkdir_cmd], timeout=30, stderr=subprocess.DEVNULL)
                    mkdir_win_cmd = f"cmd /c mkdir \"{remote_path}\\{day_name}\""
                    subprocess.run(["ssh", remote_base, mkdir_win_cmd], timeout=30, stderr=subprocess.DEVNULL)
                    
                    transfer_elapsed, avg_speed, attempts, _ = run_scp(logger, full_path, remote_full_dest)
                else:
                    # Use Rsync
                    mkdir_cmd = f"mkdir -p {remote_path}/{day_name}"
                    subprocess.run(["ssh", remote_base, mkdir_cmd], timeout=30)
                    
                    transfer_elapsed, avg_speed, attempts, _ = run_rsync(logger, full_path, remote_full_dest)
            
            transfer_record = {
                "run_time": transfer_start_time.strftime("%Y-%m-%d %H:%M:%S"),
                "start_time": transfer_start_time.strftime("%Y-%m-%d %H:%M:%S"),
                "end_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "operation": transfer_method.capitalize() if not dry_run else f"{transfer_method.capitalize()} (Dry-Run)",
                "directory": remote_full_dest,
                "duration": format_duration(transfer_elapsed),
                "transfer_speed_mbps": round(avg_speed, 2),
                "total_attempts": attempts,
                "size_gb": f"{get_dir_size_gb(full_path):.1f}",
                "status": "SUCCESS",
                "severity": "INFO",
                "remote_backup": True,
                "remote_complete": True,
                "errors_warnings": "None",
                "is_deleted": False,
                "deleted_at": None
            }
            if dry_run:
                logger.info(f"[DRY-RUN] Would append transfer history: {transfer_record}")
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

    # STEP 4: Routine Cleanup
    keep_days = BACKUP_CONFIG.get("keep_days", 7)
    cutoff = time.time() - keep_days * 86400
    for bdir in list_daily_dirs(BACKUP_CONFIG["backup_root"]):
        if os.stat(bdir).st_ctime < cutoff and bdir != daily_dir:
            shutil.rmtree(bdir, ignore_errors=True)
            logger.info(f"Routine cleanup: Removed {bdir}")
            mark_history_deleted(history_dir, bdir)

    # Push Metrics
    if dry_run:
        logger.info("[DRY-RUN] Would push metrics.")
    else:
        push_metrics(logger, MONITORING_CONFIG, oracle_sid, backup_elapsed, free_gb, required_gb, not bool(error_msg))

    # Send Daily Summary
    daily_mail_hour = MAIL_CONFIG.get("daily_mail_hour", 23)
    # Trigger mail if:
    # 1. We just finished a transfer run (implies the last backup of the cycle is done)
    # 2. OR it is the specific daily_mail_hour
    should_send_mail = (transfer_triggered or hour == daily_mail_hour)
    
    if should_send_mail and MAIL_CONFIG.get("enabled"):
        smtp_password = None
        if MAIL_CONFIG.get("use_auth", True):
            if VAULT_CONFIG.get("enabled", True):
                smtp_password = get_vault_secret(VAULT_CONFIG, logger)
            else:
                smtp_password = MAIL_CONFIG.get("smtp_password")
                
        # Use the date when the backup STARTED, in case it crossed midnight
        report_date = backup_start.strftime("%Y-%m-%d")
        send_daily_summary(history_dir, MAIL_CONFIG, smtp_password, logger, target_date=report_date)

    if error_msg:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Oracle RMAN Backup Script")
    parser.add_argument("--dry-run", action="store_true", help="Run the script without executing RMAN, Rsync/SCP, or modifying history.")
    parser.add_argument("--test-mail", action="store_true", help="Send a test email using the configured SMTP settings and exit.")
    args = parser.parse_args()

    main(dry_run=args.dry_run, test_mail=args.test_mail)
