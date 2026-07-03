"""Komut yürütme: SSH (paramiko) veya lokal (subprocess) (alt katman)."""

import os
import sys
import subprocess

import paramiko

__all__ = ["get_ssh_client", "run_command_wrapper", "execute_oracle_sql"]


def execute_oracle_sql(ssh_client, conn_str, sql_content, logger, env_dict, timeout=None, quiet=True):
    """Executes a SQL script over sqlplus safely by writing it to a temporary file via Heredoc."""
    cmd = f"""SQL_TMP=$(mktemp /tmp/oracle_query_XXXXXX.sql)
cat << 'EOF' > "$SQL_TMP"
{sql_content}
EOF
sqlplus -s '{conn_str}' @"$SQL_TMP"
rm -f "$SQL_TMP"
"""
    return run_command_wrapper(ssh_client, cmd, logger, env_dict=env_dict, timeout=timeout, quiet=quiet)


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
