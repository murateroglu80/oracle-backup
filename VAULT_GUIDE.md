# HashiCorp Vault Kurulum ve Yapılandırma Kılavuzu

Bu kılavuz, RMAN Yedekleme Sistemi için HashiCorp Vault'un kurulumu, yapılandırılması ve SMTP şifrelerinin güvenli bir şekilde saklanması adımlarını içerir.

## 1. Kurulum (Linux - RHEL/CentOS)

### Depo Ekleme
```bash
sudo yum install -y yum-utils
sudo yum-config-manager --add-repo https://rpm.releases.hashicorp.com/RHEL/hashicorp.repo
```

### Kurulum
```bash
sudo yum install vault -y
```

## 2. Temel Yapılandırma

Vault için temel bir yapılandırma dosyası oluşturun (`/etc/vault.d/vault.hcl`):

```hcl
storage "file" {
  path = "/opt/vault/data"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = 1
}

ui = true
```

### Servisi Başlatma
```bash
sudo systemctl enable vault
sudo systemctl start vault
```

## 3. Başlatma (Initialization) ve Kilit Açma (Unsealing)

Vault ilk kurulduğunda kilitlidir.

### Başlatma
```bash
export VAULT_ADDR='http://127.0.0.1:8200'
vault operator init
```
**ÖNEMLİ:** Bu komutun çıktısındaki "Unseal Keys" ve "Initial Root Token" bilgilerini güvenli bir yere kaydedin.

### Kilidi Açma
En az 3 anahtar kullanarak kilidi açın:
```bash
vault operator unseal <anahtar_1>
vault operator unseal <anahtar_2>
vault operator unseal <anahtar_3>
```

## 4. KV Secrets Engine Etkinleştirme

Şifreleri saklamak için KV (Key-Value) motorunu etkinleştirin:

```bash
vault login <root_token>
vault secrets enable -path=secret kv-v2
```

## 5. Yedekleme Sistemi İçin Şifre Tanımlama

Betiğin beklediği şifreyi oluşturun:

```bash
vault kv put secret/mail smtp_password="B!294241944903uf"
```

## 6. AppRole ve Politika Yapılandırma (Güvenli Erişim)

Betiğin root token kullanmaması için sınırlı yetkili bir AppRole oluşturun.

### Politika Oluşturma (`backup-policy.hcl`)
```hcl
path "secret/data/mail" {
  capabilities = ["read"]
}
```

Politikayı yükleyin:
```bash
vault policy write backup-policy backup-policy.hcl
```

### AppRole Kurulumu
```bash
vault auth enable approle

# Rolü oluşturun
vault write auth/approle/role/backup-role \
    secret_id_ttl=0 \
    token_num_uses=0 \
    token_ttl=10m \
    token_max_ttl=30m \
    policies="backup-policy"

# RoleID ve SecretID alın
vault read auth/approle/role/backup-role/role-id
vault write -f auth/approle/role/backup-role/secret-id
```

## 7. `config.yaml` Güncelleme

Aldığınız RoleID ve SecretID (veya Token) bilgilerini `config.yaml` dosyasına işleyin.

---
*Hazırlayan: Gemini CLI Agent*
