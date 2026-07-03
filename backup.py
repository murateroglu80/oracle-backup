#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Oracle RMAN Backup Script - v7.0.0 (Multi-Instance Edition)

Giriş noktası: argparse + main() orkestrasyon katmanı. Asıl mantık sorumluluk bazlı
`modules/` paketi modüllerinde yaşar (config, connection, secrets, locking, history,
space, rman, transfer, mailing, monitoring, logging_setup, utils).
Yapılandırma (yaml) dosyaları `config/` dizininde tutulur.

Bu sürüm SAF REFACTOR'dur: davranış v6.7.2 ile birebir aynıdır. Fonksiyonel değişiklikler
(multi-instance, SecretsProvider soyutlaması, watchdog, structured logging, --status,
host lock vb.) ayrı commit'lerde gelecek — bkz. oracle-backup-multi-instance-spec.md.

Özellikler:
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
import shutil
import time
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header

from modules.config import load_config
from modules.logging_setup import setup_logging
from modules.connection import get_ssh_client, execute_oracle_sql, run_command_wrapper
from modules.secrets import get_vault_secret, get_vault_db_credentials
from modules.locking import acquire_lock, release_lock
from modules.history import append_history, mark_history_deleted
from modules.space import ensure_free_space, get_dir_size_gb, list_daily_dirs
from modules.rman import check_standby_exists, run_rman
from modules.transfer import run_scp, run_rsync
from modules.monitoring import push_metrics
from modules.mailing import send_daily_summary
from modules.utils import format_duration


def main(config_file="config.yaml", dry_run=False, test_mail=False, test_transfer=False, test_db=False, test_query=None):
    config = load_config(config_file)
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


    if test_query:
        logger.info(f"=== STARTING CUSTOM DB QUERY ===")
        try:
            if not db_creds:
                logger.error("No DB credentials found from Vault. Cannot test DB connection.")
                return
            target_enabled = TARGET_SERVER.get("enabled", False)
            ssh_client_test = None
            if target_enabled:
                ssh_client_test = get_ssh_client(TARGET_SERVER, logger)

            env = {}
            for key, val in ORACLE_CONFIG.items():
                env[key] = str(val)
            oh = ORACLE_CONFIG.get("ORACLE_HOME", "")
            env["PATH"] = f"/usr/sbin:{oh}/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
            env["LD_LIBRARY_PATH"] = f"{oh}/lib:/lib:/usr/lib"

            user = db_creds["username"]
            pwd = db_creds["password"]
            host = db_creds.get("hostname") or db_creds.get("ip")
            db = db_creds.get("db", "")
            conn_str = f'{user}/"{pwd}"@{host}/{db} as sysdba'

            sql = f"SET HEADING ON FEEDBACK ON\n{test_query}\nEXIT;\n"

            logger.info("Executing custom query...")
            status, out, err = execute_oracle_sql(ssh_client_test, conn_str, sql, logger, env_dict=env, quiet=False)
            if status == 0:
                logger.info(f"Query Result:\n{out}")
            else:
                logger.error(f"Query Failed! Exit code {status}.\nOutput: {out}\nError: {err}")

            if ssh_client_test:
                ssh_client_test.close()
        except Exception as e:
            logger.error(f"Query Test encountered an error: {e}")
        return

    if test_db:
        logger.info("=== STARTING DB TEST ===")
        try:
            if not db_creds:
                logger.error("No DB credentials found from Vault. Cannot test DB connection.")
                return
            target_enabled = TARGET_SERVER.get("enabled", False)
            ssh_client_test = None
            if target_enabled:
                ssh_client_test = get_ssh_client(TARGET_SERVER, logger)

            env = {}
            for key, val in ORACLE_CONFIG.items():
                env[key] = str(val)
            oh = ORACLE_CONFIG.get("ORACLE_HOME", "")
            env["PATH"] = f"/usr/sbin:{oh}/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
            env["LD_LIBRARY_PATH"] = f"{oh}/lib:/lib:/usr/lib"

            user = db_creds["username"]
            pwd = db_creds["password"]
            host = db_creds.get("hostname") or db_creds.get("ip")
            db = db_creds.get("db", "")
            conn_str = f'{user}/"{pwd}"@{host}/{db} as sysdba'

            sql = "SET HEADING OFF FEEDBACK OFF PAGESIZE 0\nSELECT sys_context('userenv','db_name') FROM dual;\nEXIT;\n"

            logger.info("Running test query on Database using Vault credentials...")
            status, out, err = execute_oracle_sql(ssh_client_test, conn_str, sql, logger, env_dict=env, quiet=True)
            if status == 0:
                db_name = out.strip()
                logger.info(f"DB Test Successful! Connected to database: {db_name}")
            else:
                logger.error(f"DB Test Failed! Exit code {status}.\nOutput: {out}\nError: {err}")

            if ssh_client_test:
                ssh_client_test.close()
        except Exception as e:
            logger.error(f"DB Test encountered an error: {e}")
        return

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
        now = datetime.now()
        month_name = now.strftime("%b").upper()
        day_name_ddmmyy = now.strftime("%d%m%y")

        full_path = os.path.join(BACKUP_CONFIG["backup_root"], oracle_sid, month_name, day_name_ddmmyy)

        error_msg = None
        backup_start = datetime.now()
        overall_start = time.time()

        free_gb, required_gb = 0, 0

        try:
            # Space Check
            space_ok, free_gb, required_gb = ensure_free_space(logger, ssh_client, env, BACKUP_CONFIG, oracle_sid, db_creds=db_creds, run_rman_fn=run_rman)
            if not space_ok:
                raise RuntimeError("Insufficient disk space on target server.")

            # Create backup directory after space check so it doesn't get cleaned up
            if not dry_run:
                run_command_wrapper(ssh_client, f"mkdir -p {full_path}", logger)

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

                    archivelog_deletion_cmd = f"DELETE NOPROMPT ARCHIVELOG ALL COMPLETED BEFORE 'SYSDATE-{ret_days}' BACKED UP 1 TIMES TO DISK;"
                    if has_standby:
                        archivelog_deletion_cmd = "CONFIGURE ARCHIVELOG DELETION POLICY TO APPLIED ON ALL STANDBY;\n" + archivelog_deletion_cmd
                    else:
                        archivelog_deletion_cmd = "CONFIGURE ARCHIVELOG DELETION POLICY TO NONE;\n" + archivelog_deletion_cmd

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
                run_rman(logger, env, ssh_client, rman_script, label="test_backup" if test_transfer else "full_backup", db_creds=db_creds)

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
                # remote_suffix should be the exact final path: ORACLE_SID/MONTH/DDMMYY
                remote_suffix = f"{oracle_sid}/{month_name}/{day_name_ddmmyy}"
                # parent suffix for creating directories and scp target
                remote_parent_suffix = f"{oracle_sid}/{month_name}"

                remote_dest_parts = BACKUP_CONFIG["remote_dest"].split(":", 1)
                remote_base = remote_dest_parts[0]
                remote_path = remote_dest_parts[1] if len(remote_dest_parts) > 1 else ""

                remote_full_dest = f"{remote_base}:{remote_path}/{remote_suffix}"
                remote_transfer_dest = f"{remote_base}:{remote_path}/{remote_parent_suffix}"

                # Path only (without user@host) for reporting and mkdir
                remote_path_only = f"{remote_path}/{remote_suffix}"
                remote_path_only_parent = f"{remote_path}/{remote_parent_suffix}"

                if dry_run:
                    logger.info(f"[DRY-RUN] Would execute {transfer_method} to {remote_full_dest}")
                    transfer_elapsed, avg_speed, attempts = 0.5, 100.0, 1
                else:
                    os_type = BACKUP_CONFIG.get("os_type", "lin").lower()
                    ssh_prefix = f"ssh -o StrictHostKeyChecking=no {remote_base} "

                    mkdir_success = False
                    for mk_attempt in range(1, 4):
                        if os_type == "win":
                            win_path = remote_path_only_parent.replace("/", "\\")
                            if win_path.startswith("\\") and len(win_path) > 2 and win_path[2] == ":":
                                win_path = win_path[1:]
                            st, out, err = run_command_wrapper(ssh_client, f"{ssh_prefix} cmd /c mkdir \"{win_path}\"", logger, quiet=True)
                        else:
                            st, out, err = run_command_wrapper(ssh_client, f"{ssh_prefix} mkdir -p \"{remote_path_only_parent}\"", logger, quiet=True)

                        # Windows mkdir returns 1 if directory already exists
                        if st == 0 or "already exists" in (out + err).lower() or "zaten var" in (out + err).lower():
                            mkdir_success = True
                            break
                        else:
                            logger.warning(f"Remote directory creation failed (Attempt {mk_attempt}/3). RC={st}, Err={err.strip() or out.strip()}")
                            time.sleep(2)

                    if not mkdir_success:
                        logger.error(f"Failed to create remote directory '{remote_path_only}' after 3 attempts.")

                    if transfer_method == "scp":
                        transfer_elapsed, avg_speed, attempts, _ = run_scp(logger, ssh_client, full_path, remote_transfer_dest, get_dir_size_fn=get_dir_size_gb)
                    else:
                        transfer_elapsed, avg_speed, attempts, _ = run_rsync(logger, ssh_client, full_path, remote_transfer_dest)

                transfer_record = {
                    "run_time": transfer_start_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "start_time": transfer_start_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "end_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "operation": transfer_method.capitalize() if not dry_run else f"{transfer_method.capitalize()} (Dry-Run)",
                    "directory": remote_full_dest,
                    "remote_path_only": remote_path_only,
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
        for bdir in list_daily_dirs(ssh_client, BACKUP_CONFIG["backup_root"], oracle_sid):
            if bdir == full_path:
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
        if not dry_run:
            if db_creds and db_creds.get("username") and db_creds.get("password"):
                user = db_creds["username"]
                pwd = db_creds["password"]
                host = db_creds.get("hostname") or db_creds.get("ip")
                db = db_creds.get("db", "")
                conn_str = f'{user}/"{pwd}"@{host}/{db} as sysdba'
            else:
                conn_str = "/ as sysdba"

            report_sql = """SET HEADING OFF FEEDBACK OFF PAGESIZE 0 LINESIZE 1000
SELECT rj.session_key || '|' ||
       NVL(rj.input_type, '-') || '|' ||
       NVL(rj.status, '-') || '|' ||
       TO_CHAR(rj.start_time, 'DD.MM.YYYY HH24:MI') || '|' ||
       NVL(rj.input_bytes_display, '0') || '|' ||
       NVL(rj.output_bytes_display, '0') || '|' ||
       NVL(rj.time_taken_display, '00:00:00')
FROM (
  SELECT * FROM v$rman_backup_job_details ORDER BY start_time DESC
) rj WHERE rownum <= 10;
EXIT;"""
            status, out, err = execute_oracle_sql(ssh_client, conn_str, report_sql, logger, env_dict=env, quiet=True)
            if status == 0:
                lines = [line.strip() for line in out.splitlines() if '|' in line]
                if lines:
                    rman_report_html = """
                    <h3 style="font-family: Arial, sans-serif; color: #333; margin-bottom: 10px;">Recent RMAN Backup Jobs</h3>
                    <table style="width: 100%; border-collapse: collapse; font-family: Arial, sans-serif; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
                      <thead>
                        <tr style="background-color: #34495e; color: white; text-align: left;">
                          <th style="width: 15%; padding: 12px; border: 1px solid #ddd; text-align: left;">Session</th>
                          <th style="width: 25%; padding: 12px; border: 1px solid #ddd; text-align: left;">Type</th>
                          <th style="width: 10%; padding: 12px; border: 1px solid #ddd; text-align: center;">Status</th>
                          <th style="width: 10%; padding: 12px; border: 1px solid #ddd; text-align: left;">Start Time</th>
                          <th style="width: 15%; padding: 12px; border: 1px solid #ddd; text-align: right;">Read</th>
                          <th style="width: 10%; padding: 12px; border: 1px solid #ddd; text-align: right;">Written</th>
                          <th style="width: 15%; padding: 12px; border: 1px solid #ddd; text-align: right;">Duration</th>
                        </tr>
                      </thead>
                      <tbody>
                    """
                    for i, line in enumerate(lines):
                        parts = [p.strip() for p in line.split('|')]
                        if len(parts) >= 7:
                            bg_color = "#ffffff" if i % 2 == 0 else "#f9f9f9"
                            status_val = parts[2].upper()
                            status_color = "#27ae60" if "COMPLETED" in status_val else ("#e74c3c" if "FAILED" in status_val else "#f39c12")

                            rman_report_html += f"""
                            <tr style="background-color: {bg_color}; border-bottom: 1px solid #ddd; font-size: 14px;">
                                <td style="padding: 10px; border: 1px solid #eee;">{parts[0]}</td>
                                <td style="padding: 10px; border: 1px solid #eee;">{parts[1]}</td>
                                <td style="padding: 10px; border: 1px solid #eee; text-align: center; font-weight: bold; color: {status_color};">{parts[2]}</td>
                                <td style="padding: 10px; border: 1px solid #eee;">{parts[3]}</td>
                                <td style="padding: 10px; border: 1px solid #eee; text-align: right;">{parts[4]}</td>
                                <td style="padding: 10px; border: 1px solid #eee; text-align: right;">{parts[5]}</td>
                                <td style="padding: 10px; border: 1px solid #eee; text-align: right;">{parts[6]}</td>
                            </tr>
                            """
                    rman_report_html += "</tbody></table>"

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
    parser = argparse.ArgumentParser(description="Oracle RMAN Backup Script (Multi-Instance Edition)")
    parser.add_argument("--config", default="config.yaml", help="Path to the main configuration file.")
    parser.add_argument("--dry-run", action="store_true", help="Run the script without executing RMAN, Rsync/SCP, or modifying history.")
    parser.add_argument("--test-mail", action="store_true", help="Send a test email using the configured SMTP settings and exit.")
    parser.add_argument("--test-transfer", action="store_true", help="Run a quick backup of only the control file and transfer it via SCP/Rsync to test the remote connection.")
    parser.add_argument("--test-db", action="store_true", help="Run a test query against the database using Vault credentials and exit.")
    parser.add_argument("--test-query", type=str, help="Run a custom SQL query against the database and exit (e.g. --test-query \"SELECT * FROM v$database;\")")
    args = parser.parse_args()

    main(config_file=args.config, dry_run=args.dry_run, test_mail=args.test_mail, test_transfer=args.test_transfer, test_db=args.test_db, test_query=args.test_query)
