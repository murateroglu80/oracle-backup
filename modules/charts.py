"""E-posta raporları için basit grafik (PNG) üretimi.

Aylık özet raporundaki başarı donut'u burada üretilir. Pillow **opsiyonel** bir bağımlılıktır:
kurulu değilse `make_success_donut` None döner ve çağıran taraf CSS oran çubuğu fallback'ine
düşer (mail yine gönderilir). E-posta istemcileri JS/uzak görsel desteklemediği için grafik
CID ile maile gömülü bir PNG olarak taşınır (Outlook dahil her yerde render olur).
"""

import io

__all__ = ["make_success_donut"]

# Günlük/haftalık tablolardaki durum renkleriyle aynı (mailing.py ile tutarlı).
_COLORS = {
    "success": (39, 174, 96),   # #27ae60
    "fail": (231, 76, 60),      # #e74c3c
    "warn": (243, 156, 18),     # #f39c12
}


def make_success_donut(success, fail, warn, size=200):
    """success/fail/warn sayılarından bir donut (halka) PNG üretir; bytes döner.

    Pillow yoksa ya da toplam 0 ise None döner (çağıran fallback'e düşer). Yalnızca halka
    çizilir — yüzde/lejant metni HTML tarafında yazılır (yazı tipi bağımlılığı olmasın diye).
    Kenar yumuşatma için 4x supersample edilip küçültülür.
    """
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None

    total = success + fail + warn
    if total <= 0:
        return None

    scale = 4
    S = size * scale
    img = Image.new("RGB", (S, S), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    pad = int(S * 0.05)
    box = [pad, pad, S - pad, S - pad]
    start = -90.0  # tepeden başla (saat 12 yönü)
    for name in ("success", "fail", "warn"):
        val = {"success": success, "fail": fail, "warn": warn}[name]
        if val <= 0:
            continue
        extent = 360.0 * val / total
        # +0.6 küçük örtüşme: dilimler arasında ince beyaz çizgi kalmasını önler.
        draw.pieslice(box, start, start + extent + 0.6, fill=_COLORS[name])
        start += extent

    # Donut deliği (beyaz daire) — halka görünümü.
    hole = int(S * 0.30)
    cx = cy = S // 2
    draw.ellipse([cx - hole, cy - hole, cx + hole, cy + hole], fill=(255, 255, 255))

    img = img.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
