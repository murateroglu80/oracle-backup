"""Fleet durum özeti — `--status` modu (üst katman, salt-okunur).

Spec §8.4: hiçbir instance'ın backup akışını etkilemeden (lock almadan, SSH açmadan, RMAN
çalıştırmadan) tüm instance'ların son durumunu okur. UI fazında aynı okuma mantığı
(`collect_fleet_status`) FastAPI endpoint'ine taşınacak — o yüzden okuma mantığı ayrı tutuldu.

Çıkış kodu (backup.py tarafından hesaplanır): tüm SUCCESS -> 0, herhangi FAILED -> 1,
son kaydı stale_hours'tan eski / hiç kayıt yok -> 2.
"""

import glob
import json
import os
from datetime import datetime, timedelta

import yaml

from .config import resolve_instance_id, resolve_paths
from .history import get_history_file

__all__ = ["collect_fleet_status", "format_status_table", "STATUS_STALE_HOURS_DEFAULT"]

STATUS_STALE_HOURS_DEFAULT = 26


def _load_fleet(config_dir, script_dir):
    """fleet.yaml'ı (config/ önce, sonra proje kökü) okur; yoksa None döner."""
    for path in (os.path.join(config_dir, "fleet.yaml"), os.path.join(script_dir, "fleet.yaml")):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                return data.get("FLEET", [])
            except Exception:
                return []
    return None


def _discover_configs(config_dir):
    """fleet.yaml yoksa config/*.yaml taranır (example/vault/secrets/fleet/shared hariç).

    shared.yaml org-geneli MAIL/MONITORING içerir (bir instance DEĞİL); taranırsa host/SID
    bulunmadığı için sahte bir 'unknown' satırı üretirdi — o yüzden dışlanır.
    """
    out = []
    for f in sorted(glob.glob(os.path.join(config_dir, "*.yaml"))):
        base = os.path.basename(f)
        if (base.endswith(".example.yaml") or base.startswith("vault") or base.startswith("secrets")
                or base == "fleet.yaml" or base == "shared.yaml"):
            continue
        out.append(f)
    return out


def _latest_records(history_dir):
    """En güncel ay dosyasındaki kayıtları döner; boşsa bir önceki ayı dener."""
    for date_obj in (datetime.now(), datetime.now() - timedelta(days=31)):
        hf = get_history_file(history_dir, date_obj)
        if os.path.exists(hf):
            try:
                with open(hf, "r") as f:
                    data = json.load(f)
                if data:
                    return data
            except Exception:
                pass
    return []


def _parse_dt(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _row_for_config(cfg_path, stale_hours):
    row = {"instance_id": os.path.basename(cfg_path), "last_run": "-", "status": "NO_DATA",
           "size_gb": "-", "duration": "-", "transferred": "-", "stale": True, "failed": False}
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        row["status"] = "CONFIG_ERROR"
        row["failed"] = True
        return row

    instance_id = resolve_instance_id(cfg)
    row["instance_id"] = instance_id
    history_dir = resolve_paths(cfg, instance_id)["history_dir"]

    recs = _latest_records(history_dir)
    if not recs:
        return row  # NO_DATA, stale=True

    backup_recs = [r for r in recs if str(r.get("operation", "")).lower().startswith("backup")]
    last = backup_recs[-1] if backup_recs else recs[-1]

    row["last_run"] = last.get("start_time") or last.get("run_time", "-")
    row["status"] = str(last.get("status", "UNKNOWN")).upper()
    row["size_gb"] = str(last.get("size_gb", "-"))
    row["duration"] = str(last.get("duration", "-"))
    row["failed"] = "FAIL" in row["status"] or "ERROR" in row["status"]

    xfer = [r for r in recs if r.get("remote_backup")]
    if xfer:
        row["transferred"] = "YES" if xfer[-1].get("remote_complete") else "NO"

    dt = _parse_dt(row["last_run"])
    row["stale"] = (dt is None) or (datetime.now() - dt > timedelta(hours=stale_hours))
    return row


def collect_fleet_status(script_dir, stale_hours=STATUS_STALE_HOURS_DEFAULT):
    """Tüm instance'ların son durum satırlarını döner (salt-okunur). UI fazında tekrar kullanılır."""
    config_dir = os.path.join(script_dir, "config")
    fleet = _load_fleet(config_dir, script_dir)

    cfg_paths = []
    if fleet is not None:
        for item in fleet:
            if not item.get("enabled", True):
                continue
            cfg = item.get("config", "")
            if not os.path.isabs(cfg):
                # fleet config yolu proje köküne göre (spec §8.3 örneği: "config/db1.yaml")
                cfg = os.path.join(script_dir, cfg)
            cfg_paths.append(cfg)
    else:
        cfg_paths = _discover_configs(config_dir)

    return [_row_for_config(p, stale_hours) for p in cfg_paths]


def format_status_table(rows):
    # INSTANCE kolonu dinamik genişlik: en uzun instance_id'ye (veya başlığa) göre hizala,
    # böylece uzun isimler taşıp diğer kolonlarla üst üste binmez.
    iw = max([len("INSTANCE")] + [len(r["instance_id"]) for r in rows]) + 2
    header = f"{'INSTANCE':<{iw}}{'LAST RUN':<21}{'STATUS':<10}{'SIZE(GB)':<10}{'DURATION':<12}{'TRANSFERRED'}"
    lines = [header, "-" * len(header)]
    for r in rows:
        lines.append(
            f"{r['instance_id']:<{iw}}{r['last_run']:<21}{r['status']:<10}"
            f"{str(r['size_gb']):<10}{str(r['duration']):<12}{r['transferred']}"
        )
    if not rows:
        lines.append("(no instances found — fleet.yaml or config/*.yaml yok)")
    return "\n".join(lines)
