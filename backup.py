#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Oracle RMAN Backup Script - v7.0.0 (Multi-Instance Edition)

Giriş noktası: argparse + main() orkestrasyon katmanı. Asıl mantık sorumluluk bazlı
`modules/` paketi modüllerinde yaşar (config, connection, secrets, locking, history,
space, rman, transfer, mailing, monitoring, logging_setup, status, utils).
Yapılandırma (yaml) dosyaları `config/`, secrets dosyaları `secrets/` dizininde tutulur.

Multi-Instance özellikleri (bkz. oracle-backup-multi-instance-spec.md):
  - instance_id (host+SID) otomatik türetme + log/history/pid path namespacing
  - Modüler SecretsProvider (Vault/Local/Null/CyberArk)
  - History dosya kilidi (fcntl.flock) + atomic write + run_id + versiyonlu şema
  - Host bazlı lock (aynı host'a eşzamanlı yedekleri serileştirir)
  - Canlılık bazlı watchdog (RMAN/transfer stall tespiti)
  - JSONL structured log (vektör-DB'ye hazır)
  - --status fleet özet modu
"""

import os
import sys
import json
import argparse
import shutil
import time
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header

from modules.config import load_config
from modules.logging_setup import setup_logging
from modules.connection import get_ssh_client, execute_oracle_sql, run_command_wrapper
from modules.secrets import get_secrets_provider
from modules.locking import acquire_lock, release_lock, acquire_host_lock, release_host_lock
from modules.history import (append_history, mark_history_deleted, generate_run_id, BackupRecord,
                             get_history_file)
from modules.space import ensure_free_space, get_dir_size_gb, list_daily_dirs
from modules.rman import check_standby_exists, run_rman, make_rman_progress_check
from modules.transfer import (run_scp, run_rsync, build_remote_paths, verify_remote_backup,
                              send_backup_dir)
from modules.monitoring import push_metrics
from modules.mailing import send_daily_summary
from modules.status import collect_fleet_status, format_status_table, STATUS_STALE_HOURS_DEFAULT
from modules.utils import format_duration

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _backup_env(oracle_config, temp_dir=None):
    env = {}
    for key, val in oracle_config.items():
        env[key] = str(val)
    oh = oracle_config.get("ORACLE_HOME", "")
    env["PATH"] = f"/usr/sbin:{oh}/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
    env["LD_LIBRARY_PATH"] = f"{oh}/lib:/lib:/usr/lib"
    env["CLASSPATH"] = f"{oh}/JRE:{oh}/jlib:{oh}/rdbms/jlib"
    # TMP/TMPDIR ARTIK override EDİLMEZ: target sunucu kendi OS temp'ini (TMPDIR ya da /tmp)
    # kullanır. Aksi halde jump'taki config temp_dir'i target'a taşınır ve orada olmayan bir
    # path'e mktemp yapılmaya çalışılırdı. (temp_dir parametresi çağrı uyumu için korunur.)
    return env


def _db_conn_str(db_creds, sysdba=True):
    user = db_creds["username"]
    pwd = db_creds["password"]
    host = db_creds.get("hostname") or db_creds.get("ip")
    # DB/service adı: Vault secret'ında field adı kuruma göre değişebilir
    # (db / name / db_name hepsi kabul edilir — hangisi doluysa o kullanılır).
    db = db_creds.get("db") or db_creds.get("name") or db_creds.get("db_name") or ""
    return f'{user}/"{pwd}"@{host}/{db}{" as sysdba" if sysdba else ""}'


def run_status_mode(stale_hours=STATUS_STALE_HOURS_DEFAULT):
    """--status: salt-okunur fleet durum özeti. Exit: FAILED->1, stale->2, hepsi SUCCESS->0."""
    rows = collect_fleet_status(SCRIPT_DIR, stale_hours=stale_hours)
    print(format_status_table(rows))
    if any(r["failed"] for r in rows):
        return 1
    if any(r["stale"] for r in rows):
        return 2
    return 0


def run_clear_logs(config_file, assume_yes=False):
    """--clear-logs: YALNIZCA ilgili instance'ın log_dir'indeki log dosyalarını siler.

    GÜVENLİK: history_dir, pid_file, yedek (backup) dosyalarına ve alt dizinlere DOKUNMAZ.
    Sadece log_dir'in DOĞRUDAN içindeki `backup_*.log` / `backup_*.jsonl` dosyaları (ve
    backup_latest.log symlink'i) hedeflenir. Silmeden önce listeler ve onay ister
    (--yes ile onaysız). Log dizini yoksa / boşsa hiçbir şey yapmaz.
    """
    import glob

    config = load_config(config_file)
    log_dir = os.path.abspath(config["_resolved_paths"]["log_dir"])
    history_dir = os.path.abspath(config["_resolved_paths"]["history_dir"])

    # Ekstra güvenlik: log_dir ile history_dir yanlışlıkla aynıysa DUR (history'yi silme riski).
    if log_dir == history_dir:
        print(f"[CLEAR-LOGS] GÜVENLİK: log_dir ile history_dir aynı ({log_dir}). "
              f"History kaybı riski nedeniyle iptal edildi.")
        return 1

    if not os.path.isdir(log_dir):
        print(f"[CLEAR-LOGS] Log dizini yok, yapılacak bir şey yok: {log_dir}")
        return 0

    targets = []
    for pat in ("backup_*.log", "backup_*.jsonl"):
        for p in glob.glob(os.path.join(log_dir, pat)):
            # Sadece log_dir'in doğrudan çocukları (alt dizine inme, path traversal engeli).
            if os.path.dirname(os.path.abspath(p)) != log_dir:
                continue
            if os.path.isfile(p) or os.path.islink(p):
                targets.append(p)
    targets = sorted(set(targets))

    if not targets:
        print(f"[CLEAR-LOGS] Silinecek log dosyası yok: {log_dir}")
        return 0

    print(f"[CLEAR-LOGS] Log dizini : {log_dir}")
    print(f"[CLEAR-LOGS] Silinecek {len(targets)} dosya (history/backup KORUNUR):")
    for t in targets:
        print(f"    - {os.path.basename(t)}")

    if not assume_yes:
        try:
            resp = input("[CLEAR-LOGS] Onaylıyor musunuz? (yes/no): ").strip().lower()
        except EOFError:
            resp = ""
        if resp not in ("yes", "y", "evet", "e"):
            print("[CLEAR-LOGS] İptal edildi — hiçbir şey silinmedi.")
            return 1

    deleted = 0
    for t in targets:
        try:
            os.remove(t)
            deleted += 1
        except Exception as e:
            print(f"[CLEAR-LOGS] Silinemedi: {os.path.basename(t)} ({e})")

    print(f"[CLEAR-LOGS] {deleted}/{len(targets)} log dosyası silindi. History ve yedekler korundu.")
    return 0


def _find_last_successful_backup_dir(history_dir, oracle_sid):
    """History'den bu instance'a ait en yeni BAŞARILI backup klasörünü (`directory`) döner; yoksa None.

    Bu ay + önceki ay dosyaları okunur (ay sınırında son yedek geçen ayda olabilir). Yalnızca
    operation=='Backup', status=='SUCCESS', silinmemiş ve db_name eşleşen kayıtlar; run_time'a göre en yeni.
    """
    records = []
    seen = set()
    for d in (datetime.now(), datetime.now() - timedelta(days=31)):
        hf = get_history_file(history_dir, d)
        if hf in seen:
            continue
        seen.add(hf)
        if os.path.exists(hf):
            try:
                with open(hf, "r") as f:
                    records.extend(json.load(f))
            except Exception:
                pass
    candidates = [
        r for r in records
        if r.get("operation") == "Backup" and str(r.get("status", "")).upper() == "SUCCESS"
        and not r.get("is_deleted") and r.get("db_name", oracle_sid) == oracle_sid
        and r.get("directory") and r.get("directory") != "-"
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda r: r.get("run_time", ""))
    return candidates[-1]["directory"]


def resend_backup(logger, ssh_client, backup_config, oracle_sid, local_full_path, run_id,
                  history_dir, watchdog=None, dry_run=False, context_label="manual"):
    """Bir yedek klasörünün uzak hedefe TAM geçtiğini doğrular; eksik/bozuksa yeniden gönderir.

    Akış: verify → tamsa çık (gönderme yok) → değilse eksikleri logla, gönder, RE-VERIFY, transfer
    `BackupRecord` yaz (operation '<method> (resend)'). Döner: True (uzak tam) / False (hâlâ eksik).
    Pre-backup kancası ve `--resend` modu ikisi de bunu kullanır.
    """
    # local_full_path: {backup_root}/{SID}/{MONTH}/{DDMMYY} → month_name/ddmmyy türet.
    ddmmyy = os.path.basename(local_full_path.rstrip("/"))
    month_name = os.path.basename(os.path.dirname(local_full_path.rstrip("/")))
    paths = build_remote_paths(backup_config, oracle_sid, month_name, ddmmyy)

    v = verify_remote_backup(logger, ssh_client, backup_config, paths, local_full_path)
    if v["local_count"] == 0:
        logger.warning(f"[{context_label}] Yerel yedek klasöründe dosya yok veya erişilemedi: {local_full_path}")
        return False
    if v["ok"]:
        logger.info(f"[{context_label}] Son yedek uzak hedefte zaten tam ({v['local_count']} dosya): {paths['remote_path_only']}")
        return True

    logger.warning(f"[{context_label}] Uzak yedek EKSİK: {len(v['missing'])} eksik, "
                   f"{len(v['mismatched'])} boyut-uyumsuz (yerel {v['local_count']} / uzak {v['remote_count']}). "
                   f"Yeniden gönderiliyor: {local_full_path} → {paths['remote_transfer_dest']}")
    if v["missing"]:
        logger.info(f"[{context_label}] Eksik dosyalar: {', '.join(v['missing'][:20])}"
                    + (" ..." if len(v['missing']) > 20 else ""))
    if v["mismatched"]:
        logger.info(f"[{context_label}] Boyut-uyumsuz dosyalar: {', '.join(v['mismatched'][:20])}"
                    + (" ..." if len(v['mismatched']) > 20 else ""))

    if dry_run:
        logger.info(f"[DRY-RUN][{context_label}] Would resend to {paths['remote_transfer_dest']}")
        return False

    start = datetime.now()
    start_t = time.time()
    method = backup_config.get("transfer_method", "rsync").lower()
    try:
        method, elapsed, avg_speed, attempts = send_backup_dir(
            logger, ssh_client, backup_config, paths, local_full_path,
            watchdog=watchdog, get_dir_size_fn=get_dir_size_gb)
        # Gönderim sonrası tekrar doğrula (kısmi/yeni eksik kalmadığından emin ol).
        v2 = verify_remote_backup(logger, ssh_client, backup_config, paths, local_full_path)
        resend_ok = v2["ok"]
        if resend_ok:
            logger.info(f"[{context_label}] Yeniden gönderim tamamlandı ve doğrulandı ({v2['local_count']} dosya).")
        else:
            logger.error(f"[{context_label}] Yeniden gönderim sonrası hâlâ eksik: "
                         f"{len(v2['missing'])} eksik, {len(v2['mismatched'])} boyut-uyumsuz.")
        append_history(history_dir, BackupRecord(
            run_time=start.strftime("%Y-%m-%d %H:%M:%S"),
            start_time=start.strftime("%Y-%m-%d %H:%M:%S"),
            end_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            operation=f"{method.capitalize()} (resend)",
            directory=paths["remote_full_dest"],
            duration=format_duration(elapsed),
            size_gb=f"{get_dir_size_gb(ssh_client, local_full_path):.1f}",
            status="SUCCESS" if resend_ok else "FAILED",
            severity="INFO" if resend_ok else "ERROR",
            run_id=run_id,
            db_name=oracle_sid,
            remote_path_only=paths["remote_path_only"],
            transfer_speed_mbps=round(avg_speed, 2),
            total_attempts=attempts,
            remote_backup=True,
            remote_complete=resend_ok,
            remote_fail_desc=None if resend_ok else f"{len(v2['missing'])} missing / {len(v2['mismatched'])} size-mismatch after resend",
        ))
        return resend_ok
    except Exception as e:
        logger.error(f"[{context_label}] Yeniden gönderim başarısız: {e}")
        append_history(history_dir, BackupRecord(
            run_time=start.strftime("%Y-%m-%d %H:%M:%S"),
            start_time=start.strftime("%Y-%m-%d %H:%M:%S"),
            end_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            operation=f"{method.capitalize()} (resend)",
            directory=paths["remote_full_dest"],
            duration=format_duration(time.time() - start_t),
            size_gb="0",
            status="FAILED",
            severity="ERROR",
            run_id=run_id,
            db_name=oracle_sid,
            errors_warnings=str(e),
            remote_path_only=paths["remote_path_only"],
            remote_backup=True,
            remote_complete=False,
            remote_fail_desc=str(e),
        ))
        return False


def main(config_file="config.yaml", dry_run=False, test_mail=False, test_transfer=False, test_db=False, test_query=None, show_command=False, resend=None):
    config = load_config(config_file)
    TARGET_SERVER = config.get("TARGET_SERVER", {})
    ORACLE_CONFIG = config.get("ORACLE_CONFIG", {})
    BACKUP_CONFIG = config.get("BACKUP_CONFIG", {})
    MAIL_CONFIG = config.get("MAIL_CONFIG", {})
    CREDENTIALS_CONFIG = config.get("CREDENTIALS_CONFIG", {"enabled": False, "provider": "none"})
    MONITORING_CONFIG = config.get("MONITORING_CONFIG", {})

    # instance_id + resolved path'ler tek kaynaktan (spec §1, §3, §3.1)
    instance_id = config["_resolved_instance_id"]
    oracle_sid = ORACLE_CONFIG.get("ORACLE_SID", "")  # history db_name + mail filtre anahtarı
    paths = config["_resolved_paths"]
    log_dir, history_dir, pid_file, temp_dir = paths["log_dir"], paths["history_dir"], paths["pid_file"], paths["temp_dir"]

    os.makedirs(log_dir, exist_ok=True)
    os.makedirs(history_dir, exist_ok=True)

    now  = datetime.now()
    hour = now.hour
    day_name  = now.strftime("%d%b%Y").upper()
    file_name = now.strftime("%d%b%y%H").upper()
    run_id = generate_run_id()

    log_file = os.path.join(log_dir, f"backup_{file_name}.log")
    jsonl_file = os.path.join(log_dir, f"backup_{file_name}.jsonl")

    # Tanılama/interaktif modlar sonuçlarını logger.info ile ekrana basar → konsol INFO kalmalı.
    # Normal yedek çalışması: --show-command yoksa ekran yalnızca WARNING/ERROR (her şey log'da).
    diagnostic = bool(dry_run or test_transfer or test_mail or test_db or test_query)
    if dry_run or test_transfer:
        logger = setup_logging(os.path.join(log_dir, "backup_test.log"),
                               jsonl_file=os.path.join(log_dir, "backup_test.jsonl"),
                               instance_id=instance_id, run_id=run_id,
                               show_command=show_command, diagnostic=diagnostic)
    else:
        logger = setup_logging(log_file, jsonl_file=jsonl_file, instance_id=instance_id, run_id=run_id,
                               show_command=show_command, diagnostic=diagnostic)
        latest_link = os.path.join(log_dir, "backup_latest.log")
        try:
            if os.path.exists(latest_link) or os.path.islink(latest_link):
                os.remove(latest_link)
            os.symlink(log_file, latest_link)
        except Exception:
            pass

    logger.info(f"Resolved instance_id: {instance_id}  (run_id={run_id})")
    if dry_run:
        logger.info("=== STARTING IN DRY-RUN MODE ===")

    # Secrets provider (GLOBAL) — main yalnızca get_db_credentials/get_smtp_password bilir.
    secrets_provider = get_secrets_provider(CREDENTIALS_CONFIG, SCRIPT_DIR, logger)
    db_creds = secrets_provider.get_db_credentials(instance_id)
    if test_transfer:
        logger.info("=== STARTING TEST TRANSFER MODE ===")

    if test_query:
        logger.info(f"=== STARTING CUSTOM DB QUERY ===")
        try:
            if not db_creds:
                logger.error("No DB credentials found from secrets provider. Cannot test DB connection.")
                return
            ssh_client_test = get_ssh_client(TARGET_SERVER, logger) if TARGET_SERVER.get("enabled", False) else None
            env = _backup_env(ORACLE_CONFIG, temp_dir)
            conn_str = _db_conn_str(db_creds, sysdba=True)
            sql = f"SET HEADING ON FEEDBACK ON\n{test_query}\nEXIT;\n"
            logger.info("Executing custom query...")
            status, out, err = execute_oracle_sql(ssh_client_test, conn_str, sql, logger, env_dict=env, temp_dir=temp_dir, quiet=False)
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
                logger.error("No DB credentials found from secrets provider. Cannot test DB connection.")
                return
            ssh_client_test = get_ssh_client(TARGET_SERVER, logger) if TARGET_SERVER.get("enabled", False) else None
            env = _backup_env(ORACLE_CONFIG, temp_dir)
            conn_str = _db_conn_str(db_creds, sysdba=True)
            sql = "SET HEADING OFF FEEDBACK OFF PAGESIZE 0\nSELECT sys_context('userenv','db_name') FROM dual;\nEXIT;\n"
            logger.info("Running test query on Database using secrets-provider credentials...")
            status, out, err = execute_oracle_sql(ssh_client_test, conn_str, sql, logger, env_dict=env, temp_dir=temp_dir, quiet=True)
            db_name = out.strip()
            # status==0 tek başına yeterli DEĞİL: sqlplus bozuk connect string'de usage/help basıp
            # exit 0 dönebilir. Çıktının gerçekten bir db_name olduğunu doğrula (aksi halde
            # bağlanmadığı halde 'Successful' raporlanır — false positive).
            bad_markers = ("SQL*Plus", "Usage:", "ERROR", "ORA-", "TNS-", "SP2-")
            looks_valid = bool(db_name) and not any(m in db_name for m in bad_markers)
            if status == 0 and looks_valid:
                # db_name genelde tek satır; ilk anlamlı satırı al.
                first_line = next((ln.strip() for ln in db_name.splitlines() if ln.strip()), db_name)
                logger.info(f"DB Test Successful! Connected to database: {first_line}")
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
                smtp_password = secrets_provider.get_smtp_password(instance_id) or MAIL_CONFIG.get("smtp_password")
            try:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = Header(f"{MAIL_CONFIG['subject_prefix']} [TEST] Mail Configuration", "utf-8")
                msg["From"]    = MAIL_CONFIG["from_addr"]
                msg["To"]      = ", ".join(MAIL_CONFIG["to_addrs"])
                msg.attach(MIMEText("<html><body><h3>SMTP Test Successful</h3><p>If you see this, your SMTP and secrets settings are correct.</p></body></html>", "html", "utf-8"))
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

    # --- Instance kilidi (pid_file) ---
    locked, pid = acquire_lock(pid_file)
    if not locked:
        logger.error("Another backup process is running.")
        # SKIPPED kaydı (spec §11.1 kural 6) — --status bunu 'atlanmış run' gösterebilsin.
        skip_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        append_history(history_dir, BackupRecord(
            run_time=skip_ts, start_time=skip_ts, end_time=skip_ts, operation="Backup",
            directory="-", duration="0m 00s", size_gb="0", status="SKIPPED", severity="WARNING",
            run_id=run_id, db_name=oracle_sid, errors_warnings=f"Lock held by PID {pid}; run skipped.",
        ))
        sys.exit(2)

    ssh_client = None
    host_lock = None
    try:
        target_enabled = TARGET_SERVER.get("enabled", False)
        if target_enabled:
            ssh_client = get_ssh_client(TARGET_SERVER, logger)
        else:
            logger.info("TARGET_SERVER is disabled. Running all commands LOCALLY.")

        env = _backup_env(ORACLE_CONFIG, temp_dir)

        # --- Host bazlı lock (spec §8.2): aynı host'a yedekleri serileştir ---
        watchdog_cfg = BACKUP_CONFIG.get("watchdog", {})
        host_key = TARGET_SERVER.get("host") if target_enabled else "local"
        if BACKUP_CONFIG.get("host_lock_enabled", True):
            host_lock = acquire_host_lock(temp_dir, host_key, logger,
                                          timeout_min=BACKUP_CONFIG.get("host_lock_timeout_min", 120))
            if host_lock is None:
                fail_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                append_history(history_dir, BackupRecord(
                    run_time=fail_ts, start_time=fail_ts, end_time=fail_ts, operation="Backup",
                    directory="-", duration="0m 00s", size_gb="0", status="FAILED", severity="ERROR",
                    run_id=run_id, db_name=oracle_sid, errors_warnings=f"Host lock timeout for '{host_key}'.",
                ))
                sys.exit(1)

        # --- RESEND modu: bir yedeği uza doğrula + eksikleri gönder ve çık (RMAN backup YOK) ---
        if resend is not None:
            if not BACKUP_CONFIG.get("remote_dest"):
                logger.error("--resend: remote_dest yapılandırılmamış, yapılacak bir şey yok.")
                sys.exit(1)
            if resend == "last":
                target_dir = _find_last_successful_backup_dir(history_dir, oracle_sid)
                if not target_dir:
                    logger.error("--resend: başarılı bir backup kaydı bulunamadı.")
                    sys.exit(1)
            elif "/" in resend or "\\" in resend:
                target_dir = resend  # doğrudan yol (target sunucudaki tam yol)
            else:
                # DDMMYY klasör adı → target'taki yedek dizinlerini tara (mtime artan → en yeni son)
                matches = [d for d in list_daily_dirs(ssh_client, BACKUP_CONFIG["backup_root"], oracle_sid)
                           if os.path.basename(d.rstrip("/")) == resend]
                if not matches:
                    logger.error(f"--resend: '{resend}' adına uyan yedek klasörü bulunamadı (backup_root altında).")
                    sys.exit(1)
                target_dir = matches[-1]
            logger.info(f"=== RESEND MODE === hedef klasör: {target_dir}")
            ok = resend_backup(logger, ssh_client, BACKUP_CONFIG, oracle_sid, target_dir, run_id,
                               history_dir, watchdog=watchdog_cfg, dry_run=dry_run, context_label="manual")
            sys.exit(0 if ok else 1)

        # --- Pre-backup dayanıklılık kontrolü: yeni backup'tan ÖNCE son yedeğin uzak hedefe TAM
        # geçtiğini doğrula, eksik/bozuksa yeniden gönder. Başarısızlık yeni backup'ı BLOKLAMAZ
        # (kullanıcı kararı) — tüm blok try/except ile sarılı, hata → WARNING + devam. Host lock
        # zaten alındığı için eşzamanlı RMAN transfer'ıyla yarışmaz.
        if (BACKUP_CONFIG.get("pre_backup_resend_enabled", True)
                and BACKUP_CONFIG.get("remote_dest")
                and not dry_run and not test_transfer):
            try:
                last_dir = _find_last_successful_backup_dir(history_dir, oracle_sid)
                if last_dir:
                    resend_backup(logger, ssh_client, BACKUP_CONFIG, oracle_sid, last_dir, run_id,
                                  history_dir, watchdog=watchdog_cfg, dry_run=dry_run, context_label="pre-backup")
                else:
                    logger.info("Pre-backup: doğrulanacak önceki başarılı backup yok, atlanıyor.")
            except Exception as e:
                logger.warning(f"Pre-backup resend kontrolü hata verdi, yeni backup'a devam ediliyor: {e}")

        # Watchdog DB progress check (Sinyal 2) + run_rman sarmalayıcısı (temp_dir/watchdog inject)
        progress_check_fn = None
        if watchdog_cfg.get("progress_check_enabled", True):
            progress_check_fn = make_rman_progress_check(ssh_client, env, db_creds, logger, temp_dir,
                                                          watchdog_cfg=watchdog_cfg)

        def run_rman_fn(logger_, env_, ssh_, script_, label="rman", db_creds=None):
            return run_rman(logger_, env_, ssh_, script_, label=label, db_creds=db_creds,
                            temp_dir=temp_dir, watchdog=watchdog_cfg, progress_check_fn=progress_check_fn)

        now = datetime.now()
        month_name = now.strftime("%b").upper()
        day_name_ddmmyy = now.strftime("%d%m%y")
        full_path = os.path.join(BACKUP_CONFIG["backup_root"], oracle_sid, month_name, day_name_ddmmyy)

        error_msg = None
        backup_start = datetime.now()
        overall_start = time.time()
        rman_components = ""  # history'e yazılacak açık RMAN bileşenleri (mailde gösterilmez);
                              # RMAN üretim dalında doldurulur, blok atlanırsa boş kalır (garanti tanım).
        free_gb, required_gb = 0, 0

        try:
            # Space Check (resolved history_dir parametreyle — spec §3.1)
            space_ok, free_gb, required_gb = ensure_free_space(
                logger, ssh_client, env, BACKUP_CONFIG, oracle_sid, history_dir,
                db_creds=db_creds, run_rman_fn=run_rman_fn)
            if not space_ok:
                raise RuntimeError("Insufficient disk space on target server.")

            if not dry_run:
                run_command_wrapper(ssh_client, f"mkdir -p {full_path}", logger)

            # RMAN Backup
            parallelism = BACKUP_CONFIG.get("parallelism", 1)
            device_type = BACKUP_CONFIG.get("device_type", "DISK").upper()
            rman_script_file = BACKUP_CONFIG.get("rman_script_file", "")
            RMAN_TEMPLATE = config.get("RMAN_TEMPLATE", {})

            rman_script = None
            if rman_script_file:
                if not os.path.isabs(rman_script_file):
                    rman_script_file = os.path.join(SCRIPT_DIR, rman_script_file)
                if os.path.exists(rman_script_file):
                    logger.info(f"Using custom RMAN script from: {rman_script_file}")
                    with open(rman_script_file, "r") as f:
                        rman_script = f.read()
                    rman_components = "custom"
                else:
                    logger.warning(f"Custom RMAN script file '{rman_script_file}' not found. Falling back to RMAN_TEMPLATE.")

            if not rman_script:
                if test_transfer:
                    rman_components = "controlfile"
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
                    has_standby = check_standby_exists(logger, env, ssh_client, db_creds, temp_dir=temp_dir)
                    cleanup = RMAN_TEMPLATE.get("cleanup", {})
                    ret_days = cleanup.get("archive_retention_days", 2)
                    recovery_window = cleanup.get("recovery_window_days", 1)

                    def is_true(val):
                        if isinstance(val, str):
                            return val.lower() in ('true', 'yes', '1', 'on')
                        return bool(val)

                    # Açık RMAN bileşenlerini history için topla (mailde gösterilmez).
                    rman_components = ",".join(
                        name for name, key in (
                            ("full", "full_backup"),
                            ("archive", "archive_backup"),
                            ("controlfile", "controlfile_backup"),
                            ("spfile", "spfile_backup"),
                        ) if is_true(RMAN_TEMPLATE.get(key, True))
                    )

                    archivelog_deletion_cmd = f"DELETE NOPROMPT ARCHIVELOG ALL COMPLETED BEFORE 'SYSDATE-{ret_days}' BACKED UP 1 TIMES TO DISK;"
                    if has_standby:
                        archivelog_deletion_cmd = "CONFIGURE ARCHIVELOG DELETION POLICY TO APPLIED ON ALL STANDBY;\n" + archivelog_deletion_cmd
                    else:
                        archivelog_deletion_cmd = "CONFIGURE ARCHIVELOG DELETION POLICY TO NONE;\n" + archivelog_deletion_cmd

                    if not is_true(RMAN_TEMPLATE.get("full_backup", True)) and not is_true(RMAN_TEMPLATE.get("archive_backup", True)):
                        logger.info("Only controlfile/SPFILE backup requested. Forcing parallelism to 1.")
                        parallelism = 1

                    allocate_cmds = ""
                    release_cmds = ""
                    for i in range(1, parallelism + 1):
                        allocate_cmds += f"  ALLOCATE CHANNEL c{i} TYPE {device_type};\n"
                        release_cmds += f"  RELEASE CHANNEL c{i};\n"

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

                    spfile_cmd = ""
                    if is_true(RMAN_TEMPLATE.get("spfile_backup", True)):
                        spfile_cmd = f"""
  BACKUP SPFILE
    TAG 'SPFILE_{file_name}'
    FORMAT '{full_path}/Spfile_%d_%I_%s_%T_%U.rman';
"""

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
                run_rman_fn(logger, env, ssh_client, rman_script, label="test_backup" if test_transfer else "full_backup", db_creds=db_creds)

        except Exception as exc:
            error_msg = str(exc)
            logger.error(f"BACKUP FAILED: {error_msg}")

        backup_elapsed = time.time() - overall_start
        success_status = "FAILED" if error_msg else "SUCCESS"

        history_record = BackupRecord(
            run_time=backup_start.strftime("%Y-%m-%d %H:%M:%S"),
            start_time=backup_start.strftime("%Y-%m-%d %H:%M:%S"),
            end_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            operation="Backup" if not dry_run else "Backup (Dry-Run)",
            directory=full_path,
            duration=format_duration(backup_elapsed),
            size_gb=f"{get_dir_size_gb(ssh_client, full_path):.1f}" if not error_msg else "0",
            status=success_status,
            severity="INFO" if not error_msg else "ERROR",
            run_id=run_id,
            db_name=oracle_sid,
            rman_components=rman_components,
            errors_warnings=error_msg or "None",
        )

        if dry_run:
            logger.info(f"[DRY-RUN] Would append history locally: {history_record.to_dict()}")
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

        if not error_msg and (is_transfer_hour or test_transfer):
            transfer_triggered = True
            transfer_start_time = datetime.now()
            transfer_overall_start = time.time()
            try:
                # Uzak yol üretimi tek kaynaktan (transfer.build_remote_paths) — verify/resend ile uyumlu.
                paths = build_remote_paths(BACKUP_CONFIG, oracle_sid, month_name, day_name_ddmmyy)
                remote_full_dest = paths["remote_full_dest"]
                remote_path_only = paths["remote_path_only"]

                if dry_run:
                    logger.info(f"[DRY-RUN] Would execute {transfer_method} to {remote_full_dest}")
                    transfer_elapsed, avg_speed, attempts = 0.5, 100.0, 1
                else:
                    _method, transfer_elapsed, avg_speed, attempts = send_backup_dir(
                        logger, ssh_client, BACKUP_CONFIG, paths, full_path,
                        watchdog=watchdog_cfg, get_dir_size_fn=get_dir_size_gb)

                transfer_record = BackupRecord(
                    run_time=transfer_start_time.strftime("%Y-%m-%d %H:%M:%S"),
                    start_time=transfer_start_time.strftime("%Y-%m-%d %H:%M:%S"),
                    end_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    operation=transfer_method.capitalize() if not dry_run else f"{transfer_method.capitalize()} (Dry-Run)",
                    directory=remote_full_dest,
                    duration=format_duration(transfer_elapsed),
                    size_gb=f"{get_dir_size_gb(ssh_client, full_path):.1f}",
                    status="SUCCESS",
                    severity="INFO",
                    run_id=run_id,
                    db_name=oracle_sid,
                    remote_path_only=remote_path_only,
                    transfer_speed_mbps=round(avg_speed, 2),
                    total_attempts=attempts,
                    remote_backup=True,
                    remote_complete=True,
                )
                if dry_run:
                    logger.info(f"[DRY-RUN] Would append transfer history locally: {transfer_record.to_dict()}")
                else:
                    append_history(history_dir, transfer_record)
            except Exception as e:
                append_history(history_dir, BackupRecord(
                    run_time=transfer_start_time.strftime("%Y-%m-%d %H:%M:%S"),
                    start_time=transfer_start_time.strftime("%Y-%m-%d %H:%M:%S"),
                    end_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    operation=transfer_method.capitalize(),
                    directory="N/A",
                    duration=format_duration(time.time() - transfer_overall_start),
                    size_gb="0",
                    status="FAILED",
                    severity="ERROR",
                    run_id=run_id,
                    db_name=oracle_sid,
                    errors_warnings=str(e),
                    remote_backup=True,
                    remote_complete=False,
                    remote_fail_desc=str(e),
                ))

        # Host lock RMAN+transfer sonrası bırakılır (mail/rapor disk yarışına girmez, spec §8.2)
        if host_lock is not None:
            release_host_lock(host_lock)
            host_lock = None

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
                conn_str = _db_conn_str(db_creds, sysdba=True)
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
            status, out, err = execute_oracle_sql(ssh_client, conn_str, report_sql, logger, env_dict=env, temp_dir=temp_dir, quiet=True)
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
                smtp_password = secrets_provider.get_smtp_password(instance_id) or MAIL_CONFIG.get("smtp_password")
            report_date = backup_start.strftime("%Y-%m-%d")
            send_daily_summary(history_dir, MAIL_CONFIG, smtp_password, logger, target_date=report_date, target_server=TARGET_SERVER, oracle_config=ORACLE_CONFIG, backup_config=BACKUP_CONFIG, rman_report_html=rman_report_html, db_name=oracle_sid)

        if error_msg:
            sys.exit(1)

    finally:
        if host_lock is not None:
            release_host_lock(host_lock)
        if ssh_client:
            ssh_client.close()
        release_lock(pid_file)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Oracle RMAN Backup Script (Multi-Instance Edition)")
    parser.add_argument("--config", default="config.yaml", help="Path to the main configuration file.")
    parser.add_argument("--dry-run", action="store_true", help="Run the script without executing RMAN, Rsync/SCP, or modifying history.")
    parser.add_argument("--test-mail", action="store_true", help="Send a test email using the configured SMTP settings and exit.")
    parser.add_argument("--test-transfer", action="store_true", help="Run a quick backup of only the control file and transfer it via SCP/Rsync to test the remote connection.")
    parser.add_argument("--test-db", action="store_true", help="Run a test query against the database using secrets-provider credentials and exit.")
    parser.add_argument("--test-query", type=str, help="Run a custom SQL query against the database and exit (e.g. --test-query \"SELECT * FROM v$database;\")")
    parser.add_argument("--status", action="store_true", help="Print a read-only fleet status summary of all instances and exit.")
    parser.add_argument("--clear-logs", action="store_true", help="Delete ONLY log files (backup_*.log/.jsonl) in this instance's log_dir and exit. History and backups are NOT touched. Uses --config to resolve the log_dir.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt (use with --clear-logs for automation).")
    parser.add_argument("--show-command", action="store_true", help="Show executed commands and the RMAN script on the console during a backup run (live RMAN streaming still goes only to the log). Default console shows only WARNING/ERROR.")
    parser.add_argument("--resend", nargs="?", const="last", metavar="FOLDER", help="Verify a backup is fully present on the remote destination and re-send any missing/incomplete files, then exit (no new backup). Bare --resend targets the last successful backup; --resend <DDMMYY|path> targets a specific one.")
    args = parser.parse_args()

    if args.status:
        sys.exit(run_status_mode())

    if args.clear_logs:
        sys.exit(run_clear_logs(args.config, assume_yes=args.yes))

    main(config_file=args.config, dry_run=args.dry_run, test_mail=args.test_mail, test_transfer=args.test_transfer, test_db=args.test_db, test_query=args.test_query, show_command=args.show_command, resend=args.resend)
