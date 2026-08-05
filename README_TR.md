# Oracle RMAN Backup Script — Çok-Instance Sürümü (v7.x)

Bu Python betiği, Oracle veritabanları için gelişmiş RMAN yedekleme otomasyonu sunar ve **Çok-Instance/Çok-Veritabanı Desteği** ile **Jump Server (Merkezi Yönetim)** mimarisiyle çalışacak şekilde tasarlanmıştır. Tüm veritabanı sunucularına tek tek Python kurmak yerine, betiği sadece bir merkezi sunucuda çalıştırarak tüm veritabanlarınızı uzaktan (SSH üzerinden) yönetebilirsiniz.

## Özellikler
- **Çok-Instance/Çok-Veritabanı:** Farklı Oracle örnekleri (değişik SID'ler, değişik sunucular) tek bir codebase'den yönetin.
- **Merkezi Yönetim (Jump Server):** Loglar, geçmiş verileri ve yapılandırmalar tek bir güvenli sunucuda tutulur.
- **Org-Geneli Paylaşılmış Yapılandırma:** SMTP/monitoring ayarlarını tek `config/shared.yaml` dosyasında tanımlayın (her veritabanında tekrar yazmayın).
- **Veritabanı-Bilgisi Mail:** Geçmiş, `db_name` (ORACLE_SID) başlı tutulur; günlük mail veritabanına göre filtreler — çok-DB ortamlarında maillar karışmaz.
- **Haftalık Özet:** Haftanın belirli bir gününü seçerek günlük mail'in içine son 7 günün özet tablosu eklenir.
- **Aylık Özet:** Ayın son takvim gününde günlük mail'e "Aylık Özet" bölümü eklenir: başarı oranı donut'u (maile gömülü PNG, Outlook uyumlu; Pillow kurulu değilse saf-CSS oran çubuğuna düşer) + toplamlar tablosu (çalışma sayısı, başarılı/başarısız/uyarı, toplam veri, ort. süre).
- **Transfer Doğrulama & Yeniden Gönderme:** Her backup öncesi son başarılı yedek uzak hedefte dosya-bazında (ad+boyut) doğrulanır; eksik/yarım dosyalar önce yeniden gönderilir (`pre_backup_resend_enabled`, yeni backup'ı bloklamaz). Elle: `--resend [DDMMYY|yol]` ile son veya belirli bir yedek gönderilir. Hem Windows (scp) hem Linux (rsync) hedef desteklenir.
- **Yedek Türü Kaydı:** Hangi RMAN bileşenleri (full/archive/controlfile/spfile) açık olduğu kaydedilir (denetim/analiz için).
- Yedekleme geçmişi (JSONL yapılandırılmış logging) ve akıllı disk alanı yönetimi.
- **HashiCorp Vault, Lokal, veya Bağımsız mod:** Pluggable `SecretsProvider` ile DB & SMTP kimlik bilgileri (instance başına seçim).
- **Watchdog Tabanlı Stall Tespiti:** RMAN/transfer ilerleme monitörleme, sabit zaman-aşımları yerine canlılık sinyalleri.
- **Sunucu Bazlı Lock:** Aynı sunucudaki backupları serial hale getirerek RMAN çakışmasını önle.
- Yedekleme sonrası e-posta özetlerine otomatik RMAN SQL raporu ekleme.
- SCP/Rsync aracılığıyla uzak sunucuya yedek kopyalama.
- `--status` ile fleet genel görünümü (instance durum tablosu).
- `--clear-logs` eski yedekleme loglarını güvenle temizleme (geçmiş bozulmaz).

## Gereksinimler

- **Jump Server (Bu scriptin çalışacağı makine):**
  - Python 3.6 veya üzeri
  - `pip` paket yöneticisi
- **Veritabanı Sunucusu (Oracle):**
  - Sadece standart RMAN ve SSH erişimi (Python gerektirmez!)
- (Opsiyonel) HashiCorp Vault sunucusu
- (Opsiyonel) Prometheus veya Zabbix Server

## Kurulum

1. Depoyu **Jump Server**'ınıza kopyalayın:
   ```bash
   git clone https://github.com/murateroglu80/oracle-backup.git
   cd oracle-backup
   ```

2. Python bağımlılıklarını yükleyin:
   ```bash
   pip install -r requirements.txt
   ```

## Yapılandırma — Dosya Yapısı

```
config/
  config.example.yaml          # Instance-özgü ayarlar şablonu
  shared.example.yaml          # Org-geneli MAIL/MONITORING şablonu (opsiyonel)
  fleet.example.yaml           # Instance envanteri şablonu (opsiyonel)
  <instance>.yaml              # Her instance için config.example.yaml'ın kopyası

secrets/
  vault.example.yaml           # Vault yapılandırması (HashiCorp Vault kullanılıyorsa)
  secrets_local.example.yaml   # Lokal credentials fallback (Vault yoksa)
```

### Hızlı Başlangıç

1. Şablonları gerçek dosyalara kopyalayın:
   ```bash
   cp config/config.example.yaml config/ilk-db.yaml
   cp config/shared.example.yaml config/shared.yaml     # Org-geneli (bir kez)
   ```
2. `config/ilk-db.yaml` dosyasını kendi veritabanınız için düzenleyin (ORACLE_SID, host, yollar).
3. Vault kullanacaksanız: `secrets/vault.yaml` dosyasını Vault bağlantı bilgileriyle düzenleyin.
4. Lokal credentials kullanacaksanız: `secrets/secrets_local.yaml` dosyasını plaintext kimlik bilgileriyle düzenleyin.

Tüm gerçek yapılandırma dosyaları (`.example.yaml` hariç) `.gitignore`'da yer alır — commit etmek güvenlidir.

### Yapılandırma Önceliği (Deep-Merge)
- **Paylaşılmış (org-geneli):** `config/shared.yaml` — MAIL_CONFIG + MONITORING_CONFIG (bir yerde, tüm instanceler kullanır).
- **Instance-özgü:** `config/<instance>.yaml` — diğer her şey (ORACLE_CONFIG, TARGET_SERVER, BACKUP_CONFIG, CREDENTIALS_CONFIG).
- **Merge mantığı:** Instance config MAIL_CONFIG'i çıkarırsa shared'ı aynen kullanır. Kısmi override ederse instance anahtarları kazanır; shared'ın diğer alanları korunur.

### Kullanım Örnekleri

#### Tek bir instance'ı yedekle (dry-run)
```bash
./run.sh --config config/db-server1_orcl1.yaml --dry-run
```

#### Üretim yedeklemesi
```bash
./run.sh --config config/db-server1_prod2.yaml
```

#### E-posta ayarlarını sına
```bash
./run.sh --config config/db1.yaml --test-mail
```

#### Veritabanı bağlantısını sına
```bash
./run.sh --config config/db1.yaml --test-db
```

#### Tüm instancelerin durumunu gör (fleet view)
```bash
./run.sh --status
```

### Instance Yapılandırması (`config/<instance>.yaml`)

- **TARGET_SERVER**: Scriptin SSH ile bağlanıp RMAN işlemlerini tetikleyeceği Oracle sunucusu.
  - `enabled`: `True` ise SSH üzerinden (Jump Server'dan); `False` ise lokal makinede doğrudan çalış.
  - `host`: Veritabanı IP/Hostname
  - `user`: `oracle` veya yetkili kullanıcı
  - `key_file`: Şifresiz SSH erişimi için anahtar yolu (Örn: `~/.ssh/id_rsa`).
- **ORACLE_CONFIG**: Veritabanı bağlantı detayları (ORACLE_HOME, SID, vb.).
  - Instance_id otomatik `host + SID` kombinasyonundan türetilir; elle override edilebilir.
- **BACKUP_CONFIG**:
  - `backup_root`: Yedekleme kök dizini.
  - `log_dir`, `history_dir`, `pid_file`: Boş bırakılırsa instance_id ile otomatik namespace edilir.
  - `temp_dir`: SQL/RMAN geçici dosyaları için (örn. `/tmp`).
  - `watchdog`: Stall tespiti — output, DB progress, OS PID canlılığı monitörleme. DB progress
    kontrolü (Sinyal 2) her turda sırayla 4 kontrol çalıştırır: RMAN ilerleme (`v$rman_status`),
    granüler ilerleme (`v$session_longops`), wait-event teşhisi (`v$session`) ve stall kararını
    ETKİLEMEYEN, bağımsız FRA doluluk uyarısı (`v$recovery_file_dest`, `fra_check_enabled` /
    `fra_warning_pct`).
  - `host_lock_enabled`: Aynı sunucudaki backupları serial hale getir.
  - `transfer_method`: `scp` (Windows) veya `rsync` (Linux).
  - `transfer_hours`: Transfer yapılacak saatler; `"all"` = her run'da.

### Kimlik Bilgileri (`CREDENTIALS_CONFIG`)

Üç seçenek:
1. **Vault:** `secrets/vault.yaml` — HashiCorp Vault'ta merkezi depolama (önerilir, production).
2. **Lokal:** `secrets/secrets_local.yaml` — Plaintext credentials (dev/test, chmod 600 zorunlu).
3. **Bağımsız:** `"none"` — Vault kullanmama; OS-auth veya parolasız bağlantı (test senaryoları).

Detaylar için bkz. [VAULT_GUIDE.md](VAULT_GUIDE.md) ve `secrets/` dizin şablonları.

### En İyi Uygulama (Best Practice): RMAN Şablonu

Sorunsuz, güvenli ve disk şişmesini engelleyen standart bir üretim (production) yedekleme senaryosu için `config.yaml` içindeki `RMAN_TEMPLATE` ayarlarının şu şekilde yapılandırılması önerilir:

```yaml
RMAN_TEMPLATE:
  full_backup: True           # Veritabanının tamamını (datafile) yedekler
  archive_backup: True        # Point-in-time recovery için çok kritiktir
  controlfile_backup: True    # Veritabanının fiziksel haritasını yedekler
  spfile_backup: True         # Oracle konfigürasyon (parametre) ayarlarını yedekler
  cleanup:
    delete_obsolete: True              # recovery_window_days'den eski yedekleri siler
    recovery_window_days: 1            # Kaç günlük yedek geriye dönük tutulacak (Yer kısıtlıysa 1)
    crosscheck_archivelog: True        # İşletim sisteminden manuel silinmiş archivelog hatalarını önler
    crosscheck_backup: True            # Kayıp yedek parçalarını kontrol eder
    report_obsolete: True              # Loglara nelerin eski/gereksiz olduğunu yazar
    delete_expired_archivelog: True    # Fiziksel olarak kayıp log kayıtlarını temizler
    delete_expired_controlfile: True   # Eski kontrol dosyası kalıntılarını temizler
    delete_obsolete_orphan: True       # İşe yaramayan yetim yedek parçalarını siler
    archive_retention_days: 2          # Archivelogların diskte en az kaç gün tutulacağını belirler
```
Bu konfigürasyon, sistemi kendi haline bıraktığınızda "kendi kendini temizleyen ve sürekli güncel kalan" sağlam bir yedekleme döngüsü oluşturur.

## Güvenlik ve SSH Yetkilendirmesi (Passwordless SSH)
Eğer `TARGET_SERVER.enabled: True` kullanıyorsanız, Jump Server'ın hedef Oracle sunucusuna şifre girmeden bağlanabilmesi için SSH anahtarı oluşturup hedef sunucuya kopyalamanız gerekir:
```bash
# Jump Server'da (eğer daha önce üretmediyseniz):
ssh-keygen -t rsa

# Hedef DB Sunucusuna anahtarı kopyalamak için:
ssh-copy-id -i ~/.ssh/id_rsa.pub oracle@hedef_db_sunucusu
```

## Dizin Yapısı (Directory Structure)

Betik, hem yerel yedekleme hem de uzak sunucuya dosya transferi için son derece temiz, öngörülebilir ve sağlam bir dizin yapısı kullanır. Bu yapı, yedekleri otomatik olarak SID, Ay ve Gün bazında organize eder; saat veya SCN gibi karmaşık alt klasörleri ortadan kaldırır.

Format:
`{backup_root}/{ORACLE_SID}/{MONTH}/{DDMMYY}/`

Örnek:
`/backup/ORCL/JUL/300626/`

Bu yapı her aşamada tutarlı bir şekilde korunur:
- **Yerel (Hedef Sunucu):** RMAN çalışmadan önce bu dizin güvenle oluşturulur. Tüm `.rman`, `.arch` ve `.f` yedek parçaları doğrudan bu klasöre kaydedilir.
- **Uzak Sunucu (Hedef Aktarım Noktası):** SCP/Rsync aktarımları sırasında aynı yapı, `remote_dest` parametresinin içinde dinamik olarak birebir kopyalanır.

## Otomatik Kurulum ve Çalıştırma (`run.sh`)

Süreci çok daha kolay yönetmek ve her seferinde sanal ortam (`venv`) oluşturma/aktif etme ile uğraşmamak için `run.sh` betiğini kullanabilirsiniz.

```bash
# Test modları dahil her türlü parametreyi run.sh'a geçirebilirsiniz:
./run.sh --dry-run
./run.sh --test-mail
./run.sh --test-transfer
./run.sh --test-db

# Farklı bir konfigürasyon dosyası ile çalıştırmak isterseniz:
./run.sh --config config-db2.yaml

# Konsol ayrıntı seviyesi: normal bir yedek çalışmasında ekran varsayılan olarak sessizdir
# (yalnızca WARNING/ERROR); her şey yine de log dosyasına yazılır. Çalıştırılan komutları ve RMAN
# script'ini ekranda görmek için --show-command kullanın (RMAN'in satır satır canlı akışı yine
# yalnızca log'da kalır):
./run.sh --config config-db2.yaml --show-command
# İpucu: tam ayrıntıyı `tail -f <log_dir>/backup_latest.log` ile izleyebilirsiniz.

# Bir yedeği uzak hedefe yeniden gönder (dosya-bazında doğrula, yalnızca eksik/yarım olanı gönder):
./run.sh --config config-db2.yaml --resend            # son başarılı yedek
./run.sh --config config-db2.yaml --resend 050826     # belirli bir yedek klasörü (DDMMYY)
./run.sh --config config-db2.yaml --resend /backup/MIPDB/AUG/050826   # ya da tam yol

# Eski yedekleme loglarını temizlemek (geçmiş bozulmaz):
./run.sh --config config-db2.yaml --clear-logs     # etkileşimli onay
./run.sh --config config-db2.yaml --clear-logs --yes   # onay atlayın

# Normal çalışma (Otomasyon için):
./run.sh
```

## Otomasyon (Crontab Kurulumu)

Çalıştırma işlemi için crontab'a `run.sh` dosyasını eklemeniz yeterlidir:

```bash
crontab -e
```

Aşağıdaki satırı ekleyin:
```bash
0 * * * * /path/to/oracle-backup/run.sh >> /tmp/oracle_backup_cron.log 2>&1
```

## Yapılacaklar (TODO) & Gelecek Planları
* **SolarWinds Monitoring Entegrasyonu:** Yedekleme metriklerinin Zabbix/Prometheus'a ek olarak SolarWinds API üzerinden (SWIS REST API veya SNMP ile) SolarWinds sistemine aktarılması (Planlandı, geliştirilecek).
* **Go (Golang) ile Derlenmiş Sürüm (Rewrite):** Kurulumu kolaylaştırmak, bağımlılıkları (Python, kütüphaneler vb.) tamamen ortadan kaldırmak ve kaynak kodun dışarıdan izinsiz/yanlışlıkla değiştirilmesini engellemek için mevcut yapının tamamen Go diline taşınması ve tek bir *binary executable* (çalıştırılabilir dosya) olarak derlenmesi.
