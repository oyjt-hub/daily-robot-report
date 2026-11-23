from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class TopicSpec:
    tag: str
    name: str
    description: str


@dataclass
class CategoryStats:
    category: str
    raw_count: int = 0
    kept_count: int = 0
    total_announced: str = "0"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Paper:
    id: str
    title: str
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    subjects: str = ""
    comments: str = ""
    categories: list[str] = field(default_factory=list)
    code_urls: list[str] = field(default_factory=list)
    abs_url: str = ""
    pdf_url: str = ""
    html_url: str = ""

    def __post_init__(self) -> None:
        if not self.abs_url and self.id:
            self.abs_url = f"https://arxiv.org/abs/{self.id}"
        if not self.pdf_url and self.id:
            self.pdf_url = f"https://arxiv.org/pdf/{self.id}.pdf"
        if not self.html_url and self.id:
            self.html_url = f"https://arxiv.org/html/{self.id}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScoredPaper:
    paper: Paper
    score: int
    title_zh: str = ""
    tags: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.paper.id,
            "title_en": self.paper.title,
            "title_zh": self.title_zh,
            "abstract": self.paper.abstract,
            "authors": self.paper.authors,
            "subjects": self.paper.subjects,
            "comments": self.paper.comments,
            "categories": self.paper.categories,
            "code_urls": self.paper.code_urls,
            "abs_url": self.paper.abs_url,
            "pdf_url": self.paper.pdf_url,
            "html_url": self.paper.html_url,
            "score": self.score,
            "tags": self.tags,
            "reason": self.reason,
        }


@dataclass
class FullTextResult:
    paper_id: str
    text: str
    source: str  # "html" | "pdf" | "abstract" | "none"
    affiliations_hint: str = ""
    authors_text: str = ""
    code_urls: list[str] = field(default_factory=list)
    char_count: int = 0
    dropped_sections: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.char_count == 0 and self.text:
            self.char_count = len(self.text)


@dataclass
class DeepDiveItem:
    scored: ScoredPaper
    title_zh: str
    affiliation: str
    one_liner: str
    markdown_body: str
    fulltext_source: str = "abstract"
    code_urls: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        base = self.scored.to_dict()
        base.update({
            "title_zh": self.title_zh or self.scored.title_zh,
            "affiliation": self.affiliation,
            "one_liner": self.one_liner,
            "markdown_body": self.markdown_body,
            "fulltext_source": self.fulltext_source,
            "code_urls": self.code_urls or self.scored.paper.code_urls,
            "error": self.error,
        })
        return base


@dataclass
class DailyReport:
    date_iso: str
    date_display: str
    run_time_cst: str
    mode: str
    total_scraped: int
    total_prefiltered: int
    total_unseen: int
    category_stats: list[CategoryStats] = field(default_factory=list)
    deep_dives: list[DeepDiveItem] = field(default_factory=list)
    quick_summaries: list[ScoredPaper] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        all_tags = sorted({
            tag
            for item in self.deep_dives
            for tag in item.scored.tags
        } | {
            tag
            for sp in self.quick_summaries
            for tag in sp.tags
        })
        return {
            "date_iso": self.date_iso,
            "date_display": self.date_display,
            "run_time_cst": self.run_time_cst,
            "mode": self.mode,
            "total_scraped": self.total_scraped,
            "total_prefiltered": self.total_prefiltered,
            "total_unseen": self.total_unseen,
            "tags": all_tags,
            "category_stats": [cs.to_dict() for cs in self.category_stats],
            "deep_dives": [dd.to_dict() for dd in self.deep_dives],
            "quick_summaries": [sp.to_dict() for sp in self.quick_summaries],
            "warnings": self.warnings,
        }
