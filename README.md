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
  - `host`: Veritabanı IP/Hostname
  - `user`: `oracle` veya yetkili kullanıcı
  - `key_file`: Şifresiz SSH erişimi için anahtar yolunuz (Örn: `~/.ssh/id_rsa`). Eğer şifre kullanacaksanız `password: "şifreniz"` satırını ekleyebilirsiniz.
- **ORACLE_CONFIG**: Hedef sunucudaki Veritabanı bağlantı detayları ve ORACLE_HOME yolları.
- **BACKUP_CONFIG**: 
  - `backup_root`: Hedef sunucudaki (Oracle DB makinesindeki) yedekleme dizini.
  - `log_dir` ve `history_dir`: **Jump Server** üzerindeki lokal log yolları.
  - `remote_dest`: Hedef DB sunucusunun yedekleri kopyalayacağı nihai uzak sunucu.
  - `transfer_method`: Windows hedefler için `scp`, Linux için `rsync` önerilir.
  - `transfer_hours`: Her çalışmada transfer için `"all"` kullanabilirsiniz.
- **MAIL_CONFIG** ve **VAULT_CONFIG**: E-posta ve şifre kasası ayarları.

## Güvenlik ve SSH Yetkilendirmesi (Passwordless SSH)
Jump Server'ın hedef Oracle sunucusuna şifre girmeden bağlanabilmesi için SSH anahtarı oluşturup hedef sunucuya kopyalamanız gerekir:
```bash
# Jump Server'da (eğer daha önce üretmediyseniz):
ssh-keygen -t rsa

# Hedef DB Sunucusuna anahtarı kopyalamak için:
ssh-copy-id -i ~/.ssh/id_rsa.pub oracle@hedef_db_sunucusu
```

Aynı şekilde eğer SCP transferi yapılacaksa, **Hedef DB Sunucusu**'nun da yedeklerin gönderileceği nihai Windows/Linux sunucusuna SSH anahtarı üzerinden erişebiliyor olması gerekir.

## Test Modu (Dry-Run, Mail Test, ve SCP Test)

```bash
# RMAN, SCP/Rsync işlemlerini atlayarak scriptin çalışmasını simüle eder:
python3 backup.py --dry-run

# Sadece Mail yapılandırmasını test eder ve test e-postası gönderir:
python3 backup.py --test-mail

# Sadece Control File yedeği alıp, bunu hemen SCP/Rsync ile uzak sunucuya göndererek bağlantıyı/transferi test eder:
python3 backup.py --test-transfer
```

## Otomasyon (Crontab Kurulumu)

Jump Server üzerinde scriptin her saat başı çalışması için crontab'a ekleyebilirsiniz:

```bash
crontab -e
```

Aşağıdaki satırı ekleyin:
```bash
0 * * * * /usr/bin/python3 /path/to/oracle-backup/backup.py >> /tmp/oracle_backup_cron.log 2>&1
```
