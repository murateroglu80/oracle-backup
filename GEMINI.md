# RMAN Backup System Documentation

## Prerequisites
The following Python libraries are required for the script to run. You can install them using the provided `requirements.txt`:

```bash
pip install -r requirements.txt
```

- **PyYAML**: For reading `config.yaml`.
- **requests**: For pushing metrics to Prometheus/Pushgateway.
- **hvac**: For HashiCorp Vault integration.

## Architecture
The backup system follows a **Python-centric orchestration** model for Oracle RMAN.

### Key Components
- **`backup.py`**: Main orchestration script.
- **`config.yaml`**: Externalized configuration for environments, mail, and monitoring.
- **`CHANGELOG.md`**: Version tracking.
- **`history/`**: Directory containing monthly rotated `backup_history_YYYY_MM.json` files.
- **`Notification Levels`**: Configurable email alerts (INFO, WARNING, ERROR).

## Workflows
1. **Configuration:** All environment-specific values must be managed in `config.yaml`.
2. **Secrets:** SMTP passwords and other sensitive data are retrieved from **HashiCorp Vault**.
3. **Standby Detection:** The script dynamically checks for Data Guard Standby existence before archivelog deletion.
4. **History & Tracking:** Every operation is logged in a monthly JSON file.
    - Tracks `start_time`, `end_time`, `duration`, and `size_gb`.
    - Explicit Rsync tracking with `remote_backup` and `remote_complete` flags.
5. **Reporting:** A consolidated HTML report is sent automatically after the Rsync process or at the hour specified in `config.yaml`.
    - Emails are categorized as **[INFO]**, **[WARNING]**, or **[ERROR]**.
    - Notification threshold can be adjusted via `notification_level`.

## Conventions
- **Logging:** All logs are stored in the directory defined by `BACKUP_CONFIG.log_dir`.
- **Naming:** Backup files and tags use a dash-free date format (e.g., `DDMONYYHH`) for RMAN compatibility.
- **Safety:** Always keep a backup of the previous script version before refactoring.
