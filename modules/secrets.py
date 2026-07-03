"""HashiCorp Vault entegrasyonu (orta katman).

NOT (Faz 1 / saf refactor): Bu modül şimdilik mevcut Vault fonksiyonlarını birebir
taşır. Spec §2'deki SecretsProvider soyutlaması (Vault/Local/Null/CyberArk) ayrı bir
fonksiyonel commit'te gelecek.
"""

import sys

import hvac

__all__ = ["get_vault_secret", "get_vault_db_credentials"]


def get_vault_secret(vault_config, logger):
    logger.info("Connecting to HashiCorp Vault to fetch SMTP credentials...")
    try:
        client = hvac.Client(url=vault_config.get("url"), token=vault_config.get("token"))
        if not client.is_authenticated():
            raise Exception("Vault authentication failed.")

        secret_path = vault_config.get("secret_path")
        if not secret_path:
            logger.error("Vault secret_path for SMTP is empty or not provided.")
            return None

        parts = secret_path.strip("/").split("/", 1)
        mount_point = parts[0] if len(parts) > 1 else "secret"
        path = parts[1] if len(parts) > 1 else parts[0]
        if path.startswith("data/"):
            path = path[5:]

        read_response = client.secrets.kv.v2.read_secret_version(
            mount_point=mount_point,
            path=path,
            raise_on_deleted_version=True
        )
        password = read_response['data']['data'].get('smtp_password')
        if not password:
            password = read_response['data']['data'].get('password')

        if not password:
            raise Exception("SMTP password key not found in Vault secret.")
        logger.info("SMTP credentials retrieved successfully.")
        return password
    except Exception as e:
        logger.error(f"Vault connection or secret retrieval failed: {e}")
        sys.exit(1)


def get_vault_db_credentials(vault_config, logger):
    if not vault_config.get("enabled", False) or not vault_config.get("db_secret_path"):
        return None
    logger.info("Connecting to HashiCorp Vault to fetch DB credentials...")
    try:
        client = hvac.Client(url=vault_config.get("url"), token=vault_config.get("token"))
        if not client.is_authenticated():
            raise Exception("Vault authentication failed.")

        secret_path = vault_config.get("db_secret_path")
        parts = secret_path.strip("/").split("/", 1)
        mount_point = parts[0] if len(parts) > 1 else "secret"
        path = parts[1] if len(parts) > 1 else parts[0]
        if path.startswith("data/"):
            path = path[5:]

        read_response = client.secrets.kv.v2.read_secret_version(
            mount_point=mount_point,
            path=path,
            raise_on_deleted_version=True
        )
        data = read_response['data']['data']
        logger.info("DB credentials retrieved successfully.")
        return data
    except Exception as e:
        logger.error(f"Vault DB credentials retrieval failed: {e}")
        return None
