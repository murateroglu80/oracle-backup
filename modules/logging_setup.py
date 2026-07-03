"""Logging kurulumu — insan-okur .log + makine-okur .jsonl çift çıktı (alt katman).

JSONL (spec §10.1): vektör-DB'ye hazır structured log. Her satır bağımsız bir JSON nesnesi;
her satır aynı run'ın `run_id`'sini taşır (kabul kriteri 15). `phase`/`event`/`extra` alanları
opsiyoneldir — `logger.info(msg, extra={"phase": "rman", "event": "backup_started",
"extra_data": {...}})` ile beslenir; verilmezse phase="general".
"""

import json
import logging
import sys
from datetime import datetime

__all__ = ["setup_logging"]

# Kontrollü phase kümesi (spec §10.1): init|lock|space_check|rman|transfer|cleanup|mail|done|general
_DEFAULT_PHASE = "general"


class _ContextFilter(logging.Filter):
    """instance_id + run_id'yi her log kaydına ekler; phase/event/extra_data defaultlar."""

    def __init__(self, instance_id, run_id):
        super().__init__()
        self.instance_id = instance_id
        self.run_id = run_id

    def filter(self, record):
        record.instance_id = self.instance_id
        record.run_id = self.run_id
        if not hasattr(record, "phase"):
            record.phase = _DEFAULT_PHASE
        if not hasattr(record, "event"):
            record.event = ""
        if not hasattr(record, "extra_data"):
            record.extra_data = {}
        return True


class _JsonlFormatter(logging.Formatter):
    def format(self, record):
        obj = {
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds"),
            "level": record.levelname,
            "instance_id": getattr(record, "instance_id", ""),
            "run_id": getattr(record, "run_id", ""),
            "phase": getattr(record, "phase", _DEFAULT_PHASE),
            "event": getattr(record, "event", ""),
            "message": record.getMessage(),
            "extra": getattr(record, "extra_data", {}),
        }
        return json.dumps(obj, ensure_ascii=False)


def setup_logging(log_file, jsonl_file=None, instance_id="", run_id=""):
    logger = logging.getLogger("rman_backup")
    logger.setLevel(logging.DEBUG)
    # Aynı process içinde tekrar çağrılırsa handler birikmesini önle.
    logger.handlers.clear()
    logger.filters.clear()

    logger.addFilter(_ContextFilter(instance_id, run_id))

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)

    if jsonl_file:
        jh = logging.FileHandler(jsonl_file, encoding="utf-8")
        jh.setLevel(logging.DEBUG)
        jh.setFormatter(_JsonlFormatter())
        logger.addHandler(jh)

    return logger
