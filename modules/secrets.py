"""Secrets sağlayıcı soyutlaması: Vault / Local / Null / (ileride) CyberArk (orta katman).

Bkz. spec Bölüm 2. Çağıranlar (main) yalnızca iki metot bilir:
`get_db_credentials(instance_id)` ve `get_smtp_password(instance_id)`. Hangi backend'in
arkada çalıştığını bilmez — böylece ileride CyberArk'a geçişte main değişmez.

Provider seçimi GLOBAL'dir (tüm instance'lar aynı backend). Göreli secrets dosyaları
`script_dir/secrets/` altında aranır (spec §8.1).
"""

import os
import stat
import sys
from abc import ABC, abstractmethod

import hvac
import yaml

__all__ = [
    "SecretsProvider",
    "VaultSecretsProvider",
    "LocalSecretsProvider",
    "NullSecretsProvider",
    "CyberArkSecretsProvider",
    "get_secrets_provider",
]


def _read_kv2(client, secret_path):
    """HashiCorp Vault KV v2 secret'ını okur, {data} dict'ini döner."""
    parts = secret_path.strip("/").split("/", 1)
    mount_point = parts[0] if len(parts) > 1 else "secret"
    path = parts[1] if len(parts) > 1 else parts[0]
    if path.startswith("data/"):
        path = path[5:]
    read_response = client.secrets.kv.v2.read_secret_version(
        mount_point=mount_point,
        path=path,
        raise_on_deleted_version=True,
    )
    return read_response["data"]["data"]


class SecretsProvider(ABC):
    @abstractmethod
    def get_db_credentials(self, instance_id):
        """username, password, hostname/ip, db anahtarlarını içeren dict döner, yoksa None."""
        ...

    @abstractmethod
    def get_smtp_password(self, instance_id):
        ...


class VaultSecretsProvider(SecretsProvider):
    """Vault backend. vault.yaml formatı: VAULT_INSTANCES: {instance_id: {url, token,
    secret_path, db_secret_path}}."""

    def __init__(self, provider_config, secrets_dir, logger):
        self.logger = logger
        vault_file_raw = provider_config.get("vault_file", "vault.yaml")
        self.vault_file = vault_file_raw if os.path.isabs(vault_file_raw) else os.path.join(secrets_dir, vault_file_raw)
        if not os.path.exists(self.vault_file):
            logger.error(f"Vault file '{self.vault_file}' not found.")
            sys.exit(1)
        with open(self.vault_file, "r", encoding="utf-8") as vf:
            self.vault_data = yaml.safe_load(vf) or {}
        self.explicit_instance_id = provider_config.get("instance_id", "")

    def _resolve_lookup_key(self, instance_id):
        # explicit_instance_id yalnızca Vault lookup'ını etkiler (spec §2.2.1 uyarısı
        # load_config'te loglanır). Boşsa resolved instance_id kullanılır.
        return self.explicit_instance_id or instance_id

    def _get_instance_entry(self, instance_id):
        lookup_key = self._resolve_lookup_key(instance_id)
        instances = self.vault_data.get("VAULT_INSTANCES", {})
        if lookup_key not in instances:
            # FAIL-FAST: yanlış/eksik eşleşmeyi asla sessizce yutma (spec §2.8).
            self.logger.error(f"Vault instance '{lookup_key}' not found in '{self.vault_file}'. "
                              f"Available: {list(instances.keys())}")
            sys.exit(1)
        return instances[lookup_key]

    def _client(self, entry):
        client = hvac.Client(url=entry.get("url"), token=entry.get("token"))
        if not client.is_authenticated():
            self.logger.error("Vault authentication failed.")
            return None
        return client

    def get_db_credentials(self, instance_id):
        entry = self._get_instance_entry(instance_id)
        if not entry.get("db_secret_path"):
            return None
        self.logger.info("Connecting to HashiCorp Vault to fetch DB credentials...")
        try:
            client = self._client(entry)
            if client is None:
                return None
            data = _read_kv2(client, entry["db_secret_path"])
            self.logger.info("DB credentials retrieved successfully.")
            return data
        except Exception as e:
            self.logger.error(f"Vault DB credentials retrieval failed: {e}")
            return None

    def get_smtp_password(self, instance_id):
        entry = self._get_instance_entry(instance_id)
        secret_path = entry.get("secret_path")
        if not secret_path:
            self.logger.error("Vault secret_path for SMTP is empty or not provided.")
            return None
        self.logger.info("Connecting to HashiCorp Vault to fetch SMTP credentials...")
        try:
            client = self._client(entry)
            if client is None:
                sys.exit(1)
            data = _read_kv2(client, secret_path)
            password = data.get("smtp_password") or data.get("password")
            if not password:
                raise Exception("SMTP password key not found in Vault secret.")
            self.logger.info("SMTP credentials retrieved successfully.")
            return password
        except Exception as e:
            self.logger.error(f"Vault connection or secret retrieval failed: {e}")
            sys.exit(1)


class LocalSecretsProvider(SecretsProvider):
    """Vault/CyberArk yokken düz-metin fallback (spec §2.3).

    GÜVENLİK: credential'lar diskte plaintext. secrets_local.yaml .gitignore'da olmalı ve
    izni 600 tutulmalıdır. İzin geniş ise sadece WARNING (fail-fast YOK); ancak eksik
    instance eşleşmesi Vault ile simetrik olarak FAIL-FAST'tir.
    """

    def __init__(self, provider_config, secrets_dir, logger):
        self.logger = logger
        secrets_file_raw = provider_config.get("secrets_file", "secrets_local.yaml")
        self.secrets_file = secrets_file_raw if os.path.isabs(secrets_file_raw) else os.path.join(secrets_dir, secrets_file_raw)
        if not os.path.exists(self.secrets_file):
            logger.error(f"Local secrets file '{self.secrets_file}' not found.")
            sys.exit(1)
        self._check_file_permissions()
        with open(self.secrets_file, "r", encoding="utf-8") as f:
            self.data = yaml.safe_load(f) or {}

    def _check_file_permissions(self):
        mode = stat.S_IMODE(os.stat(self.secrets_file).st_mode)
        if mode & 0o077:
            # Karar: sadece uyar, ÇALIŞMAYI DURDURMA (spec §2.3, dosya-izni gevşetmesi).
            self.logger.warning(
                f"SECURITY: '{self.secrets_file}' dosya izinleri çok açık ({oct(mode)}). "
                f"Önerilen: chmod 600 {self.secrets_file}"
            )

    def _get_instance_entry(self, instance_id):
        instances = self.data.get("LOCAL_INSTANCES", {})
        if instance_id not in instances:
            # FAIL-FAST: eksik instance sessizce None dönmemeli — aksi halde '/ as sysdba'ya
            # düşülür ve "yanlış/eksik credential ile sessiz devam" hatası doğar (spec §2.3).
            self.logger.error(f"Local instance '{instance_id}' not found in '{self.secrets_file}'. "
                              f"Available: {list(instances.keys())}")
            sys.exit(1)
        return instances[instance_id]

    def get_db_credentials(self, instance_id):
        return self._get_instance_entry(instance_id).get("db")

    def get_smtp_password(self, instance_id):
        return self._get_instance_entry(instance_id).get("smtp_password")


class NullSecretsProvider(SecretsProvider):
    """CREDENTIALS_CONFIG.enabled=False / provider='none' iken. Script '/ as sysdba'
    (OS auth) fallback'ine sahiptir — bu bir hata değildir (spec §2.8)."""

    def get_db_credentials(self, instance_id):
        return None

    def get_smtp_password(self, instance_id):
        return None


class CyberArkSecretsProvider(SecretsProvider):
    """TODO (spec §2.5): CyberArk CCP REST API / AIM CLI entegrasyonu. Kontrat sabit —
    eklendiğinde main() değişmez, sadece factory'ye bir dal eklenir."""

    def get_db_credentials(self, instance_id):
        raise NotImplementedError

    def get_smtp_password(self, instance_id):
        raise NotImplementedError


def get_secrets_provider(credentials_config, script_dir, logger):
    """GLOBAL provider seçimi (spec §2.6). Göreli secrets dosyaları script_dir/secrets/ altında."""
    secrets_dir = os.path.join(script_dir, "secrets")
    provider_name = credentials_config.get("provider", "vault")

    if not credentials_config.get("enabled", True) or provider_name == "none":
        return NullSecretsProvider()
    if provider_name == "vault":
        return VaultSecretsProvider(credentials_config.get("vault", {}), secrets_dir, logger)
    if provider_name == "local":
        return LocalSecretsProvider(credentials_config.get("local", {}), secrets_dir, logger)
    if provider_name == "cyberark":
        raise NotImplementedError(
            "CyberArkSecretsProvider henüz implemente edilmedi. "
            "Eklemek için spec §2.5'teki iskeleti doldurup burada bir dal ekleyin."
        )
    raise ValueError(f"Bilinmeyen secrets provider: '{provider_name}'")
