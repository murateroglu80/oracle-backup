# oracle-backup: Multi-Instance Config Refactor — Implementasyon Spec'i

> **Hedef sürüm:** v7.0.0 (multi-instance mimarisi; sürümleme 6.7.2 → 7.x.x)
> **Branch:** `MultiInstance`
>
> **Review turu (2026-07-03):** Spec mevcut `backup.py` (v6.7.2, 1425 satır) ile satır satır
> karşılaştırıldı. Aşağıdaki bölümlere kod-doğrulamalı düzeltmeler işlendi:
> - **§3.1 (yeni):** `history_dir` çift-kaynak hatası — `ensure_free_space`/`get_required_gb`
>   config'ten bağımsız okuyor; resolved path parametreyle yayılmalı (yoksa TypeError).
> - **§2.8:** Ayrı `vault_config.yaml` dosyası backward-compat'i (baskın kurulum deseni) + Vault
>   fail-fast'in OS-auth `/ as sysdba` fallback'inden ayrılması.
> - **§2.2.1 (yeni):** İki `instance_id` override mekanizmasının (top-level vs vault.instance_id)
>   çelişkisinin çözümü.
> - **§2.3:** `LocalSecretsProvider` eksik instance'da fail-fast (sessiz `None` yerine).
> - **§4:** `run_rman`'in `/tmp` hardcode'u da temp_dir kapsamına + `TMPDIR` env'inin tek başına
>   yetmediği uyarısı.
> - **§11.2:** `run_rman`'deki sabit 7200s timeout'un tespiti; `run_scp`/`run_rsync`'te backoff yokluğu.
> - **§7:** Yeni kabul kriterleri 19-23.
>
> **Dizin isimlendirme (uygulama kararı, 2026-07-03):** Paket `modules/` (spec'in ilk taslağındaki
> `rmanbackup/` DEĞİL), yaml yapılandırma dosyaları `config/` (ilk taslaktaki `conf.d/` DEĞİL),
> credential dosyaları `secrets/` (chmod 700). Bu doküman bu isimlerle hizalanmıştır.
>
> **Uygulama durumu (2026-07-03):** Faz 1 (saf refactor) commit'lendi (`5a5f0e1`). Faz 2 (bu spec'in
> fonksiyonel değişiklikleri) kodlandı ve offline doğrulandı; gerçek Oracle testi bekliyor.
> Watchdog DB-progress'te yalnızca Kontrol 1 (`v$rman_status`) uygulandı — Kontrol 2-4 (§11.4.1)
> ileride. Watchdog SSH yolu yalnızca lokal path üzerinden test edildi.

## Bağlam

`oracle-backup` (murateroglu80/oracle-backup) şu anda tek bir `config.yaml` = tek bir veritabanı
instance'ı varsayımıyla çalışıyor. 5 veritabanı / 3 host senaryosunda iki kritik boşluk tespit edildi:

1. Vault entegrasyonu (`vault_config.yaml`) script dizinine sabitlenmiş — `--config` parametreli
   çoklu instance kurulumunda tüm veritabanları aynı Vault secret'ını (dolayısıyla aynı DB
   credential'ını) paylaşıyor.
2. `log_dir`, `history_dir`, `pid_file` varsayılanları SID'e göre namespace'li değil — admin
   özelleştirmeyi unutursa farklı veritabanlarının history kayıtları aynı JSON dosyasında karışıyor,
   üstelik `append_history()` dosya kilidi olmadan read-modify-write yaptığı için eşzamanlı çalışan
   iki process birbirinin kaydını sessizce siliyor.

Bu doküman, üzerinde anlaşılan çözümün tam implementasyon tanımıdır.

---

## Karar Özeti (üzerinde anlaşıldı)

| Konu | Karar |
|---|---|
| `instance_id` kaynağı | Otomatik türetilir (`TARGET_SERVER.host` + `ORACLE_CONFIG.ORACLE_SID`), istenirse elle override edilebilir |
| `vault.yaml` konumu | Sabit değil — `config.yaml` içinde yol olarak belirtilebilir |
| `temp_dir` | Tek, configurable, global path (instance başına ayrı alt klasör YOK) |
| Secrets backend | Modüler `SecretsProvider` soyutlaması — Vault, Local (plaintext fallback), ileride CyberArk |
| Provider kapsamı | **Global tek provider** — tüm instance'lar aynı backend'i kullanır (instance başına karışık provider YOK) |
| Local provider dosya izni | İzin 600 değilse sadece WARNING logla, çalışmaya devam et (fail-fast YOK) |
| Config anahtar adı | `CREDENTIALS_CONFIG` (yeni ad). `VAULT_CONFIG` geriye dönük uyumluluk için alias olarak kabul edilir |
| Kod organizasyonu | Tek `backup.py` → `modules/` paketi; sorumluluk bazlı modüller, tek yönlü bağımlılık hiyerarşisi (Bölüm 9) |
| Log/history formatı | Vektör DB'ye hazır: JSONL structured log + `run_id` + versiyonlu history şeması + no-op ingest kancası (Bölüm 10) |
| Döngü güvenliği | `while True` yasak, kısa komutlara zorunlu timeout, backoff'lu sınırlı retry, RMAN'a otomatik retry YOK (Bölüm 11) |
| Uzun iş koruması | Duvar-saati timeout DEĞİL, canlılık bazlı **watchdog**: çıktı akışı + DB progress sorgusu (default True, kapatılabilir) + OS PID kontrolü; `max_runtime` opt-in (Bölüm 11.4) |

---

## 1. `instance_id` Çözümleme Mantığı

Yeni bir yardımcı fonksiyon:

```python
import re

def sanitize_instance_id(raw: str) -> str:
    """Lowercase, alfanumerik olmayan karakterleri '_' yapar, ardışık '_' karakterlerini sıkıştırır."""
    s = raw.strip().lower()
    s = re.sub(r"[^a-z0-9_-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "default"

def resolve_instance_id(config: dict) -> str:
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
```

**Önemli:** Bu fonksiyon `load_config()` içinde, dosya okunduktan hemen sonra çağrılmalı ve sonucu
`config["_resolved_instance_id"]` anahtarına yazılmalı — hem Vault lookup hem path namespacing bu
TEK değeri kullanmalı (tutarlılık şartı: ikisi farklı instance_id kullanırsa amacın tamamı boşa çıkar).

Script başlangıcında (main() içinde), log seviyesinde açıkça yazdır:
```python
logger.info(f"Resolved instance_id: {instance_id}")
```
Bu, 5 DB'lik bir filoda "hangi config hangi instance_id'ye denk geldi" sorusunu debug ederken hayat
kurtarır.

---

## 2. Secrets Provider Soyutlaması (Vault / Local / ileride CyberArk)

### 2.0 Neden gerekli

Mevcut kod `get_vault_db_credentials()` / `get_vault_secret()` fonksiyonlarında `hvac` client'ını
doğrudan çağırıyor — Vault'a sıkı bağımlılık var. İki yeni gereksinim bunu değiştiriyor:

1. **Vault sunucusu olmayan ortamlar** için manuel/plaintext bir fallback lazım (ideal değil, ama
   sistemi çalışır tutmak için gerekli — bu bilinçli bir ödünleşim).
2. **İleride CyberArk (veya başka bir secrets manager)'a geçiş** mevcut kodun geri kalanını
   etkilememeli — sadece yeni bir provider sınıfı eklenip config'te bir satır değişmeli.

Çözüm: ortak bir `SecretsProvider` arayüzü, backend-özel kod bunun arkasına gizlenir.

### 2.1 Ortak arayüz

```python
from abc import ABC, abstractmethod

class SecretsProvider(ABC):
    @abstractmethod
    def get_db_credentials(self, instance_id: str) -> dict | None:
        """username, password, hostname/ip, db anahtarlarını içeren dict döner, yoksa None."""
        ...

    @abstractmethod
    def get_smtp_password(self, instance_id: str) -> str | None:
        ...
```

`main()` ve diğer tüm çağıranlar **sadece bu iki metodu** bilir; hangi backend'in arkada çalıştığını
hiç bilmez. Bu, CyberArk geçişinde `main()` içindeki hiçbir satırın değişmemesini garanti eder.

### 2.2 `VaultSecretsProvider` (bugünkü davranışın taşınmış hali)

```python
class VaultSecretsProvider(SecretsProvider):
    def __init__(self, provider_config, script_dir, logger):
        self.logger = logger
        vault_file_raw = provider_config.get("vault_file", "vault.yaml")
        self.vault_file = vault_file_raw if os.path.isabs(vault_file_raw) else os.path.join(script_dir, vault_file_raw)
        if not os.path.exists(self.vault_file):
            logger.error(f"Vault file '{self.vault_file}' not found.")
            sys.exit(1)
        with open(self.vault_file, "r", encoding="utf-8") as vf:
            self.vault_data = yaml.safe_load(vf) or {}
        self.explicit_instance_id = provider_config.get("instance_id", "")

    def _resolve_lookup_key(self, instance_id):
        # UYARI (Bölüm 1 tutarlılık şartıyla çelişki riski): explicit_instance_id yalnızca Vault
        # lookup'ını etkiler, path namespacing'i ETKİLEMEZ. İkisi ayrışırsa "Vault'tan db1 creds,
        # ama history db2 klasörüne" hatası doğar. Bkz. Bölüm 2.2.1 — bu sub-override kısıtlanmıştır.
        return self.explicit_instance_id or instance_id

    def _get_instance_entry(self, instance_id):
        lookup_key = self._resolve_lookup_key(instance_id)
        instances = self.vault_data.get("VAULT_INSTANCES", {})
        if lookup_key not in instances:
            # FAIL-FAST: yanlış/eksik eşleşmeyi asla sessizce yutma.
            self.logger.error(f"Vault instance '{lookup_key}' not found in '{self.vault_file}'. "
                               f"Available: {list(instances.keys())}")
            sys.exit(1)
        return instances[lookup_key]

    def get_db_credentials(self, instance_id):
        entry = self._get_instance_entry(instance_id)
        client = hvac.Client(url=entry.get("url"), token=entry.get("token"))
        if not client.is_authenticated():
            self.logger.error("Vault authentication failed.")
            return None
        # ... mevcut read_secret_version mantığı, entry["db_secret_path"] kullanılarak ...

    def get_smtp_password(self, instance_id):
        entry = self._get_instance_entry(instance_id)
        # ... mevcut read_secret_version mantığı, entry["secret_path"] kullanılarak ...
```

`vault.yaml` formatı önceki spec'teki gibi kalır (`VAULT_INSTANCES: {instance_id: {url, token,
secret_path, db_secret_path}}`), tek fark artık bu dosyanın bir sınıfın içine kapsüllenmiş olması.

### 2.2.1 İki override mekanizması çelişkisinin çözümü (Bölüm 1 ile uyum)

Sistemde `instance_id` için **iki farklı** override noktası oluşuyor ve bunlar birbiriyle çelişebilir:

1. Top-level `INSTANCE_ID` (Bölüm 1) — hem path namespacing'i **hem** Vault lookup'ını aynı değerle
   sürer (tutarlılık şartı).
2. `CREDENTIALS_CONFIG.vault.instance_id` (§2.2, `explicit_instance_id`) — **yalnızca** Vault
   lookup key'ini etkiler, path'i etkilemez.

İkisi farklı değer alırsa, Vault'tan bir instance'ın credential'ı okunurken history/log başka bir
instance'ın klasörüne yazılır — Bölüm 1'in engellemeye çalıştığı hatanın ta kendisi. **Karar:**

- `resolve_instance_id()` (Bölüm 1) her zaman **tek yetkili kaynaktır**; `_resolved_instance_id`
  hem path hem Vault için kullanılır.
- `vault.instance_id` sub-override'ı **sadece** "vault.yaml içindeki anahtar, sistem instance_id'sinden
  farklı adlandırılmış" durumunu çözmek için vardır (örn. legacy vault key). Ayarlandığında,
  `load_config()` bir **tutarlılık uyarısı** loglar:
  `logger.warning("vault.instance_id ('X') resolved instance_id ('Y')'den farklı — Vault lookup ve path namespacing AYRIŞIYOR. Yalnızca legacy vault key eşlemesi için kullanın.")`
- Öneri: yeni kurulumlarda `vault.instance_id` **boş bırakılır**; vault.yaml anahtarları doğrudan
  `resolve_instance_id()` çıktısıyla adlandırılır. Böylece tek kaynak korunur.
- Kabul kriteri 2 genişletilir: `vault.instance_id` set edilip `_resolved_instance_id`'den farklı
  olduğunda uyarı loglanmalı; set edilmediğinde ikisi de aynı değeri kullanmalı.

### 2.3 `LocalSecretsProvider` (Vault sunucusu yokken manuel fallback)

```python
class LocalSecretsProvider(SecretsProvider):
    """
    Vault/CyberArk gibi bir secrets manager mevcut değilken kullanılacak düz-metin fallback.
    GÜVENLİK NOTU: credential'lar diskte plaintext tutulur. secrets_local.yaml dosyası
    .gitignore'da olmalı ve mümkün olan en kısıtlı dosya izniyle (600) tutulmalıdır.
    """
    def __init__(self, provider_config, script_dir, logger):
        self.logger = logger
        secrets_file_raw = provider_config.get("secrets_file", "secrets_local.yaml")
        self.secrets_file = secrets_file_raw if os.path.isabs(secrets_file_raw) else os.path.join(script_dir, secrets_file_raw)
        if not os.path.exists(self.secrets_file):
            logger.error(f"Local secrets file '{self.secrets_file}' not found.")
            sys.exit(1)
        self._check_file_permissions()
        with open(self.secrets_file, "r", encoding="utf-8") as f:
            self.data = yaml.safe_load(f) or {}

    def _check_file_permissions(self):
        import stat
        mode = stat.S_IMODE(os.stat(self.secrets_file).st_mode)
        if mode & 0o077:
            # Karar: sadece uyar, ÇALIŞMAYI DURDURMA.
            self.logger.warning(
                f"SECURITY: '{self.secrets_file}' dosya izinleri çok açık ({oct(mode)}). "
                f"Önerilen: chmod 600 {self.secrets_file}"
            )

    def _get_instance_entry(self, instance_id):
        instances = self.data.get("LOCAL_INSTANCES", {})
        if instance_id not in instances:
            # FAIL-FAST: eksik instance sessizce None dönmemeli — aksi halde script '/ as sysdba'
            # OS-auth'a düşer ve "yanlış/eksik credential ile sessiz devam" (bu refactor'un önlemek
            # istediği sınıf) Local backend'de yeniden doğar. Vault provider ile simetrik davranış.
            self.logger.error(f"Local instance '{instance_id}' not found in '{self.secrets_file}'. "
                               f"Available: {list(instances.keys())}")
            sys.exit(1)
        return instances[instance_id]

    def get_db_credentials(self, instance_id):
        return self._get_instance_entry(instance_id).get("db")

    def get_smtp_password(self, instance_id):
        return self._get_instance_entry(instance_id).get("smtp_password")
```

**Not:** Dosya-izni kuralı (§2.3'ün üstündeki `_check_file_permissions`) sadece WARNING'de kalır;
gevşetme YALNIZCA dosya izni içindir. Eksik instance eşleşmesi Vault'taki gibi fail-fast'tir.
Bu, Bölüm 2.8'deki "kritik davranış" kuralının Local backend'e de uygulanmış hâlidir.

`secrets_local.yaml` formatı, `vault.yaml` ile **kasıtlı olarak yapısal benzerlik** taşır (instance_id
bazlı sözlük) — ileride Vault'a geçişte veri taşıma (migration) kafa karıştırıcı olmasın diye:

```yaml
LOCAL_INSTANCES:
  db-server1.example.local_orcl1:
    db:
      username: "rman_backup"
      password: "DEĞİŞTİR_BENİ"
      hostname: "db-server1.example.local"
      db: "ORCL1"
    smtp_password: "DEĞİŞTİR_BENİ"
```

### 2.4 `NullSecretsProvider` (Vault/Local hiç kullanılmıyorsa)

```python
class NullSecretsProvider(SecretsProvider):
    """CREDENTIALS_CONFIG.enabled=False iken. Script zaten '/ as sysdba' (OS auth) fallback'ine sahip."""
    def get_db_credentials(self, instance_id):
        return None

    def get_smtp_password(self, instance_id):
        return None
```

### 2.5 `CyberArkSecretsProvider` (ileride, şimdi yazılmıyor)

```python
class CyberArkSecretsProvider(SecretsProvider):
    """
    TODO: CyberArk CCP REST API veya AIM CLI entegrasyonu.
    Kontrat sabit: get_db_credentials() / get_smtp_password() aynı şekilde çalışmalı.
    Eklendiğinde main() içinde HİÇBİR satır değişmeyecek — sadece factory'ye bir 'elif' eklenecek
    ve config.yaml'da provider: "cyberark" yazılacak.
    """
    pass  # Şimdilik implemente edilmiyor.
```

### 2.6 Factory — hangi provider aktif olacak (GLOBAL, tek karar)

```python
def get_secrets_provider(credentials_config, script_dir, logger):
    provider_name = credentials_config.get("provider", "vault")
    if not credentials_config.get("enabled", True) or provider_name == "none":
        return NullSecretsProvider()
    if provider_name == "vault":
        return VaultSecretsProvider(credentials_config.get("vault", {}), script_dir, logger)
    if provider_name == "local":
        return LocalSecretsProvider(credentials_config.get("local", {}), script_dir, logger)
    if provider_name == "cyberark":
        raise NotImplementedError(
            "CyberArkSecretsProvider henüz implemente edilmedi. "
            "Eklemek için Bölüm 2.5'teki iskeleti doldurup burada bir 'if' bloğu ekleyin."
        )
    raise ValueError(f"Bilinmeyen secrets provider: '{provider_name}'")
```

**Not — global kapsam kararı:** Provider seçimi `CREDENTIALS_CONFIG.provider` altında **tek bir
global değer** olarak tutulur; instance başına farklı provider desteklenmez. Yani "3 DB Vault'ta, 2 DB
CyberArk'ta" gibi karma bir geçiş senaryosu bu tasarımda **desteklenmiyor** — geçiş yapılacaksa tüm
instance'lar aynı anda geçer. (Bu bilinçli bir kapsam sınırlaması; ileride ihtiyaç olursa
`CREDENTIALS_CONFIG.provider`'ı instance bazlı hale getirmek küçük bir ek iş olur, ama şimdilik
gereksiz karmaşıklık eklememek için global tutuluyor.)

### 2.7 `config.yaml`'a yeni alanlar

```yaml
CREDENTIALS_CONFIG:
  enabled: True
  provider: "vault"              # "vault" | "local" | "cyberark" (henüz yok) | "none"

  vault:
    vault_file: "vault.yaml"     # script dizinine göre relatif VEYA mutlak path
    instance_id: ""              # opsiyonel override

  local:
    secrets_file: "secrets_local.yaml"

# Geriye dönük uyumluluk: eski "VAULT_CONFIG:" bloğu hâlâ destekleniyorsa,
# load_config() bunu otomatik olarak CREDENTIALS_CONFIG'e şu şekilde çevirir:
#   provider: "vault"
#   vault.vault_file  <- VAULT_CONFIG.vault_file
#   vault.instance_id <- VAULT_CONFIG.instance_id
```

### 2.8 `load_config()` değişiklikleri

```python
def load_config(config_path="config.yaml"):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    full_path = os.path.join(script_dir, config_path)
    if not os.path.exists(full_path):
        full_path = config_path
    if not os.path.exists(full_path):
        print(f"[ERROR] Configuration file '{full_path}' not found!")
        sys.exit(1)

    try:
        with open(full_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        print(f"[ERROR] Failed to parse config file: {e}")
        sys.exit(1)

    # NOT: Eski "unreachable second try" bloğu SİLİNDİ (dead code temizliği).

    instance_id = resolve_instance_id(config)
    config["_resolved_instance_id"] = instance_id

    # --- Geriye dönük uyumluluk: AYRI vault_config.yaml dosyası ---
    # KRİTİK: Mevcut kod (backup.py:50-55) Vault ayarını çoğunlukla config.yaml İÇİNDE değil,
    # script dizinindeki AYRI bir vault_config.yaml dosyasında tutar. Bu, prodüksiyondaki BASKIN
    # kurulum desenidir. Aşağıdaki alias yalnızca inline "VAULT_CONFIG:" bloğunu yakalarsa, ayrı
    # dosyada Vault tutan kurulumlar upgrade sonrası SESSİZCE credential kaybeder. O yüzden mevcut
    # davranış korunur: config.yaml içinde VAULT_CONFIG yoksa, ayrı vault_config.yaml okunur.
    if "VAULT_CONFIG" not in config:
        legacy_vault_path = os.path.join(script_dir, "vault_config.yaml")
        if os.path.exists(legacy_vault_path):
            with open(legacy_vault_path, "r", encoding="utf-8") as vf:
                vault_cfg = yaml.safe_load(vf) or {}
            if "VAULT_CONFIG" in vault_cfg:
                config["VAULT_CONFIG"] = vault_cfg["VAULT_CONFIG"]
                # NOT: load_config() henüz logger kurulmadan (main başında) çalışır — mevcut kod da
                # bu aşamada print() kullanıyor. Deprecation uyarısı burada print ile basılır; logger
                # kurulduktan sonra main() bir kez daha logger.warning ile tekrarlayabilir.
                print("[DEPRECATION] Ayrı 'vault_config.yaml' kullanılıyor. Yeni format "
                      "CREDENTIALS_CONFIG (Bölüm 2.7) önerilir; bkz. Bölüm 6 migration.")

    # --- Geriye dönük uyumluluk: VAULT_CONFIG -> CREDENTIALS_CONFIG alias ---
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
    return config
```

`main()` içinde tek satırlık kullanım:
```python
secrets_provider = get_secrets_provider(config["CREDENTIALS_CONFIG"], script_dir, logger)
db_creds = secrets_provider.get_db_credentials(instance_id)
...
smtp_password = secrets_provider.get_smtp_password(instance_id)
```

**Kritik davranış (Vault/CyberArk için değişmedi):** İlgili provider'da instance eşleşmesi
bulunamazsa script **çökmeli** (fail-fast), yanlış/boş credential ile sessizce devam etmemeli. Bu
kural yalnızca `LocalSecretsProvider`'ın dosya-izni kontrolü için gevşetildi (sadece WARNING) —
credential eşleşmesi bulunamaması durumu için değil.

**Fail-fast ile OS-auth (`/ as sysdba`) fallback'inin ayrımı — mevcut davranışı kırma:** Bugün kod
`get_db_credentials()` `None` döndüğünde her yerde `/ as sysdba` OS-auth'a düşüyor
(`check_standby_exists`, `run_rman`'in SQL doğrulaması, RMAN raporu sorgusu — hepsi bu fallback'i
kullanıyor). Fail-fast bu **meşru** fallback'i yok etmemeli. Ayrım şu şekilde netleştirilir:

- **`provider: "none"` / `enabled: False` (NullSecretsProvider):** `get_db_credentials()` `None` döner,
  script `/ as sysdba` ile normal çalışır. **Bu bir hata DEĞİLDİR** (OS-auth kurulumları için birinci
  sınıf desteklenen senaryo).
- **`provider: "vault"` veya `"local"` ama instance eşleşmesi yok:** fail-fast (sys.exit). Yani
  "credential arayacağını söyledin ama bulunamadı" = hata; "credential aramıyorum" = OS-auth ile devam.
- **Vault sunucusu erişilemez / auth başarısız** (instance tanımlı ama Vault down): fail-fast — sessizce
  OS-auth'a düşmek, yanlış DB'yi yedekleme riskini maskeler.

Yeni kabul kriteri (Bölüm 7'ye — bkz. kriter 19): `CREDENTIALS_CONFIG.enabled: False` (veya
`provider: "none"`) ile, Vault/secrets dosyası hiç olmadan script `/ as sysdba` kullanıp backup'ı
normal tamamlamalı (OS-auth kurulumları upgrade sonrası kırılmamalı).

---

## 3. Path Namespacing: `log_dir` / `history_dir` / `pid_file`

`main()` içindeki mevcut blok:

```python
log_dir = os.path.expanduser(BACKUP_CONFIG.get("log_dir", "~/huaris/logs"))
history_dir = os.path.expanduser(BACKUP_CONFIG.get("history_dir", "~/huaris/history"))
pid_file = os.path.expanduser(BACKUP_CONFIG.get("pid_file", "/tmp/rman_backup.pid"))
```
(Not: mevcut default `backup.py:836`'da `/tmp/rman_backup.pid`'dir — SID eki yoktur; işte namespacing'in
gereği tam da budur, çünkü SID'siz sabit pid tüm instance'ları aynı kilide sokar.)

şu şekilde değişmeli:

```python
instance_id = config["_resolved_instance_id"]

log_dir = os.path.expanduser(
    BACKUP_CONFIG.get("log_dir") or f"~/huaris/logs/{instance_id}"
)
history_dir = os.path.expanduser(
    BACKUP_CONFIG.get("history_dir") or f"~/huaris/history/{instance_id}"
)
pid_file = os.path.expanduser(
    BACKUP_CONFIG.get("pid_file") or f"/tmp/rman_backup_{instance_id}.pid"
)
temp_dir = os.path.expanduser(BACKUP_CONFIG.get("temp_dir", "/tmp"))
```

**Davranış kuralı:** Alan `config.yaml`'da **boş/yok** ise otomatik namespace edilir. Admin **elle**
bir değer verdiyse (mevcut production config'ler gibi), o değer olduğu gibi kullanılır — otomatik
namespace ile üzerine yazılmaz. Bu, geriye dönük uyumluluk için zorunlu (bkz. Bölüm 6).

### 3.1 KRİTİK: resolved path'ler tek kaynaktan yayılmalı (çift-kaynak hatası)

Mevcut kodda `history_dir`'in **iki bağımsız okuma noktası** var ve bu, Bölüm 1'in `instance_id` için
uyardığı "iki kaynak farklı değer üretirse amaç boşa çıkar" hatasının `history_dir`'de zaten var olan
hâlidir:

- `main()` içinde: `history_dir = os.path.expanduser(BACKUP_CONFIG.get("history_dir", ...))` — bu bir
  **local değişken**, geri config'e yazılmıyor.
- `get_required_gb()` (`backup.py:319`) ve `ensure_free_space()` (`:348`) ise `history_dir`'i
  **doğrudan config'ten** okuyor: `history_dir = backup_config.get("history_dir")`.

Yeni namespacing'de `history_dir` config'te **boş bırakılabildiği** için bu iki fonksiyon `None` alır →
`get_history_file(None)` → `os.path.join(None, ...)` → **TypeError, backup çöker**.

**Zorunlu kural:** `log_dir` / `history_dir` / `pid_file` / `temp_dir` bir kez `load_config()`
(veya main başında) resolve edilir ve **her tüketici bu resolved değeri parametreyle alır** — hiçbir
alt fonksiyon path'i tekrar `config`'ten okumaz. Modüler geçişte (Bölüm 9) `space.py` ve `history.py`
fonksiyonları imzalarında `history_dir: str` parametresi almalı; `backup_config` dict'inden path
türetmeleri YASAK. `config["_resolved_paths"] = {"log_dir": ..., "history_dir": ..., ...}` gibi tek bir
resolved sözlük tutmak, `instance_id`'deki `_resolved_instance_id` yaklaşımıyla simetrik önerilir.

Kabul kriteri (Bölüm 7'ye eklenir): `history_dir` config'te hiç verilmeden çalıştırıldığında
`ensure_free_space` dahil tüm history okuma yolları namespaced path'i kullanmalı, hiçbir yerde `None`
path'e düşülmemeli.

---

## 4. `temp_dir` — Tek Configurable Path

`config.example.yaml`'a yeni alan:

```yaml
BACKUP_CONFIG:
  ...
  temp_dir: "/tmp"   # SQL dump ve geçici dosyalar için. Instance başına ayrılmaz — hepsi bu tek path'i paylaşır.
```

Değişecek yerler:

1. `main()` içindeki env inject bloğu:
```python
env["TMP"] = temp_dir
env["TMPDIR"] = temp_dir
```

2. `execute_oracle_sql()` imzası `temp_dir` parametresi alacak şekilde genişletilmeli:
```python
def execute_oracle_sql(ssh_client, conn_str, sql_content, logger, env_dict, temp_dir="/tmp", timeout=None, quiet=True):
    cmd = f"""SQL_TMP=$(mktemp {temp_dir}/oracle_query_XXXXXX.sql)
cat << 'EOF' > "$SQL_TMP"
{sql_content}
EOF
sqlplus -s '{conn_str}' @"$SQL_TMP"
rm -f "$SQL_TMP"
"""
    return run_command_wrapper(ssh_client, cmd, logger, env_dict=env_dict, timeout=timeout, quiet=quiet)
```

3. `main()` içindeki tüm `execute_oracle_sql(...)` çağrı noktaları (`test_query`, `test_db`, RMAN
   raporu sorgusu) `temp_dir=temp_dir` parametresiyle güncellenmeli — üç çağrı noktası da taranmalı.

4. **`run_rman()` de `/tmp`'e yazıyor — atlanmamalı** (`backup.py:429`):
   ```python
   cmd = f"""RMAN_TMP=$(mktemp /tmp/rman_script_XXXXXX.rman)
   ```
   Bu satır da `temp_dir`'i kullanacak şekilde güncellenmeli (`mktemp {temp_dir}/rman_script_XXXXXX.rman`).
   `run_rman` imzası da `temp_dir="/tmp"` parametresi almalı ve tüm çağrı noktalarına (`ensure_free_space`
   içindeki cleanup çağrıları dahil) geçirilmeli. `grep -n "mktemp" backup.py` ile İKİ nokta da doğrulanmalı.

**Kritik uyarı — `TMPDIR` env ayarı tek başına YETMEZ:** Kodda `env["TMP"]/["TMPDIR"] = temp_dir`
ayarlansa bile, `mktemp`'e **explicit `/tmp/...` template** verildiğinde bu template `$TMPDIR`'i
override eder — yani sadece env değişkenini set etmek geçici dosyaları taşımaz. Template string'inin
kendisi (`{temp_dir}/...`) değişmek zorundadır. Env ayarı, template'siz `mktemp` çağıran alt
process'ler (sqlplus/rman'in kendi iç geçici dosyaları) için hâlâ faydalıdır, o yüzden kalır.

---

## 5. Ek Sağlamlaştırma: History Dosya Kilidi (Madde 2'nin doğal uzantısı)

`append_history()` ve `mark_history_deleted()` şu anda kilitsiz read-modify-write yapıyor. Path
namespacing race condition riskini büyük ölçüde azaltsa da (farklı instance'lar artık farklı dosyaya
yazıyor), **aynı instance** için art arda hızlı tetiklemelerde (örn. cron çakışması, manuel + otomatik
tetikleme üst üste binmesi) hâlâ risk var. Atomic write + dosya kilidi ekleniyor:

```python
import fcntl

def _atomic_write_history(h_file, data):
    tmp_path = h_file + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=4)
    os.replace(tmp_path, h_file)  # atomic rename, aynı filesystem içinde

def append_history(history_dir, record):
    h_file = get_history_file(history_dir)
    lock_path = h_file + ".lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            data = []
            if os.path.exists(h_file):
                try:
                    with open(h_file, "r") as f:
                        data = json.load(f)
                except Exception:
                    pass
            data.append(record)
            _atomic_write_history(h_file, data)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
    # Bölüm 10.3 ile birleşik: kilit BIRAKILDIKTAN sonra ingest kancası çağrılır (kilit tutarken
    # değil — hook ileride yavaş/bloklayıcı olabilir). on_record_written asla exception yükseltmez.
    on_record_written(record)

def mark_history_deleted(history_dir, deleted_dir_path):
    h_file = get_history_file_for_dir(history_dir, deleted_dir_path)
    if not os.path.exists(h_file):
        return
    lock_path = h_file + ".lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            try:
                with open(h_file, "r") as f:
                    data = json.load(f)
            except Exception:
                return
            updated = False
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for record in data:
                if record.get("directory", "").startswith(deleted_dir_path):
                    if not record.get("is_deleted"):
                        record["is_deleted"] = True
                        record["deleted_at"] = now_str
                        updated = True
            if updated:
                _atomic_write_history(h_file, data)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
```

`fcntl` yalnızca Linux/Unix'te çalışır — bu proje zaten Oracle DB sunucularına (Linux) hedeflendiği
için sorun değil, ama import satırına yorum olarak not düşülmeli.

---

## 6. Geriye Dönük Uyumluluk / Migrasyon Notları

- **Mevcut `config.yaml` dosyaları** (yeni alanlar eklenmeden) hatasız çalışmaya devam etmeli.
  `INSTANCE_ID`, `VAULT_CONFIG.vault_file`, `VAULT_CONFIG.instance_id`, `BACKUP_CONFIG.temp_dir`
  hepsi opsiyonel, mantıklı defaultlarla.
- **Mevcut prodüksiyon `log_dir`/`history_dir`/`pid_file` değerleri** (örn. MIP kurulumunda halihazırda
  `~/huaris/logs` gibi elle/default değerler kullanılıyorsa): upgrade sonrası bu alanlar config.yaml'da
  hâlâ **açıkça dolu** ise davranış değişmez. Eğer admin bu alanları config.yaml'dan tamamen
  kaldırırsa (yeni otomatik namespaced davranışa geçmek isterse), **eski history dosyası yeni path'e
  taşınmadığı sürece geçmiş kayıtlar "kaybolmuş" gibi görünür** (aslında eski path'te duruyor, sadece
  script artık farklı bir dosyaya bakıyor). Bu geçiş öncesi admin'e açıkça bildirilmeli/dokümante
  edilmeli; otomatik bir taşıma scripti gerekmez ama README'de bir "Migration" bölümü şart.
- **`vault_config.yaml` → `vault.yaml` dönüşümü:** eski düz `VAULT_CONFIG:` formatı yeni
  `VAULT_INSTANCES: {instance_id: {...}}` formatına manuel taşınmalı. Tek DB'li kurulumlar için de bu
  formata geçiş zorunlu olsun (iki farklı format desteklemek, ilerideki bug yüzeyini büyütür).
- **`VAULT_CONFIG` → `CREDENTIALS_CONFIG` alias:** Bölüm 2.8'deki otomatik dönüştürme sayesinde eski
  `VAULT_CONFIG:` bloğu içeren config.yaml dosyaları **kod değişikliği gerekmeden** çalışmaya devam
  eder (provider otomatik olarak `"vault"` kabul edilir). Yeni kurulumlarda doğrudan
  `CREDENTIALS_CONFIG:` kullanılması önerilir; `VAULT_CONFIG` desteği ileride (örn. bir majör
  versiyon sonrasında) kaldırılabilir — şimdilik deprecation warning basmak yeterli, hard-break yok.
- **`requirements.txt`:** `LocalSecretsProvider` ek bir bağımlılık gerektirmez (sadece `PyYAML`,
  zaten mevcut). `hvac` bağımlılığı yalnızca `provider: "vault"` iken fiilen kullanılır ama import
  hâlâ dosya başında olduğundan kaldırılmıyor; ileride CyberArk SDK'sı eklenirse aynı şekilde
  koşulsuz import edilecek (opsiyonel/lazy import'a çevirmek ayrı bir iyileştirme olarak not
  düşülebilir, bu refactor'ın kapsamı dışında).

---

## 7. Kabul Kriterleri (Test Mühendisi Bakış Açısıyla)

1. Aynı script klasöründen, aynı `vault.yaml`'ı paylaşan 2 farklı `config.yaml` (2 farklı SID)
   çalıştırıldığında: farklı `instance_id`, farklı Vault kaydı, farklı `log_dir`/`history_dir`/
   `pid_file` üretilmeli. (`--config db1.yaml` ve `--config db2.yaml` art arda çalıştırılıp
   `_resolved_instance_id` log satırları karşılaştırılarak doğrulanabilir.)
2. `INSTANCE_ID` elle override edildiğinde, hem Vault lookup hem path namespacing **aynı** override
   edilmiş değeri kullanmalı (ikisi arasında tutarsızlık olmamalı).
3. `vault.yaml` içinde eşleşen `instance_id` bulunamazsa script **exit code ≠ 0** ile durmalı, asla
   sessizce `enabled: False` gibi davranmamalı.
4. İki process aynı `history_dir`'e (aynı instance_id, üst üste tetiklenmiş iki run) eşzamanlı
   `append_history()` çağırdığında, hiçbir kayıt kaybolmamalı — `N` eşzamanlı çağrı sonunda dosyada
   tam `N` kayıt olmalı (basit bir concurrent-write testiyle doğrulanabilir).
5. Hiçbir yeni alan eklenmeden (eski config.yaml ile) script hatasız çalışmalı, ama
   `Resolved instance_id: ...` log satırı görünmeli (admin'in yeni davranıştan haberdar olması için).
6. `temp_dir` özelleştirildiğinde (`/data/tmp` gibi), `--test-query` ile çalıştırılan bir sorgunun
   geçici SQL dosyasının o klasörde oluştuğu (ve silindiği) doğrulanmalı.
7. `CREDENTIALS_CONFIG.provider: "local"` ile çalıştırıldığında, Vault sunucusu hiç ayakta olmadan
   (hatta `VAULT_CONFIG`/`vault.yaml` dosyası hiç mevcut olmadan) script credential'ları
   `secrets_local.yaml`'dan okuyup normal şekilde tamamlanmalı.
8. `secrets_local.yaml` dosya izni `644` gibi geniş bırakıldığında script **WARNING loglayıp
   çalışmaya devam etmeli**, durmamalı.
9. Eski `VAULT_CONFIG:` bloğu içeren bir config.yaml, hiçbir değişiklik yapılmadan aynı şekilde
   çalışmaya devam etmeli (alias dönüşümü doğrulaması).
10. (İleriye dönük, implementasyon gerektirmez) `provider: "cyberark"` verildiğinde script net bir
    `NotImplementedError` ile durmalı — "sessizce vault'a düşme" gibi belirsiz bir davranış olmamalı.
11. `--config db1.yaml` gibi çıplak dosya adı verildiğinde `config/db1.yaml` bulunmalı; tam yol
    verildiğinde eski davranış korunmalı.
12. Aynı `TARGET_SERVER.host`'u hedefleyen iki instance eşzamanlı başlatıldığında ikincisi host
    kilidinde beklemeli; `host_lock_timeout_min` aşılırsa FAILED ile (history'ye kayıt düşerek)
    çıkmalı. Farklı host'ları hedefleyen instance'lar birbirini BEKLEMEMELİ.
13. `--status` komutu, hiçbir instance'ın backup akışını etkilemeden (lock almadan, SSH açmadan)
    tüm instance'ların son durum özetini basmalı; bir instance FAILED ise exit code 1, son kaydı
    26 saatten eskiyse exit code 2 dönmeli.
14. (Modülerlik) Paket bölünmesi sonrası, kod tabanında `grep -rn "while True"` sonucu **boş**
    olmalı; üst katman modülleri (`rman`, `transfer`, `mailing`, `monitoring`, `space`, `status`)
    birbirini import etmemeli (basit bir import-graph kontrolüyle doğrulanabilir).
15. (Structured logging) Bir run tamamlandığında hem `.log` hem `.jsonl` dosyası oluşmalı; `.jsonl`
    içindeki her satır geçerli JSON olmalı ve tüm satırlar ile o run'ın history kaydı **aynı
    `run_id`** değerini taşımalı.
16. (Kısa komut timeout) `command_timeout_sec: 5` gibi kasıtlı düşük bir değerle, uzun süren bir
    KISA-sınıf test komutu çalıştırıldığında script asılı kalmamalı; TIMEOUT hatasıyla FAILED
    kaydı düşüp normal şekilde sonlanmalı.
16a. (Watchdog — meşru uzun iş kesilmemeli) `idle_timeout_min: 2` ile, sürekli çıktı üreten uzun
    bir sahte iş (`for i in $(seq 1 300); do echo tick; sleep 1; done`) 5 dakika boyunca
    KESİLMEDEN tamamlanmalı — süre idle_timeout'un üstünde olsa bile, çünkü çıktı akıyor.
16b. (Watchdog — stall yakalanmalı) Aynı config ile hiç çıktı üretmeyen bir sahte iş
    (`sleep 600`), DB progress ve PID sinyalleri de sessizken ~2 dakika sonunda STALLED/FAILED
    olarak sonuçlanmalı; history'de "STALLED" içeren kayıt oluşmalı.
16c. (Watchdog — esneklik) `watchdog.enabled: False` iken hiçbir canlılık kontrolü yapılmamalı
    (eski sınırsız-bekleme davranışı); `progress_check_enabled: False` iken DB'ye hiçbir izleme
    sorgusu atılmadığı log'dan doğrulanmalı.
16d. (Watchdog — DB'yi yormama) `progress_check_interval_min: 5` ile 30 dakikalık bir run
    boyunca en fazla ~6 progress sorgusu atılmalı (log sayımıyla doğrulanabilir).
17. (RMAN retry yasağı) RMAN aşaması FAILED olduğunda aynı run içinde ikinci bir RMAN denemesi
    yapılmadığı log'dan doğrulanmalı (tek "Starting RMAN" satırı).
18. (Lock SKIPPED kaydı) pid_file kilidi alınamayıp script çekildiğinde history'de
    `status: "SKIPPED"` kaydı oluşmalı ve `--status` çıktısında görünmeli.
19. (OS-auth kırılmamalı — Bölüm 2.8) `CREDENTIALS_CONFIG.enabled: False` (veya `provider: "none"`)
    ile, hiçbir Vault/secrets dosyası olmadan script `/ as sysdba` OS-auth kullanıp backup'ı normal
    tamamlamalı. Instance eşleşmesi aramadığı için fail-fast ETMEMELİ.
20. (history_dir çift-kaynak — Bölüm 3.1) `history_dir` config'te hiç verilmeden (otomatik namespace)
    çalıştırıldığında `ensure_free_space`/`get_required_gb` dahil TÜM history okuma yolları namespaced
    path'i kullanmalı; hiçbir yerde `None` path'e düşülüp TypeError alınmamalı.
21. (temp_dir tam kapsam — Bölüm 4) `temp_dir: /data/tmp` iken hem SQL geçici dosyası (`execute_oracle_sql`)
    HEM DE RMAN script geçici dosyası (`run_rman`) o klasörde oluşup silinmeli; `/tmp`'te hiçbir
    `oracle_query_*`/`rman_script_*` dosyası kalmamalı. `grep -n "mktemp" backup.py` → iki noktanın da
    `temp_dir` kullandığı doğrulanmalı.
22. (Local fail-fast — Bölüm 2.3) `provider: "local"` iken `secrets_local.yaml`'da eşleşmeyen bir
    instance_id ile çalıştırıldığında script exit code ≠ 0 ile durmalı (sessizce `/ as sysdba`'ya
    DÜŞMEMELİ) — dosya-izni gevşetmesi yalnızca izinler içindir, eksik instance için değil.
23. (Vault key ayrışma uyarısı — Bölüm 2.2.1) `vault.instance_id` set edilip `_resolved_instance_id`'den
    farklı olduğunda tutarlılık WARNING'i loglanmalı; boş bırakıldığında Vault lookup ve path
    namespacing aynı değeri kullanmalı.
24. (Ortak config — `config/shared.yaml`) `shared.yaml`'da `MAIL_CONFIG` tanımlıyken, MAIL_CONFIG'i hiç
    içermeyen bir instance config yüklendiğinde `config["MAIL_CONFIG"]` shared değerini almalı; instance
    bir anahtarı (örn. notification_level) override ederse o anahtar instance'tan, diğerleri shared'dan
    gelmeli; `to_addrs` gibi listeler wholesale değişmeli. `shared.yaml` yoksa davranış değişmemeli
    (backward compat). shared.yaml'a whitelist dışı bir section (örn. ORACLE_CONFIG) konursa WARNING
    loglanıp yok sayılmalı.

---

## 8. Ölçekleme Hazırlığı ve Standart Dizin Yerleşimi (Faz 1'e dahil edilen ek kararlar)

### 8.1 Standart dizin yerleşimi (config ve secrets karmaşasını önlemek için)

Tüm instance config'leri ve secrets dosyaları için sabit, öngörülebilir bir yerleşim:

```
oracle-backup/
├── backup.py
├── run.sh
├── config/                        # TÜM instance config'leri burada
│   ├── db-server1_orcl1.yaml     # dosya adı = instance_id (kural, zorunlu değil ama önerilen)
│   ├── db-server1_prod2.yaml
│   └── db-server2_hr.yaml
├── secrets/                       # TÜM credential dosyaları burada (chmod 700 dizin)
│   ├── vault.yaml                 # provider: "vault" iken
│   └── secrets_local.yaml         # provider: "local" iken
├── fleet.yaml                     # instance envanteri (Bölüm 8.3)
└── ...
```

Davranış kuralları:
- `--config` parametresi göreli bir yol/dosya adı ise arama sırası: (1) verilen yol olduğu gibi,
  (2) `script_dir/config/<ad>`, (3) `script_dir/<ad>`. Böylece `--config db-server1_orcl1.yaml`
  yazmak yeterli olur.
- `CREDENTIALS_CONFIG.vault.vault_file` / `local.secrets_file` göreli verildiğinde varsayılan taban
  dizin `script_dir/secrets/` olur (mutlak yol verilirse olduğu gibi kullanılır).
- Kurulum/ilk çalıştırmada `secrets/` dizini yoksa oluşturulur ve izni `700` yapılır; dizin izni
  `700`'den genişse WARNING loglanır (LocalSecretsProvider'ın dosya-izni kuralıyla tutarlı: uyar,
  durdurma).
- `config.example.yaml` → `config/config.example.yaml`, eski `vault_config.example.yaml` →
  `secrets/vault.example.yaml` (yeni `VAULT_INSTANCES` formatında) olarak taşınır; ayrıca
  `secrets/secrets_local.example.yaml` ve `config/fleet.example.yaml` eklenir. README yolları güncellenir.

**Not (Madde 1 / config drift): UYGULANDI — `config/shared.yaml` (2026-07-03).** Org-geneli ortak
section'lar (`MAIL_CONFIG` + `MONITORING_CONFIG`) tek bir `config/shared.yaml`'da bir kez tanımlanır;
`load_config()` bunları her instance config'ine deep-merge eder (shared = taban, instance = override,
instance kazanır; liste alanları wholesale replace). Whitelist `SHAREABLE_SECTIONS = ("MAIL_CONFIG",
"MONITORING_CONFIG")` — dışındaki section'lar (ORACLE_CONFIG/TARGET_SERVER/BACKUP_CONFIG gibi
instance'a özgü olanlar) shared.yaml'a konursa WARNING loglanıp yok sayılır. `shared.yaml` yoksa
davranış eskisiyle birebir aynıdır (opsiyonel, geriye dönük uyumlu). İleride RMAN_TEMPLATE'i de ortak
yapmak = whitelist tuple'ına bir satır. Kurumda tek e-posta/monitoring ayarını 5 DB'de tekrar yazma
ve drift sorununu çözer.

### 8.2 Host bazlı lock (Faz 1 kapsamında)

Mevcut `pid_file` kilidi instance bazlıdır; aynı host'a yedek alan iki farklı instance birbirini
görmez ve `ensure_free_space()` process-local çalıştığı için ikisi de "alan yeterli" kararı verip
eşzamanlı yazmaya başlayabilir. Çözüm: instance kilidine ek olarak **host bazlı ikinci bir kilit**.

```python
def acquire_host_lock(backup_config, target_server, instance_id, logger, timeout_min=None):
    """
    Aynı TARGET_SERVER.host'a yedek alan instance'ları serileştirir.
    Lock dosyası: {temp_dir}/rman_hostlock_{sanitize(host)}.lock
    Davranış: kilit doluysa bekle (poll, örn. 30 sn aralık); timeout_min (config:
    BACKUP_CONFIG.host_lock_timeout_min, default 120) aşılırsa hata ile çık.
    fcntl.flock LOCK_EX | LOCK_NB ile alınır; process ölürse kernel kilidi otomatik bırakır
    (stale-PID problemi yaşanmaz — bu yönüyle mevcut pid_file yaklaşımından daha sağlamdır).
    """
```

- Kilit **RMAN backup + transfer** süresince tutulur, mail/rapor aşamasından önce bırakılır
  (mail göndermek disk yarışına girmez, hostu gereksiz kilitlemesin).
- `TARGET_SERVER.enabled: False` (lokal çalışma) durumunda host anahtarı `"local"` kabul edilir.
- Config'e yeni alan: `BACKUP_CONFIG.host_lock_timeout_min` (default 120) ve
  `BACKUP_CONFIG.host_lock_enabled` (default True — kapatma imkânı acil durumlar için).
- Kabul kriteri: aynı host'u hedefleyen iki instance eşzamanlı tetiklendiğinde, ikincisi birincinin
  RMAN+transfer aşaması bitene kadar beklemeli; log'da "Waiting for host lock..." satırı görünmeli.

### 8.3 Fleet envanteri — veri sözleşmesi ŞİMDİ, orkestratör UI fazında

UI fazında yeniden tasarım zorunluluğu doğmaması için, instance envanterinin formatı bu fazda
sabitlenir. Orkestratör (fleet_runner) bu fazda YAZILMAZ.

`fleet.yaml`:

```yaml
FLEET:
  - instance_id: "db-server1_orcl1"
    config: "config/db-server1_orcl1.yaml"
    enabled: true
    description: "Üretim ORCL1 - MIP"
  - instance_id: "db-server1_prod2"
    config: "config/db-server1_prod2.yaml"
    enabled: true
    description: ""
```

- Bu dosya Faz 1'de **yalnızca `--status` modu tarafından okunur** (Bölüm 8.4); backup akışını
  etkilemez, yoksa da her şey çalışır.
- UI fazında FastAPI backend'i instance listesini bu dosyadan alacak; job tetikleme de
  `config` alanındaki yolu kullanacak. Format şimdi sabitlendiği için UI fazında migration gerekmez.

### 8.4 `--status` modu (test/babysitting yükünü hafifletmek için, Faz 1 kapsamında)

Yeni CLI argümanı: `python3 backup.py --status`

- `fleet.yaml` varsa oradaki tüm instance'ları, yoksa `config/*.yaml` dosyalarını tarar.
- Her instance için ilgili `history_dir`'deki son kaydı okur ve tek bir özet tablo basar:

```
INSTANCE               LAST RUN             STATUS    SIZE(GB)  DURATION  TRANSFERRED
db-server1_orcl1       2026-07-03 02:00:11  SUCCESS   142.3     01:12:44  YES
db-server1_prod2       2026-07-03 02:05:37  SUCCESS   88.1      00:41:02  YES
db-server2_hr          2026-07-02 02:00:09  FAILED    0         00:00:31  NO
```

- Salt-okunur mod: hiçbir lock almaz, SSH bağlantısı kurmaz, RMAN çalıştırmaz — sadece lokal history
  JSON'larını okur. Bu sayede istenildiği kadar sık çalıştırılabilir (watch, cron+mail, vb.).
- Çıkış kodu: tüm instance'lar son çalışmada SUCCESS ise 0, herhangi biri FAILED ise 1, herhangi
  birinin son kaydı X saatten (config edilebilir, default 26) eskiyse 2 ("hiç çalışmamış/atlanmış"
  durumunu da yakalamak için). Böylece `--status` doğrudan Zabbix/Nagios external check olarak da
  kullanılabilir.
- Bu mod, UI dashboard'unun "instance özet" ekranının komut satırı öncüsüdür; UI fazında aynı okuma
  mantığı FastAPI endpoint'ine taşınacak (kod paylaşımı için okuma mantığı ayrı bir fonksiyonda
  tutulmalı: `collect_fleet_status(...) -> list[dict]`).

### 8.5 Bu bölümün Faz özeti

| Konu | Faz 1 (bu refactor) | UI Fazı (sonra) |
|---|---|---|
| Dizin yerleşimi (`config/`, `secrets/`) | ✅ Uygulanır | — |
| Host bazlı lock | ✅ Uygulanır | — |
| `fleet.yaml` formatı | ✅ Tanımlanır (salt-okunur kullanım) | Orkestratör + UI bunu tüketir |
| `--status` özet modu | ✅ Uygulanır | FastAPI endpoint'ine evrilir |
| Config base+override merge | ✅ Uygulandı (`config/shared.yaml`; whitelist MAIL+MONITORING) | RMAN_TEMPLATE vb. genişletme |
| fleet_runner / paralel tetikleme | ❌ | UI fazında job katmanıyla birlikte |

## 9. Modüler Paket Yapısı (tek backup.py'den paket mimarisine geçiş)

### 9.1 Hedef yapı

1424 satırlık tek dosya, sorumluluk bazlı modüllere bölünür. Kural: **her modül yalnızca kendi
sorumluluğunu bilir ve diğer modüllerle sadece tanımlı arayüzler (fonksiyon imzaları / sınıflar)
üzerinden konuşur** — bir modülün iç implementasyonu değiştiğinde, arayüz aynı kaldığı sürece diğer
modüller etkilenmez.

```
oracle-backup/
├── backup.py                  # SADECE giriş noktası: argparse + orchestration (~100-150 satır)
├── modules/                # asıl paket
│   ├── __init__.py            # __version__ burada
│   ├── config.py              # load_config, resolve_instance_id, sanitize_instance_id, path çözümleme
│   ├── connection.py          # get_ssh_client, run_command_wrapper, execute_oracle_sql
│   ├── secrets.py             # SecretsProvider ABC + Vault/Local/Null/CyberArk provider'ları + factory
│   ├── locking.py             # acquire_lock, release_lock, acquire_host_lock
│   ├── rman.py                # RMAN script üretimi (template), run_rman, check_standby_exists
│   ├── space.py               # ensure_free_space, get_dir_size_gb, list_daily_dirs
│   ├── transfer.py            # run_scp, run_rsync, uzak dizin oluşturma
│   ├── history.py             # append_history, mark_history_deleted, get_history_file, flock/atomic write
│   ├── logging_setup.py       # setup_logging (Bölüm 10'daki structured logging dahil)
│   ├── mailing.py             # send_daily_summary, test mail, HTML rapor üretimi
│   ├── monitoring.py          # push_metrics (prometheus/zabbix)
│   └── status.py              # collect_fleet_status, --status tablo çıktısı
├── config/ , secrets/ , fleet.yaml   # (Bölüm 8.1)
└── tests/                     # her modül için birim test iskeleti
```

### 9.2 "Değişiklik içeriyi bozmamalı" kuralının somutlaştırılması

- **Arayüz sabitliği:** Her modülün dışa açtığı fonksiyon/sınıf imzaları modülün başında
  `__all__` ile açıkça listelenir. `__all__` dışındaki her şey (alt fonksiyonlar, sabitler) modül-içi
  kabul edilir ve serbestçe değiştirilebilir.
- **Bağımlılık yönü tek taraflı:** `backup.py` → `modules.*` yönünde; modüller birbirini ancak
  şu hiyerarşiyle çağırabilir: `config`/`logging_setup`/`connection` alt katman; `secrets`/`locking`/
  `history` orta katman; `rman`/`space`/`transfer`/`mailing`/`monitoring`/`status` üst katman. Üst
  katman modülleri **birbirini import edemez** (örn. `transfer.py`, `mailing.py`'yi çağıramaz —
  aralarındaki koordinasyonu yalnızca `backup.py` orkestrasyon katmanı yapar). Bu kural, "transfer'i
  değiştirdim, mail bozuldu" sınıfı hataları yapısal olarak imkânsız kılar.
- **Veri alışverişi dict/dataclass ile:** Modüller arası global değişken YOK; her şey parametre ile
  girer, dönüş değeriyle çıkar. `history` kayıt şeması tek bir yerde (Bölüm 10.2) tanımlanır.
- **Geçiş stratejisi:** Bölme işlemi davranış değiştirmeden yapılır (pure refactor) — önce mevcut
  fonksiyonlar birebir taşınır, `backup.py` içinden import edilir, mevcut CLI davranışı korunur
  (`python3 backup.py --config ... --dry-run` aynen çalışmalı). Ancak bundan sonra Bölüm 1-8'deki
  fonksiyonel değişiklikler uygulanır. (İki işi aynı commit'te yapmak, hata olduğunda "refactor mu
  bozdu, yeni özellik mi" ayrımını imkânsızlaştırır.)

## 10. Vektör Veritabanına Hazır Veri Katmanı (loglar + history JSON)

Amaç: ileride log ve history verilerini bir vektör veritabanına (embedding + semantik arama;
"geçen ay ORA-19809 benzeri alan hataları hangi instance'larda oldu?" tarzı sorgular) beslemek.
Bu fazda vektör DB entegrasyonu YAPILMAZ; yapılan şey verinin **o güne hazır formatta üretilmesi**.

### 10.1 Structured logging (insan-okur log + makine-okur JSONL, çift çıktı)

`logging_setup.py` mevcut text log'a ek olarak bir **JSONL** handler ekler:

- `{log_dir}/backup_{file_name}.log` → mevcut insan-okunur format (değişmez).
- `{log_dir}/backup_{file_name}.jsonl` → her satır bağımsız bir JSON nesnesi:

```json
{"ts": "2026-07-03T02:00:11+03:00", "level": "INFO", "instance_id": "db-server1_orcl1",
 "run_id": "20260703_020011_a1b2c3", "phase": "rman", "event": "backup_started",
 "message": "Starting RMAN full backup", "extra": {"parallelism": 4}}
```

- `run_id`: her çalıştırmaya özel benzersiz kimlik (timestamp + kısa rasgele ek). Aynı run'a ait
  tüm log satırları ve history kaydı bu kimlikle bağlanır — vektör DB'de "bu hatanın tam bağlamı"
  sorgusu için kritik.
- `phase`: `init | lock | space_check | rman | transfer | cleanup | mail | done` — kontrollü küme
  (serbest metin değil), embedding öncesi filtreleme/etiketleme için.
- JSONL seçiminin nedeni: satır satır append edilebilir (tek dosyayı yeniden yazma yok → Bölüm 5'teki
  kilit sorunlarından muaf), her ingest aracı (Elastic, ChromaDB/pgvector pipeline'ları, vb.)
  doğrudan okuyabilir.

### 10.2 History kayıt şeması: tek kaynak, versiyonlu

- Kayıt şeması `history.py` içinde tek bir `dataclass` (`BackupRecord`) olarak tanımlanır; `main()`
  içindeki elle kurulan dict'ler kaldırılır. Alan eklemek/değiştirmek tek dosyada yapılır.
- Her kayda iki yeni alan eklenir: `"schema_version": 2` ve `"run_id"` (10.1 ile aynı değer).
  `schema_version`, ileride vektör DB'ye toplu ingest sırasında eski/yeni kayıtları ayırt etmeyi
  sağlar (v1 = mevcut kayıtlar, alan yok sayılır).
- Serbest metin alanları (`errors_warnings`, `remote_fail_desc`) olduğu gibi korunur — bunlar
  embedding için en değerli alanlardır; kısaltma/temizleme ingest aşamasına bırakılır.

### 10.3 İleriye dönük ingest noktası

`history.py` içine tek bir boş kanca tanımlanır:

```python
def on_record_written(record: dict) -> None:
    """İleride vektör DB / message queue ingest'i buraya bağlanacak. Şimdilik no-op.
    KURAL: Bu fonksiyon asla exception yükseltmemeli ve backup akışını asla bloklamamalı
    (ileride gerçek ingest eklenirse fire-and-forget / kuyruk kullanılmalı)."""
    pass
```

`append_history` her başarılı yazım sonrası bunu çağırır — **kilit bırakıldıktan sonra** (bkz.
Bölüm 5'teki birleşik `append_history` implementasyonu; hook, `fcntl.flock` LOCK_UN'dan sonra
çağrılır ki ileride yavaş bir ingest history kilidini tutmasın). Vektör DB günü geldiğinde tek
dokunulacak yer burasıdır.

## 11. Döngü Güvenliği (sonsuz döngü / veritabanı yıpratma önleme)

Veritabanına ve host'lara tekrar tekrar vuran kontrolsüz döngüler bu sistemdeki en tehlikeli hata
sınıfıdır. Aşağıdaki kurallar **tüm modüller için bağlayıcıdır**:

### 11.1 Genel kurallar

1. **`while True` yasak.** Her döngünün ya sabit bir iterasyon üst sınırı (`for attempt in
   range(max_retries)`) ya da mutlak bir deadline'ı (`while time.time() < deadline`) olmalı. Kod
   incelemesinde sınırsız döngü otomatik red gerekçesidir.
2. **Komutlar iki sınıfa ayrılır ve farklı korunur:**
   - **Kısa komutlar** (mkdir, stat, df, kısa SELECT'ler): klasik duvar-saati timeout yeterli.
     `run_command_wrapper` imzası `timeout=DEFAULT_CMD_TIMEOUT` olur (config:
     `BACKUP_CONFIG.command_timeout_sec`, default 600). `subprocess.run` tarafına da aynı timeout
     eklenir (şu an lokal modda hiç timeout yok).
   - **Uzun komutlar** (RMAN backup, büyük transferler, uzun SQL): duvar-saati timeout DEĞİL,
     **watchdog (canlılık takibi)** ile korunur — detay Bölüm 11.4. Temel ilke: iş ilerliyorsa
     (çıktı akıyorsa / DB tarafında RUNNING görünüyorsa) süre ne olursa olsun kesilmez; ilerleme
     durmuşsa yakalanır.
3. **Retry'lar arası bekleme sabit değil, üstel (exponential backoff):** `wait = base * 2**attempt`
   (üst sınır ile, örn. max 300 sn). Sabit 2 sn'lik agresif retry'lar (mevcut mkdir döngüsü gibi)
   sorunlu bir hedefe saniyede yarım istek atmaya devam eder; backoff bunu doğal olarak yavaşlatır.
4. **DB'ye dokunan işlemlerde retry, yalnızca güvenli (idempotent) işlemler için:** SELECT
   sorguları (standby kontrolü, RMAN raporu, alan hesabı) retry edilebilir; **RMAN backup'ın
   kendisi asla otomatik retry EDİLMEZ** — başarısız olduysa FAILED olarak kaydedilir ve bir sonraki
   planlı çalıştırmaya bırakılır. Yarım kalan bir backup'ın üzerine hemen ikinci bir tam backup
   başlatmak, tam da kaçınmak istediğin "veritabanını yorma" senaryosudur.
5. **Poll döngülerinde çift koşul:** hem deneme sayısı hem duvar-saati sınırı (hangisi önce dolarsa).
   Host lock beklemesi (Bölüm 8.2) bu kurala tabidir: `host_lock_timeout_min` aşılınca FAILED.
6. **Cron üst üste binme koruması zaten var (pid_file) ama tamamlanmalı:** mevcut `acquire_lock`
   3 × 30 sn bekleyip pes ediyor — bu doğru davranış, korunur; ancak beklerken de exit code 2 ile
   çıkış loglanmalı ve history'ye `status: "SKIPPED"` kaydı düşülmeli (şu an sessizce kayboluyor,
   `--status` bunu "atlanmış run" olarak gösterebilmeli).

### 11.2 Mevcut kodda düzeltilecek somut noktalar (envanter)

| Yer | Mevcut durum | Düzeltme |
|---|---|---|
| `run_command_wrapper` (SSH) | `timeout=None` çağrıların çoğunda | Kısa komutlar: default 600 sn. Uzun komutlar: watchdog (11.4) |
| `run_command_wrapper` (subprocess) | Hiç timeout yok | Kısa komutlar: `subprocess.run(..., timeout=...)`. Uzun komutlar: watchdog |
| Uzak mkdir döngüsü (main) | 3 deneme, sabit 2 sn | Backoff'lu, mevcut 3 deneme sınırı korunur |
| `run_scp` / `run_rsync` retry'ları | İNCELENDİ (`:483`, `:515`): `for attempt in range(...)` var ama başarısızlıkta **ne sleep, ne backoff, ne log** — sadece başarıda `return`; başarısız denemeler sessiz ve arka arkaya. `start = time.time()` atanıp kullanılmıyor (ölü satır) | Başarısız denemede WARNING logla + üstel backoff (11.1 kural 3); max deneme korunur; süre koruması watchdog (çıktı akışı) ile |
| `acquire_lock` bekleme | 3 × 30 sn, sonra `False` | Korunur + SKIPPED history kaydı eklenir |
| Host lock (yeni) | — | Deadline'lı poll (Bölüm 8.2, kural 5'e uygun) |
| RMAN çalıştırma | `stdout.read()` bloklamalı; **sabit `timeout=7200` (2sa) VAR** (`backup.py:438`) — bu, spec'in normal saydığı 6 saatlik RMAN'ı ÖLDÜRÜR | Sabit 7200 timeout KALDIRILIR; Watchdog'lu streaming okuma (11.4); otomatik retry EKLENMEZ |

### 11.3 Takılma (stall) tespit edildiğinde davranış

Watchdog "iş ilerlemiyor" kararına vardığında bu bir hata gibi ele alınır: işlem FAILED sayılır,
history'ye `errors_warnings: "STALLED: no progress for Xm (phase: rman, last_activity: ...)"`
formatında kayıt düşülür, mail/monitoring normal hata akışıyla bilgilendirilir ve **aynı run içinde
tekrar denenmez**. Uzak tarafta hâlâ çalışıyor olabilecek süreç için log'a uyarı yazılır ("remote
process may still be running, verify manually") — SSH üzerinden agresif kill denemesi varsayılan
olarak YAPILMAZ (yanlış PID'i öldürme riski). İsteğe bağlı `kill_on_stall: false` (default) alanı
ileride bilinçli olarak açılabilir.

### 11.4 Watchdog: Canlılık Bazlı Takip (uzun süren RMAN/SQL/transfer için)

**Tasarım ilkesi:** RMAN full backup 6 saat sürebilir ve bu tamamen normaldir. Ölçülen şey geçen
süre değil, **son ilerleme işaretinden bu yana geçen süre**dir. Üç bağımsız canlılık sinyali
izlenir; her biri config'ten ayrı ayrı açılıp kapatılabilir (istediğin esneklik — "her yeri ile
oynamaya imkân"):

**Sinyal 1 — Çıktı akışı (output activity):**
RMAN/sqlplus/rsync çıktısı bloklamalı `stdout.read()` yerine **streaming** okunur (paramiko
`channel.recv_ready()` polling / lokal modda `Popen` + non-blocking okuma). Her yeni satır
geldiğinde `last_activity` zaman damgası güncellenir. RMAN normalde her backup piece'te satır
bastığı için bu en ucuz ve en doğal sinyaldir.

**Sinyal 2 — DB tarafı ilerleme kontrolü (senin önerdiğin "git process live mı bak"):**
Watchdog, `progress_check_interval_min`'de bir, eldeki DB bağlantı bilgileriyle (SecretsProvider'dan
gelen credentials) ayrı ve kısa ömürlü bir sqlplus sorgusu atar:

```sql
-- RMAN gerçekten çalışıyor ve ilerliyor mu?
SELECT status, operation, mbytes_processed
FROM v$rman_status
WHERE status = 'RUNNING';

-- Daha granüler ilerleme (yüzde bazlı):
SELECT sid, opname, sofar, totalwork, ROUND(sofar/totalwork*100,1) pct
FROM v$session_longops
WHERE opname LIKE 'RMAN%' AND totalwork > 0 AND sofar < totalwork;
```

Karar mantığı: `mbytes_processed` / `sofar` bir önceki kontrole göre **artmışsa** iş canlıdır →
`last_activity` güncellenir (çıktı akmasa bile — örn. RMAN büyük tek bir datafile'da uzun süre
sessiz kalabilir). RUNNING satırı hiç yoksa VE çıktı da akmıyorsa → stall şüphesi güçlenir.
Bu sorgu kısa-komut sınıfındadır (kendi 600 sn timeout'u vardır) ve başarısız olması stall kanıtı
sayılmaz — sadece o tur sinyal alınamamış kabul edilir (DB'ye ek yük bindirmemek ve yanlış-pozitif
FAILED üretmemek için).

**Sinyal 3 — OS tarafı süreç kontrolü:**
Uzun komut başlatılırken uzak PID'i yakalanır (`echo $$` sarmalayıcısı ile); watchdog her turda
`kill -0 <pid>` (sinyal göndermez, sadece varlık kontrolü) ile sürecin yaşadığını doğrular. Süreç
ölmüşse beklemeye hiç gerek yok → anında sonuçlandırılır.

**Stall kararı:** Yalnızca **tüm etkin sinyaller** `idle_timeout_min` boyunca hiçbir canlılık
göstermediğinde verilir. Tek bir sinyalin sessizliği yeterli değildir (yanlış pozitif koruması).

**Config (tamamı ayarlanabilir, senin istediğin esneklikle):**

```yaml
BACKUP_CONFIG:
  watchdog:
    enabled: True                      # tamamen kapatılabilir (eski davranış: sınırsız bekle)
    idle_timeout_min: 30               # hiçbir sinyal olmadan bu kadar dakika = STALLED
    progress_check_enabled: True       # Sinyal 2 (DB sorgusu) — DEFAULT TRUE, false çekilebilir
    progress_check_interval_min: 5     # DB'ye ne sıklıkla sorulacak (DB'yi yormamak için seyrek)
    os_pid_check_enabled: True         # Sinyal 3 — ayrı ayrı kapatılabilir
    max_runtime_min: 0                 # mutlak üst sınır; 0 = SINIRSIZ (default). İsteyen
                                       # "ne olursa olsun 12 saatte kes" diyebilsin diye var.
    kill_on_stall: False               # stall'da uzak süreci öldürme girişimi (default kapalı)
```

- `progress_check_enabled` default **True** (senin talebin) — ama `False` çekildiğinde watchdog
  yalnızca Sinyal 1+3 ile çalışmaya devam eder; `enabled: False` ile tamamı devre dışı kalır.
- `max_runtime_min: 0` default'u bilinçli: mutlak süre sınırı **opt-in**'dir, çünkü "meşru uzun işi
  kesmek" bu sistemde "geç fark edilen stall"dan daha pahalı bir hatadır.
- Watchdog'un kendi döngüsü de Bölüm 11.1 kurallarına tabidir: poll aralığı sabit (`30 sn`),
  deadline'ları config'ten gelir, `while True` içermez (deadline/durum koşullu döngü).
- DB progress sorgusu için credentials yoksa (NullSecretsProvider / `/ as sysdba` uzak kullanılamıyorsa)
  Sinyal 2 otomatik devre dışı kalır ve log'a bir kez WARNING yazılır — hata değildir.

**Modül yerleşimi:** Watchdog `connection.py` içinde `run_long_command(...)` olarak yaşar (kısa
komutlar mevcut `run_command_wrapper`'da kalır); `rman.py` ve `transfer.py` uzun işlerini bunun
üzerinden çalıştırır. DB progress sorgusu `rman.py`'den callback olarak enjekte edilir
(`connection.py`'nin Oracle görünümlerini bilmemesi için — Bölüm 9.2 bağımlılık kuralına uygun).

### 11.4.1 DB Progress Kontrol Seti (Sinyal 2'nin detayı — her tur SIRAYLA çalıştırılan 4 kontrol)

`progress_check_enabled: True` iken, her `progress_check_interval_min`'de bir aşağıdaki 4 kontrol
**sırayla** çalıştırılır (hepsi tek bir sqlplus oturumunda art arda, ayrı ayrı bağlantı açılmaz —
DB'ye ek yük bindirmemek için). Her kontrol salt-okunur, kısa-komut sınıfındadır (kendi timeout'u
vardır) ve tek başına başarısız olması stall kanıtı sayılmaz — sadece "bu turda sinyal alınamadı"
olarak işlenir.

**Kontrol 1 — RMAN oturumu var mı, RUNNING mı, ilerliyor mu (`v$rman_status`)**

```sql
SELECT recid, status, operation, object_type,
       TO_CHAR(start_time,'HH24:MI:SS') start_time, mbytes_processed
FROM   v$rman_status
WHERE  status LIKE 'RUNNING%'
AND    start_time >= SYSDATE - (:tolerance_min/1440)   -- bizim run'ımıza ait olmayanları ele
ORDER BY start_time DESC
```

- `:tolerance_min` = backup başlangıç zamanı - birkaç dakika tolerans (run_id'nin start_time'ından
  hesaplanır). `v$rman_status` hiyerarşik olduğundan (üstte tek "BACKUP" satırı, altında piece
  bazlı alt satırlar) ve controlfile'dan okunduğundan, başka/eski RMAN işleri de görünebilir — bu
  filtre olmadan yanlış-pozitif "canlı" kararı verilebilir.
- Karar: satır var VE `mbytes_processed` önceki tura göre arttıysa → **CANLI**, `last_activity`
  güncellenir. `RUNNING WITH WARNINGS` da RUNNING sayılır (`LIKE 'RUNNING%'`).

**Kontrol 2 — Granüler ilerleme (`v$session_longops`)**

```sql
SELECT sid, serial#, opname, sofar, totalwork,
       ROUND(sofar/NULLIF(totalwork,0)*100,1) pct,
       TO_CHAR(last_update_time,'HH24:MI:SS') last_upd
FROM   v$session_longops
WHERE  opname LIKE 'RMAN%'
AND    totalwork > 0 AND sofar < totalwork
ORDER BY last_update_time DESC
```

- Kontrol 1 "canlı" demediyse (mbytes sabit görünüyorsa) buraya bakılır. Belirleyici alan
  `last_update_time` — Oracle'ın kendi "en son ne zaman ilerleme kaydettiği" damgası. Taze ise
  (son birkaç dakika içinde), `sofar` sayısal olarak sabit görünse bile **CANLI** kabul edilir.
- DBA notu: compressed backupset + büyük tek datafile'da `sofar` uzun aralıklarla güncellenebilir.
  Bu yüzden Kontrol 2 TEK BAŞINA "stall değil" kanıtı sayılır ama tek başına "stall" kanıtı SAYILMAZ
  — sıradaki kontrollere geçilir.

**Kontrol 3 — RMAN oturumları ne bekliyor (`v$session` wait event / teşhis)**

```sql
SELECT s.sid, s.program, s.status, s.event,
       s.seconds_in_wait, s.blocking_session
FROM   v$session s
WHERE  s.program LIKE '%rman%' OR s.module LIKE '%backup%' OR s.client_info LIKE '%rman%'
ORDER BY s.seconds_in_wait DESC
```

- Kontrol 1 ve 2 canlılık göstermediyse, bu kontrol "yaşıyor ama neden ilerlemiyor" sorusunu
  cevaplar ve **sonuç ne olursa olsun** (canlı/stall) elde edilen `event`/`blocking_session` bilgisi
  teşhis amacıyla loglanır ve olası bir FAILED kaydına serbest metin olarak eklenir (Bölüm 10.1'deki
  `extra` alanı, embedding için en değerli veridir).
- Yorumlama kuralı:
  - `event` = `RMAN backup & recovery I/O` veya benzer I/O bekleme → **CANLI** (yavaş ama çalışıyor).
  - `event` = `SQL*Net message from client` ve `seconds_in_wait` sürekli büyüyorsa → RMAN client
    tarafı kopmuş/askıda olabilir → CANLI DEĞİL (stall sayacı işlemeye devam eder).
  - `event` LIKE `enq:%` (kilit/kuyruk bekliyor) veya `blocking_session` doluysa → başka bir oturum
    bloke ediyor → CANLI DEĞİL, ama kök neden bilgisi doğrudan yakalanmış olur.
  - Hiç satır dönmezse (RMAN oturumu DB'de görünmüyor) → Sinyal 3'e (OS PID kontrolü) öncelik
    verilir; ikisi de negatifse süreç muhtemelen tamamen ölmüştür, beklemeye gerek yoktur.

**Kontrol 4 — Archive/FRA alan tıkanması (opsiyonel, ayrı anahtarla)**

```sql
SELECT name, ROUND(space_used/space_limit*100,1) pct_used
FROM   v$recovery_file_dest
```

- Sahada sık görülen bir senaryo: FRA/archive dest dolduğunda RMAN hata vermeden bekler; DB
  tarafında `log file switch (archiving needed)` bekleme görünür ve backup görünüşte "asılı" kalır.
  Bu kontrol stall kararını DEĞİŞTİRMEZ (`Kontrol 1-3`'ün belirlediği canlı/stall sonucu geçerlidir)
  ama `pct_used >= fra_warning_pct` (default 95) ise ayrı bir WARNING log satırı düşülür
  ("FRA nearly full — RMAN may hang on archiver") — teşhisi dakikalar değil saniyeler meselesi
  yapar. Standby/Data Guard olmayan ortamlarda `v$recovery_file_dest` boş dönebilir; bu bir hata
  değildir, kontrol sessizce atlanır.
- Ayrı config anahtarıyla açılıp kapatılabilir: `fra_check_enabled` (default True).

**4 kontrolün birlikte karar mantığı (tek tur, sırayla):**

```
K1: RUNNING var ve mbytes arttı?          → EVET: CANLI, last_activity=now, K2-K4 atlanır (K4 hariç*)
                                            → HAYIR: K2'ye geç
K2: last_update_time taze mi?             → EVET: CANLI, last_activity=now, K3-K4 atlanır (K4 hariç*)
                                            → HAYIR: K3'e geç
K3: event = I/O bekleme mi?               → EVET: CANLI, last_activity=now
    event = idle/enq/blocked mi?          → HAYIR (stall sayacı işler), teşhis bilgisi loglanır
    hiç oturum yok mu?                    → Sinyal 3 (OS PID)'e öncelik ver
K4 (fra_check_enabled ise, K1-K3'ten BAĞIMSIZ her turda çalışır*):
    pct_used >= fra_warning_pct           → WARNING logla (karar mantığını etkilemez)
```
`*K4 teşhis amaçlı olduğu için K1-K3 sonucundan bağımsız her turda ayrıca çalıştırılır.`

**Config (Bölüm 11.4'teki `watchdog` bloğuna eklenen alanlar):**

```yaml
BACKUP_CONFIG:
  watchdog:
    # ... (Bölüm 11.4'teki mevcut alanlar) ...
    progress_check_enabled: True       # Kontrol 1-3'ün tamamının anahtarı (tek anahtar, hepsi birlikte)
    progress_check_tolerance_min: 5    # K1'deki start_time filtresi toleransı
    fra_check_enabled: True            # Kontrol 4 — bağımsız açılıp kapatılabilir
    fra_warning_pct: 95                # Kontrol 4 eşik değeri
```

Kabul kriteri 16d bu 4 kontrolün tamamını kapsayacak şekilde genişletilir (bkz. Bölüm "Kabul
Kriterleri", madde 16d1-16d3).

## Kapsam Dışı (bu refactor'da ele alınmıyor, ayrı görüşülecek)

- RMAN "immutable" hatasının sessizce SUCCESS'e çevrilmesi (önceki incelemede tespit edilen ayrı bir bulgu).
- `sqlplus` connection string'inde parolanın process listesinde görünmesi.
- `CyberArkSecretsProvider`'ın **fiili implementasyonu** (Bölüm 2.5'te sadece iskelet/kontrat
  tanımlanıyor; gerçek CyberArk CCP/AIM entegrasyonu CyberArk'a geçiş kararı netleştiğinde ayrı bir
  iş kalemi olarak ele alınacak).
- Instance-bazlı (karma) provider desteği — şu an için bilinçli olarak global/tek provider ile
  sınırlandı (Bölüm 2.6'daki not).

Bu maddeler, ayrı bir "hardening" / "CyberArk migration" turunda ele alınmak üzere not düşüldü.
