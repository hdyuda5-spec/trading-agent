"""Reporting service — scheduled daily report."""

import time


class ReportingService:
    def __init__(self, config, telegram, portfolio):
        self.config = config
        self.telegram = telegram
        self.portfolio = portfolio
        self.report_hour = int(config.get("reporting", {}).get("daily_hour", 8))
        self._last_metrics_day = ""

    def should_report(self, now=None) -> bool:
        today = time.strftime("%Y-%m-%d")
        if time.localtime().tm_hour != self.report_hour or today == self._last_metrics_day:
            return False
        return True

    def mark_report(self) -> None:
        self._last_metrics_day = time.strftime("%Y-%m-%d")

    def run(self) -> None:
        self.mark_report()
        today = time.strftime("%Y-%m-%d")
        self.portfolio.notifier.info(f"[REPORT] daily report {today}")
        self.telegram.send_daily_report()
