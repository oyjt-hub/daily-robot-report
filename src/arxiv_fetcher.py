from __future__ import annotations

import datetime
import re
import time
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

from .config import AppConfig
from .models import CategoryStats, Paper

VALID_SHOW_VALUES = (25, 50, 100, 250, 500, 1000, 2000)

_ARXIV_ID_RE = re.compile(r"([a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?", re.I)
_CODE_URL_RE = re.compile(
    r"https?://(?:github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
    r"|huggingface\.co/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?"
    r"|[A-Za-z0-9_.-]+\.github\.io(?:/[A-Za-z0-9_./~-]*)?"
    r"|sites\.google\.com/(?:view|site)/[A-Za-z0-9_./~-]+)",
    re.I,
)
_MONTH_DATE_RE = re.compile(r"([A-Za-z]+,\s+\d{1,2}\s+[A-Za-z]+\s+\d{4})")


def normalize_arxiv_id(raw_id: str) -> str:
    text = (raw_id or "").strip()
    m = _ARXIV_ID_RE.search(text)
    if m:
        return m.group(1)
    text = re.sub(r"^arXiv:", "", text, flags=re.I).strip()
    text = re.sub(r"v\d+$", "", text, flags=re.I).strip()
    return text


_IGNORED_CODE_URLS = (
    "github.com/arxiv/html_feedback",
    "github.com/brucemiller/latexml",
    "huggingface.co/huggingface",
    "huggingface.co/docs",
)


def extract_code_urls(text: str) -> list[str]:
    if not text:
        return []
    seen: set[str] = set()
    urls: list[str] = []
    for match in _CODE_URL_RE.finditer(text):
        url = match.group(0).rstrip(".,);:]}>\"'")
        low = url.lower()
        if any(ignored in low for ignored in _IGNORED_CODE_URLS):
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _snap_show_value(max_show: int) -> int:
    for val in VALID_SHOW_VALUES:
        if val >= max_show:
            return val
    return VALID_SHOW_VALUES[-1]


def _cst_today() -> datetime.date:
    cst = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(cst).date()


def _parse_date_info(raw_heading: str) -> tuple[str, str]:
    """Parse arXiv heading text into (display_date, YYYY-MM-DD)."""
    m = _MONTH_DATE_RE.search(raw_heading or "")
    candidate = m.group(1).strip() if m else (raw_heading or "").strip()
    for fmt in ("%a, %d %b %Y", "%A, %d %B %Y", "%a, %d %B %Y", "%A, %d %b %Y"):
        try:
            dt = datetime.datetime.strptime(candidate, fmt)
            return dt.strftime("%a, %d %b %Y"), dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    today = _cst_today()
    return today.strftime("%a, %d %b %Y"), today.isoformat()


def _http_get_with_retry(
    url: str,
    user_agent: str,
    timeout: int = 20,
    attempts: int = 3,
) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(url, headers={"User-Agent": user_agent}, timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as exc:
            last_exc = exc
            if attempt < attempts:
                time.sleep(2 ** (attempt - 1))
    assert last_exc is not None
    raise last_exc


def _parse_listing_dl(dl_tag, category: str) -> list[Paper]:
    papers: list[Paper] = []
    dt_tags = dl_tag.find_all("dt")
    dd_tags = dl_tag.find_all("dd")
    for dt, dd in zip(dt_tags, dd_tags):
        link_tag = dt.find("a", title="Abstract")
        if not link_tag:
            continue
        pid = normalize_arxiv_id(link_tag.get_text(" ", strip=True))
        if not pid:
            continue

        title_el = dd.find("div", class_="list-title")
        title = title_el.get_text(" ", strip=True) if title_el else ""
        title = re.sub(r"^Title:\s*", "", title, flags=re.I).strip()

        authors = [a.get_text(" ", strip=True) for a in dd.select("div.list-authors a")]
        subj_el = dd.find("div", class_="list-subjects")
        subjects = re.sub(r"^Subjects:\s*", "", subj_el.get_text(" ", strip=True), flags=re.I).strip() if subj_el else ""
        comm_el = dd.find("div", class_="list-comments")
        comments = re.sub(r"^Comments:\s*", "", comm_el.get_text(" ", strip=True), flags=re.I).strip() if comm_el else ""

        abs_el = dd.find("p", class_="mathjax")
        abstract = abs_el.get_text(" ", strip=True) if abs_el else ""
        code_urls = extract_code_urls(f"{comments} {abstract}")

        papers.append(
            Paper(
                id=pid,
                title=title,
                abstract=abstract,
                authors=authors,
                subjects=subjects,
                comments=comments,
                categories=[category],
                code_urls=code_urls,
            )
        )
    return papers


def _hydrate_missing_abstracts(
    papers: list[Paper],
    user_agent: str,
    warnings: list[str] | None = None,
) -> None:
    """Batch fetch abstracts from export.arxiv.org for entries lacking inline abstracts."""
    missing = [p for p in papers if not p.abstract]
    if not missing:
        return
    by_id = {p.id: p for p in missing}
    ids = list(by_id.keys())
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for i in range(0, len(ids), 50):
        batch = ids[i : i + 50]
        url = f"https://export.arxiv.org/api/query?id_list={','.join(batch)}&max_results={len(batch)}"
        try:
            resp = _http_get_with_retry(url, user_agent=user_agent, timeout=25, attempts=2)
            root = ET.fromstring(resp.text)
            for entry in root.findall("atom:entry", ns):
                id_node = entry.find("atom:id", ns)
                summary_node = entry.find("atom:summary", ns)
                if id_node is None or summary_node is None:
                    continue
                pid = normalize_arxiv_id(id_node.text or "")
                if pid in by_id:
                    summary = re.sub(r"\s+", " ", (summary_node.text or "")).strip()
                    by_id[pid].abstract = summary
                    for cu in extract_code_urls(summary):
                        if cu not in by_id[pid].code_urls:
                            by_id[pid].code_urls.append(cu)
        except Exception as exc:
            if warnings is not None:
                warnings.append(f"批量补全摘要失败（{type(exc).__name__}: {exc}）")


def parse_new_listing_html(html: str, category: str) -> tuple[dict[str, str], list[Paper]]:
    """Parse /list/{cat}/new HTML keeping New + Cross submissions and excluding Replacements."""
    soup = BeautifulSoup(html, "lxml")
    h3_all = soup.find_all("h3")
    raw_heading = h3_all[0].get_text(" ", strip=True) if h3_all else ""
    date_display, date_iso = _parse_date_info(raw_heading)

    papers: list[Paper] = []
    seen_ids: set[str] = set()
    dls = soup.find_all("dl", id="articles")
    for dl in dls:
        prev_h3 = dl.find("h3") or dl.find_previous_sibling("h3")
        sec_title = prev_h3.get_text(" ", strip=True).lower() if prev_h3 else ""
        if "replacement" in sec_title:
            continue
        for paper in _parse_listing_dl(dl, category):
            if paper.id not in seen_ids:
                seen_ids.add(paper.id)
                papers.append(paper)

    return {
        "prefix": date_display,
        "date_iso": date_iso,
        "total": str(len(papers)),
    }, papers


def parse_recent_listing_html(html: str, category: str) -> tuple[dict[str, str], list[Paper]]:
    """Parse /list/{cat}/recent HTML (dls[0] = latest announcement day)."""
    soup = BeautifulSoup(html, "lxml")
    dls = soup.find_all("dl", id="articles")
    h3s = soup.find_all("h3")
    raw_date = h3s[0].get_text(" ", strip=True) if h3s else ""
    date_display, date_iso = _parse_date_info(raw_date)
    m = re.search(r"\(showing(?:\s+first)?\s+\d+\s+of\s+(\d+)\s+entries", raw_date, flags=re.I)
    total = m.group(1) if m else "0"
    papers = _parse_listing_dl(dls[0], category) if dls else []
    if total == "0" and papers:
        total = str(len(papers))
    return {
        "prefix": date_display,
        "date_iso": date_iso,
        "total": total,
    }, papers


def scrape_category_recent(
    category: str,
    max_show: int = 500,
    user_agent: str = "DailyRobotReport/2.0 (research-digest; +https://arxiv.org)",
    warnings: list[str] | None = None,
    prefer_new: bool = True,
) -> tuple[dict[str, str] | None, int, list[Paper]]:
    """Fetch latest daily submissions for one arXiv category.

    Tries /list/{cat}/new first (includes abstracts inline for New + Cross submissions),
    and falls back to /list/{cat}/recent?show={snapped} + Atom API abstract hydration.
    """
    if prefer_new:
        new_url = f"https://arxiv.org/list/{category}/new"
        try:
            resp = _http_get_with_retry(new_url, user_agent=user_agent, timeout=20, attempts=3)
            date_info, papers = parse_new_listing_html(resp.text, category)
            if papers:
                if any(not p.abstract for p in papers):
                    _hydrate_missing_abstracts(papers, user_agent=user_agent, warnings=warnings)
                return date_info, len(papers), papers
        except Exception as exc:
            if warnings is not None:
                warnings.append(f"抓取 arXiv {category}/new 失败，回退 /recent（{type(exc).__name__}: {exc}）")

    snapped = _snap_show_value(max_show)
    recent_url = f"https://arxiv.org/list/{category}/recent?show={snapped}"
    try:
        resp = _http_get_with_retry(recent_url, user_agent=user_agent, timeout=20, attempts=3)
        date_info, papers = parse_recent_listing_html(resp.text, category)
        if not papers:
            if warnings is not None:
                warnings.append(f"arXiv {category} 页面未解析到论文条目，可能今日未更新或页面改版。")
            return None, 0, []
        _hydrate_missing_abstracts(papers, user_agent=user_agent, warnings=warnings)
        return date_info, len(papers), papers
    except Exception as exc:
        if warnings is not None:
            warnings.append(f"⚠️ 抓取 arXiv {category} 失败（{type(exc).__name__}: {exc}）")
        return None, 0, []


def fetch_daily_papers(
    config: AppConfig,
    warnings: list[str] | None = None,
) -> tuple[dict[str, str], list[Paper], list[CategoryStats]]:
    """Scrape primary (keep all) + secondary (regex pre-filtered) categories and deduplicate."""
    warn_list = warnings if warnings is not None else []
    prefilter_re = (
        re.compile(config.arxiv.secondary_prefilter_regex)
        if config.arxiv.secondary_prefilter_regex
        else None
    )

    by_id: dict[str, Paper] = {}
    stats: list[CategoryStats] = []
    chosen_date_info: dict[str, str] | None = None

    all_cats: list[tuple[str, bool]] = [
        (c, True) for c in config.arxiv.primary_categories
    ] + [
        (c, False) for c in config.arxiv.secondary_categories
        if c not in config.arxiv.primary_categories
    ]

    for cat, is_primary in all_cats:
        date_info, raw_count, papers = scrape_category_recent(
            category=cat,
            max_show=config.arxiv.max_show,
            user_agent=config.arxiv.user_agent,
            warnings=warn_list,
        )
        if date_info and (chosen_date_info is None or is_primary):
            chosen_date_info = date_info

        kept_for_cat = 0
        for p in papers:
            if not is_primary and prefilter_re is not None:
                haystack = f"{p.title}\n{p.abstract}\n{p.comments}"
                if not prefilter_re.search(haystack):
                    continue
            kept_for_cat += 1
            if p.id in by_id:
                existing = by_id[p.id]
                for c in p.categories:
                    if c not in existing.categories:
                        existing.categories.append(c)
                if not existing.abstract and p.abstract:
                    existing.abstract = p.abstract
                if not existing.authors and p.authors:
                    existing.authors = p.authors
                for cu in p.code_urls:
                    if cu not in existing.code_urls:
                        existing.code_urls.append(cu)
            else:
                by_id[p.id] = p

        stats.append(
            CategoryStats(
                category=cat,
                raw_count=raw_count,
                kept_count=kept_for_cat,
                total_announced=(date_info or {}).get("total", str(raw_count)),
            )
        )

    if chosen_date_info is None:
        today = _cst_today()
        chosen_date_info = {
            "prefix": today.strftime("%a, %d %b %Y"),
            "date_iso": today.isoformat(),
            "total": "0",
        }

    return chosen_date_info, list(by_id.values()), stats


def fetch_paper_metadata(
    paper_id: str,
    user_agent: str = "DailyRobotReport/2.0 (research-digest; +https://arxiv.org)",
) -> Paper:
    """Fetch authoritative title, authors, abstract, subjects, and code links from /abs/{id}."""
    pid = normalize_arxiv_id(paper_id)
    url = f"https://arxiv.org/abs/{pid}"
    try:
        resp = _http_get_with_retry(url, user_agent=user_agent, timeout=15, attempts=3)
        soup = BeautifulSoup(resp.text, "lxml")

        title_tag = soup.find("h1", class_="title")
        title = re.sub(r"^Title:\s*", "", title_tag.get_text(" ", strip=True), flags=re.I).strip() if title_tag else ""

        authors = [a.get_text(" ", strip=True) for a in soup.select("div.authors a")]

        abs_tag = soup.find("blockquote", class_="abstract")
        abstract = re.sub(r"^Abstract:\s*", "", abs_tag.get_text(" ", strip=True), flags=re.I).strip() if abs_tag else ""

        subj_td = soup.select_one("td.tablecell.subjects")
        subjects = subj_td.get_text(" ", strip=True) if subj_td else ""

        comm_td = soup.select_one("td.tablecell.comments")
        comments = comm_td.get_text(" ", strip=True) if comm_td else ""

        hrefs = [a.get("href", "") for a in soup.find_all("a", href=True)]
        code_urls = extract_code_urls(" ".join([abstract, comments, *hrefs]))

        return Paper(
            id=pid,
            title=title,
            abstract=abstract,
            authors=authors,
            subjects=subjects,
            comments=comments,
            categories=[],
            code_urls=code_urls,
        )
    except Exception:
        return Paper(id=pid, title="")
