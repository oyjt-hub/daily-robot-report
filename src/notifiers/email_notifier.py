from __future__ import annotations

import html
import smtplib
from email.message import EmailMessage
from pathlib import Path

from ..config import AppConfig
from ..math_render import render_inline_math_html, render_markdown_for_email
from ..models import DailyReport
from ..site_generator import build_daily_markdown


def build_email_html(report: DailyReport, config: AppConfig) -> str:
    web_link_html = ""
    if config.site.public_base_url:
        archive_url = f"{config.site.public_base_url}/{config.site.archive_dir}/{report.date_iso}.html"
        index_url = f"{config.site.public_base_url}/index.html"
        web_link_html = (
            f"<p style='margin:10px 0 0;'>"
            f"<a href='{html.escape(archive_url)}' style='color:#93c5fd;text-decoration:none;font-weight:bold;'>🌐 打开完整网页精读（含公式渲染）</a>"
            f" &nbsp;|&nbsp; <a href='{html.escape(index_url)}' style='color:#93c5fd;text-decoration:none;'>📚 历史索引</a></p>"
        )

    table_rows: list[str] = []
    for idx, item in enumerate(report.deep_dives, 1):
        sp = item.scored
        p = sp.paper
        tags_str = ", ".join(sp.tags)
        code_html = (
            f" · <a href='{html.escape(item.code_urls[0])}'>Code</a>"
            if item.code_urls
            else ""
        )
        table_rows.append(
            f"<tr>"
            f"<td style='padding:8px;border-bottom:1px solid #e2e8f0;font-weight:bold;color:#1d4ed8;'>{idx}. ({sp.score}分)</td>"
            f"<td style='padding:8px;border-bottom:1px solid #e2e8f0;font-size:12px;color:#334155;'>{html.escape(tags_str)}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #e2e8f0;'>"
            f"<div style='font-weight:bold;color:#0f172a;'>{render_inline_math_html(item.title_zh or p.title)}</div>"
            f"<div style='font-size:12px;color:#475569;'>{render_inline_math_html(p.title)}</div>"
            f"<div style='font-size:12px;color:#64748b;'>机构：{html.escape(item.affiliation)} | "
            f"<a href='{html.escape(p.abs_url)}'>arXiv</a> · <a href='{html.escape(p.pdf_url)}'>PDF</a>{code_html}</div>"
            f"</td></tr>"
        )

    overview_table = (
        f"<table style='width:100%;border-collapse:collapse;background:#ffffff;border:1px solid #e2e8f0;margin-bottom:20px;'>"
        f"<thead><tr style='background:#f1f5f9;text-align:left;'>"
        f"<th style='padding:8px;'># / 分数</th><th style='padding:8px;'>方向</th><th style='padding:8px;'>论文与机构</th>"
        f"</tr></thead><tbody>{''.join(table_rows)}</tbody></table>"
        if table_rows
        else "<p style='color:#475569;'>今日无达到深度精读阈值的高分论文。</p>"
    )

    deep_sections: list[str] = []
    if config.notifications.email_include_full_deep_dive:
        for idx, item in enumerate(report.deep_dives, 1):
            sp = item.scored
            p = sp.paper
            body_html = render_markdown_for_email(item.markdown_body)
            deep_sections.append(
                f"<div style='background:#ffffff;border:1px solid #e2e8f0;border-radius:10px;padding:18px 20px;margin-bottom:16px;'>"
                f"<h3 style='margin:0 0 6px;color:#0f172a;'>{idx}. [{sp.score}分] {render_inline_math_html(item.title_zh or p.title)}</h3>"
                f"<p style='margin:0 0 10px;font-size:13px;color:#475569;'>{render_inline_math_html(p.title)} · 机构：{html.escape(item.affiliation)} · "
                f"<a href='{html.escape(p.abs_url)}'>{html.escape(p.id)}</a></p>"
                f"<div style='font-size:14px;line-height:1.6;color:#1e293b;'>{body_html}</div>"
                f"</div>"
            )

    quick_html = ""
    if report.quick_summaries:
        item_parts: list[str] = []
        for sp in report.quick_summaries:
            reason_span = f" — <span style='color:#0369a1;'>{render_inline_math_html(sp.reason)}</span>" if sp.reason else ""
            item_parts.append(
                f"<li style='margin:6px 0;'><strong>[{sp.score}分][{html.escape(', '.join(sp.tags))}]</strong> "
                f"<a href='{html.escape(sp.paper.abs_url)}'>{html.escape(sp.paper.id)}</a> "
                f"{render_inline_math_html(sp.title_zh or sp.paper.title)}{reason_span}</li>"
            )
        items = "".join(item_parts)
        quick_html = (
            f"<div style='background:#ffffff;border:1px solid #e2e8f0;border-radius:10px;padding:16px 20px;margin-bottom:16px;'>"
            f"<h3 style='margin:0 0 10px;'>⚡ 速览推荐（{len(report.quick_summaries)} 篇）</h3><ul style='padding-left:18px;margin:0;'>{items}</ul></div>"
        )

    warn_html = ""
    if report.warnings:
        w_items = "".join(f"<li>{html.escape(w)}</li>" for w in report.warnings)
        warn_html = (
            f"<div style='background:#fffbeb;border:1px solid #f59e0b;border-radius:10px;padding:14px 18px;'>"
            f"<strong>⚠️ 运行告警</strong><ul>{w_items}</ul></div>"
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:16px;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;color:#0f172a;">
  <div style="max-width:860px;margin:0 auto;">
    <div style="background:#1e3a8a;color:#ffffff;padding:20px 24px;border-radius:12px;margin-bottom:18px;">
      <h2 style="margin:0 0 6px;">🤖 {html.escape(config.site.title)} · {html.escape(report.date_iso)}</h2>
      <p style="margin:0;font-size:14px;opacity:0.9;">{html.escape(report.date_display)} · 抓取 {report.total_scraped} 篇 → 预筛 {report.total_prefiltered} 篇 → 精读 {len(report.deep_dives)} 篇 · 速览 {len(report.quick_summaries)} 篇</p>
      {web_link_html}
    </div>
    {overview_table}
    {''.join(deep_sections)}
    {quick_html}
    {warn_html}
  </div>
</body>
</html>
"""


def build_email_message(
    report: DailyReport,
    config: AppConfig,
    html_report_path: Path | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = (
        f"【科研日报】{report.date_iso} 具身大模型精选"
        f"（精读 {len(report.deep_dives)} 篇 / 速览 {len(report.quick_summaries)} 篇）"
    )
    sender = config.notifications.email_from or config.notifications.smtp_user or "daily-robot@localhost"
    recipients = config.notifications.email_to or ["recipient@localhost"]
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    plain_text = build_daily_markdown(report, config)
    html_body = build_email_html(report, config)

    msg.set_content(plain_text, subtype="plain", charset="utf-8")
    msg.add_alternative(html_body, subtype="html", charset="utf-8")

    if config.notifications.email_attach_html and html_report_path is not None and html_report_path.exists():
        msg.add_attachment(
            html_report_path.read_bytes(),
            maintype="text",
            subtype="html",
            filename=html_report_path.name,
        )

    return msg


def send_email(
    report: DailyReport,
    config: AppConfig,
    html_report_path: Path | None = None,
    dry_run: bool = False,
    dry_run_dir: Path | None = None,
) -> tuple[bool, str]:
    msg = build_email_message(report, config, html_report_path=html_report_path)

    if dry_run:
        if dry_run_dir is not None:
            dry_run_dir.mkdir(parents=True, exist_ok=True)
            html_part = msg.get_body(preferencelist=("html",))
            if html_part is not None:
                (dry_run_dir / "email_preview.html").write_text(html_part.get_content(), encoding="utf-8")
        return True, "dry-run 已生成邮件 HTML 预览"

    if not config.email_configured():
        return False, "SMTP/EMAIL 配置不完整（或仍为占位符），已跳过邮件推送"

    n = config.notifications
    use_ssl = n.smtp_use_ssl or n.smtp_port == 465
    if use_ssl:
        with smtplib.SMTP_SSL(n.smtp_host, n.smtp_port, timeout=30) as server:
            server.login(n.smtp_user, n.smtp_password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(n.smtp_host, n.smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(n.smtp_user, n.smtp_password)
            server.send_message(msg)

    return True, f"已发送日报邮件至 {', '.join(n.email_to)}"
