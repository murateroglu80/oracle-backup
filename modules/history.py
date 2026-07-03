"""Kalıcı yedek geçmişi yönetimi — aylık rotasyonlu JSON (orta katman)."""

import os
import json
from datetime import datetime

__all__ = [
    "get_history_file",
    "get_history_file_for_dir",
    "append_history",
    "mark_history_deleted",
]


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


def append_history(history_dir, record):
    h_file = get_history_file(history_dir)
    data = []
    if os.path.exists(h_file):
        try:
            with open(h_file, "r") as f:
                data = json.load(f)
        except Exception:
            pass
    data.append(record)
    with open(h_file, "w") as f:
        json.dump(data, f, indent=4)


def mark_history_deleted(history_dir, deleted_dir_path):
    h_file = get_history_file_for_dir(history_dir, deleted_dir_path)
    if not os.path.exists(h_file):
        return
    try:
        with open(h_file, "r") as f:
            data = json.load(f)

        updated = False
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for record in data:
            if record.get("directory", "").startswith(deleted_dir_path):
                if not record.get("is_deleted"):
                    record["is_deleted"] = True
                    record["deleted_at"] = now_str
                    updated = True

        if updated:
            with open(h_file, "w") as f:
                json.dump(data, f, indent=4)
    except Exception:
        pass
