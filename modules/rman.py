"""RMAN çalıştırma, Data Guard standby kontrolü, watchdog DB progress check (üst katman).

RMAN backup UZUN komuttur (6 saat sürebilir, normaldir). Bu yüzden duvar-saati timeout
KULLANILMAZ; `run_long_command` ile canlılık bazlı watchdog kullanılır (spec §11.2/§11.4).
RMAN başarısız olursa aynı run içinde OTOMATİK RETRY YAPILMAZ (spec §11.1 kural 4).
"""

import re
import time

from .connection import run_command_wrapper, run_long_command, execute_oracle_sql, STALL_STATUS

__all__ = ["check_standby_exists", "run_rman", "make_rman_progress_check"]


def _db_conn_str(db_creds, sysdba=True):
    user = db_creds["username"]
    pwd = db_creds["password"]
    host = db_creds.get("hostname") or db_creds.get("ip")
    # DB/service adı: Vault field adı kuruma göre değişebilir (db / name / db_name).
    db = db_creds.get("db") or db_creds.get("name") or db_creds.get("db_name") or ""
    suffix = " as sysdba" if sysdba else ""
    return f'{user}/"{pwd}"@{host}/{db}{suffix}'


def _section(out, name, next_name):
    """Sqlplus çıktısından PROMPT ===NAME=== ile ===NEXT=== arasındaki metni çıkarır."""
    try:
        after = out.split(f"==={name}===", 1)[1]
        return (after.split(f"==={next_name}===", 1)[0] if next_name else after).strip()
    except IndexError:
        return ""


def make_rman_progress_check(ssh_client, env, db_creds, logger, temp_dir="/tmp", watchdog_cfg=None):
    """Watchdog Sinyal 2 (spec §11.4.1 Kontrol 1-4): tek sqlplus oturumunda sırayla çalıştırılır.

    Kontrol 1 (v$rman_status mbytes_processed artışı) -> Kontrol 2 (v$session_longops tazelik)
    -> Kontrol 3 (v$session wait event teşhisi): ilk CANLI diyen kontrol yeterlidir, sıradakine
    geçilmez. Kontrol 4 (FRA doluluk) K1-K3 sonucundan BAĞIMSIZ her turda ayrıca değerlendirilir;
    stall kararını etkilemez, yalnızca eşik aşılınca WARNING loglar.

    DB creds yoksa None döner ve Sinyal 2 tamamen devre dışı kalır (bir kez WARNING).
    """
    if not (db_creds and db_creds.get("username") and db_creds.get("password")):
        logger.warning("Watchdog DB progress check disabled: no DB credentials (Signal 2 off).")
        return None

    wd = watchdog_cfg or {}
    tolerance_min = wd.get("progress_check_tolerance_min", 5)
    interval_min = wd.get("progress_check_interval_min", 5)
    fra_check_enabled = wd.get("fra_check_enabled", True)
    fra_warning_pct = wd.get("fra_warning_pct", 95)
    # K2 "taze" penceresi: spec "son birkaç dakika" der, sayı vermez — kontrol periyoduna göre
    # ölçekliyoruz (iki tur boyunca güncellenmemişse artık taze sayılmaz), en az 10 dk.
    freshness_min = max(interval_min * 2, 10)

    conn_str = _db_conn_str(db_creds, sysdba=True)
    # ÖNEMLİ (spec §11.4.1): TÜM zaman karşılaştırmaları DB tarafında SYSDATE ile yapılır.
    # Jump ile DB sunucusu arasında saat/timezone farkı olsa bile K1 start_time filtresi ve K2
    # tazelik kontrolü bozulmaz. (Aksi halde canlı bir backup yanlışlıkla STALL sayılır — 2026-08-05
    # yanlış-pozitif STALL olayı tam olarak jump-tarafı datetime.now() kullanımından kaynaklandı.)
    # tolerance_min: K1'in bu run'a ait olmayan eski RMAN oturumlarını elemesi için start_time
    # penceresi (dakika/1440 = gün). freshness_min: K2 longops güncellik penceresi.
    state = {"last_mbytes": -1.0, "last_diag": None}

    def check():
        sql = (
            "SET HEADING OFF FEEDBACK OFF PAGESIZE 0 LINESIZE 500 VERIFY OFF ECHO OFF TRIMSPOOL ON\n"
            "PROMPT ===K1===\n"
            "SELECT NVL(SUM(mbytes_processed),0) FROM v$rman_status\n"
            f"WHERE status LIKE 'RUNNING%' AND start_time >= SYSDATE - ({tolerance_min}/1440);\n"
            "PROMPT ===K2===\n"
            f"SELECT CASE WHEN MAX(last_update_time) >= SYSDATE - ({freshness_min}/1440)\n"
            "            THEN 'FRESH' ELSE 'STALE' END FROM v$session_longops\n"
            "WHERE opname LIKE 'RMAN%' AND totalwork > 0 AND sofar < totalwork;\n"
            "PROMPT ===K3===\n"
            "SELECT event || '|' || NVL(TO_CHAR(blocking_session),'') FROM (\n"
            "  SELECT event, blocking_session FROM v$session\n"
            "  WHERE program LIKE '%rman%' OR module LIKE '%backup%' OR client_info LIKE '%rman%'\n"
            "  ORDER BY seconds_in_wait DESC\n"
            ") WHERE ROWNUM=1;\n"
            "PROMPT ===K4===\n"
            "SELECT NVL(MAX(ROUND(space_used/space_limit*100,1)),-1) FROM v$recovery_file_dest;\n"
            "EXIT;\n"
        )
        st, out, err = execute_oracle_sql(ssh_client, conn_str, sql, logger, env_dict=env,
                                          temp_dir=temp_dir, timeout=600, quiet=True)
        if st != 0:
            # O tur sinyal alınamadı — stall kanıtı SAYILMAZ (spec §11.4.1). Ancak SESSIZ kalmaz:
            # gerçek sunucuda tekrarlayan bağlantı/SQL hatası, canlı backup'ı yanlış STALL'a
            # sürükleyebileceğinden görünür loglanır (gözlemlenebilirlik).
            snippet = (err or out or "").strip().replace("\n", " ")[:200]
            logger.warning(f"Watchdog DB progress check could not run (rc={st}): {snippet}")
            return False

        alive = False

        # Kontrol 1 — mbytes_processed önceki tura göre arttıysa CANLI.
        try:
            mbytes = float(_section(out, "K1", "K2"))
        except ValueError:
            mbytes = None
        if mbytes is not None:
            if mbytes > state["last_mbytes"]:
                alive = True
            logger.debug(f"Watchdog K1 mbytes_processed={mbytes} (prev={state['last_mbytes']}) alive={alive}")
            state["last_mbytes"] = mbytes

        # Kontrol 2 — Kontrol 1 canlılık göstermediyse: longops DB-tarafı tazelik ('FRESH'/'STALE').
        # Jump saati KULLANILMAZ; tazelik SYSDATE ile SQL içinde hesaplanır. Aktif longops satırı
        # yoksa MAX(...) NULL → 'STALE' (güvenli varsayılan).
        if not alive:
            k2_raw = _section(out, "K2", "K3").upper()
            if k2_raw:
                logger.debug(f"Watchdog K2 longops freshness={k2_raw}")
                if k2_raw.startswith("FRESH"):
                    alive = True

        # Kontrol 3 — Kontrol 1-2 canlılık göstermediyse: wait event teşhisi.
        # Sonuç ne olursa olsun (spec §11.4.1) teşhis bilgisi kaydedilir — bir sonraki
        # STALL/FAILED mesajına run_rman tarafından eklenebilsin diye state'te tutulur.
        if not alive:
            k3_raw = _section(out, "K3", "K4")
            if k3_raw:
                event, _, blocking = k3_raw.partition("|")
                event_lower = event.strip().lower()
                state["last_diag"] = f"wait_event={event.strip()} blocking_session={blocking or '-'}"
                logger.debug(f"Watchdog K3 diagnostic: {state['last_diag']}")
                if "i/o" in event_lower:
                    alive = True
                # enq:/idle/client bekleme durumları CANLI SAYILMAZ; Sinyal 3 (OS PID) devreye girer.

        # Kontrol 4 — K1-K3 sonucundan BAĞIMSIZ, karar mantığını etkilemez, yalnızca uyarır.
        if fra_check_enabled:
            k4_raw = _section(out, "K4", "")
            try:
                pct_used = float(k4_raw)
            except ValueError:
                pct_used = -1.0
            if pct_used >= fra_warning_pct:
                logger.warning(
                    f"FRA/archive dest doluluk %{pct_used} (eşik %{fra_warning_pct}) — "
                    "RMAN archiver'da beklemede kalabilir (log file switch)."
                )

        logger.debug(f"Watchdog progress check result: alive={alive}")
        return alive

    check.diag_state = state
    return check


def check_standby_exists(logger, env, ssh_client, db_creds=None, temp_dir="/tmp"):
    logger.info("Checking for Data Guard Standby existence via sqlplus...")

    if db_creds and db_creds.get("username") and db_creds.get("password"):
        conn_str = _db_conn_str(db_creds, sysdba=True)
    else:
        conn_str = "/ as sysdba"

    sql = "SET HEADING OFF FEEDBACK OFF PAGESIZE 0\nSELECT COUNT(*) FROM v$archive_dest WHERE target='STANDBY' AND destination IS NOT NULL;\nEXIT;\n"
    status, out, err = execute_oracle_sql(ssh_client, conn_str, sql, logger, env_dict=env, temp_dir=temp_dir, timeout=30, quiet=True)
    if status == 0:
        try:
            count = int(out.strip())
            if count > 0:
                logger.info(f"Standby database detected ({count} destinations).")
                return True
        except ValueError:
            pass
    return False


def run_rman(logger, env, ssh_client, rman_script, label="rman", db_creds=None,
             temp_dir="/tmp", watchdog=None, progress_check_fn=None):
    start = time.time()

    logger.info(f"Executing RMAN Script ({label}):\n{rman_script}")

    # Fail-Fast wrapper: Preserve RC. Heredoc 'EOF' (single-quoted) -> shell değişken genişletmesi YOK.
    # Geçici .rman dosyası TARGET'ın kendi OS temp'inde (mktemp -t: $TMPDIR ya da /tmp) oluşur —
    # jump'taki config temp_dir'ine bağlanmaz. Dosya yaklaşımı korunur (SQL injection koruması).
    cmd = f"""RMAN_TMP=$(mktemp -t rman_script_XXXXXX.rman)
cat << 'EOF' > $RMAN_TMP
{rman_script}
EOF
rman target / @$RMAN_TMP
RC=$?
rm -f $RMAN_TMP
exit $RC"""

    # UZUN komut: duvar-saati timeout DEĞİL, canlılık bazlı watchdog (spec §11.4).
    status, out, err = run_long_command(ssh_client, cmd, logger, env_dict=env,
                                        watchdog=watchdog, progress_check_fn=progress_check_fn)
    elapsed = time.time() - start

    # Check explicitly for RMAN/ORA errors in output even if RC=0
    error_pattern = re.compile(r'(RMAN-\d+|ORA-\d+)')
    found_error = False

    for line in (out + "\n" + err).splitlines():
        if error_pattern.search(line):
            if any(ignore in line for ignore in ["RMAN-00571", "RMAN-00569", "Recovery Manager complete", "WARNING:", "RMAN-08120", "RMAN-08137"]):
                continue
            found_error = True
            break

    if found_error or status != 0:
        full_out = out + "\n" + err
        # Watchdog Kontrol 3 teşhisi (varsa): "yaşıyor ama neden ilerlemiyor" bilgisini
        # STALL/FAILED mesajına serbest metin olarak ekler (spec §11.4.1).
        diag = getattr(progress_check_fn, "diag_state", None)
        last_diag = diag.get("last_diag") if diag else None
        diag_suffix = f" [{last_diag}]" if last_diag else ""

        if "immutable" in full_out.lower() and "ORA-19509" in full_out:
            if logger:
                logger.warning(f"RMAN {label} reported an error, but it appears to be due to immutable backups preventing deletion. Ignoring error and treating as SUCCESS.")
        elif found_error and status == 0 and db_creds and db_creds.get("username") and db_creds.get("password"):
            conn_str = _db_conn_str(db_creds, sysdba=False)

            sql = "SET HEADING OFF FEEDBACK OFF PAGESIZE 0\nSELECT status FROM (SELECT status FROM v$rman_backup_job_details ORDER BY start_time DESC) WHERE ROWNUM=1;\nEXIT;\n"
            logger.info("RMAN output reported an error but OS exit code is 0. Running SQL fallback validation...")
            sql_status, sql_out, sql_err = execute_oracle_sql(ssh_client, conn_str, sql, logger, env_dict=env, temp_dir=temp_dir, quiet=True)

            if sql_status == 0 and "COMPLETED" in sql_out.upper():
                found_error = False
                logger.info("RMAN çıktısında hata tespit edildi ancak v$rman_backup_job_details tablosu yedeğin COMPLETED olduğunu doğruladı. İşlem BAŞARILI kabul ediliyor.")
            else:
                raise RuntimeError(f"RMAN {label} failed (rc={status}). SQL validation also failed or did not report COMPLETED. See logs for ORA-/RMAN- errors.{diag_suffix}")
        else:
            raise RuntimeError(f"RMAN {label} failed (rc={status}). See logs for ORA-/RMAN- errors.{diag_suffix}")

    return elapsed, out
