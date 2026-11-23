from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

import requests

from ..config import AppConfig
from ..math_render import render_inline_math_plain
from ..models import DailyReport, DeepDiveItem


def sign_feishu(timestamp: str, secret: str) -> str:
    """Compute Feishu custom bot webhook HMAC-SHA256 signature."""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def _format_deep_item_lark_md(idx: int, item: DeepDiveItem) -> str:
    sp = item.scored
    p = sp.paper
    tags_str = " ".join(f"[{t}]" for t in sp.tags) if sp.tags else ""
    title_zh = render_inline_math_plain(item.title_zh or sp.title_zh or p.title)
    title_en = render_inline_math_plain(p.title)
    one_liner = render_inline_math_plain(item.one_liner or sp.reason or "详见全文精读")
    links = [f"[arXiv]({p.abs_url})", f"[PDF]({p.pdf_url})"]
    if item.code_urls:
        links.append(f"[Code/Page]({item.code_urls[0]})")
    return (
        f"**{idx}. [{sp.score}分]{tags_str} {title_zh}**\n"
        f"英文原题：{title_en}\n"
        f"研究机构：{item.affiliation} | {' · '.join(links)}\n"
        f"核心结论：{one_liner}"
    )


def _assemble_card(
    header_title: str,
    template_color: str,
    elements: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": header_title},
                "template": template_color,
            },
            "elements": elements,
        },
    }


def _card_byte_len(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def build_feishu_cards(report: DailyReport, config: AppConfig) -> list[dict[str, Any]]:
    """Build 1 or more Feishu interactive cards respecting count and byte limits."""
    max_per_card = max(1, config.notifications.feishu_max_papers_per_card)
    max_bytes = max(6000, config.notifications.feishu_max_bytes)

    stats_md = (
        f"📅 **{report.date_iso}** ({report.date_display}) | 抓取 **{report.total_scraped}** 篇 → "
        f"预筛 **{report.total_prefiltered}** 篇 → 精读 **{len(report.deep_dives)}** 篇 · "
        f"速览 **{len(report.quick_summaries)}** 篇"
    )

    chunks: list[list[tuple[int, DeepDiveItem]]] = []
    if report.deep_dives:
        current: list[tuple[int, DeepDiveItem]] = []
        for idx, item in enumerate(report.deep_dives, 1):
            candidate = current + [(idx, item)]
            probe_elems: list[dict[str, Any]] = []
            if not chunks:
                probe_elems.append({"tag": "div", "text": {"tag": "lark_md", "content": stats_md}})
                probe_elems.append({"tag": "hr"})
            for ci, citem in candidate:
                probe_elems.append({"tag": "div", "text": {"tag": "lark_md", "content": _format_deep_item_lark_md(ci, citem)}})
            probe_card = _assemble_card("Probe", "blue", probe_elems)
            if current and (len(candidate) > max_per_card or _card_byte_len(probe_card) > max_bytes - 2500):
                chunks.append(current)
                current = [(idx, item)]
            else:
                current = candidate
        if current:
            chunks.append(current)
    else:
        chunks = [[]]

    total_parts = len(chunks)
    cards: list[dict[str, Any]] = []

    for part_idx, chunk in enumerate(chunks, 1):
        suffix = f" ({part_idx}/{total_parts})" if total_parts > 1 else ""
        header = f"🤖 具身科研日报 | {report.date_iso}{suffix}"
        elements: list[dict[str, Any]] = []
        if part_idx == 1:
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": stats_md}})
            elements.append({"tag": "hr"})

        if not chunk and part_idx == 1:
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "今日无达到深度精读阈值的高分论文。"}})
        else:
            for i, (paper_idx, item) in enumerate(chunk):
                if i > 0:
                    elements.append({"tag": "hr"})
                elements.append(
                    {"tag": "div", "text": {"tag": "lark_md", "content": _format_deep_item_lark_md(paper_idx, item)}}
                )

        if part_idx == total_parts:
            if report.quick_summaries:
                quick_lines = [
                    f"- [{sp.score}分] [{sp.paper.id}]({sp.paper.abs_url}) {render_inline_math_plain(sp.title_zh or sp.paper.title)}"
                    for sp in report.quick_summaries[:12]
                ]
                if len(report.quick_summaries) > 12:
                    quick_lines.append(f"...其余 {len(report.quick_summaries) - 12} 篇见完整归档网页。")
                elements.append({"tag": "hr"})
                elements.append(
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**⚡ 速览推荐（{len(report.quick_summaries)} 篇）**\n" + "\n".join(quick_lines),
                        },
                    }
                )

            if report.warnings:
                elements.append({"tag": "hr"})
                warn_text = "\n".join(report.warnings[:4])
                elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**⚠️ 运行告警**\n{warn_text}"}})

            if config.site.public_base_url:
                archive_url = f"{config.site.public_base_url}/{config.site.archive_dir}/{report.date_iso}.html"
                elements.append(
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "🌐 查看完整精读网页"},
                                "type": "primary",
                                "url": archive_url,
                            },
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "📚 历史索引"},
                                "type": "default",
                                "url": f"{config.site.public_base_url}/index.html",
                            },
                        ],
                    }
                )

        cards.append(_assemble_card(header, "blue", elements))

    return cards


def send_feishu(
    report: DailyReport,
    config: AppConfig,
    dry_run: bool = False,
    dry_run_dir: Path | None = None,
) -> tuple[bool, str]:
    cards = build_feishu_cards(report, config)

    if dry_run:
        if dry_run_dir is not None:
            dry_run_dir.mkdir(parents=True, exist_ok=True)
            out_path = dry_run_dir / "feishu_cards.json"
            out_path.write_text(json.dumps(cards, ensure_ascii=False, indent=2), encoding="utf-8")
        return True, f"dry-run 已生成 {len(cards)} 张飞书卡片预览"

    if not config.feishu_configured():
        return False, "FEISHU_WEBHOOK 未配置（或仍为占位符），已跳过飞书推送"

    webhook = config.notifications.feishu_webhook
    secret = config.notifications.feishu_secret

    for idx, card_payload in enumerate(cards, 1):
        body = dict(card_payload)
        if secret:
            ts = str(int(time.time()))
            body["timestamp"] = ts
            body["sign"] = sign_feishu(ts, secret)
        resp = requests.post(webhook, json=body, timeout=15)
        if resp.status_code != 200:
            return False, f"飞书推送第 {idx}/{len(cards)} 张卡片 HTTP {resp.status_code}: {resp.text[:200]}"
        try:
            data = resp.json()
        except ValueError:
            return False, f"飞书响应非 JSON: {resp.text[:200]}"
        ok = data.get("code") == 0 or data.get("StatusCode") == 0
        if not ok:
            return False, f"飞书返回错误（第 {idx}/{len(cards)} 张）: {data}"

    return True, f"已推送 {len(cards)} 张飞书交互卡片"
