"""Kalıcı yedek geçmişi yönetimi — aylık rotasyonlu JSON (orta katman).

Bkz. spec Bölüm 5 (dosya kilidi + atomic write), Bölüm 10.2 (versiyonlu şema, run_id),
Bölüm 10.3 (vektör-DB ingest kancası).

NOT: `fcntl` yalnızca Linux/Unix'te çalışır — bu proje Oracle DB sunucularına (Linux)
hedeflendiği için sorun değildir.
"""

import fcntl
import json
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime

__all__ = [
    "get_history_file",
    "get_history_file_for_dir",
    "append_history",
    "mark_history_deleted",
    "generate_run_id",
    "on_record_written",
    "BackupRecord",
    "HISTORY_SCHEMA_VERSION",
]

HISTORY_SCHEMA_VERSION = 2


def generate_run_id():
    """Her çalıştırmaya özel benzersiz kimlik: timestamp + kısa rasgele ek (spec §10.1)."""
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.urandom(3).hex()}"


@dataclass
class BackupRecord:
    """History kayıt şeması — TEK kaynak (spec §10.2). Alan eklemek/değiştirmek tek yerde.

    schema_version: ileride vektör-DB'ye toplu ingest'te eski (v1, alan yok) / yeni kayıtları
    ayırt etmeyi sağlar. run_id: aynı run'ın tüm log satırları + history kaydını bağlar.
    """
    run_time: str
    start_time: str
    end_time: str
    operation: str
    directory: str
    duration: str
    size_gb: str
    status: str
    severity: str
    run_id: str = ""
    schema_version: int = HISTORY_SCHEMA_VERSION
    errors_warnings: str = "None"
    # DB ayrımı: mail bu alanla filtreler (aynı history_dir paylaşılsa bile DB'ler karışmaz).
    db_name: str = ""
    # RMAN'de açık olan bileşenler ("full,archive,controlfile,spfile") — yalnızca JSON/analiz
    # için tutulur, mailde GÖSTERİLMEZ. Transfer/SKIPPED kayıtlarında boş kalır.
    rman_components: str = ""
    is_deleted: bool = False
    deleted_at: str = None
    # Transfer'a özgü opsiyonel alanlar (backup kayıtlarında None/False kalır)
    remote_path_only: str = None
    transfer_speed_mbps: float = None
    total_attempts: int = None
    remote_backup: bool = False
    remote_complete: bool = None
    remote_fail_desc: str = None

    def to_dict(self):
        return asdict(self)


def on_record_written(record):
    """İleride vektör-DB / message queue ingest'i buraya bağlanacak. Şimdilik no-op (spec §10.3).

    KURAL: Bu fonksiyon ASLA exception yükseltmemeli ve backup akışını asla bloklamamalı
    (gerçek ingest eklenirse fire-and-forget / kuyruk kullanılmalı).
    """
    pass


def get_history_file(history_dir, date_obj=None):
    if date_obj is None:
        date_obj = datetime.now()
    filename = f"backup_history_{date_obj.strftime('%Y_%m')}.json"
    return os.path.join(history_dir, filename)


def get_history_file_for_dir(history_dir, dir_path):
    try:
        dir_name = os.path.basename(dir_path)
        dt = datetime.strptime(dir_name, "%d%b%Y")
        return get_history_file(history_dir, dt)
    except Exception:
        return get_history_file(history_dir)


def _atomic_write_history(h_file, data):
    """Aynı filesystem içinde atomik rename ile yaz (yarım dosya bırakmaz)."""
    tmp_path = h_file + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=4)
    os.replace(tmp_path, h_file)


def append_history(history_dir, record):
    """Kayıt ekler. Aynı instance için eşzamanlı tetiklemelerde (cron çakışması, manuel+otomatik
    üst üste) kayıt kaybını fcntl.flock ile önler (spec §5)."""
    if hasattr(record, "to_dict"):
        record = record.to_dict()
    h_file = get_history_file(history_dir)
    lock_path = h_file + ".lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            data = []
            if os.path.exists(h_file):
                try:
                    with open(h_file, "r") as f:
                        data = json.load(f)
                except Exception:
                    pass
            data.append(record)
            _atomic_write_history(h_file, data)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
    # Kilit BIRAKILDIKTAN sonra ingest kancası (spec §10.3) — hook ileride yavaş olabilir,
    # history kilidini tutmasın. on_record_written asla exception yükseltmez.
    on_record_written(record)


def mark_history_deleted(history_dir, deleted_dir_path):
    h_file = get_history_file_for_dir(history_dir, deleted_dir_path)
    if not os.path.exists(h_file):
        return
    lock_path = h_file + ".lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            try:
                with open(h_file, "r") as f:
                    data = json.load(f)
            except Exception:
                return
            updated = False
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for record in data:
                if record.get("directory", "").startswith(deleted_dir_path):
                    if not record.get("is_deleted"):
                        record["is_deleted"] = True
                        record["deleted_at"] = now_str
                        updated = True
            if updated:
                _atomic_write_history(h_file, data)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
