"""Konfigürasyon yükleme (alt katman)."""

import os
import sys

import yaml

__all__ = ["load_config"]


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
            config = yaml.safe_load(f)

        # vault_config.yaml: önce config/ dizini, sonra proje kökü (geriye dönük uyum).
        for vault_config_path in (os.path.join(config_dir, "vault_config.yaml"),
                                  os.path.join(script_dir, "vault_config.yaml")):
            if os.path.exists(vault_config_path):
                with open(vault_config_path, "r", encoding="utf-8") as vf:
                    vault_cfg = yaml.safe_load(vf)
                    if vault_cfg and "VAULT_CONFIG" in vault_cfg:
                        config["VAULT_CONFIG"] = vault_cfg["VAULT_CONFIG"]
                break

        if "VAULT_CONFIG" not in config:
            config["VAULT_CONFIG"] = {"enabled": False}

        return config
    except Exception as e:
        print(f"[ERROR] Failed to parse config file: {e}")
        sys.exit(1)
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"[ERROR] Failed to parse config file: {e}")
        sys.exit(1)
