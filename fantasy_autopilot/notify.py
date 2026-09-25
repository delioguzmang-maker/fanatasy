"""Push notifications: ntfy (phone app, free, no account), Telegram, e-mail."""

from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Optional

import requests


@dataclass
class Notifier:
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    telegram_token: str = ""
    telegram_chat_id: str = ""
    email_to: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    echo: bool = True

    @property
    def enabled(self) -> bool:
        return bool(self.ntfy_topic or (self.telegram_token and self.telegram_chat_id) or (self.email_to and self.smtp_user))

    def send(self, title: str, body: str, urgent: bool = False, click_url: Optional[str] = None) -> list[str]:
        """Send to every configured channel; returns the list of failures (empty = all good)."""
        errors: list[str] = []
        if self.echo:
            print(f"\n=== {title} ===\n{body}\n")
        if self.ntfy_topic:
            try:
                # JSON publishing keeps accents/emoji intact in the title.
                payload = {"topic": self.ntfy_topic, "title": title, "message": body[:4000],
                           "priority": 5 if urgent else 3, "tags": ["football"]}
                if click_url:
                    payload["click"] = click_url
                resp = requests.post(self.ntfy_server.rstrip("/"), json=payload, timeout=20)
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"ntfy: {exc}")
        if self.telegram_token and self.telegram_chat_id:
            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                    json={"chat_id": self.telegram_chat_id, "text": f"{title}\n\n{body}"[:4000],
                          "disable_web_page_preview": True},
                    timeout=20,
                )
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"telegram: {exc}")
        if self.email_to and self.smtp_user and self.smtp_password:
            try:
                msg = EmailMessage()
                msg["Subject"], msg["From"], msg["To"] = title, self.smtp_user, self.email_to
                msg.set_content(body)
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as smtp:
                    smtp.starttls()
                    smtp.login(self.smtp_user, self.smtp_password)
                    smtp.send_message(msg)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"email: {exc}")
        return errors
