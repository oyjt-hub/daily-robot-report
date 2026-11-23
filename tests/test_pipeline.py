from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import patch

import pymupdf

from src.arxiv_fetcher import (
    fetch_daily_papers,
    normalize_arxiv_id,
    parse_new_listing_html,
    parse_recent_listing_html,
)
from src.config import load_config
from src.fulltext import parse_html_fulltext_document, parse_pdf_bytes
from src.llm_pipeline import OpenAILLMClient, compute_retry_delay, extract_json_array
from src.models import CategoryStats, DailyReport, DeepDiveItem, Paper, ScoredPaper
from src.notifiers.email_notifier import build_email_message
from src.notifiers.feishu import build_feishu_cards, sign_feishu
from src.site_generator import rebuild_index, save_daily_outputs
from src.state import SeenPaperStore


def _make_config(tmp_path: Path):
    cfg_file = Path(__file__).resolve().parent.parent / "config.yaml"
    return load_config(config_path=cfg_file, env_path=None)


def test_normalize_arxiv_id():
    assert normalize_arxiv_id("arXiv:2611.24940v2") == "2611.24940"
    assert normalize_arxiv_id("https://arxiv.org/abs/2611.24938") == "2611.24938"
    assert normalize_arxiv_id(" 2611.12345 ") == "2611.12345"


def test_parse_new_and_recent_listing_html():
    new_html = """
    <html><body>
      <h3>Showing new listings for Monday, 23 November 2026</h3>
      <h3>New submissions (showing 1 of 1 entries)</h3>
      <dl id="articles">
        <dt><a title="Abstract" href="/abs/2611.00001">arXiv:2611.00001</a></dt>
        <dd><div class="meta">
          <div class="list-title mathjax"><span class="descriptor">Title:</span> VLA Policy</div>
          <div class="list-authors"><a>Alice</a>, <a>Bob</a></div>
          <div class="list-subjects"><span class="descriptor">Subjects:</span> Robotics (cs.RO)</div>
          <p class="mathjax">We propose a VLA model. Code at https://github.com/org/vla-repo.</p>
        </div></dd>
      </dl>
      <h3>Cross submissions (showing 1 of 1 entries)</h3>
      <dl id="articles">
        <dt><a title="Abstract" href="/abs/2611.00002">arXiv:2611.00002</a></dt>
        <dd><div class="meta">
          <div class="list-title mathjax"><span class="descriptor">Title:</span> World Model</div>
          <p class="mathjax">World-action model abstract.</p>
        </div></dd>
      </dl>
      <dl id="articles">
        <h3>Replacement submissions (showing 1 of 1 entries)</h3>
        <dt><a title="Abstract" href="/abs/2501.99999">arXiv:2501.99999</a></dt>
        <dd><div class="meta"><div class="list-title mathjax">Old Replacement</div></div></dd>
      </dl>
    </body></html>
    """
    date_info, papers = parse_new_listing_html(new_html, "cs.RO")
    assert date_info["date_iso"] == "2026-11-23"
    assert [p.id for p in papers] == ["2611.00001", "2611.00002"]
    assert papers[0].code_urls == ["https://github.com/org/vla-repo"]

    recent_html = """
    <html><body>
      <h3>Mon, 23 Nov 2026 (showing first 50 of 89 entries )</h3>
      <dl id="articles">
        <dt><a title="Abstract" href="/abs/2611.00001">arXiv:2611.00001</a></dt>
        <dd><div class="meta"><div class="list-title">Title: Test</div></div></dd>
      </dl>
    </body></html>
    """
    r_info, r_papers = parse_recent_listing_html(recent_html, "cs.RO")
    assert r_info["date_iso"] == "2026-11-23"
    assert r_info["total"] == "89"
    assert len(r_papers) == 1


def test_fetch_daily_papers_multicat_dedup_and_prefilter(tmp_path):
    cfg = _make_config(tmp_path)
    p_ro = Paper(id="2611.10001", title="General Legged Control", abstract="Mechanical gear.", categories=["cs.RO"])
    p_shared = Paper(id="2611.10002", title="Unified VLA for Bimanual Manipulation", abstract="Robot VLA policy.", categories=["cs.RO"])
    p_cv_keep = Paper(id="2611.10002", title="Unified VLA for Bimanual Manipulation", abstract="Robot VLA policy.", categories=["cs.CV"])
    p_cv_drop = Paper(id="2611.10003", title="Cartoon Portrait Super-Resolution", abstract="Image restoration only.", categories=["cs.CV"])
    p_lg_keep = Paper(id="2611.10004", title="Offline Reinforcement Learning for Dexterous Robot Hand", abstract="Robot RL sim2real.", categories=["cs.LG"])

    fake_map = {
        "cs.RO": ({"prefix": "Mon, 23 Nov 2026", "date_iso": "2026-11-23", "total": "2"}, 2, [p_ro, p_shared]),
        "cs.CV": ({"prefix": "Mon, 23 Nov 2026", "date_iso": "2026-11-23", "total": "2"}, 2, [p_cv_keep, p_cv_drop]),
        "cs.LG": ({"prefix": "Mon, 23 Nov 2026", "date_iso": "2026-11-23", "total": "1"}, 1, [p_lg_keep]),
        "cs.AI": ({"prefix": "Mon, 23 Nov 2026", "date_iso": "2026-11-23", "total": "0"}, 0, []),
    }

    with patch("src.arxiv_fetcher.scrape_category_recent", side_effect=lambda category, **kw: fake_map[category]):
        date_info, papers, stats = fetch_daily_papers(cfg)

    assert date_info["date_iso"] == "2026-11-23"
    ids = [p.id for p in papers]
    # Primary cs.RO keeps all (even non-prefilter p_ro), secondary drops p_cv_drop, deduplicates 2611.10002
    assert ids == ["2611.10001", "2611.10002", "2611.10004"]
    shared = next(p for p in papers if p.id == "2611.10002")
    assert shared.categories == ["cs.RO", "cs.CV"]
    assert len(stats) == 4


def test_html_fulltext_authors_priority_and_partial_truncation():
    related_text = "R" * 2400
    method_text = "M" * 2200
    exp_text = "E" * 6000
    html_doc = f"""
    <html><body>
      <h1 class="ltx_title">Phoenix World Model</h1>
      <div class="ltx_authors">Jane Doe (MIT CSAIL), John Smith (Stanford University)</div>
      <div class="ltx_abstract">Abstract: We build a fast WAM at <a href="https://github.com/mit/phoenix-wam">code</a>.</div>
      <section><h2>1 Introduction</h2><p>{"I" * 800}</p></section>
      <section><h2>2 Related Work</h2><p>{related_text}</p></section>
      <section><h2>3 Method</h2><p>{method_text}</p></section>
      <section><h2>4 Experiments and Results</h2><p>{exp_text}</p></section>
    </body></html>
    """
    res = parse_html_fulltext_document("2611.24938", html_doc, budget=4500)
    assert res is not None
    assert res.source == "html"
    assert "MIT CSAIL" in res.affiliations_hint
    assert "https://github.com/mit/phoenix-wam" in res.code_urls
    # Priority 0 HEADER + ABSTRACT + truncated Experiments must be kept before Related Work
    assert "## HEADER_AND_AFFILIATIONS" in res.text
    assert "## 4 Experiments and Results" in res.text
    assert "...[truncated]" in res.text
    assert "## 2 Related Work" not in res.text


def test_pdf_fallback_strips_references_and_extracts_links():
    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((72, 72), "RoboAgent: Self-Correcting Embodied Agent\nAlice Zhang, Tsinghua University & BAAI\n" + ("Method details. " * 120))
    p1.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 100, 220, 120), "uri": "https://github.com/tsinghua/roboagent"})
    p2 = doc.new_page()
    p2.insert_text((72, 72), "Experiments show 85.4% success rate.\n" + ("Evaluation details. " * 150))
    p3 = doc.new_page()
    p3.insert_text((72, 72), "More ablations and sim2real transfer.\n" + ("Ablation text. " * 120))
    p4 = doc.new_page()
    p4.insert_text((72, 72), "References\n[1] Author et al. 2024.\n[2] Another citation.\n")
    pdf_bytes = doc.tobytes()
    doc.close()

    res = parse_pdf_bytes("2611.99999", pdf_bytes, budget=50000)
    assert res is not None
    assert res.source == "pdf"
    assert "Tsinghua University" in res.affiliations_hint
    assert "https://github.com/tsinghua/roboagent" in res.code_urls
    assert "85.4% success rate" in res.text
    assert "[1] Author et al." not in res.text


def test_extract_json_array_variants():
    assert extract_json_array('[{"id": "1"}]') == [{"id": "1"}]
    assert extract_json_array('{"papers": [{"id": "2", "score": 9}]}') == [{"id": "2", "score": 9}]
    assert extract_json_array("Here is JSON:\n```json\n{\"papers\": [{\"id\": \"3\"}]}\n```") == [{"id": "3"}]
    assert extract_json_array('Noise [invalid json] and valid [{"id": "4"}] end') == [{"id": "4"}]
    assert extract_json_array("not json") is None


def test_seen_paper_store_atomic_and_prune(tmp_path):
    store_file = tmp_path / "state" / "seen_papers.json"
    store = SeenPaperStore(store_file, retention_days=30)
    old_date = (datetime.date.today() - datetime.timedelta(days=45)).isoformat()
    recent_date = datetime.date.today().isoformat()
    store.mark_seen(["2501.00001"], old_date)
    store.mark_seen(["2611.00001"], recent_date)
    store.save()

    reloaded = SeenPaperStore(store_file, retention_days=30)
    assert not reloaded.is_seen("2501.00001")
    assert reloaded.is_seen("2611.00001")
    papers = [Paper(id="2611.00001", title="Seen"), Paper(id="2611.00002", title="New")]
    assert [p.id for p in reloaded.filter_unseen(papers)] == ["2611.00002"]


def test_feishu_cards_chunking_signing_and_email_and_site(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.notifications.feishu_max_papers_per_card = 3
    cfg.notifications.feishu_max_bytes = 9000
    cfg.site.public_base_url = "https://example.github.io/daily-robot-report"

    deep_items = []
    for i in range(1, 6):
        p = Paper(id=f"2611.0000{i}", title=f"VLA Paper {i}", code_urls=[f"https://github.com/org/p{i}"])
        sp = ScoredPaper(paper=p, score=9, title_zh=f"具身论文 {i}", tags=["VLA", "Robot-RL"], reason="高相关")
        md = (
            "**0. 论文标题**\n"
            f"- **英文标题**: VLA Paper {i}\n"
            f"- **中文标题**: 具身论文 {i}\n"
            "- **研究机构**: Stanford University\n"
            f"- **一句话总结**: 验证公式 $\\mathcal{{L}}_{{\\text{{flow}}}}$ 效果好。\n"
        )
        deep_items.append(
            DeepDiveItem(
                scored=sp,
                title_zh=f"具身论文 {i}",
                affiliation="Stanford University",
                one_liner="验证公式 $\\mathcal{L}_{\\text{flow}}$ 效果好。",
                markdown_body=md,
                fulltext_source="html",
                code_urls=p.code_urls,
            )
        )

    quick = [
        ScoredPaper(
            paper=Paper(id="2611.00009", title="Borderline RL"),
            score=7,
            title_zh="边缘 RL 论文",
            tags=["Robot-RL"],
            reason="偏向仿真基线对比",
        )
    ]

    report = DailyReport(
        date_iso="2026-11-23",
        date_display="Mon, 23 Nov 2026",
        run_time_cst="2026-11-23 10:00:00",
        mode="auto",
        total_scraped=120,
        total_prefiltered=45,
        total_unseen=45,
        category_stats=[CategoryStats(category="cs.RO", raw_count=89, kept_count=89, total_announced="89")],
        deep_dives=deep_items,
        quick_summaries=quick,
    )

    # Signing test
    ts = "1700000000"
    secret = "my-secret"
    expected_sig = base64.b64encode(hmac.new(f"{ts}\n{secret}".encode(), digestmod=hashlib.sha256).digest()).decode()
    assert sign_feishu(ts, secret) == expected_sig

    cards = build_feishu_cards(report, cfg)
    assert len(cards) == 2
    assert "(1/2)" in cards[0]["card"]["header"]["title"]["content"]
    assert "(2/2)" in cards[1]["card"]["header"]["title"]["content"]
    # Action button only on final card
    assert not any(el.get("tag") == "action" for el in cards[0]["card"]["elements"])
    assert any(el.get("tag") == "action" for el in cards[1]["card"]["elements"])
    assert "###" not in json.dumps(cards, ensure_ascii=False)

    # Site generation & math safety
    json_path, md_path, html_path = save_daily_outputs(report, tmp_path, cfg)
    assert json_path.exists() and md_path.exists() and html_path.exists()
    html_text = html_path.read_text(encoding="utf-8")
    assert r"$\mathcal{L}_{\text{flow}}$" in html_text
    assert "<em>" not in html_text.split("一句话总结")[1].splitlines()[0]
    assert "copySources()" in html_text

    idx_path = rebuild_index(tmp_path, cfg)
    idx_text = idx_path.read_text(encoding="utf-8")
    assert "2026-11-23" in idx_text
    assert "具身论文 1" in idx_text

    # Email MIME + attachment
    msg = build_email_message(report, cfg, html_report_path=html_path)
    attachments = list(msg.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "2026-11-23.html"
    email_html = msg.get_body(preferencelist=("html",)).get_content()
    assert "具身论文 1" in email_html
    assert "Stanford University" in email_html
def test_gemini_config_autodetect_and_override(tmp_path, monkeypatch):
    cfg_file = Path(__file__).resolve().parent.parent / "config.yaml"
    for var in ("LLM_API_KEY", "GEMINI_API_KEY", "DASHSCOPE_API_KEY", "LLM_BASE_URL", "LLM_FILTER_MODEL", "LLM_DEEP_MODEL", "LLM_FALLBACK_MODELS"):
        monkeypatch.delenv(var, raising=False)

    # 1. GEMINI_API_KEY defaults to gemini-3.8-flash + fallback list
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFakeTestKey")
    cfg = load_config(config_path=cfg_file, env_path=None)
    assert cfg.llm_configured()
    assert cfg.llm.api_key == "AIzaFakeTestKey"
    assert cfg.llm.base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert cfg.llm.filter_model == "gemini-3.8-flash"
    assert cfg.llm.deep_model == "gemini-3.8-flash"
    assert "gemini-3.5-flash-lite" in cfg.llm.fallback_models

    # 2. AQ.-prefixed key passed via LLM_API_KEY also resolves Gemini endpoint
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "AQ.FakeAuthKeyTest")
    cfg_aq = load_config(config_path=cfg_file, env_path=None)
    assert cfg_aq.llm.api_key == "AQ.FakeAuthKeyTest"
    assert cfg_aq.llm.base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert cfg_aq.llm.deep_model == "gemini-3.8-flash"

    # 3. Explicit overrides take precedence
    monkeypatch.setenv("LLM_BASE_URL", "https://custom.proxy/v1")
    monkeypatch.setenv("LLM_DEEP_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "gemini-3.6-flash,gemini-3.5-flash")
    cfg2 = load_config(config_path=cfg_file, env_path=None)
    assert cfg2.llm.base_url == "https://custom.proxy/v1"
    assert cfg2.llm.deep_model == "gemini-3.5-flash-lite"
    assert cfg2.llm.fallback_models == ["gemini-3.6-flash", "gemini-3.5-flash"]


def test_compute_retry_delay():
    assert compute_retry_delay(1, "HTTP 429 RESOURCE_EXHAUSTED") == 5.0
    assert compute_retry_delay(1, "Error code: 503 UNAVAILABLE high demand") == 5.0
    assert compute_retry_delay(2, "rate limit exceeded") == 10.0
    assert compute_retry_delay(5, "quota exceeded") == 60.0
    assert compute_retry_delay(1, "TimeoutError: read timed out") == 1.0
    assert compute_retry_delay(3, "ConnectionResetError") == 4.0


def test_openai_client_fallback_on_503_and_404():
    client = OpenAILLMClient(
        api_key="AQ.FakeKey",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        fallback_models=["gemini-3.7-flash", "gemini-3.5-flash-lite"],
    )
    called_models: list[str] = []

    class _Msg:
        content = '{"papers": []}'

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    def fake_create(**kwargs):
        model = kwargs["model"]
        called_models.append(model)
        if model == "gemini-3.8-flash":
            raise RuntimeError("Error code: 503 - UNAVAILABLE high demand")
        if model == "gemini-3.7-flash":
            raise RuntimeError("Error code: 404 - NOT_FOUND")
        return _Resp()

    assert client._client.max_retries == 0
    with patch.object(client._client.chat.completions, "create", side_effect=fake_create):
        out = client.chat_json("hello", model="gemini-3.8-flash")
        assert out == '{"papers": []}'
        assert called_models == ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash-lite"]

        called_models.clear()
        out_txt = client.chat_text("hello", model="gemini-3.8-flash")
        assert out_txt == '{"papers": []}'
        assert called_models == ["gemini-3.5-flash-lite"]
def test_env_secret_strips_utf8_bom_and_zwsp(monkeypatch):
    cfg_file = Path(__file__).resolve().parent.parent / "config.yaml"
    for var in ("LLM_API_KEY", "GEMINI_API_KEY", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "\ufeff\u200bAQ.TestKeyClean  ")
    cfg = load_config(config_path=cfg_file, env_path=None)
    assert cfg.llm.api_key == "AQ.TestKeyClean"
    assert cfg.llm.api_key.isascii()
def test_agent_bridge_publish_roundtrip(tmp_path, monkeypatch):
    import argparse
    import shutil
    from ops.agent_bridge import cmd_publish

    shutil.copy(Path(__file__).resolve().parent.parent / "config.yaml", tmp_path / "config.yaml")
    batch_file = tmp_path / "batch.json"
    result_file = tmp_path / "result.json"
    manifest_file = tmp_path / "manifest.json"

    batch_file.write_text(
        json.dumps({
            "status": "ok",
            "date_iso": "2026-11-23",
            "date_display": "Mon, 23 Nov 2026",
            "total_scraped": 100,
            "total_prefiltered": 20,
            "total_unseen": 20,
            "category_stats": [{"category": "cs.RO", "raw_count": 89, "kept_count": 89, "total_announced": "89"}],
            "papers": [
                {"id": "2611.27187", "title": "SCAP: Stochastic Context for Action Expert Pretraining", "abstract": "VLA abstract", "authors": ["A. Author"], "categories": ["cs.RO"]}
            ],
        }),
        encoding="utf-8",
    )
    manifest_file.write_text(
        json.dumps({
            "2611.27187": {
                "id": "2611.27187",
                "title": "SCAP: Stochastic Context for Action Expert Pretraining",
                "authors": ["A. Author"],
                "code_urls": ["https://github.com/org/scap"],
                "source": "html",
            }
        }),
        encoding="utf-8",
    )
    result_file.write_text(
        json.dumps({
            "deep_dives": [
                {
                    "id": "2611.27187",
                    "score": 9,
                    "tags": ["VLA"],
                    "title_zh": "SCAP：面向动作专家预训练的随机上下文",
                    "affiliation": "Test Lab",
                    "one_liner": "提出随机上下文掩码防止视觉捷径。",
                    "markdown_body": "**0. 论文标题**\n\n- **英文标题**: SCAP\n- **中文标题**: SCAP：随机上下文",
                }
            ],
            "quick_summaries": [],
        }),
        encoding="utf-8",
    )

    args = argparse.Namespace(
        batch=str(batch_file),
        result=str(result_file),
        fulltext_manifest=str(manifest_file),
        config=str(tmp_path / "config.yaml"),
        env=None,
        no_notify=True,
        push_github="",
        skip_seen_update=True,
    )
    rc = cmd_publish(args)
    assert rc == 0
    assert (tmp_path / "data" / "2026-11-23.json").exists()
    assert (tmp_path / "archive" / "2026-11-23.md").exists()
    assert (tmp_path / "archive" / "2026-11-23.html").exists()
    assert (tmp_path / "index.html").exists()
    assert "Test Lab" in (tmp_path / "archive" / "2026-11-23.md").read_text(encoding="utf-8")


def test_agent_bridge_defaults_resolve_from_different_cwd(tmp_path, monkeypatch):
    from ops.agent_bridge import PROJECT_ROOT, build_parser

    monkeypatch.chdir(tmp_path)
    args = build_parser().parse_args(["fetch"])
    assert Path(args.config) == PROJECT_ROOT / "config.yaml"
    assert Path(args.env) == PROJECT_ROOT / ".env"
    cfg = load_config(config_path=args.config, env_path=None)
    assert cfg.project_root == PROJECT_ROOT.resolve()
    assert bool(cfg.arxiv.secondary_prefilter_regex)

