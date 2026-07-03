"""RMAN çalıştırma, Data Guard standby kontrolü (üst katman)."""

import time
import re

from .connection import run_command_wrapper, execute_oracle_sql

__all__ = ["check_standby_exists", "run_rman"]


def check_standby_exists(logger, env, ssh_client, db_creds=None):
    logger.info("Checking for Data Guard Standby existence via sqlplus...")

    if db_creds and db_creds.get("username") and db_creds.get("password"):
        user = db_creds["username"]
        pwd = db_creds["password"]
        host = db_creds.get("hostname") or db_creds.get("ip")
        db = db_creds.get("db", "")
        conn_str = f'{user}/"{pwd}"@{host}/{db} as sysdba'
    else:
        conn_str = "/ as sysdba"

    sql = "SET HEADING OFF FEEDBACK OFF PAGESIZE 0\nSELECT COUNT(*) FROM v$archive_dest WHERE target='STANDBY' AND destination IS NOT NULL;\nEXIT;\n"
    status, out, err = execute_oracle_sql(ssh_client, conn_str, sql, logger, env_dict=env, timeout=30, quiet=True)
    if status == 0:
        try:
            count = int(out.strip())
            if count > 0:
                logger.info(f"Standby database detected ({count} destinations).")
                return True
        except ValueError:
            pass
    return False


def run_rman(logger, env, ssh_client, rman_script, label="rman", db_creds=None):
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
            user = db_creds["username"]
            pwd = db_creds["password"]
            host = db_creds.get("hostname") or db_creds.get("ip")
            db = db_creds.get("db", "")
            conn_str = f'{user}/"{pwd}"@{host}/{db}'

            sql = "SET HEADING OFF FEEDBACK OFF PAGESIZE 0\nSELECT status FROM (SELECT status FROM v$rman_backup_job_details ORDER BY start_time DESC) WHERE ROWNUM=1;\nEXIT;\n"
            logger.info("RMAN output reported an error but OS exit code is 0. Running SQL fallback validation...")
            sql_status, sql_out, sql_err = execute_oracle_sql(ssh_client, conn_str, sql, logger, env_dict=env, quiet=True)

            if sql_status == 0 and "COMPLETED" in sql_out.upper():
                found_error = False
                logger.info("RMAN çıktısında hata tespit edildi ancak v$rman_backup_job_details tablosu yedeğin COMPLETED olduğunu doğruladı. İşlem BAŞARILI kabul ediliyor.")
            else:
                raise RuntimeError(f"RMAN {label} failed (rc={status}). SQL validation also failed or did not report COMPLETED. See logs for ORA-/RMAN- errors.")
        else:
            raise RuntimeError(f"RMAN {label} failed (rc={status}). See logs for ORA-/RMAN- errors.")

    return elapsed, out
