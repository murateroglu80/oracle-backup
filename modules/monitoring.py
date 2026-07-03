"""Metrik gönderimi: Prometheus Pushgateway / Zabbix (üst katman)."""

import subprocess

import requests

__all__ = ["push_metrics"]


def push_metrics(logger, monitoring_config, oracle_sid, elapsed, free_gb, required_gb, success):
    if not monitoring_config.get("enabled", False):
        logger.info("Monitoring is disabled. Skipping metric push.")
        return

    monitor_type = monitoring_config.get("type", "").lower()

    if monitor_type == "prometheus":
        url = monitoring_config.get("pushgateway_url")
        if not url:
            return
        data = (
            f"backup_status{{db=\"{oracle_sid}\"}} {1 if success else 0}\n"
            f"backup_duration_seconds{{db=\"{oracle_sid}\"}} {elapsed}\n"
            f"backup_free_space_gb{{db=\"{oracle_sid}\"}} {free_gb}\n"
            f"backup_required_space_gb{{db=\"{oracle_sid}\"}} {required_gb}\n"
        )
        try:
            requests.post(url, data=data, timeout=10)
            logger.info("Pushed metrics to Prometheus Pushgateway.")
        except Exception as e:
            logger.warning(f"Failed to push metrics to Prometheus: {e}")

    elif monitor_type == "zabbix":
        zabbix_server = monitoring_config.get("zabbix_server")
        zabbix_host = monitoring_config.get("zabbix_host")
        if not zabbix_server or not zabbix_host:
            return
        metrics = [
            (zabbix_host, "backup.status", 1 if success else 0),
            (zabbix_host, "backup.duration", elapsed),
            (zabbix_host, "backup.free_gb", free_gb),
            (zabbix_host, "backup.required_gb", required_gb)
        ]
        try:
            for host, key, val in metrics:
                cmd = ["zabbix_sender", "-z", zabbix_server, "-s", host, "-k", key, "-o", str(val)]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            logger.info("Pushed metrics to Zabbix Server.")
        except Exception as e:
            logger.warning(f"Failed to push metrics to Zabbix: {e}")
