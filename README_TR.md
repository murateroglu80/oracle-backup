# Oracle RMAN Backup Script (Jump Server Edition)

Bu Python betiği, Oracle veritabanları için gelişmiş RMAN yedekleme otomasyonu sunar. **Jump Server (Merkezi Yönetim)** mimarisiyle çalışacak şekilde tasarlanmıştır. Bu sayede tüm veritabanı sunucularına tek tek Python kurmak yerine, betiği sadece bir merkezi sunucuda çalıştırarak tüm veritabanlarınızı uzaktan (SSH üzerinden) yönetebilirsiniz.

## Özellikler
- **Merkezi Yönetim (Jump Server):** Loglar, geçmiş verileri (history) ve yapılandırmalar (Vault token dahil) tek bir güvenli sunucuda tutulur.
- Yedekleme geçmişi tutma ve akıllı disk alanı yönetimi.
- HashiCorp Vault entegrasyonu (SMTP ve DB kimlik bilgileri için).
- Yedekleme sonrası e-posta özetlerine otomatik RMAN SQL raporu ekleme.
- SCP/Rsync aracılığıyla hedef DB sunucusundan bir diğer uzak sunucuya yedek kopyalama.
- Esnek yapılandırma desteği (Çoklu ortamlar için `--config` argümanı).

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

## Yapılandırma (`config.yaml` ve `vault_config.yaml`)

**Not:** Güvenlik ve esneklik amacıyla ayarlar iki dosyaya ayrılmıştır. 
- Temel ayarlar için `config.example.yaml` dosyasını kopyalayarak `config.yaml` oluşturun.
- Vault ve hassas veriler için `vault_config.yaml` dosyasını oluşturun. Bu dosyalar git takibinden çıkarılmıştır (`.gitignore`).

### Temel Ayarlar (`config.yaml`)

- **TARGET_SERVER**: Scriptin SSH ile bağlanıp yedekleme işlemlerini (RMAN) tetikleyeceği asıl Oracle veritabanı sunucusu.
  - `enabled`: `True` ise işlemler SSH ile Jump Server üzerinden yürütülür. `False` ise script **doğrudan çalıştığı makinede (Lokal)** tüm işlemleri (SSH kullanmadan) gerçekleştirir.
  - `host`: Veritabanı IP/Hostname
  - `user`: `oracle` veya yetkili kullanıcı
  - `key_file`: Şifresiz SSH erişimi için anahtar yolunuz (Örn: `~/.ssh/id_rsa`).
- **ORACLE_CONFIG**: Veritabanı bağlantı detayları ve ORACLE_HOME yolları.
- **BACKUP_CONFIG**: 
  - `backup_root`: Hedef sunucudaki (veya Lokal makinedeki) yedekleme dizini (Örn: `/backup`).
  - `log_dir` ve `history_dir`: Log ve geçmiş dosyalarının yolları. Tanımlanmazsa varsayılan olarak `~/huaris/logs` klasörleri otomatik olarak yaratılır.
  - `device_type`: `DISK` veya `SBT_TAPE`.
  - `parallelism`: Paralellik derecesi.
  - `rman_script_file`: Eğer özel bir script kullanacaksanız bu dosyanın adı (Örn: `backup.rman`).
  - `remote_dest`: Yedeklerin kopyalanacağı nihai uzak sunucu.
  - `transfer_method`: Windows hedefler için `scp`, Linux için `rsync`.
  - `transfer_hours`: Transfer saati veya her çalışmada transfer için `"all"`.
  - `transfer_hours`: Transfer saati veya her çalışmada transfer için `"all"`.
- **MAIL_CONFIG**: E-posta ayarları.

### Hassas Ayarlar (`vault_config.yaml`)
- Vault sunucu adresi, token ve veritabanı ile SMTP şifrelerinin (`db_secret_path` ve `secret_path`) bulunduğu Vault dizinleri burada tanımlanır. Bu sayede DB bağlantılarında OS auth yerine güvenli Vault verileri kullanılır.

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

## Otomatik Kurulum ve Çalıştırma (`run.sh`)

Süreci çok daha kolay yönetmek ve her seferinde sanal ortam (`venv`) oluşturma/aktif etme ile uğraşmamak için `run.sh` betiğini kullanabilirsiniz.

```bash
# Test modları dahil her türlü parametreyi run.sh'a geçirebilirsiniz:
./run.sh --dry-run
./run.sh --test-mail
./run.sh --test-transfer

# Farklı bir konfigürasyon dosyası ile çalıştırmak isterseniz:
./run.sh --config config-db2.yaml

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
