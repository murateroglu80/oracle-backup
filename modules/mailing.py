"""E-posta raporlama: günlük özet + HTML rapor üretimi (üst katman)."""

import json
import os
import re
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from email.header import Header

from .history import get_history_file
from .utils import format_duration
from .charts import make_success_donut

__all__ = ["send_daily_summary"]

# Aylık donut'un maile gömüldüğü Content-ID (HTML'de src="cid:..." ile eşleşir).
_MONTHLY_CHART_CID = "monthly_success_chart"


def _duration_to_seconds(dur):
    """format_duration çıktısını ('Xh YYm ZZs' / 'Ym ZZs') saniyeye çevirir. Bozuksa 0."""
    if not dur:
        return 0
    h = re.search(r"(\d+)h", dur)
    m = re.search(r"(\d+)m", dur)
    s = re.search(r"(\d+)s", dur)
    return ((int(h.group(1)) * 3600 if h else 0)
            + (int(m.group(1)) * 60 if m else 0)
            + (int(s.group(1)) if s else 0))


def _load_records_range(history_dir, start_date, end_date, db_name=None):
    """start_date..end_date (dahil, datetime) arası history kayıtlarını döner.

    Aralık ay sınırını geçebileceği için (örn son 7 gün önceki aya taşabilir) aralığı kapsayan
    TÜM aylık dosyaları okur (ay bazında tekilleştirir). db_name verilirse yalnızca o DB'nin
    kayıtları döner (None → filtre yok).
    """
    records = []
    seen = set()
    d = start_date
    while d <= end_date:
        hf = get_history_file(history_dir, d)
        if hf not in seen:
            seen.add(hf)
            if os.path.exists(hf):
                try:
                    with open(hf, "r") as f:
                        records.extend(json.load(f))
                except Exception:
                    pass
        d += timedelta(days=1)

    start_s = start_date.strftime("%Y-%m-%d")
    end_s = end_date.strftime("%Y-%m-%d")
    out = []
    for r in records:
        rt = (r.get("run_time", "") or "")[:10]
        if rt and start_s <= rt <= end_s and (db_name is None or r.get("db_name") == db_name):
            out.append(r)
    return out


def _render_run_rows(runs):
    """History kayıt listesinden HTML tablo satırları üretir (rman_components GÖSTERİLMEZ)."""
    html = ""
    for i, run in enumerate(runs):
        if run.get("is_deleted"):
            continue
        run_status = run.get("status", "UNKNOWN").upper()
        if "SUCCESS" in run_status or "COMPLETED" in run_status:
            color = "#27ae60"
        elif "FAIL" in run_status or "ERROR" in run_status:
            color = "#e74c3c"
        else:
            color = "#f39c12"
        row_color = "#ffffff" if i % 2 == 0 else "#f9f9f9"
        details = run.get('errors_warnings', '-')
        if run.get("remote_backup"):
            remote_status = "OK" if run.get("remote_complete") else "FAIL"
            details = f"Remote: {remote_status} | {run.get('remote_fail_desc', details)}"
            if run.get("transfer_speed_mbps"):
                details += f" ({run.get('transfer_speed_mbps')} MB/s)"
        html += f"""
        <tr style="background-color: {row_color}; border-bottom: 1px solid #ddd; font-size: 14px;">
            <td style="padding: 10px; border: 1px solid #eee; text-align: left;">{run.get('run_time', '-')[:10]}</td>
            <td style="padding: 10px; border: 1px solid #eee; text-align: left;">{run.get('operation', 'Backup')}</td>
            <td style="padding: 10px; border: 1px solid #eee; text-align: right;">{run.get('duration', '-')}</td>
            <td style="padding: 10px; border: 1px solid #eee; text-align: right;">{run.get('size_gb', '0')} GB</td>
            <td style="padding: 10px; border: 1px solid #eee; text-align: center; font-weight: bold; color: {color};">{run_status}</td>
            <td style="padding: 10px; border: 1px solid #eee; text-align: left; color: #666;">{details}</td>
        </tr>
        """
    return html


def _build_weekly_section(history_dir, target_date_str, db_name):
    """Haftanın belirli gününde günlük mailin altına eklenecek 'Geçen Hafta Özeti' HTML bloğu.

    Kapsam: target_date'ten geriye son 7 gün (dahil). db_name ile süzülür. Kayıt yoksa boş döner.
    """
    end_dt = datetime.strptime(target_date_str, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=6)  # son 7 gün (bugün dahil)
    runs = _load_records_range(history_dir, start_dt, end_dt, db_name=db_name)
    # Kronolojik sırala (run_time'a göre)
    runs.sort(key=lambda r: r.get("run_time", ""))
    rows = _render_run_rows(runs)
    if not rows.strip():
        rows = ('<tr><td colspan="6" style="padding:10px; text-align:center; color:#999;">'
                'Bu aralıkta kayıt yok.</td></tr>')
    range_label = f"{start_dt.strftime('%d.%m.%Y')} - {end_dt.strftime('%d.%m.%Y')}"
    return f"""
        <h3 style="border-bottom: 2px solid #eee; padding-bottom: 10px; color: #555; margin-top: 30px;">
            Geçen Hafta Özeti ({range_label})
        </h3>
        <table style="width: 100%; border-collapse: collapse; font-family: Arial, sans-serif; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
            <thead>
                <tr style="background-color: #34495e; color: white;">
                    <th style="padding: 12px; border: 1px solid #ddd; text-align: left;">Date</th>
                    <th style="padding: 12px; border: 1px solid #ddd; text-align: left;">Operation</th>
                    <th style="padding: 12px; border: 1px solid #ddd; text-align: right;">Duration</th>
                    <th style="padding: 12px; border: 1px solid #ddd; text-align: right;">Size</th>
                    <th style="padding: 12px; border: 1px solid #ddd; text-align: center;">Status</th>
                    <th style="padding: 12px; border: 1px solid #ddd; text-align: left;">Details</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    """


def _legend_row(color, label, count, total):
    pct = round(100.0 * count / total, 1) if total else 0
    return f"""
        <tr>
            <td style="padding:4px 8px;"><span style="display:inline-block;width:12px;height:12px;
                background:{color};border-radius:2px;vertical-align:middle;"></span></td>
            <td style="padding:4px 8px;color:#444;font-size:14px;">{label}</td>
            <td style="padding:4px 8px;color:#444;font-size:14px;text-align:right;font-weight:bold;">{count}</td>
            <td style="padding:4px 8px;color:#888;font-size:13px;text-align:right;">%{pct}</td>
        </tr>"""


def _css_bar(success, fail, warn, total):
    """Pillow yoksa donut yerine kullanılan saf-CSS oran çubuğu (her istemcide uyumlu)."""
    cells = ""
    for color, val in (("#27ae60", success), ("#e74c3c", fail), ("#f39c12", warn)):
        if val <= 0:
            continue
        w = round(100.0 * val / total, 2)
        cells += f'<td style="background:{color};height:28px;width:{w}%;"></td>'
    return f"""
        <table style="width:200px;border-collapse:collapse;border-radius:4px;overflow:hidden;">
            <tr>{cells}</tr>
        </table>"""


def _build_monthly_section(history_dir, target_date_str, db_name):
    """Ayın son gününde günlük mailin altına eklenecek 'Aylık Özet' bloğu.

    Kapsam: içinde bulunulan ayın 1'i .. target_date (dahil). Başarı donut'u (Pillow varsa PNG,
    yoksa CSS oran çubuğu) + toplamlar tablosu. Ay boşsa ("", None) döner (bölüm eklenmez).
    Dönüş: (html, png_bytes|None) — png_bytes maile CID ile gömülür.
    """
    end_dt = datetime.strptime(target_date_str, "%Y-%m-%d")
    start_dt = end_dt.replace(day=1)
    runs = [r for r in _load_records_range(history_dir, start_dt, end_dt, db_name=db_name)
            if not r.get("is_deleted")]

    success = fail = warn = 0
    total_gb = 0.0
    dur_total = dur_count = 0
    for r in runs:
        st = (r.get("status") or "").upper()
        if "SUCCESS" in st or "COMPLETED" in st:
            success += 1
        elif "FAIL" in st or "ERROR" in st:
            fail += 1
        else:
            warn += 1
        try:
            total_gb += float(r.get("size_gb") or 0)
        except (TypeError, ValueError):
            pass
        secs = _duration_to_seconds(r.get("duration") or "")
        if secs > 0:
            dur_total += secs
            dur_count += 1

    total = success + fail + warn
    if total == 0:
        return "", None

    rate = round(100.0 * success / total, 1)
    avg_dur = format_duration(dur_total / dur_count) if dur_count else "-"
    range_label = f"{start_dt.strftime('%d.%m.%Y')} - {end_dt.strftime('%d.%m.%Y')}"

    png = make_success_donut(success, fail, warn)
    if png:
        visual = (f'<img src="cid:{_MONTHLY_CHART_CID}" width="180" height="180" '
                  f'alt="Başarı oranı" style="display:block;">')
    else:
        visual = _css_bar(success, fail, warn, total)

    legend = (_legend_row("#27ae60", "Başarılı", success, total)
              + _legend_row("#e74c3c", "Başarısız", fail, total)
              + _legend_row("#f39c12", "Uyarı/Diğer", warn, total))

    def _cell(label, value):
        return (f'<td style="padding:10px;border:1px solid #eee;text-align:center;">'
                f'<div style="font-size:12px;color:#888;">{label}</div>'
                f'<div style="font-size:18px;font-weight:bold;color:#333;">{value}</div></td>')

    totals_table = f"""
        <table style="width:100%;border-collapse:collapse;margin-top:16px;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
            <tr style="background:#fafafa;">
                {_cell("Toplam Çalışma", total)}
                {_cell("Başarılı", success)}
                {_cell("Başarısız", fail)}
                {_cell("Uyarı/Diğer", warn)}
                {_cell("Başarı Oranı", f"%{rate}")}
                {_cell("Toplam Veri", f"{round(total_gb, 1)} GB")}
                {_cell("Ort. Süre", avg_dur)}
            </tr>
        </table>"""

    html = f"""
        <h3 style="border-bottom: 2px solid #eee; padding-bottom: 10px; color: #555; margin-top: 30px;">
            Aylık Özet ({range_label})
        </h3>
        <table style="width:100%;border-collapse:collapse;">
            <tr>
                <td style="width:200px;vertical-align:middle;text-align:center;padding:10px;">{visual}</td>
                <td style="vertical-align:middle;padding:10px;">
                    <table style="border-collapse:collapse;">{legend}</table>
                </td>
            </tr>
        </table>
        {totals_table}
    """
    return html, png


def send_daily_summary(history_dir, mail_config, smtp_password, logger, target_date=None, target_server=None, oracle_config=None, backup_config=None, rman_report_html="", db_name=None):
    h_file = get_history_file(history_dir)
    if not os.path.exists(h_file):
        return

    try:
        with open(h_file, "r") as f:
            runs = json.load(f)
    except Exception:
        return

    if not target_date:
        target_date = datetime.now().strftime("%Y-%m-%d")

    # db_name verilirse yalnızca o DB'nin kayıtları (aynı history_dir paylaşılsa bile karışmaz).
    day_runs = [r for r in runs
                if r.get("run_time", "").startswith(target_date)
                and (db_name is None or r.get("db_name") == db_name)]
    if not day_runs:
        return

    severity_map = {"INFO": 1, "WARNING": 2, "ERROR": 3}
    notification_level = mail_config.get("notification_level", "INFO").upper()
    min_severity_score = severity_map.get(notification_level, 1)

    max_day_severity = 1
    html_rows = ""
    success_count = 0
    total_count = 0

    for i, run in enumerate(day_runs):
        if run.get("is_deleted"):
            continue
        total_count += 1

        run_status = run.get("status", "UNKNOWN").upper()
        if run_status == "SUCCESS":
            success_count += 1

        run_severity = run.get("severity", "INFO").upper()

        run_score = severity_map.get(run_severity, 1)
        if run_score > max_day_severity:
            max_day_severity = run_score

        # Status text color matching the second table
        if "SUCCESS" in run_status or "COMPLETED" in run_status:
            color = "#27ae60"
        elif "FAIL" in run_status or "ERROR" in run_status:
            color = "#e74c3c"
        else:
            color = "#f39c12"

        # Zebra striping
        row_color = "#ffffff" if i % 2 == 0 else "#f9f9f9"

        details = run.get('errors_warnings', '-')
        if run.get("remote_backup"):
            remote_status = "OK" if run.get("remote_complete") else "FAIL"
            details = f"Remote: {remote_status} | {run.get('remote_fail_desc', details)}"
            if run.get("transfer_speed_mbps"):
                details += f" ({run.get('transfer_speed_mbps')} MB/s)"

        html_rows += f"""
        <tr style="background-color: {row_color}; border-bottom: 1px solid #ddd; font-size: 14px;">
            <td style="padding: 10px; border: 1px solid #eee; text-align: left;">{run.get('operation', 'Backup')}</td>
            <td style="padding: 10px; border: 1px solid #eee; text-align: left;">{run.get('start_time', run.get('run_time', '-'))} - {run.get('end_time', '-')}</td>
            <td style="padding: 10px; border: 1px solid #eee; text-align: right;">{run.get('duration', '-')}</td>
            <td style="padding: 10px; border: 1px solid #eee; text-align: right;">{run.get('size_gb', '0')} GB</td>
            <td style="padding: 10px; border: 1px solid #eee; text-align: left; word-break: break-all;">{run.get('remote_path_only', '-')}</td>
            <td style="padding: 10px; border: 1px solid #eee; text-align: center; font-weight: bold; color: {color};">{run_status}</td>
            <td style="padding: 10px; border: 1px solid #eee; text-align: left; color: #666;">{details}</td>
        </tr>
        """

    if max_day_severity < min_severity_score:
        logger.info(f"Day max severity ({max_day_severity}) below notification level ({min_severity_score}). Skipping mail.")
        return

    final_severity_label = "INFO"
    overall_status = "ALL OK"
    status_color = "#28a745"  # Green

    if max_day_severity == 2:
        final_severity_label = "WARNING"
        overall_status = "WARNING / PARTIAL"
        status_color = "#ffc107"  # Yellow
    elif max_day_severity == 3:
        final_severity_label = "ERROR"
        overall_status = "ERROR / FAILED"
        status_color = "#dc3545"  # Red

    # Extract info safely
    oracle_sid = oracle_config.get("ORACLE_SID", "N/A") if oracle_config else "N/A"

    # Subject: tarih yerine SID (mailin gövdesinde tarih zaten var).
    sid_label = db_name or oracle_sid
    subject = f"{mail_config['subject_prefix']} [{final_severity_label}] Daily Summary | {sid_label}"

    # Db host is either ORACLE_HOSTNAME or TARGET_SERVER host
    db_host = "Unknown"
    if oracle_config and oracle_config.get("ORACLE_HOSTNAME"):
        db_host = oracle_config.get("ORACLE_HOSTNAME")
    elif target_server and target_server.get("host"):
        db_host = target_server.get("host")
    else:
        import socket
        db_host = socket.gethostname()

    # Transfer target is remote_dest in BACKUP_CONFIG
    transfer_target = backup_config.get("remote_dest", "None") if backup_config else "None"

    success_count = sum(1 for r in day_runs if r.get('status', '').upper() == 'SUCCESS')
    total_count = len(day_runs)

    # Haftalık özet: bugün weekly_summary_day ise günlük tablonun altına "Geçen Hafta" bölümü ekle.
    # weekly_summary_day: 0=Pzt..6=Paz; -1/geçersiz → kapalı (opt-out).
    weekly_html = ""
    try:
        weekly_day = int(mail_config.get("weekly_summary_day", 0))
    except (ValueError, TypeError):
        weekly_day = -1
    if 0 <= weekly_day <= 6:
        today_weekday = datetime.strptime(target_date, "%Y-%m-%d").weekday()
        if today_weekday == weekly_day:
            weekly_html = _build_weekly_section(history_dir, target_date, db_name)

    # Aylık özet: ayın SON gününde günlük mailin altına başarı donut'u + toplamlar eklenir.
    # Son gün tespiti: yarın ayın 1'i ise bugün ayın son günüdür.
    monthly_html = ""
    monthly_png = None
    try:
        _md = datetime.strptime(target_date, "%Y-%m-%d")
        is_month_end = (_md + timedelta(days=1)).day == 1
    except (ValueError, TypeError):
        is_month_end = False
    if is_month_end:
        monthly_html, monthly_png = _build_monthly_section(history_dir, target_date, db_name)

    html_body = f"""
    <html>
    <body style="font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #333; line-height: 1.6;">
        <div style="max-width: 950px; margin: 0 auto; border: 1px solid #ddd; border-radius: 8px; overflow: hidden;">
            <div style="background-color: {status_color}; color: white; padding: 20px; text-align: center;">
                <h2 style="margin: 0;">Oracle RMAN Backup Summary</h2>
                <p style="margin: 5px 0 0 0;">Status: {overall_status} | Server: {db_host} | DB: {oracle_sid}</p>
            </div>

            <div style="padding: 20px;">
                <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
                    <tr>
                        <td style="width: 50%; padding: 10px; background: #f4f4f4;"><strong>Date:</strong> {target_date}</td>
                        <td style="width: 50%; padding: 10px; background: #f4f4f4;"><strong>DB Hostname:</strong> {db_host}</td>
                    </tr>
                    <tr>
                        <td style="padding: 10px;"><strong>Oracle SID:</strong> {oracle_sid}</td>
                        <td style="padding: 10px;"><strong>Transfer Target:</strong> {transfer_target}</td>
                    </tr>
                </table>

                <h3 style="border-bottom: 2px solid #eee; padding-bottom: 10px; color: #555;">Execution Details</h3>
                <table style="width: 100%; border-collapse: collapse; font-family: Arial, sans-serif; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
                    <thead>
                        <tr style="background-color: #34495e; color: white;">
                            <th style="width: 15%; padding: 12px; border: 1px solid #ddd; text-align: left;">Operation</th>
                            <th style="width: 25%; padding: 12px; border: 1px solid #ddd; text-align: left;">Time (Start - End)</th>
                            <th style="width: 10%; padding: 12px; border: 1px solid #ddd; text-align: right;">Duration</th>
                            <th style="width: 10%; padding: 12px; border: 1px solid #ddd; text-align: right;">Size</th>
                            <th style="width: 15%; padding: 12px; border: 1px solid #ddd; text-align: left;">Path</th>
                            <th style="width: 10%; padding: 12px; border: 1px solid #ddd; text-align: center;">Status</th>
                            <th style="width: 15%; padding: 12px; border: 1px solid #ddd; text-align: left;">Details</th>
                        </tr>
                    </thead>
                    <tbody>
                        {html_rows}
                    </tbody>
                </table>

                <h3 style="border-bottom: 2px solid #eee; padding-bottom: 10px; color: #555; margin-top: 30px;">Latest RMAN Jobs (from DB)</h3>
                <div style="font-size: 14px; overflow-x: auto;">
                    {rman_report_html}
                </div>

                {weekly_html}

                {monthly_html}

                <div style="margin-top: 20px; font-size: 0.9em; color: #777; border-top: 1px solid #eee; padding-top: 10px;">
                    Daily Overview: {success_count} Success / {total_count} Total runs today.<br>
                    Notification Level: {notification_level}
                </div>
            </div>
            <div style="background-color: #f4f4f4; padding: 10px; text-align: center; font-size: 0.8em; color: #999;">
                This is an automated RMAN report generated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}.
            </div>
        </div>
    </body>
    </html>
    """

    to_addrs_raw = mail_config.get("to_addrs", [])
    if isinstance(to_addrs_raw, str):
        # Handle string like "a@b.com; c@d.com" or "a@b.com,c@d.com"
        to_addrs_list = [addr.strip() for addr in to_addrs_raw.replace(';', ',').split(',') if addr.strip()]
    else:
        to_addrs_list = to_addrs_raw

    if not to_addrs_list:
        logger.warning("No valid recipient addresses found. Skipping email.")
        return

    # Aylık donut PNG'si varsa CID ile gömmek için "related" sarmalayıcı kullan; yoksa düz
    # "alternative" (mevcut davranış). Gömülü görsel Outlook dahil her istemcide render olur.
    if monthly_png:
        msg = MIMEMultipart("related")
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt)
        img = MIMEImage(monthly_png, "png")
        img.add_header("Content-ID", f"<{_MONTHLY_CHART_CID}>")
        img.add_header("Content-Disposition", "inline", filename="monthly.png")
        msg.attach(img)
    else:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"]    = mail_config["from_addr"]
    msg["To"]      = ", ".join(to_addrs_list)

    try:
        with smtplib.SMTP(mail_config["smtp_host"], mail_config["smtp_port"], timeout=30) as srv:
            srv.ehlo()
            if mail_config.get("use_tls"):
                srv.starttls()
                srv.ehlo()
            if mail_config.get("use_auth", True):
                srv.login(mail_config["smtp_user"], smtp_password)
            srv.sendmail(mail_config["from_addr"], to_addrs_list, msg.as_string())
        logger.info(f"Daily summary email sent successfully ([{final_severity_label}]).")
    except Exception as e:
        logger.error(f"Failed to send daily email: {e}")
