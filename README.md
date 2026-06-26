# Oracle RMAN Backup Script (Jump Server Edition)

This Python script provides advanced RMAN backup automation for Oracle databases. It is designed to work with a **Jump Server (Centralized Management)** architecture. Instead of installing Python on every database server individually, you can run the script on a single centralized server and manage all your databases remotely (via SSH).

## Features
- **Centralized Management (Jump Server):** Logs, historical data, and configurations (including Vault token) are kept on a single secure server.
- Backup history tracking and smart disk space management.
- HashiCorp Vault integration (for SMTP and DB credentials).
- Automatic RMAN SQL reporting embedded in post-backup email summaries.
- Copy backups to another remote server via SCP/Rsync from the target DB server.
- Flexible configuration support (`--config` argument for multiple environments).

## Requirements

- **Jump Server (The machine where this script runs):**
  - Python 3.6 or higher
  - `pip` package manager
- **Database Server (Oracle):**
  - Only standard RMAN and SSH access (No Python required!)
- (Optional) HashiCorp Vault server
- (Optional) Prometheus or Zabbix Server

## Installation

1. Clone the repository to your **Jump Server**:
   ```bash
   git clone https://github.com/murateroglu80/oracle-backup.git
   cd oracle-backup
   ```

2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Configuration (`config.yaml` and `vault_config.yaml`)

**Note:** For security and flexibility, settings are split into two files.
- Copy `config.example.yaml` to create `config.yaml` for main settings.
- Create `vault_config.yaml` for Vault and sensitive data. These files are excluded from git tracking (`.gitignore`).

### Main Settings (`config.yaml`)

- **TARGET_SERVER**: The actual Oracle database server where the script will connect via SSH and trigger backup operations (RMAN).
  - `enabled`: If `True`, operations are executed over SSH via the Jump Server. If `False`, the script performs all operations directly on the machine it's running on (**Local**) without using SSH.
  - `host`: Database IP/Hostname
  - `user`: `oracle` or authorized user
  - `key_file`: Your key path for passwordless SSH access (e.g., `~/.ssh/id_rsa`).
- **ORACLE_CONFIG**: Database connection details and ORACLE_HOME paths.
- **BACKUP_CONFIG**: 
  - `backup_root`: The backup directory on the target server (or local machine) (e.g., `/backup`).
  - `log_dir` and `history_dir`: Paths for logs and history files. If undefined, it defaults to creating `~/huaris/logs` automatically.
  - `device_type`: `DISK` or `SBT_TAPE`.
  - `parallelism`: Degree of parallelism.
  - `rman_script_file`: The name of the file if you are using a custom script (e.g., `backup.rman`).
  - `remote_dest`: The final remote server where backups will be copied.
  - `transfer_method`: `scp` for Windows targets, `rsync` for Linux.
  - `transfer_hours`: Transfer hour, or `"all"` for transferring on every run.
  - `transfer_hours`: Transfer hour, or `"all"` for transferring on every run.
- **MAIL_CONFIG**: Email settings.

### Sensitive Settings (`vault_config.yaml`)
- The Vault server address, token, and the Vault paths for DB and SMTP passwords (`db_secret_path` and `secret_path`) are defined here. This allows the script to securely fetch credentials instead of using OS authentication.

### Best Practice: RMAN Template

For a smooth, secure, and disk-bloat-preventing standard production backup scenario, it is recommended to configure the `RMAN_TEMPLATE` settings in `config.yaml` as follows:

```yaml
RMAN_TEMPLATE:
  full_backup: True           # Backs up the entire database (datafile)
  archive_backup: True        # Very critical for Point-in-time recovery
  controlfile_backup: True    # Backs up the physical map of the database
  spfile_backup: True         # Backs up Oracle configuration (parameter) settings
  cleanup:
    delete_obsolete: True              # Deletes backups older than recovery_window_days
    recovery_window_days: 1            # How many days of backups to retain (1 if space is tight)
    crosscheck_archivelog: True        # Prevents errors from manually deleted archivelogs at the OS level
    crosscheck_backup: True            # Checks for missing backup pieces
    report_obsolete: True              # Writes obsolete/unnecessary items to logs
    delete_expired_archivelog: True    # Cleans up physically missing log records
    delete_expired_controlfile: True   # Cleans up old control file remnants
    delete_obsolete_orphan: True       # Deletes useless orphan backup pieces
    archive_retention_days: 2          # Determines the minimum days archivelogs are kept on disk
```
When left running, this configuration creates a robust backup cycle that is "self-cleaning and always up-to-date".

## Security and SSH Authorization (Passwordless SSH)
If you are using `TARGET_SERVER.enabled: True`, you must generate an SSH key on the Jump Server and copy it to the target server so the Jump Server can connect without entering a password:
```bash
# On Jump Server (if not already generated):
ssh-keygen -t rsa

# To copy the key to the Target DB Server:
ssh-copy-id -i ~/.ssh/id_rsa.pub oracle@target_db_server
```

## Automated Installation and Execution (`run.sh`)

To manage the process much easier and avoid creating/activating a virtual environment (`venv`) every time, you can use the `run.sh` script.

```bash
# You can pass any parameters to run.sh, including test modes:
./run.sh --dry-run
./run.sh --test-mail
./run.sh --test-transfer
./run.sh --test-db

# If you want to use a different configuration file:
./run.sh --config config-db2.yaml

# Normal execution (For automation):
./run.sh
```

## Automation (Crontab Setup)

Simply add the `run.sh` file to crontab for automated execution:

```bash
crontab -e
```

Add the following line:
```bash
0 * * * * /path/to/oracle-backup/run.sh >> /tmp/oracle_backup_cron.log 2>&1
```

## TODO & Future Plans
* **SolarWinds Monitoring Integration:** Pushing backup metrics to the SolarWinds system via SolarWinds API (SWIS REST API or SNMP) in addition to Zabbix/Prometheus (Planned, to be developed).
* **Compiled Version with Go (Golang) (Rewrite):** Rewriting the entire existing structure in the Go language and compiling it as a single *binary executable* to simplify installation, completely eliminate dependencies (Python, libraries, etc.), and prevent unauthorized/accidental modification of the source code.
