# HashiCorp Vault Installation and Configuration Guide

This guide covers the steps for installing HashiCorp Vault, configuring it, and securely storing SMTP passwords for the RMAN Backup System.

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

## 5. Define Password for the Backup System

Create the password expected by the script:

```bash
vault kv put secret/mail smtp_password="changeme"
```

## 6. AppRole and Policy Configuration (Secure Access)

Create an AppRole with limited privileges so the script does not need to use the root token.

### Create Policy (`backup-policy.hcl`)
```hcl
path "secret/data/mail" {
  capabilities = ["read"]
}
```

Upload the policy:
```bash
vault policy write backup-policy backup-policy.hcl
```

### Setup AppRole
```bash
vault auth enable approle

# Create the role
vault write auth/approle/role/backup-role \
    secret_id_ttl=0 \
    token_num_uses=0 \
    token_ttl=10m \
    token_max_ttl=30m \
    policies="backup-policy"

# Get RoleID and SecretID
vault read auth/approle/role/backup-role/role-id
vault write -f auth/approle/role/backup-role/secret-id
```

## 7. Update `config.yaml`

Enter the obtained RoleID and SecretID (or Token) into your `config.yaml` file.

---
*Prepared by: Gemini CLI Agent*
