from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .models import TopicSpec

_PLACEHOLDER_PREFIXES = ("YOUR_", "<", "CHANGE_ME")
_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def is_placeholder(value: str | None) -> bool:
    if value is None:
        return True
    stripped = value.strip()
    if not stripped:
        return True
    upper = stripped.upper()
    if any(upper.startswith(p) for p in _PLACEHOLDER_PREFIXES):
        return True
    if "YOUR_EMAIL" in upper or "YOUR_API" in upper or "YOUR_FEISHU" in upper:
        return True
    return False


def _parse_bool(raw: str | bool | None, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class ArxivConfig:
    primary_categories: list[str] = field(default_factory=lambda: ["cs.RO"])
    secondary_categories: list[str] = field(default_factory=lambda: ["cs.CV", "cs.LG", "cs.AI"])
    max_show: int = 500
    secondary_prefilter_regex: str = ""
    fulltext_budget: int = 80000
    max_pdf_mb: int = 30
    user_agent: str = "DailyRobotReport/2.0 (research-digest; +https://arxiv.org)"


@dataclass
class FilteringConfig:
    batch_size: int = 30
    min_score_report: int = 7
    min_score_deep_dive: int = 8
    max_deep_dive_per_day: int = 12
    topics: list[TopicSpec] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=list)


_DEFAULT_FALLBACK_MODELS = [
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
]


@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = _GEMINI_BASE_URL
    filter_model: str = "gemini-3.8-flash"
    deep_model: str = "gemini-3.8-flash"
    fallback_models: list[str] = field(default_factory=lambda: list(_DEFAULT_FALLBACK_MODELS))
    timeout_seconds: int = 180
    max_retries: int = 4
    concurrency: int = 2
    request_interval_seconds: float = 1.5


@dataclass
class SiteConfig:
    title: str = "具身智能与机器人科研日报"
    subtitle: str = "VLA · WAM · Agent for Robotics · Robot RL"
    archive_dir: str = "archive"
    data_dir: str = "data"
    public_base_url: str = ""


@dataclass
class NotificationConfig:
    feishu_webhook: str = ""
    feishu_secret: str = ""
    feishu_max_papers_per_card: int = 7
    feishu_max_bytes: int = 22000
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_use_ssl: bool = True
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = ""
    email_to: list[str] = field(default_factory=list)
    email_attach_html: bool = True
    email_include_full_deep_dive: bool = True


@dataclass
class StateConfig:
    seen_file: str = "state/seen_papers.json"
    retention_days: int = 90


@dataclass
class AppConfig:
    project_root: Path
    arxiv: ArxivConfig
    filtering: FilteringConfig
    llm: LLMConfig
    site: SiteConfig
    notifications: NotificationConfig
    state: StateConfig

    def llm_configured(self) -> bool:
        return not is_placeholder(self.llm.api_key)

    def feishu_configured(self) -> bool:
        return (
            not is_placeholder(self.notifications.feishu_webhook)
            and self.notifications.feishu_webhook.startswith(("http://", "https://"))
        )

    def email_configured(self) -> bool:
        n = self.notifications
        return (
            not is_placeholder(n.smtp_host)
            and not is_placeholder(n.smtp_user)
            and not is_placeholder(n.smtp_password)
            and len(n.email_to) > 0
            and not any(is_placeholder(addr) for addr in n.email_to)
        )


def _env_or_default(name: str, default: str = "") -> str:
    val = os.getenv(name)
    if val is None or not val.strip():
        return default
    return val.strip()


def _resolve_llm_credentials(llm_raw: dict[str, Any]) -> tuple[str, str, str, str]:
    raw_llm_key = _env_or_default("LLM_API_KEY")
    raw_gemini_key = _env_or_default("GEMINI_API_KEY")
    raw_dash_key = _env_or_default("DASHSCOPE_API_KEY")

    provider_default_url = str(llm_raw.get("default_base_url", _GEMINI_BASE_URL))
    provider_default_filter = str(llm_raw.get("default_filter_model", "gemini-3.8-flash"))
    provider_default_deep = str(llm_raw.get("default_deep_model", "gemini-3.8-flash"))

    if not is_placeholder(raw_llm_key):
        api_key = raw_llm_key
        if api_key.startswith(("AIza", "AQ.")):
            provider_default_url = _GEMINI_BASE_URL
            provider_default_filter = "gemini-3.8-flash"
            provider_default_deep = "gemini-3.8-flash"
    elif not is_placeholder(raw_gemini_key):
        api_key = raw_gemini_key
        provider_default_url = _GEMINI_BASE_URL
        provider_default_filter = "gemini-3.8-flash"
        provider_default_deep = "gemini-3.8-flash"
    elif not is_placeholder(raw_dash_key):
        api_key = raw_dash_key
        provider_default_url = _DASHSCOPE_BASE_URL
        provider_default_filter = "qwen-plus"
        provider_default_deep = "qwen-plus"
    else:
        api_key = raw_llm_key or raw_gemini_key or raw_dash_key

    base_url = _env_or_default("LLM_BASE_URL", provider_default_url)
    filter_model = _env_or_default("LLM_FILTER_MODEL", provider_default_filter)
    deep_model = _env_or_default("LLM_DEEP_MODEL", provider_default_deep)
    return api_key, base_url, filter_model, deep_model


def load_config(
    config_path: str | Path = "config.yaml",
    env_path: str | Path | None = ".env",
) -> AppConfig:
    cfg_path = Path(config_path).resolve()
    project_root = cfg_path.parent

    if env_path is not None:
        ep = Path(env_path)
        if not ep.is_absolute():
            ep = project_root / ep
        if ep.exists():
            load_dotenv(dotenv_path=ep, override=False)

    raw: dict[str, Any] = {}
    if cfg_path.exists():
        with cfg_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

    arxiv_raw = raw.get("arxiv", {})
    arxiv_cfg = ArxivConfig(
        primary_categories=list(arxiv_raw.get("primary_categories", ["cs.RO"])),
        secondary_categories=list(arxiv_raw.get("secondary_categories", ["cs.CV", "cs.LG", "cs.AI"])),
        max_show=int(arxiv_raw.get("max_show", 500)),
        secondary_prefilter_regex=str(arxiv_raw.get("secondary_prefilter_regex", "")),
        fulltext_budget=int(arxiv_raw.get("fulltext_budget", 80000)),
        max_pdf_mb=int(arxiv_raw.get("max_pdf_mb", 30)),
        user_agent=str(arxiv_raw.get("user_agent", ArxivConfig.user_agent)),
    )

    filt_raw = raw.get("filtering", {})
    topics = [
        TopicSpec(
            tag=str(t["tag"]),
            name=str(t["name"]),
            description=str(t["description"]),
        )
        for t in filt_raw.get("topics", [])
    ]
    filtering_cfg = FilteringConfig(
        batch_size=int(filt_raw.get("batch_size", 30)),
        min_score_report=int(filt_raw.get("min_score_report", 7)),
        min_score_deep_dive=int(filt_raw.get("min_score_deep_dive", 8)),
        max_deep_dive_per_day=int(filt_raw.get("max_deep_dive_per_day", 12)),
        topics=topics,
        exclusions=[str(x) for x in filt_raw.get("exclusions", [])],
    )

    llm_raw = raw.get("llm", {})
    llm_api_key, base_url, filter_model, deep_model = _resolve_llm_credentials(llm_raw)
    raw_fb_env = _env_or_default("LLM_FALLBACK_MODELS")
    if raw_fb_env:
        fallback_models = [m.strip() for m in raw_fb_env.split(",") if m.strip()]
    else:
        fallback_models = [str(m) for m in llm_raw.get("fallback_models", _DEFAULT_FALLBACK_MODELS)]
    llm_cfg = LLMConfig(
        api_key=llm_api_key,
        base_url=base_url,
        filter_model=filter_model,
        deep_model=deep_model,
        fallback_models=fallback_models,
        timeout_seconds=int(llm_raw.get("timeout_seconds", 180)),
        max_retries=int(llm_raw.get("max_retries", 4)),
        concurrency=int(llm_raw.get("concurrency", 2)),
        request_interval_seconds=float(llm_raw.get("request_interval_seconds", 1.5)),
    )

    site_raw = raw.get("site", {})
    public_base_url = _env_or_default("PUBLIC_BASE_URL")
    if not public_base_url and os.getenv("GITHUB_REPOSITORY"):
        repo_full = os.environ["GITHUB_REPOSITORY"]
        owner = os.getenv("GITHUB_REPOSITORY_OWNER") or repo_full.split("/")[0]
        repo_name = repo_full.split("/")[-1]
        public_base_url = f"https://{owner}.github.io/{repo_name}"
    site_cfg = SiteConfig(
        title=str(site_raw.get("title", SiteConfig.title)),
        subtitle=str(site_raw.get("subtitle", SiteConfig.subtitle)),
        archive_dir=str(site_raw.get("archive_dir", "archive")),
        data_dir=str(site_raw.get("data_dir", "data")),
        public_base_url=public_base_url.rstrip("/"),
    )

    notif_raw = raw.get("notifications", {})
    raw_email_to = _env_or_default("EMAIL_TO")
    email_to = [addr.strip() for addr in raw_email_to.split(",") if addr.strip()]
    smtp_user = _env_or_default("SMTP_USER")
    smtp_port_str = _env_or_default("SMTP_PORT", "465")
    try:
        smtp_port = int(smtp_port_str)
    except ValueError:
        smtp_port = 465

    notif_cfg = NotificationConfig(
        feishu_webhook=_env_or_default("FEISHU_WEBHOOK"),
        feishu_secret=_env_or_default("FEISHU_SECRET"),
        feishu_max_papers_per_card=int(notif_raw.get("feishu_max_papers_per_card", 7)),
        feishu_max_bytes=int(notif_raw.get("feishu_max_bytes", 22000)),
        smtp_host=_env_or_default("SMTP_HOST"),
        smtp_port=smtp_port,
        smtp_use_ssl=_parse_bool(_env_or_default("SMTP_USE_SSL", "true"), default=True),
        smtp_user=smtp_user,
        smtp_password=_env_or_default("SMTP_PASSWORD"),
        email_from=_env_or_default("EMAIL_FROM", smtp_user),
        email_to=email_to,
        email_attach_html=bool(notif_raw.get("email_attach_html", True)),
        email_include_full_deep_dive=bool(notif_raw.get("email_include_full_deep_dive", True)),
    )

    state_raw = raw.get("state", {})
    state_cfg = StateConfig(
        seen_file=str(state_raw.get("seen_file", "state/seen_papers.json")),
        retention_days=int(state_raw.get("retention_days", 90)),
    )

    return AppConfig(
        project_root=project_root,
        arxiv=arxiv_cfg,
        filtering=filtering_cfg,
        llm=llm_cfg,
        site=site_cfg,
        notifications=notif_cfg,
        state=state_cfg,
    )
