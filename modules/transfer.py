"""Yedek transferi: rsync / scp (üst katman).

Katman kuralı (spec §9.2): `run_scp` transfer hızını hesaplamak için uzak dizin boyutuna
ihtiyaç duyar (`get_dir_size_gb`, space katmanında). Üst→üst import yerine `get_dir_size_fn`
parametresiyle (dependency injection) verilir.

Transferler UZUN komuttur: duvar-saati timeout yerine canlılık bazlı watchdog (spec §11.4).
Başarısız denemeler arasında üstel backoff + WARNING log (spec §11.1 kural 3, §11.2).
"""

import time

from .connection import run_long_command, run_command_wrapper

__all__ = [
    "run_rsync", "run_scp",
    "build_remote_paths", "ensure_remote_dir", "verify_remote_backup", "send_backup_dir",
]

_BACKOFF_BASE = 5      # saniye
_BACKOFF_CAP = 300     # saniye


def _backoff_sleep(logger, attempt, max_retries, label, err):
    """Başarısız denemeyi loglar ve son deneme değilse üstel backoff uygular."""
    logger.warning(f"{label} attempt {attempt}/{max_retries} failed. "
                   f"Err: {(err or '').strip()[:200]}")
    if attempt < max_retries:
        wait = min(_BACKOFF_BASE * (2 ** (attempt - 1)), _BACKOFF_CAP)
        logger.info(f"{label}: retrying in {wait}s (backoff)...")
        time.sleep(wait)


def run_rsync(logger, ssh_client, source_dir, remote_dest, max_retries=3, watchdog=None):
    cmd = f"rsync -avz --progress --stats --partial {source_dir} {remote_dest}"
    logger.info(f"rsync starting: {source_dir} --> {remote_dest}")

    overall_start = time.time()
    last_err = ""
    for attempt in range(1, max_retries + 1):
        status, out, err = run_long_command(ssh_client, cmd, logger, watchdog=watchdog)
        total_elapsed = time.time() - overall_start

        if status == 0:
            def parse_rsync_bytes(line_str):
                parts = line_str.split(":")
                if len(parts) < 2:
                    return 0
                val = parts[1].strip().split()[0].replace(",", "")
                suffixes = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
                if val and val[-1].upper() in suffixes:
                    try:
                        return float(val[:-1]) * suffixes[val[-1].upper()]
                    except ValueError:
                        return 0
                try:
                    return float(val)
                except ValueError:
                    return 0

            total_bytes = 0
            for line in out.splitlines():
                if "Total file size" in line:
                    total_bytes = parse_rsync_bytes(line)
                    break
            avg_speed_mbps = (total_bytes / (1024 ** 2)) / total_elapsed if total_elapsed > 0 else 0
            return total_elapsed, avg_speed_mbps, attempt, out

        last_err = err
        _backoff_sleep(logger, attempt, max_retries, "rsync", err)

    raise RuntimeError(f"rsync failed after {max_retries} attempts. Last error: {last_err.strip()}")


def run_scp(logger, ssh_client, source_dir, remote_dest, max_retries=3, watchdog=None, get_dir_size_fn=None):
    cmd = f"scp -r {source_dir} {remote_dest}"
    logger.info(f"scp starting: {source_dir} --> {remote_dest}")

    overall_start = time.time()
    last_err = ""
    for attempt in range(1, max_retries + 1):
        status, out, err = run_long_command(ssh_client, cmd, logger, watchdog=watchdog)
        total_elapsed = time.time() - overall_start

        if status == 0:
            total_bytes = get_dir_size_fn(ssh_client, source_dir) * (1024 ** 3)
            avg_speed_mbps = (total_bytes / (1024 ** 2)) / total_elapsed if total_elapsed > 0 else 0
            return total_elapsed, avg_speed_mbps, attempt, out

        last_err = err
        _backoff_sleep(logger, attempt, max_retries, "scp", err)

    raise RuntimeError(f"scp failed after {max_retries} attempts. Last error: {last_err.strip()}")


# ---------------------------------------------------------------------------
# Transfer doğrulama + yeniden gönderme (resend) yardımcıları
#
# Uzak yol üretimi TEK kaynaktan (`build_remote_paths`): canlı transfer bloğu, pre-backup
# doğrulaması ve `--resend` modu aynı yolları kullanır → uçlar her zaman uyumludur.
# ---------------------------------------------------------------------------


def _to_win_path(p):
    """POSIX-stil uzak yolu Windows'a çevirir (backup.py mkdir mantığıyla aynı): '/D:/x' → 'D:\\x'."""
    w = p.replace("/", "\\")
    if w.startswith("\\") and len(w) > 2 and w[2] == ":":
        w = w[1:]
    return w


def build_remote_paths(backup_config, oracle_sid, month_name, ddmmyy):
    """remote_dest + SID/MONTH/DDMMYY'den tüm uzak yol parçalarını üretir (saf, I/O yok)."""
    remote_dest = backup_config["remote_dest"]
    parts = remote_dest.split(":", 1)
    remote_base = parts[0]
    remote_path = parts[1] if len(parts) > 1 else ""
    suffix = f"{oracle_sid}/{month_name}/{ddmmyy}"
    parent_suffix = f"{oracle_sid}/{month_name}"
    return {
        "remote_base": remote_base,
        "remote_path": remote_path,
        "remote_path_only": f"{remote_path}/{suffix}",
        "remote_path_only_parent": f"{remote_path}/{parent_suffix}",
        "remote_full_dest": f"{remote_base}:{remote_path}/{suffix}",
        "remote_transfer_dest": f"{remote_base}:{remote_path}/{parent_suffix}",
    }


def ensure_remote_dir(logger, ssh_client, backup_config, remote_base, remote_path_only_parent):
    """Uzak hedefte MONTH klasörünü oluşturur (win: cmd mkdir, lin: mkdir -p); retry+backoff.
    Zaten varsa başarı sayar. Döner: True/False."""
    os_type = backup_config.get("os_type", "lin").lower()
    ssh_prefix = f"ssh -o StrictHostKeyChecking=no {remote_base} "
    for attempt in range(1, 4):
        if os_type == "win":
            win_path = _to_win_path(remote_path_only_parent)
            st, out, err = run_command_wrapper(ssh_client, f"{ssh_prefix} cmd /c mkdir \"{win_path}\"", logger, quiet=True)
        else:
            st, out, err = run_command_wrapper(ssh_client, f"{ssh_prefix} mkdir -p \"{remote_path_only_parent}\"", logger, quiet=True)
        if st == 0 or "already exists" in (out + err).lower() or "zaten var" in (out + err).lower():
            return True
        wait = min(2 * (2 ** (attempt - 1)), 30)  # backoff (spec §11.2)
        logger.warning(f"Remote directory creation failed (Attempt {attempt}/3). "
                       f"RC={st}, Err={err.strip() or out.strip()}. Retrying in {wait}s...")
        time.sleep(wait)
    logger.error(f"Failed to create remote directory '{remote_path_only_parent}' after 3 attempts.")
    return False


def _parse_manifest(text):
    """'ad|boyut' satırlarını {ad: boyut(int)} sözlüğüne çevirir. Bozuk/boş satırlar atlanır."""
    manifest = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        name, _, size = line.rpartition("|")
        name = name.strip()
        try:
            manifest[name] = int(size.strip())
        except ValueError:
            continue
    return manifest


def _local_manifest(ssh_client, local_full_path, logger):
    """DB/target sunucudaki (Linux) yerel yedek klasöründe {dosya_adı: boyut} manifesti."""
    cmd = f"find '{local_full_path}' -maxdepth 1 -type f -printf '%f|%s\\n'"
    _st, out, _err = run_command_wrapper(ssh_client, cmd, None, quiet=True)
    return _parse_manifest(out)


def _remote_manifest(ssh_client, backup_config, remote_base, remote_path_only):
    """Uzak hedefteki {dosya_adı: boyut} manifesti. os_type=win → PowerShell (.Length, locale-bağımsız),
    lin → find -printf. Uzak dizin yoksa boş manifest (2>/dev/null / SilentlyContinue)."""
    os_type = backup_config.get("os_type", "lin").lower()
    ssh_prefix = f"ssh -o StrictHostKeyChecking=no {remote_base} "
    if os_type == "win":
        win_path = _to_win_path(remote_path_only)
        # f-string YOK: PowerShell süslü parantezleri literal kalsın (kaçış derdi olmasın).
        ps = ("powershell -NoProfile -Command \""
              "$ErrorActionPreference='SilentlyContinue';"
              "Get-ChildItem -File -LiteralPath '" + win_path + "' | "
              "ForEach-Object { $_.Name + '|' + $_.Length }\"")
        cmd = f"{ssh_prefix} {ps}"
    else:
        cmd = f"{ssh_prefix} \"find '{remote_path_only}' -maxdepth 1 -type f -printf '%f|%s\\n' 2>/dev/null\""
    _st, out, _err = run_command_wrapper(ssh_client, cmd, None, quiet=True)
    return _parse_manifest(out)


def verify_remote_backup(logger, ssh_client, backup_config, paths, local_full_path):
    """Yerel yedek klasörünün uzak hedefe TAM geçtiğini dosya adı+boyut bazında doğrular.

    Döner: {ok, missing:[ad...], mismatched:[ad...], local_count, remote_count}.
      missing    = yerelde olup uzakta olmayan dosyalar (hiç gitmemiş / silinmiş).
      mismatched = iki tarafta da olup boyutu farklı (yarım/truncated transfer).
      ok         = yerelde en az 1 dosya var VE missing VE mismatched boş.
    """
    local = _local_manifest(ssh_client, local_full_path, logger)
    remote = _remote_manifest(ssh_client, backup_config, paths["remote_base"], paths["remote_path_only"])
    missing = sorted(n for n in local if n not in remote)
    mismatched = sorted(n for n in local if n in remote and remote[n] != local[n])
    ok = (len(local) > 0 and not missing and not mismatched)
    return {"ok": ok, "missing": missing, "mismatched": mismatched,
            "local_count": len(local), "remote_count": len(remote)}


def send_backup_dir(logger, ssh_client, backup_config, paths, local_full_path,
                    watchdog=None, get_dir_size_fn=None):
    """MONTH klasörünü oluşturup yedek klasörünü uzak hedefe gönderir (scp/rsync).
    Döner: (method, elapsed, avg_speed_mbps, attempts)."""
    method = backup_config.get("transfer_method", "rsync").lower()
    ensure_remote_dir(logger, ssh_client, backup_config, paths["remote_base"], paths["remote_path_only_parent"])
    if method == "scp":
        elapsed, speed, attempts, _ = run_scp(logger, ssh_client, local_full_path,
                                              paths["remote_transfer_dest"], watchdog=watchdog,
                                              get_dir_size_fn=get_dir_size_fn)
    else:
        elapsed, speed, attempts, _ = run_rsync(logger, ssh_client, local_full_path,
                                                paths["remote_transfer_dest"], watchdog=watchdog)
    return method, elapsed, speed, attempts
