from __future__ import annotations

import argparse
import base64
import datetime
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.arxiv_fetcher import fetch_daily_papers, fetch_paper_metadata, normalize_arxiv_id
from src.config import AppConfig, load_config
from src.fulltext import get_paper_fulltext
from src.models import CategoryStats, DailyReport, DeepDiveItem, Paper, ScoredPaper
from src.notifiers.email_notifier import send_email
from src.notifiers.feishu import send_feishu
from src.site_generator import save_daily_outputs
from src.state import SeenPaperStore


def _cst_now_str() -> str:
    cst = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(cst).strftime("%Y-%m-%d %H:%M:%S")


def _cst_today_tuple() -> tuple[str, str]:
    cst = datetime.timezone(datetime.timedelta(hours=8))
    now = datetime.datetime.now(cst)
    return now.strftime("%a, %d %b %Y"), now.strftime("%Y-%m-%d")


def _paper_from_dict(raw: dict[str, Any]) -> Paper:
    pid = normalize_arxiv_id(str(raw.get("id", "")))
    return Paper(
        id=pid,
        title=str(raw.get("title") or raw.get("title_en") or ""),
        abstract=str(raw.get("abstract", "")),
        authors=list(raw.get("authors") or []),
        subjects=str(raw.get("subjects", "")),
        comments=str(raw.get("comments", "")),
        categories=list(raw.get("categories") or []),
        code_urls=list(raw.get("code_urls") or []),
    )


def cmd_fetch(args: argparse.Namespace) -> int:
    config = load_config(config_path=args.config, env_path=args.env)
    warnings: list[str] = []
    date_info, prefiltered, cat_stats = fetch_daily_papers(config=config, warnings=warnings)
    total_scraped = sum(cs.raw_count for cs in cat_stats)
    date_iso = date_info.get("date_iso") or _cst_today_tuple()[1]
    date_display = date_info.get("prefix") or _cst_today_tuple()[0]

    seen_store = SeenPaperStore(
        config.project_root / config.state.seen_file,
        retention_days=config.state.retention_days,
    )
    unseen = prefiltered if args.force_rescrape else seen_store.filter_unseen(prefiltered)

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = config.project_root / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    status = "ok" if unseen else "no_new_papers"
    payload = {
        "status": status,
        "date_iso": date_iso,
        "date_display": date_display,
        "total_scraped": total_scraped,
        "total_prefiltered": len(prefiltered),
        "total_unseen": len(unseen),
        "category_stats": [cs.to_dict() for cs in cat_stats],
        "topics": [
            {"tag": t.tag, "name": t.name, "description": t.description}
            for t in config.filtering.topics
        ],
        "exclusions": list(config.filtering.exclusions),
        "warnings": warnings,
        "papers": [
            {
                "id": p.id,
                "title": p.title,
                "abstract": p.abstract[:1400],
                "authors": p.authors[:6],
                "categories": p.categories,
                "subjects": p.subjects,
                "comments": p.comments,
                "code_urls": p.code_urls,
            }
            for p in unseen
        ],
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"STATUS={'OK' if unseen else 'NO_NEW_PAPERS'} | date={date_iso} | "
        f"scraped={total_scraped} | prefiltered={len(prefiltered)} | unseen={len(unseen)} | out={out_path}"
    )
    return 0


def _fetch_one_fulltext(
    pid: str,
    paper: Paper,
    config: AppConfig,
    out_dir: Path,
) -> tuple[str, dict[str, Any]]:
    if not paper.title or not paper.abstract:
        meta = fetch_paper_metadata(pid, user_agent=config.arxiv.user_agent)
        if meta.title and not paper.title:
            paper.title = meta.title
        if meta.abstract and not paper.abstract:
            paper.abstract = meta.abstract
        if meta.authors and not paper.authors:
            paper.authors = meta.authors
        if meta.subjects and not paper.subjects:
            paper.subjects = meta.subjects
        for cu in meta.code_urls:
            if cu not in paper.code_urls:
                paper.code_urls.append(cu)

    ft = get_paper_fulltext(paper, config=config)
    safe_id = pid.replace("/", "_")
    txt_path = out_dir / f"{safe_id}.txt"
    header = (
        f"arXiv ID: {pid}\n"
        f"Title: {paper.title}\n"
        f"Authors: {', '.join(paper.authors[:12]) if paper.authors else (ft.authors_text or '未知')}\n"
        f"Affiliations Hint: {ft.affiliations_hint or '未显式提取'}\n"
        f"Code URLs: {', '.join(ft.code_urls) if ft.code_urls else '无'}\n"
        f"Fulltext Source: {ft.source} ({ft.char_count} chars)\n"
        + "=" * 60
        + "\n\n"
    )
    txt_path.write_text(header + (ft.text or paper.abstract or ""), encoding="utf-8")
    return pid, {
        "id": pid,
        "title": paper.title,
        "authors": paper.authors,
        "subjects": paper.subjects,
        "comments": paper.comments,
        "categories": paper.categories,
        "affiliations_hint": ft.affiliations_hint,
        "code_urls": ft.code_urls,
        "source": ft.source,
        "char_count": ft.char_count,
        "text_file": str(txt_path),
    }


def cmd_fulltext(args: argparse.Namespace) -> int:
    config = load_config(config_path=args.config, env_path=args.env)
    if args.budget:
        config.arxiv.fulltext_budget = int(args.budget)

    batch_papers: dict[str, Paper] = {}
    if args.batch:
        bp = Path(args.batch)
        if not bp.is_absolute():
            bp = config.project_root / bp
        if bp.exists():
            data = json.loads(bp.read_text(encoding="utf-8"))
            for raw in data.get("papers", []):
                p = _paper_from_dict(raw)
                batch_papers[p.id] = p

    raw_ids = [
        normalize_arxiv_id(tok)
        for tok in args.ids.replace(" ", ",").split(",")
        if tok.strip()
    ]
    ids = list(dict.fromkeys(raw_ids))
    if not ids:
        print("未传入有效 arXiv ID。", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = config.project_root / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(ids))) as pool:
        futs = {
            pool.submit(
                _fetch_one_fulltext,
                pid,
                batch_papers.get(pid, Paper(id=pid, title="")),
                config,
                out_dir,
            ): pid
            for pid in ids
        }
        for fut in as_completed(futs):
            pid, info = fut.result()
            manifest[pid] = info

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"FULLTEXT_OK | count={len(manifest)} | manifest={manifest_path}")
    return 0


def _gh_upload_file(repo: str, rel_path: str, local_path: Path, message: str) -> None:
    rel_posix = rel_path.replace("\\", "/")
    endpoint = f"repos/{repo}/contents/{rel_posix}"
    sha_proc = subprocess.run(
        ["gh", "api", f"{endpoint}?ref=main", "--jq", ".sha"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    existing_sha = sha_proc.stdout.strip() if sha_proc.returncode == 0 else ""

    payload: dict[str, Any] = {
        "message": message,
        "branch": "main",
        "content": base64.b64encode(local_path.read_bytes()).decode("ascii"),
    }
    if existing_sha:
        payload["sha"] = existing_sha

    put_proc = subprocess.run(
        ["gh", "api", "-X", "PUT", endpoint, "--input", "-"],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
    )
    if put_proc.returncode != 0:
        err = put_proc.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"gh api 上传 {rel_posix} 失败: {err}")


def build_report_from_agent_result(
    batch_data: dict[str, Any],
    result_data: dict[str, Any],
    manifest_data: dict[str, Any],
    config: AppConfig,
    fetch_missing_meta: bool = True,
) -> DailyReport:
    date_iso = str(batch_data.get("date_iso") or result_data.get("date_iso") or _cst_today_tuple()[1])
    date_display = str(batch_data.get("date_display") or result_data.get("date_display") or _cst_today_tuple()[0])

    papers_by_id: dict[str, Paper] = {}
    for raw in batch_data.get("papers", []):
        p = _paper_from_dict(raw)
        papers_by_id[p.id] = p

    def _resolve_paper(pid: str, fallback_title: str = "") -> Paper:
        if pid in papers_by_id:
            p = papers_by_id[pid]
        else:
            p = Paper(id=pid, title=fallback_title)
        if pid in manifest_data:
            m = manifest_data[pid]
            if m.get("title") and not p.title:
                p.title = str(m["title"])
            if m.get("authors") and not p.authors:
                p.authors = list(m["authors"])
            if m.get("subjects") and not p.subjects:
                p.subjects = str(m["subjects"])
            for cu in m.get("code_urls") or []:
                if cu not in p.code_urls:
                    p.code_urls.append(cu)
        elif fetch_missing_meta and not p.title:
            meta = fetch_paper_metadata(pid, user_agent=config.arxiv.user_agent)
            if meta.title:
                p.title = meta.title
            if meta.abstract and not p.abstract:
                p.abstract = meta.abstract
            if meta.authors and not p.authors:
                p.authors = meta.authors
            for cu in meta.code_urls:
                if cu not in p.code_urls:
                    p.code_urls.append(cu)
        if not p.title:
            p.title = f"arXiv:{pid}"
        return p

    deep_dives: list[DeepDiveItem] = []
    for entry in result_data.get("deep_dives", []):
        pid = normalize_arxiv_id(str(entry.get("id", "")))
        if not pid:
            continue
        paper = _resolve_paper(pid, str(entry.get("title_en", "")))
        score = max(1, min(10, int(entry.get("score", 8))))
        tags = [str(t) for t in (entry.get("tags") or []) if t]
        title_zh = str(entry.get("title_zh") or "").strip() or paper.title
        affiliation = str(entry.get("affiliation") or "原文未提及").strip()
        one_liner = str(entry.get("one_liner") or entry.get("reason") or "").strip()
        reason = str(entry.get("reason") or one_liner).strip()
        md_body = str(entry.get("markdown_body") or "").strip()
        if not md_body:
            md_body = (
                "**0. 论文标题**\n\n"
                f"- **英文标题**: {paper.title}\n"
                f"- **中文标题**: {title_zh}\n"
                f"- **研究机构**: {affiliation}\n"
                f"- **一句话总结**: {one_liner or '详见原文。'}"
            )
        m_info = manifest_data.get(pid, {})
        ft_source = str(entry.get("fulltext_source") or m_info.get("source") or "html")
        code_urls = list(paper.code_urls)
        for cu in entry.get("code_urls") or []:
            if cu not in code_urls:
                code_urls.append(cu)
        sp = ScoredPaper(paper=paper, score=score, title_zh=title_zh, tags=tags, reason=reason)
        deep_dives.append(
            DeepDiveItem(
                scored=sp,
                title_zh=title_zh,
                affiliation=affiliation,
                one_liner=one_liner,
                markdown_body=md_body,
                fulltext_source=ft_source,
                code_urls=code_urls,
            )
        )

    quick_summaries: list[ScoredPaper] = []
    for entry in result_data.get("quick_summaries", []):
        pid = normalize_arxiv_id(str(entry.get("id", "")))
        if not pid:
            continue
        paper = _resolve_paper(pid, str(entry.get("title_en", "")))
        score = max(1, min(10, int(entry.get("score", 7))))
        tags = [str(t) for t in (entry.get("tags") or []) if t]
        title_zh = str(entry.get("title_zh") or "").strip()
        reason = str(entry.get("reason") or "").strip()
        quick_summaries.append(ScoredPaper(paper=paper, score=score, title_zh=title_zh, tags=tags, reason=reason))

    cat_stats = [
        CategoryStats(
            category=str(cs.get("category", "")),
            raw_count=int(cs.get("raw_count", 0)),
            kept_count=int(cs.get("kept_count", 0)),
            total_announced=str(cs.get("total_announced", "0")),
            error=cs.get("error"),
        )
        for cs in batch_data.get("category_stats", [])
    ]

    warnings = list(batch_data.get("warnings") or []) + list(result_data.get("warnings") or [])
    return DailyReport(
        date_iso=date_iso,
        date_display=date_display,
        run_time_cst=_cst_now_str(),
        mode="antigravity",
        total_scraped=int(batch_data.get("total_scraped", len(papers_by_id))),
        total_prefiltered=int(batch_data.get("total_prefiltered", len(papers_by_id))),
        total_unseen=int(batch_data.get("total_unseen", len(papers_by_id))),
        category_stats=cat_stats,
        deep_dives=deep_dives,
        quick_summaries=quick_summaries,
        warnings=warnings,
    )


def cmd_publish(args: argparse.Namespace) -> int:
    config = load_config(config_path=args.config, env_path=args.env)

    def _load_json(path_str: str | None) -> dict[str, Any]:
        if not path_str:
            return {}
        p = Path(path_str)
        if not p.is_absolute():
            p = config.project_root / p
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))

    batch_data = _load_json(args.batch)
    result_data = _load_json(args.result)
    manifest_data = _load_json(args.fulltext_manifest)

    report = build_report_from_agent_result(batch_data, result_data, manifest_data, config=config)
    json_path, md_path, html_path = save_daily_outputs(report=report, output_root=config.project_root, config=config)
    index_path = config.project_root / "index.html"
    print(f"[归档] JSON={json_path}\n[归档] Markdown={md_path}\n[归档] HTML={html_path}")

    seen_path = config.project_root / config.state.seen_file
    if not args.skip_seen_update:
        seen_store = SeenPaperStore(seen_path, retention_days=config.state.retention_days)
        seen_ids = [str(p.get("id", "")) for p in batch_data.get("papers", []) if p.get("id")]
        for dd in report.deep_dives:
            if dd.scored.paper.id not in seen_ids:
                seen_ids.append(dd.scored.paper.id)
        for sp in report.quick_summaries:
            if sp.paper.id not in seen_ids:
                seen_ids.append(sp.paper.id)
        seen_store.mark_seen(seen_ids, report.date_iso)
        seen_store.save()
        print(f"[状态] 已标记 {len(seen_ids)} 篇论文为已读 -> {seen_path}")

    exit_code = 0
    if not args.no_notify:
        ok_mail, msg_mail = send_email(report, config=config, html_report_path=html_path)
        print(f"[邮件] ({'OK' if ok_mail else 'SKIP/FAIL'}) {msg_mail}")
        if config.email_configured() and not ok_mail:
            exit_code = 1
        ok_fs, msg_fs = send_feishu(report, config=config)
        print(f"[飞书] ({'OK' if ok_fs else 'SKIP/FAIL'}) {msg_fs}")

    if args.push_github:
        commit_msg = f"chore: Antigravity daily report {report.date_iso}"
        upload_targets = [json_path, md_path, html_path, index_path]
        if not args.skip_seen_update and seen_path.exists():
            upload_targets.append(seen_path)
        for fpath in upload_targets:
            rel = fpath.resolve().relative_to(config.project_root.resolve()).as_posix()
            _gh_upload_file(args.push_github, rel, fpath, commit_msg)
            print(f"[GitHub] 已同步 {rel}")

    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description="Bridge CLI for Antigravity-native daily robot report")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="Scrape arXiv and dump unseen candidate batch JSON")
    p_fetch.add_argument("--out", default="tmp_agent/batch.json")
    p_fetch.add_argument("--config", default="config.yaml")
    p_fetch.add_argument("--env", default=".env")
    p_fetch.add_argument("--force-rescrape", action="store_true")

    p_ft = sub.add_parser("fulltext", help="Extract HTML/PDF fulltext for selected paper IDs")
    p_ft.add_argument("--ids", required=True, help="Comma-separated arXiv IDs")
    p_ft.add_argument("--batch", default="tmp_agent/batch.json")
    p_ft.add_argument("--out-dir", default="tmp_agent/fulltexts")
    p_ft.add_argument("--budget", type=int, default=45000)
    p_ft.add_argument("--config", default="config.yaml")
    p_ft.add_argument("--env", default=".env")

    p_pub = sub.add_parser("publish", help="Render archive, send notifications, and optionally sync to GitHub")
    p_pub.add_argument("--batch", default="tmp_agent/batch.json")
    p_pub.add_argument("--result", required=True)
    p_pub.add_argument("--fulltext-manifest", default="tmp_agent/fulltexts/manifest.json")
    p_pub.add_argument("--config", default="config.yaml")
    p_pub.add_argument("--env", default=".env")
    p_pub.add_argument("--no-notify", action="store_true")
    p_pub.add_argument("--push-github", default="", help="owner/repo to upload archive via gh api")
    p_pub.add_argument("--skip-seen-update", action="store_true")

    args = parser.parse_args()
    if args.command == "fetch":
        return cmd_fetch(args)
    if args.command == "fulltext":
        return cmd_fulltext(args)
    if args.command == "publish":
        return cmd_publish(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
