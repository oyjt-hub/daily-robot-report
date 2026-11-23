from __future__ import annotations

import re

import pymupdf
import requests
from bs4 import BeautifulSoup

from .arxiv_fetcher import extract_code_urls
from .config import AppConfig
from .models import FullTextResult, Paper

SECTION_PRIORITY: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"experiment|result|evaluation|ablation|benchmark", re.I), 0),
    (re.compile(r"method|approach|model|architecture|framework|algorithm", re.I), 1),
    (re.compile(r"introduction|conclusion|discussion", re.I), 2),
    (re.compile(r"related work|background|preliminar", re.I), 4),
    (re.compile(r"appendix|acknowledg|impact statement|ethic", re.I), 5),
]

_REFERENCES_HEADING_RE = re.compile(r"(?im)^\s*(?:\d+\.?\s+)?(?:references|bibliography)\s*$")
_MIN_HTML_CHARS = 1500
_MIN_PARTIAL_KEEP_CHARS = 2000


def section_priority(heading: str) -> int:
    for pattern, prio in SECTION_PRIORITY:
        if pattern.search(heading or ""):
            return prio
    return 3


def _budget_parts(
    parts: list[tuple[str, int, str]],
    budget: int,
) -> tuple[str, list[str]]:
    """Fill character budget by (priority, original_order) and reassemble in document order."""
    ordered = sorted(range(len(parts)), key=lambda idx: (parts[idx][1], idx))
    kept_text: dict[int, str] = {}
    used = 0
    dropped: list[str] = []

    for idx in ordered:
        heading, prio, text = parts[idx]
        if not text:
            continue
        if used + len(text) <= budget:
            kept_text[idx] = text
            used += len(text)
        elif prio <= 1 and (budget - used) >= _MIN_PARTIAL_KEEP_CHARS:
            remaining = budget - used
            suffix = "\n...[truncated]"
            cut = max(0, remaining - len(suffix))
            kept_text[idx] = text[:cut].rstrip() + suffix
            used = budget
            dropped.append(f"{heading} (truncated)")
        else:
            dropped.append(heading)

    chunks = [f"## {parts[idx][0]}\n{kept_text[idx]}" for idx in range(len(parts)) if idx in kept_text]
    return "\n\n".join(chunks), dropped


def parse_html_fulltext_document(
    paper_id: str,
    html: str,
    budget: int = 80000,
) -> FullTextResult | None:
    """Parse arXiv HTML into prioritized full text, preserving authors & affiliations."""
    soup = BeautifulSoup(html, "lxml")

    title_el = soup.select_one("h1.ltx_title")
    title_text = title_el.get_text(" ", strip=True) if title_el else ""

    authors_div = soup.select_one("div.ltx_authors")
    authors_text = authors_div.get_text(" ", strip=True) if authors_div else ""

    extra_notes: list[str] = []
    for note_el in soup.select(".ltx_author_notes, .ltx_dates, .ltx_note"):
        if authors_div is not None and note_el in authors_div.descendants:
            continue
        note_txt = note_el.get_text(" ", strip=True)
        if note_txt and note_txt not in extra_notes:
            extra_notes.append(note_txt)

    header_lines: list[str] = []
    if title_text:
        header_lines.append(f"Title: {title_text}")
    if authors_text:
        header_lines.append(f"Authors & Affiliations: {authors_text}")
    if extra_notes:
        header_lines.append("Notes: " + " | ".join(extra_notes[:6]))
    affiliations_hint = "\n".join(header_lines).strip()

    href_candidates = " ".join(a.get("href", "") for a in soup.find_all("a", href=True))

    for tag in soup(["script", "style"]):
        tag.decompose()

    ref_tags = soup.find_all(["section", "div"], class_=re.compile(r"bibliography|references", re.I))
    ref_tags += soup.find_all(["section", "div"], id=re.compile(r"bib|references", re.I))
    for tag in ref_tags:
        tag.decompose()

    code_urls = extract_code_urls(f"{href_candidates} {soup.get_text(' ', strip=True)[:20000]}")

    parts: list[tuple[str, int, str]] = []
    if affiliations_hint:
        parts.append(("HEADER_AND_AFFILIATIONS", 0, affiliations_hint))

    abstract_el = soup.find("div", class_=re.compile(r"ltx_abstract", re.I))
    if abstract_el:
        abs_text = abstract_el.get_text(" ", strip=True)
        if abs_text:
            parts.append(("ABSTRACT", 0, abs_text))

    sections = [s for s in soup.find_all("section") if s.find_parent("section") is None]
    for sec in sections:
        head_tag = sec.find(["h1", "h2", "h3", "h4"])
        heading = head_tag.get_text(" ", strip=True) if head_tag else "(untitled)"
        sec_text = sec.get_text(" ", strip=True)
        if not sec_text:
            continue
        parts.append((heading, section_priority(heading), sec_text))

    if not sections:
        body_text = soup.get_text(" ", strip=True)
        if len(body_text) < _MIN_HTML_CHARS:
            return None
        combined = f"## HEADER_AND_AFFILIATIONS\n{affiliations_hint}\n\n{body_text}" if affiliations_hint else body_text
        combined = combined[:budget]
        return FullTextResult(
            paper_id=paper_id,
            text=combined,
            source="html",
            affiliations_hint=affiliations_hint,
            authors_text=authors_text,
            code_urls=code_urls,
        )

    assembled, dropped = _budget_parts(parts, budget=budget)
    if len(assembled) < _MIN_HTML_CHARS:
        return None

    return FullTextResult(
        paper_id=paper_id,
        text=assembled,
        source="html",
        affiliations_hint=affiliations_hint,
        authors_text=authors_text,
        code_urls=code_urls,
        dropped_sections=dropped,
    )


def extract_html_fulltext(
    paper_id: str,
    budget: int = 80000,
    user_agent: str = "DailyRobotReport/2.0 (research-digest; +https://arxiv.org)",
) -> FullTextResult | None:
    url = f"https://arxiv.org/html/{paper_id}"
    try:
        resp = requests.get(url, headers={"User-Agent": user_agent}, timeout=25)
        if resp.status_code != 200:
            return None
        return parse_html_fulltext_document(paper_id=paper_id, html=resp.text, budget=budget)
    except Exception:
        return None


def parse_pdf_bytes(
    paper_id: str,
    pdf_bytes: bytes,
    budget: int = 80000,
) -> FullTextResult | None:
    """Extract PDF text & hyperlink annotations using PyMuPDF, stripping trailing References."""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        page_texts: list[str] = []
        link_uris: list[str] = []
        for page in doc:
            page_texts.append(page.get_text("text"))
            for link in page.get_links():
                uri = link.get("uri")
                if uri:
                    link_uris.append(uri)
    finally:
        doc.close()

    if not page_texts:
        return None

    full_raw = "\n\n".join(page_texts)
    front_text = "\n".join(page_texts[:2])[:2200].strip()

    if len(page_texts) > 1:
        min_ref_offset = max(len(page_texts[0]) + 10, len(full_raw) // 4)
    else:
        min_ref_offset = max(500, len(full_raw) // 2)
    for match in _REFERENCES_HEADING_RE.finditer(full_raw):
        if match.start() >= min_ref_offset:
            full_raw = full_raw[: match.start()].rstrip()
            break

    full_raw = re.sub(r"\n{3,}", "\n\n", full_raw).strip()
    if len(full_raw) < 300:
        return None

    code_urls = extract_code_urls(" ".join(link_uris) + " " + full_raw[:30000])
    if len(full_raw) > budget:
        suffix = "\n...[truncated]"
        full_raw = full_raw[: max(0, budget - len(suffix))].rstrip() + suffix

    return FullTextResult(
        paper_id=paper_id,
        text=full_raw,
        source="pdf",
        affiliations_hint=front_text,
        authors_text="",
        code_urls=code_urls,
    )


def extract_pdf_fulltext(
    paper_id: str,
    budget: int = 80000,
    max_pdf_mb: int = 30,
    user_agent: str = "DailyRobotReport/2.0 (research-digest; +https://arxiv.org)",
) -> FullTextResult | None:
    url = f"https://arxiv.org/pdf/{paper_id}.pdf"
    max_bytes = max_pdf_mb * 1024 * 1024
    try:
        with requests.get(url, headers={"User-Agent": user_agent}, timeout=45, stream=True) as resp:
            if resp.status_code != 200:
                return None
            content_len = resp.headers.get("Content-Length")
            if content_len and content_len.isdigit() and int(content_len) > max_bytes:
                return None
            chunks: list[bytes] = []
            downloaded = 0
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > max_bytes:
                    return None
                chunks.append(chunk)
            pdf_bytes = b"".join(chunks)
        return parse_pdf_bytes(paper_id=paper_id, pdf_bytes=pdf_bytes, budget=budget)
    except Exception:
        return None


def get_paper_fulltext(
    paper: Paper,
    config: AppConfig,
    force_pdf_fallback: bool = False,
) -> FullTextResult:
    """Try HTML -> PDF -> Abstract fallback and merge discovered code URLs."""
    result: FullTextResult | None = None
    if not force_pdf_fallback:
        result = extract_html_fulltext(
            paper.id,
            budget=config.arxiv.fulltext_budget,
            user_agent=config.arxiv.user_agent,
        )
    if result is None:
        result = extract_pdf_fulltext(
            paper.id,
            budget=config.arxiv.fulltext_budget,
            max_pdf_mb=config.arxiv.max_pdf_mb,
            user_agent=config.arxiv.user_agent,
        )
    if result is not None:
        merged_urls = list(paper.code_urls)
        for cu in result.code_urls:
            if cu not in merged_urls:
                merged_urls.append(cu)
        result.code_urls = merged_urls
        return result

    if paper.abstract:
        return FullTextResult(
            paper_id=paper.id,
            text=f"Title: {paper.title}\nAbstract: {paper.abstract}",
            source="abstract",
            affiliations_hint="",
            authors_text=", ".join(paper.authors),
            code_urls=list(paper.code_urls),
        )

    return FullTextResult(
        paper_id=paper.id,
        text="",
        source="none",
        code_urls=list(paper.code_urls),
    )
