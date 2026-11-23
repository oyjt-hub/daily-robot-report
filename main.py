from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path

from src.arxiv_fetcher import fetch_daily_papers, normalize_arxiv_id
from src.config import AppConfig, load_config
from src.llm_pipeline import (
    LLMClient,
    MockLLMClient,
    OpenAILLMClient,
    deep_dive_papers,
    filter_and_score_papers,
)
from src.models import DailyReport, Paper, ScoredPaper
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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Daily Robot Report — 具身智能与机器人科研日报全自动流水线"
    )
    parser.add_argument(
        "--mode",
        choices=("auto", "filter", "deep"),
        default="auto",
        help="运行模式: auto(全自动初筛+精读，默认), filter(仅初筛), deep(仅精读指定ID)",
    )
    parser.add_argument(
        "--ids",
        default="",
        help="逗号分隔的 arXiv ID（传入后自动切换为 deep 模式，也兼容环境变量 TARGET_IDS）",
    )
    parser.add_argument("--config", default="config.yaml", help="YAML 配置文件路径")
    parser.add_argument("--env", default=".env", help=".env 密钥文件路径")
    parser.add_argument("--max-deep", type=int, default=None, help="覆盖每日最大深度精读篇数")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="输出根目录（默认为项目根目录；--dry-run 默认输出至 dry_run_output/）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="演练模式：输出至 dry_run_output/，不写入生产 seen 状态，不真实发送飞书与邮件",
    )
    parser.add_argument(
        "--mock-llm",
        action="store_true",
        help="使用本地确定性 Mock 模型，不调用外部 LLM API（无需 API Key）",
    )
    parser.add_argument(
        "--force-rescrape",
        action="store_true",
        help="忽略 state/seen_papers.json 去重记录，强制重新处理当天论文",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="仅生成 HTML/Markdown/JSON 归档与索引，跳过飞书和邮件推送",
    )
    return parser.parse_args(argv)


def _create_llm_client(config: AppConfig, use_mock: bool) -> LLMClient:
    if use_mock:
        return MockLLMClient()
    if not config.llm_configured():
        raise RuntimeError(
            "未检测到有效的 LLM_API_KEY（请编辑 .env 填入真实 API Key，或加 --mock-llm 进行离线测试）。"
        )
    return OpenAILLMClient(
        api_key=config.llm.api_key,
        base_url=config.llm.base_url,
        timeout_seconds=config.llm.timeout_seconds,
        fallback_models=config.llm.fallback_models,
    )


def _resolve_output_root(args: argparse.Namespace, config: AppConfig) -> Path:
    if args.output_dir:
        out = Path(args.output_dir)
        return out if out.is_absolute() else (config.project_root / out)
    if args.dry_run:
        return config.project_root / "dry_run_output"
    return config.project_root


def run_pipeline(args: argparse.Namespace) -> int:
    config = load_config(config_path=args.config, env_path=args.env)
    output_root = _resolve_output_root(args, config)
    output_root.mkdir(parents=True, exist_ok=True)

    raw_ids = args.ids or os.getenv("TARGET_IDS", "")
    manual_ids = [
        normalize_arxiv_id(token)
        for token in raw_ids.replace(" ", ",").split(",")
        if token.strip()
    ]
    mode = "deep" if manual_ids else args.mode

    try:
        llm_client = _create_llm_client(config, use_mock=args.mock_llm)
    except RuntimeError as exc:
        print(f"[配置未就绪] {exc}")
        return 1

    warnings: list[str] = []
    run_time = _cst_now_str()
    max_deep = args.max_deep if args.max_deep is not None else config.filtering.max_deep_dive_per_day

    if mode == "deep":
        if not manual_ids:
            print("[错误] deep 模式需要通过 --ids 或 TARGET_IDS 提供至少一个 arXiv ID。")
            return 2
        date_display, date_iso = _cst_today_tuple()
        scored_manual = [
            ScoredPaper(paper=Paper(id=pid, title=""), score=10, tags=["Manual"])
            for pid in manual_ids
        ]
        print(f"[deep] 开始定向深度精读 {len(scored_manual)} 篇论文: {', '.join(manual_ids)}")
        deep_items = deep_dive_papers(scored_manual, config=config, llm_client=llm_client, warnings=warnings)
        report = DailyReport(
            date_iso=date_iso,
            date_display=date_display,
            run_time_cst=run_time,
            mode="deep",
            total_scraped=len(scored_manual),
            total_prefiltered=len(scored_manual),
            total_unseen=len(scored_manual),
            deep_dives=deep_items,
            quick_summaries=[],
            warnings=warnings,
        )
    else:
        print(
            f"[{mode}] 开始抓取 arXiv: 主类={config.arxiv.primary_categories}, "
            f"扩展={config.arxiv.secondary_categories}"
        )
        date_info, prefiltered_papers, cat_stats = fetch_daily_papers(config=config, warnings=warnings)
        total_scraped = sum(cs.raw_count for cs in cat_stats)
        date_iso = date_info.get("date_iso") or _cst_today_tuple()[1]
        date_display = date_info.get("prefix") or _cst_today_tuple()[0]

        seen_path = (
            (output_root / "state" / "seen_papers.json")
            if args.dry_run
            else (config.project_root / config.state.seen_file)
        )
        seen_store = SeenPaperStore(seen_path, retention_days=config.state.retention_days)

        if args.force_rescrape or args.dry_run:
            unseen_papers = prefiltered_papers
        else:
            unseen_papers = seen_store.filter_unseen(prefiltered_papers)

        print(
            f"[{mode}] 抓取原始总计 {total_scraped} 篇 -> 预筛去重后 {len(prefiltered_papers)} 篇 -> "
            f"未处理 {len(unseen_papers)} 篇"
        )

        if not unseen_papers:
            print("[完成] 今日无新增未处理论文（可能为 arXiv 休刊日或今日已处理），跳过模型打分与推送。")
            return 0

        scored = filter_and_score_papers(
            unseen_papers,
            config=config,
            llm_client=llm_client,
            warnings=warnings,
        )
        print(f"[{mode}] 初筛 >= {config.filtering.min_score_report} 分共 {len(scored)} 篇")

        if mode == "filter":
            deep_candidates: list[ScoredPaper] = []
            quick_items = scored
        else:
            eligible_deep = [
                sp for sp in scored if sp.score >= config.filtering.min_score_deep_dive
            ]
            deep_candidates = eligible_deep[: max(0, max_deep)]
            deep_ids = {sp.paper.id for sp in deep_candidates}
            quick_items = [sp for sp in scored if sp.paper.id not in deep_ids]

        deep_items = (
            deep_dive_papers(deep_candidates, config=config, llm_client=llm_client, warnings=warnings)
            if deep_candidates
            else []
        )

        report = DailyReport(
            date_iso=date_iso,
            date_display=date_display,
            run_time_cst=run_time,
            mode=mode,
            total_scraped=total_scraped,
            total_prefiltered=len(prefiltered_papers),
            total_unseen=len(unseen_papers),
            category_stats=cat_stats,
            deep_dives=deep_items,
            quick_summaries=quick_items,
            warnings=warnings,
        )

        if not args.dry_run:
            seen_store.mark_seen([p.id for p in unseen_papers], date_iso)
            seen_store.save()

    json_path, md_path, html_path = save_daily_outputs(
        report=report,
        output_root=output_root,
        config=config,
    )
    print(f"[归档] JSON={json_path}")
    print(f"[归档] Markdown={md_path}")
    print(f"[归档] HTML={html_path}")

    if not args.no_notify:
        preview_dir = output_root if args.dry_run else None
        feishu_ok, feishu_msg = send_feishu(
            report,
            config=config,
            dry_run=args.dry_run,
            dry_run_dir=preview_dir,
        )
        print(f"[飞书] ({'OK' if feishu_ok else 'SKIP/FAIL'}) {feishu_msg}")

        email_ok, email_msg = send_email(
            report,
            config=config,
            html_report_path=html_path,
            dry_run=args.dry_run,
            dry_run_dir=preview_dir,
        )
        print(f"[邮件] ({'OK' if email_ok else 'SKIP/FAIL'}) {email_msg}")
    else:
        print("[通知] 已指定 --no-notify，跳过飞书与邮件推送。")

    return 0


def main() -> int:
    args = _parse_args()
    return run_pipeline(args)


if __name__ == "__main__":
    sys.exit(main())
