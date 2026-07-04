# Vault Setup Rehberi (Oracle Backup Multi-Instance)

Bu belge, oracle-backup için Vault AppRole authentication kurulumunun adım adım yapılmasını açıklar.

---

## 1. Policy Dosyası Oluştur

Vault sunucusunda (root erişimi ile):

```bash
cat > /opt/vault/oracle-backup-policy.hcl << 'EOF'
# Read permissions for database credentials (oracle-backup)

# KV v2 engine path: secret/oracle/*
path "secret/data/oracle/*" {
  capabilities = ["read"]
}

path "secret/metadata/oracle/*" {
  capabilities = ["list"]
}

# Legacy path (eski setup'lar): database/*
path "database/data/*" {
  capabilities = ["read"]
}

path "database/metadata/*" {
  capabilities = ["list"]
}
EOF
```

---

## 2. Policy'yi Vault'a Yükle

```bash
vault policy write oracle-backup /opt/vault/oracle-backup-policy.hcl
```

Kontrol:
```bash
vault policy read oracle-backup
```

---

## 3. AppRole Authentication Enable Et (İlk Kez)

Eğer AppRole zaten enabled değilse:

```bash
vault auth enable approle
```

Kontrol:
```bash
vault auth list | grep approle
```

---

## 4. AppRole Role'ü Oluştur

**Option A: Never Expired (Geliştirme/Test)**
```bash
vault write auth/approle/role/oracle-backup-role \
    secret_id_ttl=0 \
    token_num_uses=0 \
    token_ttl=0 \
    token_max_ttl=0 \
    policies="oracle-backup"
```

**Option B: 1 Yıl Geçerli (Production)**
```bash
vault write auth/approle/role/oracle-backup-role \
    secret_id_ttl=0 \
    token_num_uses=0 \
    token_ttl=8760h \
    token_max_ttl=8760h \
    policies="oracle-backup"
```

(Yukarıdakilerden birini seç ve çalıştır)

Kontrol:
```bash
vault read auth/approle/role/oracle-backup-role
```

---

## 5. RoleID'yi Oku (Statik, Gizli Tutma)

```bash
vault read auth/approle/role/oracle-backup-role/role-id
```

Örnek output:
```
Key        Value
---        -----
role_id    a1b2c3d4-e5f6-4a5b-9c8d-7e6f5a4b3c2d
```

**Bu değeri not et** (sekretleri güvenli tut).

---

## 6. SecretID'yi Oluştur (Bir Kerelik)

```bash
vault write -f auth/approle/role/oracle-backup-role/secret-id
```

Örnek output:
```
Key                   Value
---                   -----
secret_id             xyz789...
secret_id_accessor    pqr456...
secret_id_ttl         0s
```

**Bu değeri not et** (sonra silinecek).

---

## 7. Vault Token'ı Al (Login)

RoleID ve SecretID'yi kullanarak token al:

```bash
vault write auth/approle/login \
    role_id="a1b2c3d4-e5f6-4a5b-9c8d-7e6f5a4b3c2d" \
    secret_id="xyz789..."
```

Örnek output:
```
Key                     Value
---                     -----
token                   hvs.CAESIg5VvbjvZgQQG...
token_accessor          5xvFSpM13pxVrfT6D...
token_duration          10m
token_max_ttl           30m
token_policies          ["oracle-backup"]
token_ttl               10m
```

**Token değerini kopyala** (bu `secrets/vault.yaml`'da kullanılacak).

---

## 8. Vault Credentials Test Et

SecretID'nin hâlâ geçerli olduğunu kontrol et (henüz silme):

```bash
vault kv get secret/oracle/mydb1_MYDB
# ya da
vault kv get database/mydb1
```

---

## 9. SecretID'yi Sil (Güvenlik)

SecretID bir kerelik kullanılıyor — login sonrası silebilirsin:

```bash
vault write -f auth/approle/role/oracle-backup-role/secret-id/destroy
```

Ya da yeni bir secret-id oluştur ve eskisini sil:

```bash
vault list auth/approle/role/oracle-backup-role/secret-id
vault write -f auth/approle/role/oracle-backup-role/secret-id/ACCESSOR_ID/destroy
```

---

## 10. secrets/vault.yaml'ı Güncelle

Adım 7'deki token'ı kullanarak `secrets/vault.yaml`'ı düzenle:

```yaml
VAULT_INSTANCES:
  mydb1_MYDB:
    vault_file: "vault.yaml"
    url: "http://vault.example.com:8200"
    token: "hvs.CAESIg5VvbjvZgQQG..."
    secret_path: "database/mydb1"               # Legacy path
    db_secret_path: "database/mydb1"            # Legacy path
```

Veya yeni path convention'u kullanıyorsan:

```yaml
VAULT_INSTANCES:
  mydb1_MYDB:
    vault_file: "vault.yaml"
    url: "http://vault.example.com:8200"
    token: "hvs.CAESIg5VvbjvZgQQG..."
    secret_path: "secret/oracle/mydb1_MYDB"
    db_secret_path: "secret/oracle/mydb1_MYDB"
```

---

## 11. oracle-backup Test Et

```bash
cd /path/to/oracle-backup

# Test 1: Vault bağlantısı
./run.sh --config config/mydb1_MYDB.yaml --test-db

# Test 2: E-mail (relay, şifre gerekmez)
./run.sh --config config/mydb1_MYDB.yaml --test-mail

# Test 3: Dry-run backup
./run.sh --config config/mydb1_MYDB.yaml --dry-run
```

---

## 12. Token Tazeleme (Token Expired Olursa — Gerekirse)

**Eğer Adım 4'te `token_ttl=0` (never expired) seçtiysen:** Bu adımı atla. ✓

**Eğer Adım 4'te `token_ttl=8760h` (1 yıl) seçtiysen:** 1 yıl sonra token'ı refresh et.

**Eğer Adım 4'te `token_ttl=10m` (10 dakika) seçtiysen:** Her çalıştırmadan önce yeni token al:

```bash
# Adım 6 & 7'yi tekrarla (SecretID + Login)
vault write -f auth/approle/role/oracle-backup-role/secret-id
vault write auth/approle/login \
    role_id="..." \
    secret_id="..."

# Yeni token'ı secrets/vault.yaml'da güncelle
```

---

## Sık Sorulan Sorular

### Q: Token kaç süre geçerli?
**A:** Adım 4'te seçtiğine bağlı:
- `token_ttl=0` → Hiçbir zaman expire olmaz (development)
- `token_ttl=8760h` → 1 yıl geçerli (production)
- `token_ttl=10m` → 10 dakika (güvenli ama sık refresh gerekir)

### Q: SecretID silebilir mi?
**A:** Evet, login sonrası silebilirsin. Yeni bir token lazımsa, yeni SecretID oluştur.

### Q: Vault down olursa ne olur?
**A:** Script **fail-fast** ile çıkar (sessiz devam etmez). Logs'ları kontrol et.

### Q: Vault credentials'ı nerede tutmalı?
**A:** `secrets/vault.yaml` dosyası `.gitignore`'da. Secure tutun (chmod 600).

### Q: Multi-instance setup'ta her instance için ayrı token lazım mı?
**A:** Hayır. Tek token ile birden fazla instance'a erişebilir (policy `secret/data/oracle/*` tüm path'ları kapsıyor).

---

## Kaynaklar

- VAULT_GUIDE.md — Detaylı Vault integration
- oracle-backup-multi-instance-spec.md (Bölüm 2) — Secrets Provider spec
- Vault AppRole docs: https://www.vaultproject.io/docs/auth/approle
