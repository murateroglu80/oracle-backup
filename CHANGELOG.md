# Changelog - Oracle RMAN Backup System

All notable changes to the backup system are documented in this file.

## [6.1.1] - 2026-06-25

### Improved
- **RMAN Parallelism Optimization:** Automatically reduces parallelism to 1 channel when only `controlfile_backup` or `spfile_backup` are requested, avoiding unnecessary system load.
- **RMAN Script Logging:** Explicitly logs the exact, fully constructed RMAN script (`[INFO] Executing RMAN Script...`) to the output/log right before execution.
- **Robust Configuration Parsing:** Added a safe boolean parser (`is_true`) for `config.yaml` to ensure string inputs (like `"False"`, `"false"`) are correctly evaluated and do not unintentionally enable disabled features (like SPFILE backup).
- **Archivelog Cleanup Logic:** The `DELETE NOPROMPT ARCHIVELOG ALL` command is now strictly tied to the `archive_backup: True` condition.

### Fixed
- **Maintenance Channel Allocation (RMAN-06091):** Moved all `DELETE` and `CROSSCHECK` maintenance commands completely outside the `RUN { ... }` block to allow RMAN to auto-allocate maintenance channels correctly (preventing failures if tape backups exist in the catalog but no tape channels are allocated).
- **Benign RMAN Warnings (rc=0):** Updated the error parser to safely ignore benign RMAN warnings (e.g., `RMAN-08120`, `RMAN-08137` related to standby logs) so they no longer trigger a hard script failure when the exit code is 0.
- **Removed Unnecessary Commands:** Removed `LIST BACKUP SUMMARY` to reduce clutter.

## [6.1.0] - 2026-06-25

### Added
- **RMAN Template System:** Introduced `RMAN_TEMPLATE` structure in `config.yaml` allowing modular toggling (True/False) for `full_backup`, `archive_backup`, `controlfile_backup`, `spfile_backup`, and granular `cleanup` actions.
- **Custom RMAN Commands:** Added `extra_commands` list within the template for dynamic injection of custom RMAN commands (e.g., Standby controlfile backup).
- **Comprehensive Dependency Management:** Delegated all Python library checks to `run.sh` and removed redundant `try/except` import blocks for `paramiko` and `hvac`.

### Improved
- **RMAN Script Execution:** All primary backup commands are now bundled inside a unified `RUN { ... }` block to strictly honor parallel channel allocations (`ALLOCATE CHANNEL cX`), resolving unintended fallbacks to tape (`sbtbackup`).
- **Space Reclamation Efficiency:** Optimized disk space recovery by executing RMAN catalog cleanup once, outside the directory deletion loop, followed by a post-cleanup `CROSSCHECK` to maintain catalog sync.
- **Syntax Compatibility:** Moved `LIST BACKUP SUMMARY` outside the RMAN `RUN` block to resolve RMAN-01009 syntax errors.
- **Error Handling:** Added safe fallback logging instead of silent zeroes for `get_free_gb` and `get_dir_size_gb` functions when disk utilities fail.
- **Configuration Security:** Completely anonymized `config.yaml` for version control, replacing real IP addresses, hostnames, and credentials with safe placeholder values, and setting remote/vault integrations to `False` by default.

### Fixed
- **Heredoc Variable Conflict:** Fixed an issue where RMAN script variables (`$`) were improperly escaped by using `mktemp` for safe RMAN script file generation.
- **Invalid RMAN Command:** Removed the invalid `REPORT OBSOLETE ORPHAN` command.
- **Bare Exceptions:** Replaced all `except: pass` anti-patterns with explicit exception handling (`except Exception: pass`).
- **Custom Script Fallback:** Script now explicitly logs a warning if a custom `.rman` file is specified in the config but cannot be found on disk.


## [5.2.0] - 2026-05-30

### Added
- **Severity-Based Reporting:** Introduced `notification_level` (INFO, WARNING, ERROR) to filter email alerts.
- **Enhanced History Schema:** 
    - Added `start_time` and `end_time` for precise operation tracking.
    - Added `severity` field (INFO/WARNING/ERROR) to every history record.
    - Added explicit Rsync metadata: `remote_backup`, `remote_complete`, and `remote_fail_desc`.
- **Intelligent Email Triggering:** 
    - Reporting now fires automatically upon Rsync completion.
    - Added "Midnight Boundary" handling; reports correctly attribute long-running backups to their start date.

### Improved
- HTML Email Template: Added Start/End time columns and dynamic color coding based on severity.
- Email Subjects: Added severity prefixes (e.g., `[ERROR]`, `[INFO]`) for better visibility.
- Rsync Reliability: Improved failure reporting and metadata capture during connection drops.

## [5.1.0] - 2026-05-29

### Added
- **Persistent JSON History:** Replaced transient `daily_status.json` with a permanent, trackable history system.
- **Monthly File Rotation:** History files are now rotated monthly (e.g., `backup_history_2026_05.json`) for performance and easier archiving.
- **Deletion Tracking:** Records in JSON are now marked as `is_deleted: true` when their corresponding backup directories are removed from disk.
- **Advanced Modeling:** JSON schema now includes `operation` type, `directory` path, and `deleted_at` timestamps for full observability.
- **Ultra-Fast Disk Space Calculation:** `get_required_gb` now reads the JSON history (O(1) speed) instead of scanning the disk, with automatic fallback to previous month's file.

### Improved
- Refined `get_required_gb` logic to correctly handle failed/aborted runs by scanning for the last "valid" size.
- Improved Data Guard detection reliability.

## [5.0.0] - 2026-05-25

### Added
- **External Configuration:** Moved all hardcoded settings (Oracle, Backup, Mail) to `config.yaml`.
- **HashiCorp Vault Integration:** Implemented dynamic SMTP password retrieval using the `hvac` library.
- **Consolidated Email Reporting:** 
    - Operations are now logged to a local `daily_status.json`.
    - A single HTML summary email is sent at a configured daily hour.
    - Automatic cleanup of the status file after successful email dispatch.
- **Dynamic Data Guard Detection:** 
    - Added `check_standby_exists()` function using `sqlplus` to detect Standby destinations.
    - Conditionally applies `APPLIED ON ALL STANDBY` to RMAN archivelog deletion based on real-time DB status.
- **Centralized Monitoring:** 
    - Added support for Prometheus Pushgateway and Zabbix (via `zabbix_sender`).
    - Included an `enabled` toggle in configuration to safely bypass monitoring if not configured.
- **RMAN Performance:** Added `PARALLELISM` support configurable via `config.yaml`.

### Changed
- Refactored `backup.py` for better modularity and error handling.
- Unified disk space check and cleanup logic.
- Standardized logging and status tracking.

### Security
- Removed cleartext passwords from the script.
- Added Vault authentication support.
- Created `backup.py.bak_original` for disaster recovery.

---
*Generated by Gemini CLI Agent*
