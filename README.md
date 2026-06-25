# Oracle RMAN Backup Script (Jump Server Edition)

Bu Python betiği, Oracle veritabanları için gelişmiş RMAN yedekleme otomasyonu sunar. **Jump Server (Merkezi Yönetim)** mimarisiyle çalışacak şekilde tasarlanmıştır. Bu sayede tüm veritabanı sunucularına tek tek Python kurmak yerine, betiği sadece bir merkezi sunucuda çalıştırarak tüm veritabanlarınızı uzaktan (SSH üzerinden) yönetebilirsiniz.

## Özellikler
- **Merkezi Yönetim (Jump Server):** Loglar, geçmiş verileri (history) ve yapılandırmalar (Vault token dahil) tek bir güvenli sunucuda tutulur.
- Yedekleme geçmişi tutma ve akıllı disk alanı yönetimi.
- HashiCorp Vault entegrasyonu ve SMTP bildirimleri.
- SCP/Rsync aracılığıyla hedef DB sunucusundan bir diğer uzak sunucuya yedek kopyalama.

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
   git clone https://bitbucket.org/mipsoftdev/oracle-backup.git
   cd oracle-backup
   ```

2. Python bağımlılıklarını yükleyin:
   ```bash
   pip install -r requirements.txt
   ```

## Yapılandırma (`config.yaml`)

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
- **MAIL_CONFIG** ve **VAULT_CONFIG**: E-posta ve şifre kasası ayarları.

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
