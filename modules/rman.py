"""RMAN çalıştırma, Data Guard standby kontrolü, watchdog DB progress check (üst katman).

RMAN backup UZUN komuttur (6 saat sürebilir, normaldir). Bu yüzden duvar-saati timeout
KULLANILMAZ; `run_long_command` ile canlılık bazlı watchdog kullanılır (spec §11.2/§11.4).
RMAN başarısız olursa aynı run içinde OTOMATİK RETRY YAPILMAZ (spec §11.1 kural 4).
"""

import re
import time

from .connection import run_command_wrapper, run_long_command, execute_oracle_sql

__all__ = ["check_standby_exists", "run_rman", "make_rman_progress_check"]


def _db_conn_str(db_creds, sysdba=True):
    user = db_creds["username"]
    pwd = db_creds["password"]
    host = db_creds.get("hostname") or db_creds.get("ip")
    db = db_creds.get("db", "")
    suffix = " as sysdba" if sysdba else ""
    return f'{user}/"{pwd}"@{host}/{db}{suffix}'


def make_rman_progress_check(ssh_client, env, db_creds, logger, temp_dir="/tmp"):
    """Watchdog Sinyal 2 (spec §11.4.1 Kontrol 1): v$rman_status RUNNING + mbytes_processed
    artıyor mu? Artıyorsa iş CANLI kabul edilir (çıktı akmasa bile). DB creds yoksa None döner
    ve Sinyal 2 otomatik devre dışı kalır (bir kez WARNING).

    NOT: Kontrol 2-4 (v$session_longops, wait event, FRA — spec §11.4.1) ileride bu callback'e
    eklenebilir; Kontrol 1 birincil ilerleme sinyalidir.
    """
    if not (db_creds and db_creds.get("username") and db_creds.get("password")):
        logger.warning("Watchdog DB progress check disabled: no DB credentials (Signal 2 off).")
        return None

    conn_str = _db_conn_str(db_creds, sysdba=True)
    state = {"last_mbytes": -1.0}

    def check():
        sql = ("SET HEADING OFF FEEDBACK OFF PAGESIZE 0\n"
               "SELECT NVL(SUM(mbytes_processed),0) FROM v$rman_status WHERE status LIKE 'RUNNING%';\n"
               "EXIT;\n")
        st, out, err = execute_oracle_sql(ssh_client, conn_str, sql, logger, env_dict=env,
                                          temp_dir=temp_dir, timeout=600, quiet=True)
        if st != 0:
            return False  # o tur sinyal alınamadı — stall kanıtı SAYILMAZ (spec §11.4.1)
        try:
            mbytes = float(out.strip())
        except ValueError:
            return False
        alive = mbytes > state["last_mbytes"]
        state["last_mbytes"] = mbytes
        return alive

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
                raise RuntimeError(f"RMAN {label} failed (rc={status}). SQL validation also failed or did not report COMPLETED. See logs for ORA-/RMAN- errors.")
        else:
            raise RuntimeError(f"RMAN {label} failed (rc={status}). See logs for ORA-/RMAN- errors.")

    return elapsed, out
