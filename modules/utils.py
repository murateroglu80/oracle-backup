"""Paylaşılan düşük seviye yardımcılar (paket-içi bağımlılığı yoktur — en alt katman)."""

__all__ = ["format_duration"]


def format_duration(seconds):
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    s = int(seconds) % 60
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"
