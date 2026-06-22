# Oracle RMAN Backup Script

Bu Python betiği, Oracle veritabanları için gelişmiş RMAN yedekleme otomasyonu sunar. Yedekleme geçmişi tutma, akıllı disk alanı yönetimi, HashiCorp Vault entegrasyonu, SMTP bildirimleri ve SCP/Rsync aracılığıyla uzak sunucuya yedek kopyalama özelliklerini içerir.

## Gereksinimler

- Python 3.6 veya üzeri
- `pip` paket yöneticisi
- (Opsiyonel) HashiCorp Vault sunucusu
- (Opsiyonel) Prometheus veya Zabbix Server
- Hedef sunucu için SSH/SCP bağlantı altyapısı (Key tabanlı kimlik doğrulama önerilir)

## Kurulum

1. Depoyu sunucunuza kopyalayın:
   ```bash
   git clone https://bitbucket.org/mipsoftdev/oracle-backup.git
   cd oracle-backup
   ```

2. Python bağımlılıklarını yükleyin:
   ```bash
   pip install -r requirements.txt
   ```
   *Not: Ortamınızda `pip` yoksa veya virtual environment kullanmak isterseniz `python3 -m venv venv` ile bir sanal ortam oluşturup kurulum yapabilirsiniz.*

## Yapılandırma (`config.yaml`)

Scriptin tüm davranışları `config.yaml` dosyasından yönetilir. Herhangi bir kod değişikliği yapmadan ortamınıza göre bu dosyayı düzenlemelisiniz.

- **ORACLE_CONFIG**: Veritabanı bağlantı detayları ve ORACLE_HOME yolları.
- **BACKUP_CONFIG**: 
  - `backup_root`: Yerel yedekleme dizini.
  - `remote_dest`: Uzak sunucu dizini (Örn: `oracle@192.168.210.124:/share/oracle` Windows için de geçerlidir).
  - `transfer_method`: Dosyaları uzaktaki sunucuya atarken `scp` mi yoksa `rsync` mi kullanılacağını seçin. Windows sunucular için `scp` önerilir.
  - `transfer_hours`: Uzak sunucuya dosyaların atılacağı saati belirler (Örn: `[20]`). Her çalışmada transfer yapılmasını istiyorsanız `"all"` yazabilirsiniz.
- **MAIL_CONFIG**:
  - `enabled`: Mail gönderimini açar/kapatır.
  - `use_auth`: Eğer SMTP relay kullanıyorsanız ve şifreye gerek yoksa `False` yapın. Eğer Office365 vb. şifreli bir sistem kullanıyorsanız `True` yapın.
  - `smtp_password`: Vault devre dışıyken kullanılacak statik şifre.
- **VAULT_CONFIG**: HashiCorp Vault ayarları. Vault kullanmıyorsanız `enabled: False` yapabilirsiniz.

## Test Modu (Dry-Run, Mail Test, ve SCP Test)

Yedekleme ve transfer işlemlerini gerçekten çalıştırmadan yapılandırmanızı ve scriptin doğru çalıştığını test etmek için scripti argümanlarla çalıştırabilirsiniz:

```bash
# RMAN, SCP/Rsync işlemlerini atlayarak (sadece loglara yazar) scriptin çalışmasını simüle eder:
python3 backup.py --dry-run

# Sadece Mail yapılandırmasını, Vault bağlantısını/SMTP şifresini test eder ve test e-postası gönderir:
python3 backup.py --test-mail

# Sadece Control File yedeği alıp, bunu hemen SCP/Rsync ile uzak sunucuya göndererek bağlantıyı/transferi test eder:
python3 backup.py --test-transfer
```

## Otomasyon (Crontab Kurulumu)

Scriptin her saat başı çalışarak kendi takvimini yönetmesi için crontab'a ekleyebilirsiniz:

```bash
crontab -e
```

Aşağıdaki satırı ekleyin (dosya yollarını kendi ortamınıza göre güncelleyin):
```bash
0 * * * * /usr/bin/python3 /path/to/oracle-backup/backup.py >> /tmp/oracle_backup_cron.log 2>&1
```

## Güvenlik ve Yetkilendirme (SCP Windows)
Eğer Windows bir sunucuya SCP ile aktarım yapacaksanız, Oracle sunucusundaki kullanıcının SSH anahtarını Windows sunucusunda `authorized_keys` içine ekleyerek parolasız bağlantı sağladığınızdan emin olun.
