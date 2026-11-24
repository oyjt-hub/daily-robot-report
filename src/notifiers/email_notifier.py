from __future__ import annotations

import html
import smtplib
from email.message import EmailMessage
from pathlib import Path

from ..config import AppConfig
from ..math_render import (
    prepare_body_for_display,
    render_inline_math_html,
    render_markdown_for_email,
    style_email_markdown_html,
)
from ..models import DailyReport
from ..site_generator import build_daily_markdown


def build_email_html(report: DailyReport, config: AppConfig) -> str:
    web_link_html = ""
    if config.site.public_base_url:
        archive_url = f"{config.site.public_base_url}/{config.site.archive_dir}/{report.date_iso}.html"
        index_url = f"{config.site.public_base_url}/index.html"
        web_link_html = (
            f"<div style='margin-top:12px;padding-top:12px;border-top:1px solid #f1f5f9;font-size:13px;'>"
            f"<a href='{html.escape(archive_url)}' style='color:#2563eb;text-decoration:none;font-weight:600;'>打开完整网页精读 ↗</a>"
            f"<span style='color:#cbd5e1;margin:0 10px;'>·</span>"
            f"<a href='{html.escape(index_url)}' style='color:#64748b;text-decoration:none;font-weight:500;'>历史归档索引</a>"
            f"</div>"
        )

    index_items: list[str] = []
    for idx, item in enumerate(report.deep_dives, 1):
        sp = item.scored
        p = sp.paper
        tag_pills = "".join(
            f"<span style='display:inline-block;white-space:nowrap;background:#f1f5f9;color:#475569;"
            f"font-size:11px;padding:1px 6px;border-radius:4px;margin-left:5px;'>{html.escape(t)}</span>"
            for t in sp.tags
        )
        link_parts = [
            f"<a href='{html.escape(p.abs_url)}' style='color:#2563eb;text-decoration:none;'>arXiv</a>",
            f"<a href='{html.escape(p.pdf_url)}' style='color:#2563eb;text-decoration:none;'>PDF</a>",
        ]
        if item.code_urls:
            link_parts.append(
                f"<a href='{html.escape(item.code_urls[0])}' style='color:#059669;text-decoration:none;'>Code</a>"
            )
        links_html = "<span style='color:#cbd5e1;margin:0 5px;'>·</span>".join(link_parts)
        border_css = "border-top:1px solid #f1f5f9;" if idx > 1 else ""
        index_items.append(
            f"<div style='padding:12px 0;{border_css}'>"
            f"<div style='line-height:1.55;'>"
            f"<span style='display:inline-block;white-space:nowrap;background:#eff6ff;color:#1d4ed8;"
            f"font-size:11.5px;font-weight:600;padding:1px 7px;border-radius:4px;margin-right:6px;'>{sp.score}分</span>"
            f"<a href='#paper-{idx}' style='font-size:14.5px;font-weight:600;color:#0f172a;text-decoration:none;'>"
            f"{idx}. {render_inline_math_html(item.title_zh or p.title)}</a>"
            f"{tag_pills}"
            f"</div>"
            f"<div style='font-size:12.5px;color:#64748b;margin-top:3px;line-height:1.45;'>{render_inline_math_html(p.title)}</div>"
            f"<div style='font-size:12px;color:#94a3b8;margin-top:4px;line-height:1.5;'>"
            f"{html.escape(item.affiliation)}"
            f"<span style='color:#cbd5e1;margin:0 7px;'>|</span>"
            f"<span style='white-space:nowrap;'>{links_html}</span>"
            f"</div>"
            f"</div>"
        )

    overview_section = (
        f"<div style='background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;padding:18px 24px;margin-bottom:16px;'>"
        f"<div style='font-size:12px;font-weight:600;color:#64748b;letter-spacing:0.03em;margin-bottom:6px;'>"
        f"本期精读目录 · {len(index_items)} 篇</div>"
        f"{''.join(index_items)}"
        f"</div>"
        if index_items
        else "<div style='background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;padding:18px 24px;margin-bottom:16px;color:#64748b;font-size:13.5px;'>今日无达到深度精读阈值的高分论文。</div>"
    )

    deep_sections: list[str] = []
    if config.notifications.email_include_full_deep_dive:
        for idx, item in enumerate(report.deep_dives, 1):
            sp = item.scored
            p = sp.paper
            tag_pills = "".join(
                f"<span style='display:inline-block;white-space:nowrap;background:#f1f5f9;color:#475569;"
                f"font-size:11px;padding:1px 6px;border-radius:4px;margin-left:5px;'>{html.escape(t)}</span>"
                for t in sp.tags
            )
            link_parts = [
                f"<a href='{html.escape(p.abs_url)}' style='color:#2563eb;text-decoration:none;'>arXiv</a>",
                f"<a href='{html.escape(p.pdf_url)}' style='color:#2563eb;text-decoration:none;'>PDF</a>",
            ]
            if item.code_urls:
                link_parts.append(
                    f"<a href='{html.escape(item.code_urls[0])}' style='color:#059669;text-decoration:none;'>Code</a>"
                )
            card_links = "<span style='color:#cbd5e1;margin:0 5px;'>·</span>".join(link_parts)

            tldr, cleaned_md = prepare_body_for_display(item.markdown_body, fallback_one_liner=item.one_liner)
            tldr_html = (
                f"<div style='background:#f8fafc;border-left:3px solid #2563eb;padding:10px 14px;"
                f"border-radius:0 6px 6px 0;margin:0 0 14px;font-size:13.5px;line-height:1.68;color:#1e293b;'>"
                f"<strong style='color:#0f172a;'>一句话总结：</strong>{render_inline_math_html(tldr)}</div>"
                if tldr
                else ""
            )
            body_html = style_email_markdown_html(render_markdown_for_email(cleaned_md)) if cleaned_md else ""
            deep_sections.append(
                f"<div id='paper-{idx}' style='background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;padding:22px 24px;margin-bottom:16px;'>"
                f"<div style='font-size:12px;line-height:1.6;margin-bottom:6px;'>"
                f"<span style='color:#94a3b8;font-weight:600;margin-right:6px;'>#{idx:02d}</span>"
                f"<span style='display:inline-block;white-space:nowrap;background:#eff6ff;color:#1d4ed8;"
                f"font-size:11.5px;font-weight:600;padding:1px 7px;border-radius:4px;'>{sp.score}分</span>"
                f"{tag_pills}"
                f"<span style='color:#cbd5e1;margin:0 8px;'>|</span>"
                f"<span style='white-space:nowrap;'>{card_links}</span>"
                f"</div>"
                f"<h3 style='margin:6px 0 3px;font-size:16.5px;font-weight:700;color:#0f172a;line-height:1.4;'>"
                f"{render_inline_math_html(item.title_zh or p.title)}</h3>"
                f"<div style='margin:0 0 6px;font-size:13px;color:#64748b;line-height:1.45;'>{render_inline_math_html(p.title)}</div>"
                f"<div style='margin:0 0 14px;font-size:12px;color:#64748b;'>{html.escape(item.affiliation)} · <code>{html.escape(p.id)}</code></div>"
                f"{tldr_html}"
                f"<div>{body_html}</div>"
                f"</div>"
            )

    quick_html = ""
    if report.quick_summaries:
        item_parts: list[str] = []
        for qi, sp in enumerate(report.quick_summaries):
            q_tags = "".join(
                f"<span style='display:inline-block;white-space:nowrap;background:#f1f5f9;color:#475569;"
                f"font-size:11px;padding:1px 6px;border-radius:4px;margin-left:4px;'>{html.escape(t)}</span>"
                for t in sp.tags
            )
            reason_div = (
                f"<div style='font-size:13px;color:#475569;line-height:1.6;margin-top:3px;'>{render_inline_math_html(sp.reason)}</div>"
                if sp.reason
                else ""
            )
            border_css = "border-top:1px solid #f1f5f9;" if qi > 0 else ""
            item_parts.append(
                f"<div style='padding:10px 0;{border_css}'>"
                f"<div style='line-height:1.55;'>"
                f"<span style='display:inline-block;white-space:nowrap;background:#eff6ff;color:#1d4ed8;"
                f"font-size:11.5px;font-weight:600;padding:1px 6px;border-radius:4px;margin-right:5px;'>{sp.score}分</span>"
                f"<a href='{html.escape(sp.paper.abs_url)}' style='font-size:13.8px;font-weight:600;color:#0f172a;text-decoration:none;'>"
                f"{render_inline_math_html(sp.title_zh or sp.paper.title)}</a>"
                f"{q_tags}"
                f"<span style='font-size:12px;color:#94a3b8;margin-left:6px;'>{html.escape(sp.paper.id)}</span>"
                f"</div>"
                f"{reason_div}"
                f"</div>"
            )
        quick_html = (
            f"<div style='background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;padding:20px 24px;margin-bottom:16px;'>"
            f"<div style='font-size:13px;font-weight:600;color:#0f172a;margin-bottom:6px;'>速览推荐 · {len(report.quick_summaries)} 篇</div>"
            f"{''.join(item_parts)}"
            f"</div>"
        )

    warn_html = ""
    if report.warnings:
        w_items = "".join(f"<li style='margin:4px 0;font-size:13px;color:#92400e;'>{html.escape(w)}</li>" for w in report.warnings)
        warn_html = (
            f"<div style='background:#fffbeb;border:1px solid #fcd34d;border-radius:10px;padding:14px 18px;'>"
            f"<div style='font-size:13px;font-weight:600;color:#92400e;margin-bottom:4px;'>运行告警</div>"
            f"<ul style='margin:0;padding-left:18px;'>{w_items}</ul></div>"
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px 14px;background:#f7f8fa;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;color:#0f172a;">
  <div style="max-width:720px;margin:0 auto;">
    <div style="background:#ffffff;border:1px solid #e5e7eb;border-top:3px solid #2563eb;border-radius:10px;padding:22px 24px;margin-bottom:16px;">
      <div style="font-size:12px;color:#64748b;margin-bottom:4px;">{html.escape(report.date_iso)} · {html.escape(report.date_display)}</div>
      <h2 style="margin:0 0 6px;font-size:21px;font-weight:700;color:#0f172a;line-height:1.3;">{html.escape(config.site.title)}</h2>
      <div style="font-size:13px;color:#64748b;">抓取 {report.total_scraped} 篇 · 初筛 {report.total_prefiltered} 篇 · 精读 {len(report.deep_dives)} 篇 · 速览 {len(report.quick_summaries)} 篇</div>
      {web_link_html}
    </div>
    {overview_section}
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
