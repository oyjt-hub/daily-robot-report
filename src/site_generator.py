from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path
from typing import Any

import markdown

from .config import AppConfig
from .math_render import (
    MATH_PATTERNS,
    prepare_body_for_display,
    render_inline_math_html,
    render_inline_math_plain,
)
from .models import DailyReport


def render_markdown_math_safe(md_text: str) -> str:
    """Convert Markdown to HTML while preserving LaTeX math delimiters for MathJax."""
    placeholders: dict[str, str] = {}

    def _stash(match: re.Match[str]) -> str:
        token = f"@@MATHTOKEN{len(placeholders)}@@"
        placeholders[token] = html.escape(match.group(0))
        return token

    protected = MATH_PATTERNS.sub(_stash, md_text or "")
    rendered = markdown.markdown(
        protected,
        extensions=["extra", "tables", "fenced_code", "sane_lists"],
    )
    for token, raw_math in placeholders.items():
        rendered = rendered.replace(token, raw_math)
    return rendered


def build_daily_markdown(report: DailyReport, config: AppConfig) -> str:
    lines: list[str] = [
        f"# {config.site.title} - {report.date_iso} ({report.date_display})",
        "",
        f"- **运行时间（北京时间）**: `{report.run_time_cst}` | **模式**: `{report.mode}`",
        f"- **扫描统计**: 原始抓取 **{report.total_scraped}** 篇 → 预筛后 **{report.total_prefiltered}** 篇 → 未读去重 **{report.total_unseen}** 篇 → 深度精读 **{len(report.deep_dives)}** 篇 · 速览 **{len(report.quick_summaries)}** 篇",
    ]
    if report.category_stats:
        cat_desc = " | ".join(
            f"`{cs.category}`: {cs.kept_count}/{cs.raw_count}"
            for cs in report.category_stats
        )
        lines.append(f"- **分学科统计**: {cat_desc}")
    lines.append("")

    if report.deep_dives:
        lines.append(f"## 深度精读（{len(report.deep_dives)} 篇）")
        lines.append("")
        for idx, item in enumerate(report.deep_dives, 1):
            sp = item.scored
            p = sp.paper
            tags_str = ", ".join(sp.tags) if sp.tags else "General"
            links = [f"[arXiv]({p.abs_url})", f"[PDF]({p.pdf_url})", f"[HTML]({p.html_url})"]
            for ci, cu in enumerate(item.code_urls[:2], 1):
                label = "Code/Page" if ci == 1 else f"Link{ci}"
                links.append(f"[{label}]({cu})")
            title_zh = item.title_zh or p.title
            lines.append(f"### {idx}. [{p.title}] ({title_zh})")
            lines.append(
                f"- **评分/方向**: `{sp.score}/10` | **标签**: `{tags_str}` | **来源**: `{item.fulltext_source}`"
            )
            lines.append(f"- **研究机构**: `{item.affiliation}`")
            lines.append(f"- **arXiv ID**: `{p.id}` | {' | '.join(links)}")
            lines.append("")
            lines.append(item.markdown_body)
            lines.append("")
            lines.append("---")
            lines.append("")
    else:
        lines.append("## 深度精读")
        lines.append("")
        lines.append("今日暂无达到深度精读阈值的高分论文。")
        lines.append("")

    if report.quick_summaries:
        lines.append(f"## 速览推荐（{len(report.quick_summaries)} 篇）")
        lines.append("")
        for sp in report.quick_summaries:
            p = sp.paper
            tags_str = ", ".join(sp.tags) if sp.tags else "General"
            title_part = f"{sp.title_zh}（{p.title}）" if sp.title_zh else p.title
            reason_part = f" —— {sp.reason}" if sp.reason else ""
            lines.append(
                f"- **[{sp.score}分][{tags_str}]** [`{p.id}`]({p.abs_url}) {title_part}{reason_part}"
            )
        lines.append("")

    if report.warnings:
        lines.append("## 运行告警")
        lines.append("")
        for w in report.warnings:
            lines.append(f"- {w}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def _render_daily_html(report: DailyReport, config: AppConfig) -> str:
    page_title = f"{config.site.title} - {report.date_iso}"
    cat_badges = " ".join(
        f"<span class='stat-pill'>{html.escape(cs.category)}: {cs.kept_count}/{cs.raw_count}</span>"
        for cs in report.category_stats
    )

    toc_items: list[str] = []
    paper_cards: list[str] = []
    source_urls: list[str] = []

    for idx, item in enumerate(report.deep_dives, 1):
        sp = item.scored
        p = sp.paper
        source_urls.append(p.html_url)
        title_zh = item.title_zh or sp.title_zh or p.title
        toc_items.append(
            f"<li><a href='#paper-{idx}'><span class='score-badge'>{sp.score}分</span> "
            f"<span class='toc-title'>{html.escape(title_zh)}</span></a> <small>({html.escape(p.id)})</small></li>"
        )
        tag_pills = " ".join(
            f"<span class='tag-pill'>{html.escape(t)}</span>" for t in sp.tags
        )
        code_links = "".join(
            f"<a class='btn-link btn-code' href='{html.escape(cu)}' target='_blank'>Code/Project</a>"
            for cu in item.code_urls[:2]
        )
        tldr, cleaned_md = prepare_body_for_display(item.markdown_body, fallback_one_liner=item.one_liner)
        tldr_box = ""
        if tldr:
            tldr_inner = re.sub(
                r"^<p>([\s\S]*)</p>$",
                r"\1",
                render_markdown_math_safe(tldr).strip(),
            ).replace("\n", " ")
            tldr_box = f'<div class="tldr-box"><strong>一句话总结：</strong>{tldr_inner}</div>'
        body_html = render_markdown_math_safe(cleaned_md) if cleaned_md else ""
        paper_cards.append(
            f"""
            <article class="paper-card" id="paper-{idx}">
              <div class="paper-header">
                <div class="paper-meta">
                  <span class="paper-idx">#{idx:02d}</span>
                  <span class="score-badge">{sp.score}分</span>
                  {tag_pills}
                  <span class="source-pill">来源: {html.escape(item.fulltext_source)}</span>
                </div>
                <h2>{html.escape(title_zh)}</h2>
                <p class="title-en">{html.escape(p.title)}</p>
                <p class="aff-line">{html.escape(item.affiliation)} &middot; <code>{html.escape(p.id)}</code></p>
                <div class="paper-links">
                  <a class="btn-link" href="{html.escape(p.abs_url)}" target="_blank">arXiv</a>
                  <a class="btn-link" href="{html.escape(p.pdf_url)}" target="_blank">PDF</a>
                  <a class="btn-link" href="{html.escape(p.html_url)}" target="_blank">HTML 原文</a>
                  {code_links}
                </div>
              </div>
              {tldr_box}
              <div class="paper-body markdown-body">
                {body_html}
              </div>
            </article>
            """
        )

    quick_rows: list[str] = []
    for sp in report.quick_summaries:
        p = sp.paper
        tags_html = " ".join(f"<span class='tag-pill'>{html.escape(t)}</span>" for t in sp.tags)
        reason_div = f"<div class='quick-reason'>{html.escape(sp.reason)}</div>" if sp.reason else ""
        quick_rows.append(
            f"<li><div class='quick-head'><span class='score-badge'>{sp.score}分</span> {tags_html} "
            f"<a class='quick-title' href='{html.escape(p.abs_url)}' target='_blank'>{html.escape(sp.title_zh or p.title)}</a> "
            f"<code>{html.escape(p.id)}</code></div>"
            f"<div class='quick-en'>{html.escape(p.title)}</div>"
            f"{reason_div}</li>"
        )

    quick_section = ""
    if quick_rows:
        quick_section = f"""
        <section class="panel">
          <h2 class="section-label">速览推荐 · {len(quick_rows)} 篇</h2>
          <ul class="quick-list">{''.join(quick_rows)}</ul>
        </section>
        """

    warn_section = ""
    if report.warnings:
        warn_lis = "".join(f"<li>{html.escape(w)}</li>" for w in report.warnings)
        warn_section = f"<section class='panel warn-panel'><h3 class='section-label'>运行告警</h3><ul>{warn_lis}</ul></section>"

    sources_text = html.escape("\n".join(source_urls))
    sources_box = ""
    if source_urls:
        sources_box = f"""
        <section class="panel sources-box">
          <h3 class="section-label">NotebookLM / 批量精读链接 · {len(source_urls)} 篇</h3>
          <textarea id="sources-text" readonly rows="{min(8, max(3, len(source_urls)))}">{sources_text}</textarea>
          <div class="sources-actions">
            <button id="copy-btn" class="copy-btn" onclick="copySources()">复制全部 HTML 原文链接</button>
            <span id="copy-status"></span>
          </div>
        </section>
        """

    toc_html = (
        f"<section class='panel toc-panel'><h3 class='section-label'>本期精读目录 · {len(toc_items)} 篇</h3><ol>{''.join(toc_items)}</ol></section>"
        if toc_items
        else ""
    )

    empty_banner = (
        "<section class='panel'><p>今日未筛选出达到精读阈值的高分论文。</p></section>"
        if not paper_cards
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(page_title)}</title>
  <script>
    window.MathJax = {{
      tex: {{ inlineMath: [['$', '$'], ['\\\\(', '\\\\)']], displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']] }},
      options: {{ skipHtmlTags: ['script', 'style', 'textarea', 'code', 'pre'] }}
    }};
    (function() {{
      var cdns = [
        'https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js',
        'https://cdnjs.cloudflare.com/ajax/libs/mathjax/3.2.2/es5/tex-mml-chtml.min.js',
        'https://cdn.bootcdn.net/ajax/libs/mathjax/3.2.2/es5/tex-mml-chtml.min.js',
        'https://unpkg.com/mathjax@3/es5/tex-mml-chtml.js'
      ];
      var loaded = false;
      function mathReady() {{
        return !!(window.MathJax && (window.MathJax.typesetPromise || window.MathJax.version));
      }}
      function tryLoad(idx) {{
        if (loaded || mathReady() || idx >= cdns.length) return;
        var finished = false;
        var s = document.createElement('script');
        s.id = idx === 0 ? 'MathJax-script' : ('MathJax-script-' + idx);
        s.src = cdns[idx];
        s.async = true;
        var timer = setTimeout(function() {{
          if (finished || mathReady()) {{
            loaded = mathReady();
            return;
          }}
          finished = true;
          try {{ s.remove(); }} catch (e) {{}}
          tryLoad(idx + 1);
        }}, 5000);
        s.onload = function() {{
          clearTimeout(timer);
          if (mathReady()) {{
            loaded = true;
          }} else if (!finished) {{
            finished = true;
            tryLoad(idx + 1);
          }}
        }};
        s.onerror = function() {{
          clearTimeout(timer);
          if (finished) return;
          finished = true;
          tryLoad(idx + 1);
        }};
        document.head.appendChild(s);
      }}
      tryLoad(0);
    }})();
  </script>
  <style>
    :root {{ --bg:#f7f8fa; --card:#ffffff; --border:#e5e7eb; --hairline:#f1f5f9; --text:#0f172a; --body:#334155; --muted:#64748b; --accent:#2563eb; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif; background:var(--bg); color:var(--text); line-height:1.7; }}
    .container {{ max-width:820px; margin:0 auto; padding:28px 18px 64px; }}
    .back-link {{ display:inline-block; margin-bottom:14px; color:var(--muted); text-decoration:none; font-size:.88rem; font-weight:500; }}
    .back-link:hover {{ color:var(--accent); }}
    .hero {{ background:var(--card); border:1px solid var(--border); border-top:3px solid var(--accent); padding:22px 26px; border-radius:10px; margin-bottom:16px; }}
    .hero-eyebrow {{ font-size:.8rem; color:var(--muted); margin-bottom:4px; }}
    .hero h1 {{ margin:0 0 6px; font-size:1.45rem; font-weight:700; color:var(--text); letter-spacing:-0.01em; }}
    .hero .sub {{ margin:0 0 12px; color:var(--muted); font-size:.9rem; }}
    .stat-row {{ display:flex; flex-wrap:wrap; gap:6px; }}
    .stat-pill {{ background:#f8fafc; border:1px solid var(--hairline); color:var(--muted); padding:2px 9px; border-radius:6px; font-size:.78rem; white-space:nowrap; }}
    .panel {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:20px 24px; margin-bottom:16px; }}
    .section-label {{ margin:0 0 12px; font-size:.85rem; font-weight:600; color:var(--muted); letter-spacing:.03em; }}
    .toc-panel ol {{ list-style:none; padding:0; margin:0; counter-reset:toc; }}
    .toc-panel li {{ counter-increment:toc; padding:8px 0; border-top:1px solid var(--hairline); display:flex; flex-wrap:wrap; align-items:baseline; gap:8px; font-size:.93rem; }}
    .toc-panel li:first-child {{ border-top:none; }}
    .toc-panel li::before {{ content:counter(toc, decimal-leading-zero); font-size:.78rem; font-weight:600; color:#94a3b8; min-width:20px; }}
    .toc-panel li a {{ text-decoration:none; color:var(--text); font-weight:500; }}
    .toc-panel li a:hover .toc-title {{ color:var(--accent); }}
    .toc-panel li small {{ color:#94a3b8; font-size:.8rem; }}
    .warn-panel {{ border-color:#fcd34d; background:#fffbeb; }}
    .paper-card {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:24px 26px; margin-bottom:18px; }}
    .paper-header {{ margin-bottom:14px; }}
    .paper-meta {{ display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin-bottom:8px; }}
    .paper-idx {{ font-size:.8rem; font-weight:600; color:#94a3b8; margin-right:2px; }}
    .paper-header h2 {{ margin:0 0 4px; font-size:1.22rem; font-weight:700; line-height:1.4; color:var(--text); }}
    .title-en {{ margin:0 0 6px; font-size:.9rem; color:var(--muted); line-height:1.45; }}
    .aff-line {{ margin:0 0 12px; color:#94a3b8; font-size:.84rem; }}
    .aff-line code {{ background:#f8fafc; color:var(--muted); padding:1px 6px; border-radius:4px; font-size:.8rem; }}
    .score-badge {{ display:inline-block; white-space:nowrap; background:#eff6ff; color:#1d4ed8; font-weight:600; font-size:.76rem; padding:1px 7px; border-radius:4px; }}
    .tag-pill {{ display:inline-block; white-space:nowrap; background:#f1f5f9; color:#475569; font-size:.74rem; padding:1px 7px; border-radius:4px; }}
    .source-pill {{ font-size:.76rem; color:#94a3b8; margin-left:auto; }}
    .paper-links {{ display:flex; flex-wrap:wrap; gap:6px; }}
    .btn-link {{ display:inline-block; padding:3px 10px; border-radius:6px; border:1px solid var(--border); color:var(--body); background:#fff; text-decoration:none; font-size:.8rem; font-weight:500; }}
    .btn-link:hover {{ border-color:var(--accent); color:var(--accent); }}
    .btn-code {{ color:#047857; border-color:#d1fae5; background:#f0fdf4; }}
    .tldr-box {{ background:#f8fafc; border-left:3px solid var(--accent); padding:10px 14px; border-radius:0 6px 6px 0; margin:0 0 16px; font-size:.93rem; color:#1e293b; line-height:1.68; }}
    .paper-body {{ font-size:.94rem; color:var(--body); line-height:1.75; }}
    .paper-body > p {{ margin:14px 0 6px; }}
    .paper-body > p > strong:only-child {{ display:block; padding-bottom:4px; border-bottom:1px solid var(--hairline); color:var(--text); font-size:.95rem; }}
    .paper-body ul {{ margin:6px 0 12px; padding-left:20px; }}
    .paper-body li {{ margin:5px 0; }}
    .paper-body code {{ background:#f1f5f9; padding:1px 5px; border-radius:4px; font-size:.85em; }}
    .quick-list {{ list-style:none; padding:0; margin:0; }}
    .quick-list li {{ padding:11px 0; border-top:1px solid var(--hairline); }}
    .quick-list li:first-child {{ border-top:none; }}
    .quick-head {{ display:flex; flex-wrap:wrap; gap:6px; align-items:baseline; }}
    .quick-title {{ font-weight:600; color:var(--text); text-decoration:none; }}
    .quick-title:hover {{ color:var(--accent); }}
    .quick-head code {{ font-size:.78rem; color:#94a3b8; }}
    .quick-en {{ color:var(--muted); font-size:.84rem; margin-top:2px; }}
    .quick-reason {{ color:#475569; font-size:.86rem; margin-top:4px; line-height:1.6; }}
    .sources-box textarea {{ width:100%; font-family:ui-monospace,monospace; font-size:.82rem; padding:10px 12px; border:1px solid var(--border); border-radius:8px; background:#f8fafc; color:var(--body); }}
    .sources-actions {{ margin-top:10px; display:flex; align-items:center; gap:12px; }}
    .copy-btn {{ background:#0f172a; color:#fff; border:none; padding:7px 14px; border-radius:6px; cursor:pointer; font-size:.84rem; font-weight:500; }}
    .copy-btn:hover {{ background:var(--accent); }}
    #copy-status {{ color:#047857; font-size:.84rem; font-weight:500; }}
    @media (max-width:640px) {{ .paper-card {{ padding:18px 16px; }} .panel {{ padding:16px; }} .source-pill {{ margin-left:0; }} }}
  </style>
</head>
<body>
  <div class="container">
    <a class="back-link" href="../index.html">← 历史归档</a>
    <header class="hero">
      <div class="hero-eyebrow">{html.escape(report.date_iso)} &middot; {html.escape(report.date_display)}</div>
      <h1>{html.escape(config.site.title)}</h1>
      <p class="sub">抓取 {report.total_scraped} 篇 &middot; 初筛 {report.total_prefiltered} 篇 &middot; 精读 {len(report.deep_dives)} 篇 &middot; 速览 {len(report.quick_summaries)} 篇 &middot; {html.escape(report.run_time_cst)} (CST)</p>
      <div class="stat-row">
        {cat_badges}
      </div>
    </header>
    {toc_html}
    {empty_banner}
    {''.join(paper_cards)}
    {quick_section}
    {sources_box}
    {warn_section}
  </div>
  <script>
    function copySources() {{
      const el = document.getElementById('sources-text');
      const status = document.getElementById('copy-status');
      if (!el) return;
      const text = el.value;
      if (navigator.clipboard && window.isSecureContext) {{
        navigator.clipboard.writeText(text).then(() => {{
          status.textContent = '✅ 已复制到剪贴板';
        }}).catch(() => fallbackCopy(el, status));
      }} else {{
        fallbackCopy(el, status);
      }}
    }}
    function fallbackCopy(el, status) {{
      el.select();
      el.setSelectionRange(0, 99999);
      try {{
        document.execCommand('copy');
        status.textContent = '✅ 已复制到剪贴板';
      }} catch (e) {{
        status.textContent = '请手动全选复制上方链接';
      }}
    }}
  </script>
</body>
</html>
"""


def save_daily_outputs(
    report: DailyReport,
    output_root: Path,
    config: AppConfig,
) -> tuple[Path, Path, Path]:
    data_dir = output_root / config.site.data_dir
    archive_dir = output_root / config.site.archive_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)

    json_path = data_dir / f"{report.date_iso}.json"
    md_path = archive_dir / f"{report.date_iso}.md"
    html_path = archive_dir / f"{report.date_iso}.html"

    tmp_json = json_path.with_suffix(".json.tmp")
    with tmp_json.open("w", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, ensure_ascii=False, indent=2)
    os.replace(tmp_json, json_path)

    md_text = build_daily_markdown(report, config)
    md_path.write_text(md_text, encoding="utf-8")

    html_text = _render_daily_html(report, config)
    html_path.write_text(html_text, encoding="utf-8")

    rebuild_index(output_root, config)
    return json_path, md_path, html_path


def rebuild_index(output_root: Path, config: AppConfig) -> Path:
    data_dir = output_root / config.site.data_dir
    records: list[dict[str, Any]] = []
    if data_dir.exists():
        for jf in sorted(data_dir.glob("*.json"), reverse=True):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "date_iso" in data:
                    records.append(data)
            except Exception:
                continue

    records.sort(key=lambda r: r.get("date_iso", ""), reverse=True)
    all_tags = ["VLA", "WAM", "Robot-Agent", "Robot-RL"]
    for t in config.filtering.topics:
        if t.tag not in all_tags:
            all_tags.append(t.tag)

    filter_buttons = ["<button class='filter-btn active' data-tag='ALL'>全部</button>"] + [
        f"<button class='filter-btn' data-tag='{html.escape(tag)}'>{html.escape(tag)}</button>"
        for tag in all_tags
    ]

    cards_html: list[str] = []
    for rec in records:
        date_iso = str(rec.get("date_iso", ""))
        deep = rec.get("deep_dives", [])
        quick = rec.get("quick_summaries", [])
        tags = rec.get("tags", [])
        tag_str = ",".join(tags)
        previews: list[str] = []
        search_chunks: list[str] = [date_iso, str(rec.get("date_display", ""))]
        for item in deep[:5]:
            t_zh = str(item.get("title_zh") or item.get("title_en") or "")
            t_en = str(item.get("title_en") or "")
            pid = str(item.get("id") or "")
            aff = str(item.get("affiliation") or "")
            search_chunks.extend([render_inline_math_plain(t_zh), render_inline_math_plain(t_en), pid, aff])
            if len(previews) < 3:
                previews.append(
                    f"<li><span class='mini-score'>{item.get('score', '?')}分</span> "
                    f"<strong>{render_inline_math_html(t_zh)}</strong> "
                    f"<span class='mini-meta'>({html.escape(pid)})</span></li>"
                )
        for sp in quick:
            search_chunks.extend([
                render_inline_math_plain(str(sp.get("title_zh", ""))),
                render_inline_math_plain(str(sp.get("title_en", ""))),
                str(sp.get("id", "")),
            ])

        tag_pills = " ".join(f"<span class='tag-pill'>{html.escape(t)}</span>" for t in tags)
        cards_html.append(
            f"""
            <article class="day-card" data-tags="{html.escape(tag_str)}" data-search="{html.escape(' '.join(search_chunks).lower())}">
              <div class="day-top">
                <div>
                  <h2><a href="{html.escape(config.site.archive_dir)}/{html.escape(date_iso)}.html">{html.escape(date_iso)}</a></h2>
                  <p class="day-sub">{html.escape(str(rec.get('date_display', '')))} · 抓取 {rec.get('total_scraped', 0)} · 预筛 {rec.get('total_prefiltered', 0)} · 精读 {len(deep)} · 速览 {len(quick)}</p>
                </div>
                <div class="day-actions">
                  <a class="btn-view" href="{html.escape(config.site.archive_dir)}/{html.escape(date_iso)}.html">网页精读</a>
                  <a class="btn-md" href="{html.escape(config.site.archive_dir)}/{html.escape(date_iso)}.md">Markdown</a>
                </div>
              </div>
              <div class="day-tags">{tag_pills}</div>
              <ul class="preview-list">{''.join(previews) if previews else '<li>本期无高分精读，详情请见归档页。</li>'}</ul>
            </article>
            """
        )

    index_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(config.site.title)}</title>
  <style>
    :root {{ --bg:#f7f8fa; --card:#fff; --border:#e5e7eb; --hairline:#f1f5f9; --text:#0f172a; --muted:#64748b; --accent:#2563eb; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif; background:var(--bg); color:var(--text); line-height:1.65; }}
    .container {{ max-width:820px; margin:0 auto; padding:28px 18px 60px; }}
    .hero {{ background:var(--card); border:1px solid var(--border); border-top:3px solid var(--accent); border-radius:10px; padding:22px 26px; margin-bottom:16px; }}
    .hero h1 {{ margin:0 0 6px; font-size:1.45rem; font-weight:700; color:var(--text); }}
    .hero p {{ margin:0; color:var(--muted); font-size:.9rem; }}
    .toolbar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }}
    #search-input {{ flex:1; min-width:220px; padding:8px 12px; border:1px solid var(--border); border-radius:8px; font-size:.9rem; background:#fff; }}
    .filter-group {{ display:flex; flex-wrap:wrap; gap:6px; }}
    .filter-btn {{ border:1px solid var(--border); background:#fff; color:#475569; padding:5px 12px; border-radius:999px; cursor:pointer; font-size:.82rem; font-weight:500; }}
    .filter-btn.active {{ background:var(--text); color:#fff; border-color:var(--text); }}
    .day-card {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:18px 22px; margin-bottom:14px; }}
    .day-top {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; flex-wrap:wrap; }}
    .day-top h2 {{ margin:0 0 3px; font-size:1.15rem; }}
    .day-top h2 a {{ color:var(--text); text-decoration:none; }}
    .day-top h2 a:hover {{ color:var(--accent); }}
    .day-sub {{ margin:0; color:var(--muted); font-size:.85rem; }}
    .day-actions {{ display:flex; gap:8px; }}
    .btn-view, .btn-md {{ text-decoration:none; padding:5px 12px; border-radius:6px; font-size:.82rem; font-weight:500; }}
    .btn-view {{ background:var(--text); color:#fff; }}
    .btn-view:hover {{ background:var(--accent); }}
    .btn-md {{ border:1px solid var(--border); color:#475569; background:#fff; }}
    .btn-md:hover {{ border-color:var(--accent); color:var(--accent); }}
    .day-tags {{ margin:10px 0 8px; display:flex; flex-wrap:wrap; gap:5px; }}
    .tag-pill {{ background:#f1f5f9; color:#475569; border-radius:4px; padding:1px 7px; font-size:.75rem; }}
    .preview-list {{ margin:0; padding-left:18px; color:#334155; font-size:.88rem; }}
    .preview-list li {{ margin:5px 0; }}
    .mini-score {{ display:inline-block; white-space:nowrap; background:#eff6ff; color:#1d4ed8; font-weight:600; font-size:.74rem; padding:1px 6px; border-radius:4px; margin-right:5px; }}
    .mini-meta {{ color:#94a3b8; font-size:.8rem; }}
  </style>
</head>
<body>
  <div class="container">
    <header class="hero">
      <h1>{html.escape(config.site.title)}</h1>
      <p>{html.escape(config.site.subtitle)} · 共收录 {len(records)} 期日报</p>
    </header>
    <div class="toolbar">
      <input id="search-input" type="search" placeholder="搜索论文标题、arXiv ID、机构或日期..." oninput="applyFilters()">
      <div class="filter-group">{''.join(filter_buttons)}</div>
    </div>
    <main id="archive-list">
      {''.join(cards_html) if cards_html else '<p>暂无历史归档记录。</p>'}
    </main>
  </div>
  <script>
    let activeTag = 'ALL';
    document.querySelectorAll('.filter-btn').forEach(btn => {{
      btn.addEventListener('click', () => {{
        document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        activeTag = btn.dataset.tag;
        applyFilters();
      }});
    }});
    function applyFilters() {{
      const q = (document.getElementById('search-input').value || '').trim().toLowerCase();
      document.querySelectorAll('.day-card').forEach(card => {{
        const tags = (card.dataset.tags || '').split(',').filter(Boolean);
        const text = card.dataset.search || '';
        const matchTag = activeTag === 'ALL' || tags.includes(activeTag);
        const matchQuery = !q || text.includes(q);
        card.style.display = (matchTag && matchQuery) ? '' : 'none';
      }});
    }}
  </script>
</body>
</html>
"""
    index_path = output_root / "index.html"
    index_path.write_text(index_html, encoding="utf-8")
    return index_path
