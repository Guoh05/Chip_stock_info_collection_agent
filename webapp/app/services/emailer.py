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


_PHASE_DISPLAY = {
    "api": "Phase 1: API sweep",
    "scraper_main": "Phase 2: Web scraper",
    "merge": "Phase 3: 合并结果",
}
_PHASE_ICON = {"ok": "✅", "failed": "❌", "skipped": "⊝", "running": "▣", "pending": "▢"}
_PHASE_TEXT = {"ok": "完成", "failed": "失败", "skipped": "跳过", "running": "运行中", "pending": "等待"}


def _format_phase_summary(phases: dict | None, *, html: bool) -> str:
    if not phases:
        return ""
    lines = []
    for key in ("api", "scraper_main", "merge"):
        st = phases.get(key, "pending")
        icon = _PHASE_ICON.get(st, "▢")
        label = _PHASE_DISPLAY[key]
        text = _PHASE_TEXT.get(st, st)
        lines.append(f"{icon} {label} [{text}]")
    if html:
        rows = "".join(f"<li>{line}</li>" for line in lines)
        return f'<ul style="list-style:none; padding-left:0; font-family:Consolas,monospace;">{rows}</ul>'
    return "\n".join(lines)


def send_run_complete(
    to: str,
    run_id: str,
    status: str,
    row_count: int,
    view_url: str,
    error_text: str | None = None,
    phases: dict | None = None,
) -> bool:
    """Notify owner that a run finished. status ∈ {done, done_empty, failed}."""
    phase_summary_html = _format_phase_summary(phases, html=True)
    phase_summary_text = _format_phase_summary(phases, html=False)

    if status == "done":
        subject = f"查询结果就绪 — {run_id}（{row_count} 行现货）"
        headline = "查询完成"
        body_intro = f"共找到 <strong>{row_count}</strong> 行现货数据。"
        text_intro = f"共找到 {row_count} 行现货数据。"
    elif status == "done_empty":
        subject = f"查询完成(无现货) — {run_id}"
        headline = "查询完成 — 无现货"
        body_intro = (
            "本次查询的 MPN 在搜索的 source 都没有现货库存。"
            "可下载完整 xlsx 查看 Lead Time / 未来到货时间等信息。"
        )
        text_intro = body_intro
    elif status == "failed":
        subject = f"查询失败 — {run_id}"
        headline = "查询失败"
        body_intro = "Pipeline 报错。点击下方按钮查看错误详情。"
        text_intro = body_intro
        if phase_summary_html:
            body_intro += f"<br><br><strong>各阶段状态：</strong>{phase_summary_html}"
            text_intro += f"\n\n各阶段状态：\n{phase_summary_text}"
        if error_text:
            body_intro += (
                f'<br><pre style="background:#f7f7f7;padding:8px;'
                f'font-size:12px;white-space:pre-wrap;max-height:200px;'
                f'overflow:auto;">{error_text[:1200]}</pre>'
            )
            text_intro += f"\n\n{error_text[:1200]}"
    else:
        subject = f"查询状态更新 — {run_id}"
        headline = f"查询状态：{status}"
        body_intro = ""
        text_intro = ""

    text = (
        f"{headline}\n\n"
        f"{text_intro}\n\n"
        f"查看结果：{view_url}\n\n"
        f"run_id: {run_id}\n"
    )
    html = f"""
    <div style="font-family: Calibri, Arial, sans-serif; color: #222; max-width: 600px;">
      <h2 style="color: #1F4E78;">Chip Stock Webapp</h2>
      <h3>{headline}</h3>
      <p>{body_intro}</p>
      <p>
        <a href="{view_url}" style="display:inline-block; padding: 10px 24px;
            background: #1F4E78; color: #fff; text-decoration: none;
            border-radius: 4px;">查看结果</a>
      </p>
      <p style="font-size: 12px; color: #666;">
        如按钮不可点，复制此链接：<br>
        <code style="word-break: break-all;">{view_url}</code>
      </p>
      <p style="font-size: 12px; color: #666;">run_id: <code>{run_id}</code></p>
    </div>
    """
    return _send(to, subject, html, text)
