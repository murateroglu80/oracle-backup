# HashiCorp Vault Integration Guide (v7.0.0 Multi-Instance)

This guide covers HashiCorp Vault setup and integration with the oracle-backup multi-instance system.
Vault stores **database credentials** and **SMTP passwords** for all instances in a centralized, secure manner.

**Faz 2 (v7.0.0) changes:** Instance-scoped credential lookup via `VAULT_INSTANCES` config; org-wide
SMTP password optional (can use shared `config/shared.yaml` instead, though not recommended for production).

## 1. Installation (Linux - RHEL/CentOS)

### Add Repository
```bash
sudo yum install -y yum-utils
sudo yum-config-manager --add-repo https://rpm.releases.hashicorp.com/RHEL/hashicorp.repo
```

### Install Vault
```bash
sudo yum install vault -y
```

## 2. Basic Configuration

Create a basic configuration file for Vault (`/etc/vault.d/vault.hcl`):

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

### Start the Service
```bash
sudo systemctl enable vault
sudo systemctl start vault
```

## 3. Initialization and Unsealing

When Vault is first installed, it is sealed.

### Initialize
```bash
export VAULT_ADDR='http://127.0.0.1:8200'
vault operator init
```
**IMPORTANT:** Securely save the "Unseal Keys" and "Initial Root Token" from the output of this command.

### Unseal
Unseal using at least 3 keys:
```bash
vault operator unseal <key_1>
vault operator unseal <key_2>
vault operator unseal <key_3>
```

## 4. Enable KV Secrets Engine

Enable the KV (Key-Value) engine to store passwords:

```bash
vault login <root_token>
vault secrets enable -path=secret kv-v2
```

## 5. Create Secrets for Instances

Multi-instance system requires one secret per instance. Each instance retrieves its own DB & SMTP credentials.

### Example: Two instances (db-server1_orcl1, db-server1_prod2)

```bash
# Instance 1: database + SMTP password
vault kv put secret/oracle/db-server1_orcl1 \
  db_username="rman_backup" \
  db_password="ORCL1_PWD" \
  db_hostname="db-server1.example.local" \
  db_name="ORCL1" \
  smtp_password="org_smtp_pwd"

# Instance 2: database + SMTP password
vault kv put secret/oracle/db-server1_prod2 \
  db_username="rman_backup" \
  db_password="PROD2_PWD" \
  db_hostname="db-server1.example.local" \
  db_name="PROD2" \
  smtp_password="org_smtp_pwd"
```

**Note:** SMTP password can be same across instances (org-wide). Database credentials are per-instance.

## 6. AppRole and Policy Configuration (Secure Access)

Create an AppRole with limited privileges so the script does not need to use the root token.

### Create Policy (`oracle-backup-policy.hcl`)
```hcl
# Allow reading all instance secrets under secret/oracle/*
path "secret/data/oracle/*" {
  capabilities = ["read", "list"]
}
```

Upload the policy:
```bash
vault policy write oracle-backup-policy oracle-backup-policy.hcl
```

### Setup AppRole
```bash
vault auth enable approle

# Create the role
vault write auth/approle/role/oracle-backup-role \
    secret_id_ttl=0 \
    token_num_uses=0 \
    token_ttl=10m \
    token_max_ttl=30m \
    policies="oracle-backup-policy"

# Get RoleID and SecretID (save these securely!)
vault read auth/approle/role/oracle-backup-role/role-id
vault write -f auth/approle/role/oracle-backup-role/secret-id
```

## 7. Configure oracle-backup to Use Vault

### Create `secrets/vault.yaml` (from template)

Copy and edit the template:
```bash
cp secrets/vault.example.yaml secrets/vault.yaml
chmod 600 secrets/vault.yaml
```

### Edit `secrets/vault.yaml` with your Vault details:
```yaml
VAULT_INSTANCES:
  db-server1_orcl1:
    vault_file: "vault.yaml"  # This file itself
    url: "http://vault.example.local:8200"
    token: "<your-approle-token>"  # Or generate from AppRole role_id + secret_id
    secret_path: "secret/oracle/db-server1_orcl1"
    db_secret_path: "secret/oracle/db-server1_orcl1"
  db-server1_prod2:
    url: "http://vault.example.local:8200"
    token: "<your-approle-token>"
    secret_path: "secret/oracle/db-server1_prod2"
    db_secret_path: "secret/oracle/db-server1_prod2"
```

### Update `config/<instance>.yaml`:
```yaml
CREDENTIALS_CONFIG:
  enabled: True
  provider: "vault"
  vault:
    vault_file: "vault.yaml"     # Relative to script dir or absolute path
    instance_id: ""               # Leave empty (uses resolved instance_id)
```

**Instance lookup:** `backup.py --config config/db-server1_orcl1.yaml` will:
1. Resolve `instance_id = "db-server1_orcl1"` (from ORACLE_SID + host, or explicit override)
2. Look up `VAULT_INSTANCES.db-server1_orcl1` in `vault.yaml`
3. Query `secret/oracle/db-server1_orcl1` for credentials
4. Fail-fast if instance not found in Vault config

### Backward Compatibility (Legacy `vault_config.yaml`)

If you have an old `vault_config.yaml` in the project root:
```bash
cp vault_config.yaml secrets/vault.yaml  # Rename and move (auto-migrates)
```

The script auto-discovers and aliases `vault_config.yaml` to `CREDENTIALS_CONFIG.provider=vault`.
New projects should use `secrets/vault.yaml` instead.

---
*Prepared by: Gemini CLI Agent*
