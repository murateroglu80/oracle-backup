"""E-posta raporlama: günlük özet + HTML rapor üretimi (üst katman)."""

import json
import os
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header

from .history import get_history_file

__all__ = ["send_daily_summary"]


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

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"]    = mail_config["from_addr"]
    msg["To"]      = ", ".join(to_addrs_list)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

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
