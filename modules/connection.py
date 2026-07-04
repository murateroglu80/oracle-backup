"""Komut yürütme: SSH (paramiko) veya lokal (subprocess) (alt katman).

İki komut sınıfı (spec §11.1/§11.4):
- KISA komutlar (mkdir, df, du, stat, kısa SELECT): `run_command_wrapper`, duvar-saati timeout
  (default `DEFAULT_CMD_TIMEOUT`=600s). Timeout aşılırsa status 124 döner (exception fırlatmaz).
- UZUN komutlar (RMAN, büyük transfer, uzun SQL): `run_long_command`, duvar-saati timeout DEĞİL —
  canlılık bazlı watchdog (çıktı akışı + DB progress callback + OS PID). İş ilerliyorsa süre ne
  olursa olsun kesilmez; ilerleme durursa STALL olarak sonlandırılır.
"""

import os
import re
import select
import shlex
import socket
import subprocess
import sys
import time

import paramiko

__all__ = [
    "get_ssh_client",
    "run_command_wrapper",
    "run_long_command",
    "execute_oracle_sql",
    "DEFAULT_CMD_TIMEOUT",
    "STALL_STATUS",
]

DEFAULT_CMD_TIMEOUT = 600      # kısa komutlar için duvar-saati timeout (saniye)
STALL_STATUS = 124            # watchdog stall / timeout için dönüş status'u
_WD_PID_MARKER = "__WD_PID__"


def _build_full_cmd(cmd, env_dict):
    env_prefix = ""
    if env_dict:
        for k, v in env_dict.items():
            env_prefix += f'export {k}="{v}"; '
    return env_prefix + cmd


def execute_oracle_sql(ssh_client, conn_str, sql_content, logger, env_dict, temp_dir=None, timeout=DEFAULT_CMD_TIMEOUT, quiet=True):
    """SQL script'ini sqlplus'a geçici dosya (Heredoc) üzerinden güvenli çalıştırır.

    Geçici .sql dosyası TARGET sunucunun kendi OS temp'inde (mktemp default: $TMPDIR ya da /tmp)
    oluşturulur — jump'taki config temp_dir'ine bağlanmaz. Dosya yaklaşımı SQL injection'a karşı
    korumayı sürdürür (SQL heredoc ile literal yazılır, komut satırında yorumlanmaz).
    (temp_dir parametresi geriye dönük çağrı uyumu için korunur; artık kullanılmıyor.)

    conn_str shlex.quote ile shell'e güvenli geçirilir: DB şifresi tek tırnak/boşluk gibi
    shell-özel karakter içerse bile komut satırı bozulmaz. Katmanlama: shell quoting dışta
    (shlex), sqlplus quoting içte (_db_conn_str şifreyi "..." ile sarar)."""
    cmd = f"""SQL_TMP=$(mktemp -t oracle_query_XXXXXX.sql)
cat << 'EOF' > "$SQL_TMP"
{sql_content}
EOF
sqlplus -s {shlex.quote(conn_str)} @"$SQL_TMP"
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


def run_command_wrapper(ssh_client, cmd, logger, env_dict=None, timeout=DEFAULT_CMD_TIMEOUT, quiet=False):
    """KISA komutlar için. Duvar-saati timeout aşılırsa status=124 döner (exception yutulur)."""
    full_cmd = _build_full_cmd(cmd, env_dict)

    if not quiet and logger:
        logger.debug(f"[CMD] {cmd}")

    if ssh_client:
        try:
            stdin, stdout, stderr = ssh_client.exec_command(full_cmd, timeout=timeout)
            out = stdout.read().decode('utf-8', errors='ignore')
            err = stderr.read().decode('utf-8', errors='ignore')
            status = stdout.channel.recv_exit_status()
        except socket.timeout:
            if logger:
                logger.warning(f"Command TIMEOUT after {timeout}s (short-command): {cmd[:80]}")
            return STALL_STATUS, "", f"TIMEOUT after {timeout}s"
    else:
        try:
            proc = subprocess.run(full_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  executable='/bin/bash', timeout=timeout)
            out = proc.stdout.decode('utf-8', errors='ignore')
            err = proc.stderr.decode('utf-8', errors='ignore')
            status = proc.returncode
        except subprocess.TimeoutExpired as e:
            if logger:
                logger.warning(f"Command TIMEOUT after {timeout}s (short-command, local): {cmd[:80]}")
            out = (e.stdout or b"").decode('utf-8', errors='ignore') if isinstance(e.stdout, bytes) else (e.stdout or "")
            return STALL_STATUS, out, f"TIMEOUT after {timeout}s"

    if not quiet and logger:
        for line in out.splitlines():
            logger.debug(f"  [STDOUT] {line}")
        for line in err.splitlines():
            logger.debug(f"  [STDERR] {line}")

    return status, out, err


def _pid_alive_remote(ssh_client, pid, logger):
    """SSH üzerinden `kill -0` ile uzak sürecin yaşadığını kontrol eder (sinyal göndermez)."""
    if not pid:
        return True  # PID yakalanamadıysa 'canlı' varsay (yanlış-pozitif stall üretme)
    status, _, _ = run_command_wrapper(ssh_client, f"kill -0 {pid} 2>/dev/null; echo $?", None, timeout=30, quiet=True)
    return status == 0


def run_long_command(ssh_client, cmd, logger, env_dict=None, watchdog=None, progress_check_fn=None, quiet=False):
    """UZUN komutları canlılık bazlı watchdog ile çalıştırır (spec §11.4).

    Canlılık sinyalleri (her biri config'ten kapatılabilir):
      1. Çıktı akışı (streaming stdout) — her yeni veri last_activity'yi tazeler.
      2. DB progress — `progress_check_fn()` True dönerse last_activity tazelenir (opsiyonel callback).
      3. OS PID — süreç öldüyse beklemeden sonuçlandırılır (canlı PID stall'ı ENGELLEMEZ; yalnızca
         'öldü mü' kısayolu — keepalive sinyali değildir, spec §11.4).

    STALL kararı: yalnızca çıktı VE DB progress `idle_timeout_min` boyunca sessizken verilir.
    Dönüş: (status, out, err). Stall'da status=STALL_STATUS ve err "STALLED: ..." içerir.
    """
    wd = watchdog or {}
    wd_enabled = wd.get("enabled", True)
    idle_timeout = wd.get("idle_timeout_min", 30) * 60
    pid_check_enabled = wd.get("os_pid_check_enabled", True)
    progress_enabled = wd.get("progress_check_enabled", True) and progress_check_fn is not None
    progress_interval = wd.get("progress_check_interval_min", 5) * 60
    max_runtime = wd.get("max_runtime_min", 0) * 60  # 0 = SINIRSIZ
    kill_on_stall = wd.get("kill_on_stall", False)

    if not wd_enabled:
        # Watchdog kapalı: eski sınırsız-bekleme davranışı (yine de blocking okuma).
        return run_command_wrapper(ssh_client, cmd, logger, env_dict=env_dict, timeout=None, quiet=quiet)

    full_cmd = _build_full_cmd(cmd, env_dict)
    # Uzak/yerel PID'i ilk satırda yakala (Sinyal 3 için).
    wrapped_cmd = f'echo "{_WD_PID_MARKER}:$$"; {full_cmd}'

    if not quiet and logger:
        logger.debug(f"[LONG-CMD] {cmd}")

    start = time.time()
    last_activity = start
    last_progress = start
    last_pid_check = start
    remote_pid = None
    out_buf = []
    check_interval = 30  # PID/progress kontrol cadence (spec §11.4)

    def _consume_pid(text):
        nonlocal remote_pid
        if remote_pid is None:
            m = re.search(rf"{_WD_PID_MARKER}:(\d+)", text)
            if m:
                remote_pid = m.group(1)

    def _clean(buf):
        # PID işaretçi SATIRINI çıktıdan temizle (RMAN log/parse'ını kirletmesin). Satır bazlı —
        # SSH'de marker gerçek çıktıyla aynı chunk'ta olabilir, o yüzden chunk değil satır atılır.
        text = "".join(buf)
        return "\n".join(l for l in text.split("\n") if _WD_PID_MARKER not in l)

    def _stall_result(reason):
        phase_info = f"STALLED: no progress for {int((time.time()-last_activity))}s ({reason})"
        if logger:
            logger.error(phase_info)
            logger.warning("remote process may still be running, verify manually")
        return STALL_STATUS, _clean(out_buf), phase_info

    if ssh_client:
        chan = ssh_client.get_transport().open_session()
        chan.exec_command(wrapped_cmd)
        chan.setblocking(0)
        # Sınırsız döngü YASAK (spec §11.1): çıkış koşulları açık — done bayrağı ile bounded.
        done = False
        while not done:
            got = False
            try:
                while chan.recv_ready():
                    data = chan.recv(65536).decode('utf-8', errors='ignore')
                    if data:
                        out_buf.append(data); got = True
                while chan.recv_stderr_ready():
                    data = chan.recv_stderr(65536).decode('utf-8', errors='ignore')
                    if data:
                        out_buf.append(data); got = True
            except socket.timeout:
                pass
            if got:
                last_activity = time.time()
                _consume_pid("".join(out_buf[-4:]))
                if not quiet and logger:
                    logger.debug(f"  [STREAM] {out_buf[-1].rstrip()}")
            if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                done = True
                continue
            now = time.time()
            if max_runtime and now - start > max_runtime:
                if kill_on_stall and remote_pid:
                    run_command_wrapper(ssh_client, f"kill {remote_pid}", None, timeout=30, quiet=True)
                chan.close()
                return _stall_result(f"max_runtime_min exceeded")
            if progress_enabled and now - last_progress >= progress_interval:
                try:
                    if progress_check_fn():
                        last_activity = now
                except Exception:
                    pass
                last_progress = now
            if pid_check_enabled and now - last_pid_check >= check_interval:
                last_pid_check = now
                if remote_pid and not _pid_alive_remote(ssh_client, remote_pid, logger):
                    # Süreç öldü: çıkış statüsü hazırsa döngü biter; değilse kısa bekle.
                    if chan.exit_status_ready():
                        done = True
                        continue
            if wd_enabled and now - last_activity > idle_timeout:
                if kill_on_stall and remote_pid:
                    run_command_wrapper(ssh_client, f"kill {remote_pid}", None, timeout=30, quiet=True)
                chan.close()
                return _stall_result("output+db idle")
            time.sleep(1)
        status = chan.recv_exit_status()
        return status, _clean(out_buf), ""

    # ---- Lokal (subprocess.Popen + non-blocking select) ----
    proc = subprocess.Popen(wrapped_cmd, shell=True, executable='/bin/bash',
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1, text=True)
    # 'while True' YASAK (spec §11.1): çıkış koşulları açık — done bayrağı ile bounded.
    done = False
    while not done:
        rlist, _, _ = select.select([proc.stdout], [], [], 1)
        if rlist:
            line = proc.stdout.readline()
            if line:
                out_buf.append(line); last_activity = time.time()
                _consume_pid(line)
                if not quiet and logger:
                    logger.debug(f"  [STREAM] {line.rstrip()}")
        if proc.poll() is not None:
            for line in proc.stdout:
                out_buf.append(line)
            done = True
            continue
        now = time.time()
        if max_runtime and now - start > max_runtime:
            proc.kill() if kill_on_stall else proc.terminate()
            return _stall_result("max_runtime_min exceeded")
        if progress_enabled and now - last_progress >= progress_interval:
            try:
                if progress_check_fn():
                    last_activity = now
            except Exception:
                pass
            last_progress = now
        # Lokal modda süreç canlılığı doğrudan proc.poll() ile bilinir (PID/kill-0 gerekmez).
        if wd_enabled and now - last_activity > idle_timeout:
            (proc.kill() if kill_on_stall else proc.terminate())
            return _stall_result("output+db idle")
    status = proc.returncode
    return status, _clean(out_buf), ""
