# Oracle RMAN Backup Script — Multi-Instance Edition (v7.x)

Advanced RMAN backup automation for Oracle databases with **multi-instance/multi-database support**, designed for a **Jump Server (Centralized Management)** architecture. Run once on a centralized server and manage all your databases remotely (via SSH).

## Features
- **Multi-Instance/Multi-Database:** Manage multiple Oracle instances (different SIDs, different servers) from a single codebase.
- **Centralized Management (Jump Server):** Logs, historical data, and configurations are kept on a single secure server.
- **Org-Wide Shared Config:** Common SMTP/monitoring settings in single `config/shared.yaml` (no per-database repetition).
- **Database-Aware Mail:** History tracks `db_name` (ORACLE_SID); daily mail filters by database — multi-DB environments don't mix in one mail.
- **Weekly Summary:** Configurable weekly summary section (last 7 days) in daily mail on designated day-of-week.
- **Monthly Summary:** On the last calendar day of the month the daily mail gains a "Monthly Summary" section: a success-rate donut (embedded PNG, Outlook-safe; falls back to a pure-CSS bar if Pillow is not installed) plus a totals table (runs, success/fail/warn, total data, avg duration).
- **Transfer Verification & Resend:** Before each backup, the last successful backup is verified file-by-file (name + size) on the remote destination and any missing/incomplete files are re-sent first (`pre_backup_resend_enabled`, non-blocking). Manual re-send of the last or a specific backup via `--resend [DDMMYY|path]`. Works with both Windows (scp) and Linux (rsync) targets.
- **Backup Type Tracking:** Records which RMAN components (full/archive/controlfile/spfile) were enabled per backup for audit/analytics.
- Backup history tracking (JSONL structured logging + JSON file history) and smart disk space management.
- **HashiCorp Vault, Local, or standalone mode** for DB & SMTP credentials (pluggable `SecretsProvider`).
- **Watchdog-based stall detection:** Monitors RMAN/transfer progress with automatic stall timeout (instead of hard timeouts).
- **Host-based locking:** Serializes backups on the same server to avoid concurrent RMAN conflicts.
- Automatic RMAN SQL reporting embedded in post-backup email summaries.
- Copy backups to another remote server via SCP/Rsync.
- `--status` fleet overview (instance status table).
- `--clear-logs` safely purge old backup logs without touching history.

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

## Configuration

### File Structure
```
config/
  config.example.yaml          # Template for instance-specific settings
  shared.example.yaml          # Template for org-wide MAIL/MONITORING (optional)
  fleet.example.yaml           # Template for instance inventory (optional)
  <instance>.yaml              # Copy of config.example.yaml, per instance

secrets/
  vault.example.yaml           # Vault configuration (if using HashiCorp Vault)
  secrets_local.example.yaml   # Local secrets fallback (if not using Vault)
```

### Quick Start
1. Copy templates to actual files:
   ```bash
   cp config/config.example.yaml config/your-db1.yaml
   cp config/shared.example.yaml config/shared.yaml     # Org-wide (once per org)
   ```
2. Edit `config/your-db1.yaml` for your database (ORACLE_SID, host, paths).
3. If using Vault: Edit `secrets/vault.yaml` with Vault connection details.
4. If using local secrets: Edit `secrets/secrets_local.yaml` with plaintext credentials.

All real configs (`.yaml`, not `.example.yaml`) are in `.gitignore` — safe to commit.

### Config Priority
- **Shared (org-wide):** `config/shared.yaml` — MAIL_CONFIG + MONITORING_CONFIG (one place, all instances use it).
- **Instance-specific:** `config/<instance>.yaml` — everything else (ORACLE_CONFIG, TARGET_SERVER, BACKUP_CONFIG, CREDENTIALS_CONFIG).
- **Deep-merge:** If instance config omits MAIL_CONFIG, it inherits from shared. If it includes a partial override, instance anahtarları wins; shared'ın diğer alanları korunur.

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
  - `transfer_hours`: Transfer hour(s), or `"all"` for transferring on every run.
  - `watchdog`: Stall detection for long-running RMAN/transfer; see spec §11.4 for details. DB
    progress check (Signal 2) runs 4 sequential checks per interval: RMAN progress
    (`v$rman_status`), granular progress (`v$session_longops`), wait-event diagnosis (`v$session`),
    and an independent FRA fullness warning (`v$recovery_file_dest`, `fra_check_enabled` /
    `fra_warning_pct`) that never affects the stall decision, only logs a warning.
- **MAIL_CONFIG**: Email settings.
  - `daily_mail_hour`: Hour to send summary (23 = 11 PM), or `"all"` for every run.
  - `weekly_summary_day`: Day-of-week (0=Monday–6=Sunday) to include 7-day history in daily mail (e.g., 0=Monday morning shows last week). Use -1 to disable.
  - Monthly summary: no config needed — automatically appended on the last calendar day of the month. Requires `Pillow` for the donut chart (optional; falls back to a CSS bar otherwise).
  - `subject_prefix`: Prepended to mail subject; actual subject format: `[prefix] [severity] Daily Summary | SID` (e.g., `[HUARIS-BACKUP] [INFO] Daily Summary | MIPDB`).

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

## Directory Structure

The script uses a clean, predictable, and robust directory structure for both local backup creation and remote file transfer. The structure automatically organizes backups by SID, Month, and Date, eliminating timezone-dependent hours or SCN clutter.

Format:
`{backup_root}/{ORACLE_SID}/{MONTH}/{DDMMYY}/`

Example:
`/backup/ORCL/JUL/300626/`

This exact hierarchy is enforced consistently:
- **Locally (Target Server):** Before RMAN execution, this directory is safely created. All `.rman`, `.arch`, and `.f` backup pieces are saved inside it.
- **Remotely (Destination Server):** During SCP/Rsync transfers, the same structure is replicated dynamically inside the defined `remote_dest` parameter.

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

# Console verbosity: by default a normal backup run keeps the screen quiet (only WARNING/ERROR);
# everything still goes to the log file. Use --show-command to echo the executed commands and the
# RMAN script to the console (the line-by-line live RMAN stream stays only in the log):
./run.sh --config config-db2.yaml --show-command
# Tip: follow the full detail with `tail -f <log_dir>/backup_latest.log`.

# Re-send a backup to the remote destination (verify file-by-file, send only what's missing/incomplete):
./run.sh --config config-db2.yaml --resend            # last successful backup
./run.sh --config config-db2.yaml --resend 050826     # a specific backup folder (DDMMYY)
./run.sh --config config-db2.yaml --resend /backup/MIPDB/AUG/050826   # or an explicit path

# Clean up old backup logs (keeps history):
./run.sh --config config-db2.yaml --clear-logs     # interactive confirmation
./run.sh --config config-db2.yaml --clear-logs --yes   # skip confirmation

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
