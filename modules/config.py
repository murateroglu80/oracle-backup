"""Konfigürasyon yükleme + instance_id çözümleme + path namespacing (alt katman).

Bkz. spec Bölüm 1 (instance_id), Bölüm 3 (path namespacing) ve Bölüm 2.7/2.8
(CREDENTIALS_CONFIG + geriye dönük VAULT_CONFIG alias).
"""

import copy
import os
import re
import sys

import yaml

__all__ = ["load_config", "resolve_instance_id", "sanitize_instance_id", "resolve_paths"]

# Org-geneli ortak (paylaşılan) section'lar: tek kurumda e-posta ve monitoring hedefi genelde
# tektir. Bunlar config/shared.yaml'da bir kez tanımlanıp her instance'a merge edilir (instance
# override eder). Bilinçli WHITELIST — ORACLE_CONFIG/TARGET_SERVER/BACKUP_CONFIG gibi instance'a
# özgü section'lar shared'a KONMAMALI. İleride RMAN_TEMPLATE eklemek = bu tuple'a bir satır.
SHAREABLE_SECTIONS = ("MAIL_CONFIG", "MONITORING_CONFIG")


def sanitize_instance_id(raw):
    """Lowercase, alfanumerik olmayan karakterleri '_' yapar, ardışık '_'leri sıkıştırır."""
    s = raw.strip().lower()
    s = re.sub(r"[^a-z0-9_-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "default"


def resolve_instance_id(config):
    """
    Öncelik sırası:
    1. Top-level config["INSTANCE_ID"] (elle override, boş değilse)
    2. Otomatik: f"{TARGET_SERVER.host}_{ORACLE_CONFIG.ORACLE_SID}"
    """
    override = config.get("INSTANCE_ID", "")
    if override:
        return sanitize_instance_id(override)

    host = config.get("TARGET_SERVER", {}).get("host", "local")
    sid = config.get("ORACLE_CONFIG", {}).get("ORACLE_SID", "unknown")
    return sanitize_instance_id(f"{host}_{sid}")


def _resolve_paths(config, instance_id):
    """
    Path namespacing (spec §3): alan config'te boş/yok ise instance_id ile otomatik
    namespace edilir; admin elle değer verdiyse o değer olduğu gibi korunur.

    KRİTİK (spec §3.1): resolved path'ler TEK kaynaktan (`config["_resolved_paths"]`)
    yayılır. Hiçbir alt fonksiyon path'i tekrar config'ten okumaz — böylece
    ensure_free_space/get_required_gb gibi eski çift-kaynak okumalarındaki
    None -> TypeError riski ortadan kalkar.
    """
    bc = config.get("BACKUP_CONFIG", {})
    return {
        "log_dir": os.path.expanduser(bc.get("log_dir") or f"~/huaris/logs/{instance_id}"),
        "history_dir": os.path.expanduser(bc.get("history_dir") or f"~/huaris/history/{instance_id}"),
        "pid_file": os.path.expanduser(bc.get("pid_file") or f"/tmp/rman_backup_{instance_id}.pid"),
        "temp_dir": os.path.expanduser(bc.get("temp_dir", "/tmp")),
    }


def resolve_paths(config, instance_id=None):
    """Public sarmalayıcı — status modu gibi load_config'i tam çalıştırmadan path çözümlemek için."""
    if instance_id is None:
        instance_id = resolve_instance_id(config)
    return _resolve_paths(config, instance_id)


def _apply_credentials_config(config, script_dir, config_dir, instance_id):
    """
    Geriye dönük uyumluluk + CREDENTIALS_CONFIG normalizasyonu (spec §2.8).

    1. Ayrı vault_config.yaml dosyası (baskın kurulum deseni) hâlâ okunur.
    2. Eski VAULT_CONFIG bloğu CREDENTIALS_CONFIG'e alias'lanır.
    3. vault.instance_id resolved instance_id'den ayrışırsa uyarı loglanır (spec §2.2.1).
    """
    # 1) Ayrı vault_config.yaml (config/ önce, sonra proje kökü)
    if "VAULT_CONFIG" not in config:
        for legacy in (os.path.join(config_dir, "vault_config.yaml"),
                       os.path.join(script_dir, "vault_config.yaml")):
            if os.path.exists(legacy):
                with open(legacy, "r", encoding="utf-8") as vf:
                    vault_cfg = yaml.safe_load(vf) or {}
                if "VAULT_CONFIG" in vault_cfg:
                    config["VAULT_CONFIG"] = vault_cfg["VAULT_CONFIG"]
                    print("[DEPRECATION] Ayrı 'vault_config.yaml' kullanılıyor. Yeni format "
                          "CREDENTIALS_CONFIG (spec §2.7) önerilir; bkz. spec Bölüm 6 migration.")
                break

    # 2) VAULT_CONFIG -> CREDENTIALS_CONFIG alias
    if "CREDENTIALS_CONFIG" not in config and "VAULT_CONFIG" in config:
        old = config["VAULT_CONFIG"]
        config["CREDENTIALS_CONFIG"] = {
            "enabled": old.get("enabled", False),
            "provider": "vault",
            "vault": {
                "vault_file": old.get("vault_file", "vault.yaml"),
                "instance_id": old.get("instance_id", ""),
            },
        }

    config.setdefault("CREDENTIALS_CONFIG", {"enabled": False, "provider": "none"})

    # 3) vault.instance_id ayrışma uyarısı (spec §2.2.1)
    vault_iid = config["CREDENTIALS_CONFIG"].get("vault", {}).get("instance_id", "")
    if vault_iid and sanitize_instance_id(vault_iid) != instance_id:
        print(f"[WARNING] vault.instance_id ('{vault_iid}') resolved instance_id ('{instance_id}')'den "
              f"farklı — Vault lookup ve path namespacing AYRIŞIYOR. Yalnızca legacy vault key "
              f"eşlemesi için kullanın (spec §2.2.1).")


def _deep_merge(base, override):
    """base'i taban alıp override'ı üzerine bindirir; override kazanır. base'i mutate etmez.

    - dict + dict -> recursive merge.
    - list / skaler / tip uyuşmazlığı -> override değeri base'i TÜMÜYLE değiştirir
      (örn. to_addrs listesi wholesale replace).
    """
    if not isinstance(base, dict) or not isinstance(override, dict):
        return copy.deepcopy(override)
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


def _apply_shared_config(config, script_dir, config_dir):
    """Org-geneli ortak section'ları config/shared.yaml'dan merge eder (spec §8.5).

    shared = taban (org varsayılanı), instance config = override (kazanır). shared.yaml yoksa
    hiçbir şey yapmaz (tümüyle opsiyonel, geriye dönük uyumlu).
    """
    shared_path = next((p for p in (os.path.join(config_dir, "shared.yaml"),
                                    os.path.join(script_dir, "shared.yaml")) if os.path.exists(p)), None)
    if shared_path is None:
        return
    with open(shared_path, "r", encoding="utf-8") as f:
        shared = yaml.safe_load(f) or {}

    for key, val in shared.items():
        if key not in SHAREABLE_SECTIONS:
            # Footgun koruması: instance'a özgü section shared'a konmuş — merge ETME, uyar.
            print(f"[WARNING] shared.yaml içindeki '{key}' paylaşılabilir bir section değil "
                  f"(izinli: {list(SHAREABLE_SECTIONS)}); yok sayıldı.")
            continue
        config[key] = _deep_merge(val, config.get(key, {}))


def load_config(config_path="config.yaml"):
    # script_dir = proje kökü (bu dosya modules/ altında olduğu için iki üst dizin)
    script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_dir = os.path.join(script_dir, "config")

    # Config arama sırası: (1) verilen yol, (2) proje kökü (geriye dönük uyum),
    # (3) config/ dizini (yeni standart yerleşim).
    candidates = [
        config_path,
        os.path.join(script_dir, config_path),
        os.path.join(config_dir, config_path),
    ]
    full_path = next((c for c in candidates if os.path.exists(c)), None)

    if full_path is None:
        print(f"[ERROR] Configuration file '{config_path}' not found! "
              f"Searched: {candidates}")
        sys.exit(1)

    try:
        with open(full_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[ERROR] Failed to parse config file: {e}")
        sys.exit(1)

    # Org-geneli ortak section'ları (MAIL/MONITORING) config/shared.yaml'dan merge et (spec §8.5)
    _apply_shared_config(config, script_dir, config_dir)

    # instance_id + resolved path'ler tek kaynaktan (spec §1, §3, §3.1)
    instance_id = resolve_instance_id(config)
    config["_resolved_instance_id"] = instance_id
    config["_resolved_paths"] = _resolve_paths(config, instance_id)

    # Credentials normalizasyonu + geriye dönük uyum (spec §2.8)
    _apply_credentials_config(config, script_dir, config_dir, instance_id)

    return config
