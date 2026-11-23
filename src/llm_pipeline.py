from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Protocol

from openai import OpenAI

from .arxiv_fetcher import fetch_paper_metadata, normalize_arxiv_id
from .config import AppConfig
from .fulltext import get_paper_fulltext
from .models import DeepDiveItem, Paper, ScoredPaper

_SOURCE_LABEL = {
    "html": "arXiv HTML 优先级裁剪全文",
    "pdf": "arXiv PDF 提取全文（已剥离参考文献）",
    "abstract": "仅摘要（HTML 与 PDF 均未取回）",
    "none": "无可用正文与摘要",
}


def extract_json_array(text: str | None) -> list[Any] | None:
    """Robustly parse a JSON array from LLM output (raw list, dict wrapper, fenced, or embedded)."""
    if not text:
        return None
    cleaned = text.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, flags=re.I)
    if fence_match:
        fenced = fence_match.group(1).strip()
        parsed = extract_json_array(fenced)
        if parsed is not None:
            return parsed

    try:
        obj = json.loads(cleaned)
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            for val in obj.values():
                if isinstance(val, list):
                    return val
    except json.JSONDecodeError:
        pass

    start = cleaned.find("[")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for pos in range(start, len(cleaned)):
            ch = cleaned[pos]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        candidate = json.loads(cleaned[start : pos + 1])
                        if isinstance(candidate, list):
                            return candidate
                    except json.JSONDecodeError:
                        break
        start = cleaned.find("[", start + 1)
    return None


class LLMClient(Protocol):
    def chat_json(self, prompt: str, model: str) -> str: ...
    def chat_text(self, prompt: str, model: str) -> str: ...


_FALLBACK_TRIGGERS = (
    "503",
    "unavailable",
    "high demand",
    "overloaded",
    "404",
    "not found",
    "no longer available",
)


class OpenAILLMClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout_seconds: int = 180,
        fallback_models: list[str] | None = None,
    ) -> None:
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
        )
        self._supports_json_object: bool | None = None
        self._fallback_models = [m for m in (fallback_models or []) if m]
        self._dead_models: set[str] = set()
        self._lock = threading.Lock()

    def _call_with_fallback(self, call_fn, primary_model: str) -> str:
        all_candidates = [primary_model] + [m for m in self._fallback_models if m != primary_model]
        with self._lock:
            candidates = [m for m in all_candidates if m not in self._dead_models]
            if not candidates:
                self._dead_models.clear()
                candidates = list(all_candidates)
        last_exc: Exception | None = None
        for idx, model_name in enumerate(candidates):
            try:
                return call_fn(model_name)
            except Exception as exc:
                last_exc = exc
                msg = str(exc).lower()
                if any(tok in msg for tok in _FALLBACK_TRIGGERS) and idx + 1 < len(candidates):
                    with self._lock:
                        self._dead_models.add(model_name)
                    print(
                        f"[LLM] 模型 {model_name} 暂不可用（{type(exc).__name__}），"
                        f"自动切换备用模型 {candidates[idx + 1]}"
                    )
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    def _chat_json_single(self, prompt: str, model: str) -> str:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self._supports_json_object is not False:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = self._client.chat.completions.create(**kwargs)
            if self._supports_json_object is None:
                self._supports_json_object = True
            return resp.choices[0].message.content or ""
        except Exception as exc:
            msg = str(exc).lower()
            if "response_format" in msg or "json_object" in msg:
                self._supports_json_object = False
                kwargs.pop("response_format", None)
                resp = self._client.chat.completions.create(**kwargs)
                return resp.choices[0].message.content or ""
            raise

    def chat_json(self, prompt: str, model: str) -> str:
        return self._call_with_fallback(lambda m: self._chat_json_single(prompt, m), model)

    def chat_text(self, prompt: str, model: str) -> str:
        def _single(m: str) -> str:
            resp = self._client.chat.completions.create(
                model=m,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content or ""

        return self._call_with_fallback(_single, model)


class MockLLMClient:
    """Deterministic zero-cost LLM client for --mock-llm testing & dry-run checks."""

    _TAG_RULES: list[tuple[str, re.Pattern[str]]] = [
        ("VLA", re.compile(r"\b(VLA|vision[- ]language[- ]action|action chunk|bimanual|dexterous|manipulation|policy)\b", re.I)),
        ("WAM", re.compile(r"\b(world[- ]action|WAM|world model|video generation|future|dynamics|latent)\b", re.I)),
        ("Robot-Agent", re.compile(r"\b(agent|planning|reasoning|harness|self-correction|LLM|VLM|human-robot)\b", re.I)),
        ("Robot-RL", re.compile(r"\b(reinforcement learning|RL|PPO|SAC|sim2real|sim-to-real|locomotion|humanoid|reward)\b", re.I)),
    ]

    def chat_json(self, prompt: str, model: str) -> str:
        marker = "待筛选论文数据：\n"
        idx = prompt.find(marker)
        chunk_data: list[dict[str, Any]] = []
        if idx != -1:
            raw_json = prompt[idx + len(marker) :].strip()
            try:
                chunk_data = json.loads(raw_json)
            except json.JSONDecodeError:
                chunk_data = []

        results = []
        for item in chunk_data:
            text = f"{item.get('title', '')} {item.get('abstract', '')}"
            tags = [tag for tag, pat in self._TAG_RULES if pat.search(text)]
            if not tags:
                continue
            score = min(10, 6 + len(tags) + (1 if re.search(r"VLA|WAM|world model|embodied|robot", text, re.I) else 0))
            if score >= 7:
                results.append({
                    "id": item["id"],
                    "title_en": item.get("title", ""),
                    "title_zh": f"[模拟翻译] {item.get('title', '')[:60]}",
                    "score": score,
                    "tags": tags[:3],
                    "reason": f"命中方向 {', '.join(tags[:2])}，包含机器人/具身策略相关核心设计。",
                })
        return json.dumps({"papers": results}, ensure_ascii=False)

    def chat_text(self, prompt: str, model: str) -> str:
        m_id = re.search(r"arXiv ID:\s*([^\n]+)", prompt)
        m_title = re.search(r"英文标题:\s*([^\n]+)", prompt)
        m_aff = re.search(r"页眉与作者机构原文线索:\n([\s\S]*?)\n\n关于输入正文", prompt)
        pid = m_id.group(1).strip() if m_id else "unknown"
        title_en = m_title.group(1).strip() if m_title else f"arXiv:{pid}"
        aff_line = "原文未提及"
        if m_aff:
            raw_aff = m_aff.group(1).strip()
            for line in raw_aff.splitlines():
                if line.startswith("Authors & Affiliations:") or line.startswith("Notes:"):
                    aff_line = line.split(":", 1)[1].strip()[:140]
                    break
            else:
                aff_line = raw_aff.replace("\n", " ")[:140]

        return (
            "**0. 论文标题**\n"
            f"- **英文标题**: {title_en}\n"
            f"- **中文标题**: [Mock 精读] {title_en}\n"
            f"- **研究机构**: {aff_line}\n"
            f"- **一句话总结**: （Mock 模式生成）基于抓取的真实论文正文（{pid}）验证端到端解析、机构提取与多渠道推送排版。\n\n"
            "**1. 整体逻辑**\n"
            "- **研究任务**: 面向具身智能与机器人控制的策略学习、世界建模与系统协同。\n"
            "- **研究动机**: 验证从 arXiv 列表抓取、HTML/PDF 全文抽取到大模型结构化输出与多渠道推送的完整流水线。\n"
            "- **本质改动**: 当前由 `--mock-llm` 生成确定性占位报告；配置真实 `LLM_API_KEY` 后将输出完整技术细节。\n"
            "- **技术溯源**: VLA / WAM / Robot-Agent / Robot-RL。\n\n"
            "**2. 技术拆解**\n"
            "- **核心方法**: 原文全自动抽取后交由大模型按四段式模板解析。\n"
            "- **关键细节**: 真实运行时会提取输入输出维度、骨干网络参数量与消融设计。\n"
            "- **训练/优化目标**: 原文未提及（Mock 占位）。\n\n"
            "**3. 实验结果**\n"
            "- **实验设置**: 原文未提及（Mock 占位）。\n"
            "- **评价指标**: 原文未提及（Mock 占位）。\n"
            "- **主要结果**: Mock 全流程验证通过。"
        )


def compute_retry_delay(attempt: int, error_msg: str) -> float:
    """Use longer backoff for 429 / rate-limit / quota errors."""
    low = (error_msg or "").lower()
    if any(tok in low for tok in ("429", "rate", "quota", "resource_exhausted", "503", "unavailable", "high demand", "overloaded")):
        return min(60.0, 5.0 * (2 ** (attempt - 1)))
    return float(2 ** (attempt - 1))


def _replace_report_field(report: str, field_name: str, value: str) -> str:
    pattern = rf"(-\s*\*\*{re.escape(field_name)}\*\*\s*[:：]\s*).*$"
    return re.sub(pattern, lambda m: m.group(1) + value, report, count=1, flags=re.M)


def _extract_report_field(report: str, field_name: str, default: str = "") -> str:
    pattern = rf"-\s*\*\*{re.escape(field_name)}\*\*\s*[:：]\s*(.+)$"
    m = re.search(pattern, report, flags=re.M)
    return m.group(1).strip().strip("`") if m else default


def _build_filter_prompt(chunk_payload: list[dict[str, Any]], config: AppConfig) -> str:
    topics_block = "\n".join(
        f"  {idx}. [{t.tag}] {t.name}：{t.description}"
        for idx, t in enumerate(config.filtering.topics, 1)
    )
    exclusions_block = "\n".join(
        f"  {idx}. {rule}"
        for idx, rule in enumerate(config.filtering.exclusions, 1)
    )
    allowed_tags = ", ".join(t.tag for t in config.filtering.topics)
    min_report = config.filtering.min_score_report
    return f"""你是一位专注【具身大模型与机器人学习】的顶级研究员。请从以下待筛选论文中挑出符合关注方向的论文并打分（1-10分）。

✅ 重点关注方向（相关度 7-10 分，允许标签仅限：{allowed_tags}）：
{topics_block}

🛑 需要剔除的方向（直接忽略，不要出现在输出里）：
{exclusions_block}

📏 打分标准（只输出 {min_report} 分及以上的论文）：
- 9-10 分：核心命题高度契合上述四个方向，且有明确新方法、新架构、重要泛化发现或大规模基准/数据集。
- 7-8 分：属于关注方向，但偏向增量改进、工程验证或纯评测/综述。
- {min_report} 分以下：直接过滤，不要输出。

⚠️ 严格输出格式要求：只输出一个合法 JSON 对象，键名固定为 papers：
{{
  "papers": [
    {{
      "id": "原样复制输入中的 id",
      "title_en": "英文原标题",
      "title_zh": "准确简练的中文标题",
      "score": 9,
      "tags": ["VLA"],
      "reason": "不超过25字的中文推荐理由"
    }}
  ]
}}
- id 必须逐字复制待筛选数据中的 id，不得篡改、截断或凭空捏造。
- 若本批没有达到 {min_report} 分的论文，返回 {{"papers": []}}。

待筛选论文数据：
{json.dumps(chunk_payload, ensure_ascii=False)}
"""


def filter_and_score_papers(
    papers: list[Paper],
    config: AppConfig,
    llm_client: LLMClient,
    warnings: list[str] | None = None,
    failed_ids_out: set[str] | None = None,
) -> list[ScoredPaper]:
    """Batch score papers against user topics, validating IDs against input chunk."""
    if not papers:
        return []

    warn_list = warnings if warnings is not None else []
    valid_tags = {t.tag for t in config.filtering.topics}
    batch_size = max(1, config.filtering.batch_size)
    max_retries = max(1, config.llm.max_retries)

    scored_map: dict[str, ScoredPaper] = {}
    failed_batches: list[tuple[int, int]] = []

    for start in range(0, len(papers), batch_size):
        chunk = papers[start : start + batch_size]
        batch_no = start // batch_size + 1
        chunk_by_id = {p.id: p for p in chunk}
        payload = [
            {
                "id": p.id,
                "title": p.title,
                "categories": p.categories,
                "abstract": p.abstract[:1400],
            }
            for p in chunk
        ]
        prompt = _build_filter_prompt(payload, config)

        parsed: list[Any] | None = None
        last_err = ""
        for attempt in range(1, max_retries + 1):
            try:
                raw_resp = llm_client.chat_json(prompt, model=config.llm.filter_model)
                candidate = extract_json_array(raw_resp)
                if candidate is not None:
                    parsed = candidate
                    break
                last_err = "模型输出未能解析为 JSON 数组"
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            if attempt < max_retries:
                time.sleep(compute_retry_delay(attempt, last_err))

        if parsed is None:
            failed_batches.append((batch_no, len(chunk)))
            if failed_ids_out is not None:
                failed_ids_out.update(p.id for p in chunk)
            warn_list.append(f"⚠️ 初筛批次 {batch_no}（{len(chunk)} 篇）失败: {last_err}")
            continue

        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            pid = normalize_arxiv_id(str(entry.get("id", "")))
            if pid not in chunk_by_id:
                continue
            try:
                score = int(entry.get("score", 0))
            except (TypeError, ValueError):
                score = 0
            score = max(1, min(10, score))
            if score < config.filtering.min_score_report:
                continue

            raw_tags = entry.get("tags") or []
            if isinstance(raw_tags, str):
                raw_tags = [raw_tags]
            tags = [str(t) for t in raw_tags if str(t) in valid_tags]
            if not tags and config.filtering.topics:
                tags = [config.filtering.topics[0].tag]

            sp = ScoredPaper(
                paper=chunk_by_id[pid],
                score=score,
                title_zh=str(entry.get("title_zh", "")).strip(),
                tags=tags,
                reason=str(entry.get("reason", "")).strip(),
            )
            if pid not in scored_map or sp.score > scored_map[pid].score:
                scored_map[pid] = sp

        if (
            not isinstance(llm_client, MockLLMClient)
            and config.llm.request_interval_seconds > 0
            and start + batch_size < len(papers)
        ):
            time.sleep(config.llm.request_interval_seconds)

    if failed_batches:
        lost = sum(n for _, n in failed_batches)
        warn_list.append(f"⚠️ 共 {len(failed_batches)} 个初筛批次失败，约 {lost} 篇论文未纳入本次打分。")

    return sorted(scored_map.values(), key=lambda x: (-x.score, x.paper.id))


def _build_deep_dive_prompt(paper: Paper, fulltext_res) -> str:
    authors_str = ", ".join(paper.authors[:12]) if paper.authors else (fulltext_res.authors_text or "原文未提及")
    aff_hint = fulltext_res.affiliations_hint or "（页眉未显式提供单位，请从正文首段与脚注谨慎识别；找不到则写「原文未提及」）"
    code_hint = ", ".join(fulltext_res.code_urls[:4]) if fulltext_res.code_urls else "原文未提及"
    source_desc = _SOURCE_LABEL.get(fulltext_res.source, fulltext_res.source)
    body = fulltext_res.text or "（正文与摘要均不可用，请在每个字段填写「原文未提及」）"

    return f"""Role: 你是一位资深的具身智能与机器人研究专家，能用平实、高信息密度的中文快速讲透一篇论文。
Task: 像在组会上给同事讲论文一样，直接讲清这篇工作做了什么、改了哪里、架构与损失函数细节、实验数据对比。严禁空话套话，严禁编造任何原文未出现的数字或机构。

已确认的 arXiv 元数据：
- arXiv ID: {paper.id}
- 英文标题: {paper.title or f"arXiv:{paper.id}"}
- 作者: {authors_str}
- 分类: {paper.subjects or ", ".join(paper.categories) or "cs.RO"}
- 已知代码/主页链接: {code_hint}
- 页眉与作者机构原文线索:
{aff_hint}

关于输入正文（{source_desc}）：
请仅依据下方提供的正文与页眉信息作答。论文英文标题必须逐字使用上面给出的「{paper.title or f"arXiv:{paper.id}"}」；任何字段若原文未提及，请如实写「原文未提及」。

请严格按以下 Markdown 结构输出，保留所有字段名：

**0. 论文标题**
- **英文标题**: {paper.title or f"arXiv:{paper.id}"}
- **中文标题**: [精准的中文标题]
- **研究机构**: [从页眉/作者单位/邮箱域名识别主要科研单位或公司，找不到写「原文未提及」]
- **一句话总结**: [用一句话点清最核心贡献]

**1. 整体逻辑**
- **研究任务**: [解决什么具身/机器人/世界模型/RL 问题]
- **研究动机**: [现有方法的具体瓶颈或缺失]
- **本质改动**: [核心方法思路创新，一句话点透区别]
- **技术溯源**: [基于哪些基座模型或代表工作，如 Qwen2.5-VL / π0 / DINOv2 / Wan2.2 / PPO 等]

**2. 技术拆解**
- **核心方法**: [算法或系统整体设计与模块交互机制]
- **关键细节**: [输入输出模态、动作空间/chunk长度、参数量、控制频率或跨具身设计]
- **训练/优化目标**: [关键损失函数、蒸馏/对齐/RL 奖励设计]

**3. 实验结果**
- **实验设置**: [仿真基准、真实机器人平台与对比 Baseline]
- **评价指标**: [主要评价指标]
- **主要结果**: [关键数值对比与消融结论；仅引用正文真实出现的数字]

待处理论文内容：
{body}
"""


def _deep_dive_single(
    scored: ScoredPaper,
    config: AppConfig,
    llm_client: LLMClient,
    force_pdf_fallback: bool = False,
) -> DeepDiveItem:
    paper = scored.paper
    if not paper.title or not paper.abstract:
        meta = fetch_paper_metadata(paper.id, user_agent=config.arxiv.user_agent)
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

    ft = get_paper_fulltext(paper, config=config, force_pdf_fallback=force_pdf_fallback)
    prompt = _build_deep_dive_prompt(paper, ft)

    max_retries = max(1, config.llm.max_retries)
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            report_md = llm_client.chat_text(prompt, model=config.llm.deep_model)
            display_title = paper.title or f"arXiv:{paper.id}"
            report_md = _replace_report_field(report_md, "英文标题", display_title)
            title_zh = _extract_report_field(report_md, "中文标题", scored.title_zh or display_title)
            affiliation = _extract_report_field(report_md, "研究机构", "原文未提及")
            one_liner = _extract_report_field(report_md, "一句话总结", scored.reason or "")
            return DeepDiveItem(
                scored=scored,
                title_zh=title_zh,
                affiliation=affiliation,
                one_liner=one_liner,
                markdown_body=report_md.strip(),
                fulltext_source=ft.source,
                code_urls=ft.code_urls,
            )
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                time.sleep(compute_retry_delay(attempt, str(exc)))

    err_msg = f"{type(last_exc).__name__}: {last_exc}" if last_exc else "未知错误"
    fallback_md = (
        "**0. 论文标题**\n\n"
        f"- **英文标题**: {paper.title or paper.id}\n"
        f"- **中文标题**: {scored.title_zh or paper.title or paper.id}\n"
        "- **研究机构**: 原文未提及\n"
        f"- **一句话总结**: 深度解析调用失败（{err_msg}），以下附原始摘要供参考。\n\n"
        f"> **Abstract**: {paper.abstract or '暂无摘要'}"
    )
    return DeepDiveItem(
        scored=scored,
        title_zh=scored.title_zh or paper.title or paper.id,
        affiliation="原文未提及",
        one_liner=scored.reason or f"解析异常（{err_msg}）",
        markdown_body=fallback_md,
        fulltext_source=ft.source,
        code_urls=ft.code_urls,
        error=err_msg,
    )


def deep_dive_papers(
    scored_papers: list[ScoredPaper],
    config: AppConfig,
    llm_client: LLMClient,
    warnings: list[str] | None = None,
    force_pdf_fallback: bool = False,
) -> list[DeepDiveItem]:
    """Run full-text extraction and LLM deep-dive concurrently, preserving input rank."""
    if not scored_papers:
        return []
    warn_list = warnings if warnings is not None else []
    max_workers = max(1, min(config.llm.concurrency, len(scored_papers)))
    ordered_results: list[DeepDiveItem | None] = [None] * len(scored_papers)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {
            pool.submit(_deep_dive_single, sp, config, llm_client, force_pdf_fallback): idx
            for idx, sp in enumerate(scored_papers)
        }
        for future in as_completed(future_map):
            idx = future_map[future]
            try:
                item = future.result()
                ordered_results[idx] = item
                if item.error:
                    warn_list.append(f"⚠️ 论文 {item.scored.paper.id} 深度解析降级为摘要: {item.error}")
            except Exception as exc:
                sp = scored_papers[idx]
                warn_list.append(f"⚠️ 论文 {sp.paper.id} 解析发生未捕获异常: {exc}")
                ordered_results[idx] = DeepDiveItem(
                    scored=sp,
                    title_zh=sp.title_zh or sp.paper.title or sp.paper.id,
                    affiliation="原文未提及",
                    one_liner=sp.reason or "",
                    markdown_body=f"> **Abstract**: {sp.paper.abstract or '暂无摘要'}",
                    fulltext_source="abstract",
                    code_urls=list(sp.paper.code_urls),
                    error=str(exc),
                )

    return [item for item in ordered_results if item is not None]
