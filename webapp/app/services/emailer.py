"""SMTP emailer (decision #12 Hotmail via STARTTLS on 587).

Sends Magic Link login emails (M3) and run-complete notifications (M4).

If SMTP_USER/SMTP_PASS aren't configured (decision #42 .env missing values),
falls back to LOGGING the email content — useful for local dev + bootstrap.
"""
from __future__ import annotations
import logging
import smtplib
from email.message import EmailMessage

from ..config import SMTP_CONFIGURED, SMTP_HOST, SMTP_PASS, SMTP_PORT, SMTP_USER

log = logging.getLogger("webapp.emailer")


def _send(to: str, subject: str, html_body: str, text_fallback: str) -> bool:
    """Send via SMTP. Returns True on success.

    If SMTP isn't configured, logs the content (for dev / fallback) and returns
    True so the caller can proceed (user can still pick up the link from logs).
    """
    if not SMTP_CONFIGURED:
        log.warning(
            "[email] SMTP not configured — would have sent to=%s subject=%r\n"
            "-- text fallback --\n%s\n-- end --",
            to, subject, text_fallback,
        )
        return True

    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text_fallback)
    msg.add_alternative(html_body, subtype="html")

    # Port-based mode selection:
    #   465 → implicit SSL (SMTP_SSL) — 163 / qq / aliyun-dm pattern
    #   else (587 / 25 / 80) → STARTTLS upgrade (smtp-mail.outlook.com pattern)
    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as s:
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
                s.ehlo()
                s.starttls()
                s.ehlo()
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        log.info("[email] sent to=%s subject=%r via %s:%s",
                 to, subject, SMTP_HOST, SMTP_PORT)
        return True
    except Exception as e:  # noqa: BLE001
        # Log error only — do NOT echo body (Magic Link URLs are sensitive).
        # When SMTP is broken, fix it; don't expose login credentials in logs.
        # For dev without SMTP, the SMTP_CONFIGURED=False path above logs the
        # body intentionally (no real send happening).
        log.error("[email] send failed to=%s subject=%r: %s", to, subject, e)
        return False


def send_magic_link(to: str, link: str) -> bool:
    subject = "Chip Stock Webapp — 登录链接"
    text = (
        f"点击下方链接登录 Chip Stock Webapp（15 分钟内有效）：\n\n"
        f"  {link}\n\n"
        f"如非本人申请，忽略此邮件。链接只能使用一次。\n"
    )
    html = f"""
    <div style="font-family: Calibri, Arial, sans-serif; color: #222; max-width: 600px;">
      <h2 style="color: #1F4E78;">Chip Stock Webapp</h2>
      <p>点击下方按钮登录（15 分钟内有效）：</p>
      <p>
        <a href="{link}" style="display:inline-block; padding: 10px 24px;
            background: #1F4E78; color: #fff; text-decoration: none;
            border-radius: 4px;">登录 Webapp</a>
      </p>
      <p style="font-size: 12px; color: #666;">
        如果按钮无法点击，复制此链接到浏览器地址栏：<br>
        <code style="word-break: break-all;">{link}</code>
      </p>
      <p style="font-size: 12px; color: #666;">
        如非本人申请，请忽略此邮件。链接只能使用一次。
      </p>
    </div>
    """
    return _send(to, subject, html, text)


def send_run_complete(to: str, run_id: str, summary_html: str, xlsx_path: str | None,
                      view_url: str) -> bool:
    """M4 entry point. Stub for now — implement when M4 starts."""
    subject = f"查询结果就绪 — {run_id}"
    text = f"查询 {run_id} 已完成。打开 {view_url} 查看结果。"
    # TODO M4: attach xlsx_path if < 20 MB, else just view_url
    return _send(to, subject, summary_html or text, text)
