"""oracle-backup (modules) — Oracle RMAN Backup, modular package.

v7.0.0 — Multi-Instance edition.

Bu commit yalnızca SAF REFACTOR'dur: tek `backup.py` (v6.7.2) dosyası, sorumluluk
bazlı modüllere bölündü. Davranış v6.7.2 ile birebir aynıdır; fonksiyonel değişiklikler
(multi-instance, SecretsProvider, watchdog, structured logging vb.) ayrı bir commit'te
gelecek — bkz. oracle-backup-multi-instance-spec.md.
"""

__version__ = "7.0.0"
