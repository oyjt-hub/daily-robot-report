from __future__ import annotations

import html
import re
from typing import Any

import markdown

MATH_PATTERNS = re.compile(
    r"(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|(?<!\\)\$(?!\s)([^\n$]+?)(?<!\s)\$)"
)

_FONT_FAMILY = "Cambria,Georgia,'Times New Roman',serif"

_IGNORED_COMMANDS = frozenset({
    "left", "right", "big", "Big", "bigg", "Bigg",
    "bigl", "bigr", "Bigl", "Bigr", "biggl", "biggr", "Biggl", "Biggr",
    "middle", "displaystyle", "textstyle", "scriptstyle", "scriptscriptstyle",
    "limits", "nolimits",
})

_UPRIGHT_TEXT_COMMANDS = frozenset({
    "text", "textrm", "mathrm", "operatorname", "mathsf", "texttt", "mbox",
})

_BOLD_COMMANDS = frozenset({"mathbf", "boldsymbol", "bm", "textbf"})
_CAL_COMMANDS = frozenset({"mathcal", "mathscr"})

_ACCENT_COMBINING: dict[str, str] = {
    "hat": "\u0302",
    "widehat": "\u0302",
    "tilde": "\u0303",
    "widetilde": "\u0303",
    "bar": "\u0304",
    "overline": "\u0304",
    "vec": "\u20d7",
    "dot": "\u0307",
    "ddot": "\u0308",
}

_BLACKBOARD_MAP: dict[str, str] = {
    "R": "ℝ",
    "N": "ℕ",
    "Z": "ℤ",
    "Q": "ℚ",
    "C": "ℂ",
}

_SYMBOL_MAP: dict[str, str] = {
    # Lowercase Greek
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "epsilon": "ϵ", "varepsilon": "ε", "zeta": "ζ", "eta": "η",
    "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "omicron": "ο", "pi": "π", "varpi": "ϖ", "rho": "ρ",
    "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ",
    "upsilon": "υ", "phi": "ϕ", "varphi": "φ", "chi": "χ",
    "psi": "ψ", "omega": "ω", "ell": "ℓ", "hbar": "ℏ",
    # Uppercase Greek
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ",
    "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ",
    "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    # Operators & sets
    "sum": "∑", "prod": "∏", "int": "∫", "oint": "∮",
    "partial": "∂", "nabla": "∇", "infty": "∞",
    "in": "∈", "notin": "∉", "ni": "∋",
    "subset": "⊂", "subseteq": "⊆", "supset": "⊃", "supseteq": "⊇",
    "cup": "∪", "cap": "∩", "emptyset": "∅", "varnothing": "∅",
    "forall": "∀", "exists": "∃", "nexists": "∄",
    "land": "∧", "wedge": "∧", "lor": "∨", "vee": "∨",
    "neg": "¬", "lnot": "¬",
    # Arrows
    "to": "→", "rightarrow": "→", "leftarrow": "←", "gets": "←",
    "leftrightarrow": "↔", "Rightarrow": "⇒", "Leftarrow": "⇐",
    "Leftrightarrow": "⇔", "iff": "⇔", "mapsto": "↦",
    # Relations
    "approx": "≈", "sim": "∼", "simeq": "≃", "cong": "≅",
    "equiv": "≡", "neq": "≠", "ne": "≠",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥",
    "ll": "≪", "gg": "≫", "propto": "∝",
    # Arithmetic & binary ops
    "pm": "±", "mp": "∓", "times": "×", "div": "÷",
    "cdot": "·", "circ": "∘", "bullet": "•",
    "odot": "⊙", "oplus": "⊕", "otimes": "⊗", "ominus": "⊖",
    "star": "★", "ast": "∗", "dagger": "†", "ddagger": "‡",
    "top": "⊤", "bot": "⊥", "perp": "⊥",
    # Brackets & punctuation
    "langle": "⟨", "rangle": "⟩",
    "lceil": "⌈", "rceil": "⌉", "lfloor": "⌊", "rfloor": "⌋",
    "dots": "…", "ldots": "…", "cdots": "⋯", "vdots": "⋮", "ddots": "⋱",
    "mid": "|", "Vert": "‖",
    "quad": "  ", "qquad": "    ", "enspace": " ", "thinspace": " ",
}

_OP_WORDS = frozenset({
    "arg", "max", "min", "argmax", "argmin", "sup", "inf",
    "lim", "limsup", "liminf", "log", "ln", "lg", "exp",
    "sin", "cos", "tan", "cot", "sec", "csc",
    "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh",
    "det", "dim", "ker", "deg", "gcd", "Pr", "Var", "Cov", "diag",
})

_ESCAPED_SINGLE: dict[str, str] = {
    "{": "{", "}": "}", "|": "‖",
    ",": " ", ";": " ", ":": " ", " ": " ", "!": "",
    "%": "%", "_": "_", "&": "&", "#": "#", "$": "$", "\\": "\\",
}

_ONE_ARG_COMMANDS = (
    _UPRIGHT_TEXT_COMMANDS | _BOLD_COMMANDS | _CAL_COMMANDS | set(_ACCENT_COMBINING) | {"mathbb", "sqrt"}
)
_TWO_ARG_COMMANDS = frozenset({"frac", "dfrac", "tfrac"})

_SUP_UNICODE = str.maketrans("0123456789+\u2212-=()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾")
_SUB_UNICODE = str.maketrans("0123456789+\u2212-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎")


def _strip_delimiters(raw_math: str) -> tuple[str, bool]:
    s = raw_math.strip()
    if s.startswith("$$") and s.endswith("$$") and len(s) >= 4:
        return s[2:-2].strip(), True
    if s.startswith(r"\[") and s.endswith(r"\]") and len(s) >= 4:
        return s[2:-2].strip(), True
    if s.startswith(r"\(") and s.endswith(r"\)") and len(s) >= 4:
        return s[2:-2].strip(), False
    if s.startswith("$") and s.endswith("$") and len(s) >= 2:
        return s[1:-1].strip(), False
    return s, False


def _skip_ws(text: str, pos: int) -> int:
    n = len(text)
    while pos < n and text[pos].isspace():
        pos += 1
    return pos


def _read_command(text: str, pos: int) -> tuple[str, int]:
    """Read LaTeX command starting after backslash; pos points to backslash."""
    n = len(text)
    if pos + 1 >= n:
        return "\\", pos + 1
    nxt = text[pos + 1]
    if nxt.isalpha():
        end = pos + 2
        while end < n and text[end].isalpha():
            end += 1
        return text[pos + 1:end], end
    return nxt, pos + 2


def _read_group(text: str, pos: int) -> tuple[str, int]:
    """Read one braced group or single LaTeX token/command at pos."""
    pos = _skip_ws(text, pos)
    n = len(text)
    if pos >= n:
        return "", pos
    if text[pos] == "{":
        depth = 1
        cur = pos + 1
        while cur < n and depth > 0:
            ch = text[cur]
            if ch == "\\" and cur + 1 < n:
                cur += 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[pos + 1:cur], cur + 1
            cur += 1
        return text[pos + 1:], n
    if text[pos] == "\\":
        cmd, end = _read_command(text, pos)
        if cmd in _ONE_ARG_COMMANDS:
            if cmd == "operatorname" and end < n and text[end] == "*":
                end += 1
            arg, end2 = _read_group(text, end)
            return text[pos:end] + "{" + arg + "}", end2
        if cmd in _TWO_ARG_COMMANDS:
            arg1, e1 = _read_group(text, end)
            arg2, e2 = _read_group(text, e1)
            return f"\\{cmd}{{{arg1}}}{{{arg2}}}", e2
        return text[pos:end], end
    return text[pos], pos + 1


def _esc_text(text: str, html_mode: bool) -> str:
    return html.escape(text, quote=False) if html_mode else text


def _is_simple_fraction_part(part: str) -> bool:
    stripped = part.strip()
    return len(stripped) <= 3 and stripped.replace("\\", "").isalnum()


def _format_script(inner: str, is_sup: bool, html_mode: bool) -> str:
    if html_mode:
        tag = "sup" if is_sup else "sub"
        return f"<{tag}>{inner}</{tag}>"
    marker = "^" if is_sup else "_"
    table = _SUP_UNICODE if is_sup else _SUB_UNICODE
    if inner and all(ch in "0123456789+\u2212-=()" for ch in inner):
        return inner.translate(table)
    clean = inner.strip()
    if len(clean) == 1 or (clean.isalnum() and " " not in clean):
        return f"{marker}{clean}"
    return f"{marker}{{{clean}}}"


def _render_latex_nodes(expr: str, html_mode: bool, upright: bool = False) -> str:
    out: list[str] = []
    i = 0
    n = len(expr)

    while i < n:
        ch = expr[i]

        if ch in ("^", "_"):
            is_sup = ch == "^"
            arg, nxt = _read_group(expr, i + 1)
            rendered_arg = _render_latex_nodes(arg, html_mode=html_mode, upright=upright)
            out.append(_format_script(rendered_arg, is_sup=is_sup, html_mode=html_mode))
            i = nxt
            continue

        if ch == "{":
            arg, nxt = _read_group(expr, i)
            out.append(_render_latex_nodes(arg, html_mode=html_mode, upright=upright))
            i = nxt
            continue

        if ch == "}":
            i += 1
            continue

        if ch == "\\":
            cmd, nxt = _read_command(expr, i)
            if len(cmd) == 1 and cmd in _ESCAPED_SINGLE:
                out.append(_esc_text(_ESCAPED_SINGLE[cmd], html_mode))
                i = nxt
                continue
            if cmd in _IGNORED_COMMANDS:
                if cmd in ("left", "right") and nxt < n and expr[nxt] == ".":
                    nxt += 1
                i = nxt
                continue
            if cmd in _TWO_ARG_COMMANDS:
                num_raw, p1 = _read_group(expr, nxt)
                den_raw, p2 = _read_group(expr, p1)
                num = _render_latex_nodes(num_raw, html_mode=html_mode, upright=upright)
                den = _render_latex_nodes(den_raw, html_mode=html_mode, upright=upright)
                num_s = num if _is_simple_fraction_part(num_raw) else f"({num})"
                den_s = den if _is_simple_fraction_part(den_raw) else f"({den})"
                out.append(f"{num_s}/{den_s}")
                i = p2
                continue
            if cmd in _UPRIGHT_TEXT_COMMANDS:
                if cmd == "operatorname" and nxt < n and expr[nxt] == "*":
                    nxt += 1
                arg, p1 = _read_group(expr, nxt)
                out.append(_render_latex_nodes(arg, html_mode=html_mode, upright=True))
                i = p1
                continue
            if cmd in _BOLD_COMMANDS:
                arg, p1 = _read_group(expr, nxt)
                inner = _render_latex_nodes(arg, html_mode=html_mode, upright=True)
                out.append(f"<b>{inner}</b>" if html_mode else inner)
                i = p1
                continue
            if cmd in _CAL_COMMANDS:
                arg, p1 = _read_group(expr, nxt)
                inner = _render_latex_nodes(arg, html_mode=html_mode, upright=True)
                out.append(f'<span style="font-style:italic;">{inner}</span>' if html_mode else inner)
                i = p1
                continue
            if cmd == "mathbb":
                arg, p1 = _read_group(expr, nxt)
                stripped = arg.strip()
                if stripped in _BLACKBOARD_MAP:
                    out.append(_BLACKBOARD_MAP[stripped])
                else:
                    inner = _render_latex_nodes(arg, html_mode=html_mode, upright=True)
                    out.append(f"<b>{inner}</b>" if html_mode else inner)
                i = p1
                continue
            if cmd in _ACCENT_COMBINING:
                arg, p1 = _read_group(expr, nxt)
                comb = _ACCENT_COMBINING[cmd]
                stripped = arg.strip()
                if html_mode and len(stripped) == 1 and stripped.isalpha() and not upright:
                    out.append(f"<i>{_esc_text(stripped, True)}{comb}</i>")
                else:
                    inner = _render_latex_nodes(arg, html_mode=html_mode, upright=upright)
                    out.append(f"{inner}{comb}")
                i = p1
                continue
            if cmd == "sqrt":
                arg, p1 = _read_group(expr, nxt)
                inner = _render_latex_nodes(arg, html_mode=html_mode, upright=upright)
                if len(arg.strip()) == 1:
                    out.append(f"√{inner}")
                else:
                    out.append(f"√({inner})")
                i = p1
                continue
            if cmd in _SYMBOL_MAP:
                out.append(_esc_text(_SYMBOL_MAP[cmd], html_mode))
                i = nxt
                continue
            if cmd in _OP_WORDS:
                out.append(_esc_text(cmd, html_mode))
                i = nxt
                continue
            # Fallback: emit command name without raw backslash
            out.append(_esc_text(cmd, html_mode))
            i = nxt
            continue

        if ch.isascii() and ch.isalpha():
            j = i + 1
            while j < n and expr[j].isascii() and expr[j].isalpha():
                j += 1
            word = expr[i:j]
            if html_mode and not upright:
                out.append(f"<i>{word}</i>")
            else:
                out.append(word)
            i = j
            continue

        if ch == "-":
            out.append("−" if not upright else "-")
            i += 1
            continue

        if ch.isspace():
            if not out or out[-1] != " ":
                out.append(" ")
            i = _skip_ws(expr, i + 1)
            continue

        out.append(_esc_text(ch, html_mode))
        i += 1

    return "".join(out).strip()


def latex_to_html(raw_math: str) -> str:
    """Convert a LaTeX math span ($...$, $$...$$, \\(...\\), \\[...\\]) into JS-free HTML."""
    inner, is_display = _strip_delimiters(raw_math)
    body = _render_latex_nodes(inner, html_mode=True, upright=False)
    if is_display:
        return f'<div style="text-align:center;margin:8px 0;font-family:{_FONT_FAMILY};">{body}</div>'
    return f'<span style="font-family:{_FONT_FAMILY};">{body}</span>'


def latex_to_plain(raw_math: str) -> str:
    """Convert a LaTeX math span into readable Unicode plain text."""
    inner, _ = _strip_delimiters(raw_math)
    plain = _render_latex_nodes(inner, html_mode=False, upright=False)
    return re.sub(r"\s{2,}", " ", plain).strip()


def render_inline_math_html(text: str) -> str:
    """Render mixed prose and inline LaTeX math into HTML-safe markup without raw $."""
    if not text:
        return ""
    parts: list[str] = []
    last = 0
    for match in MATH_PATTERNS.finditer(text):
        if match.start() > last:
            parts.append(html.escape(text[last:match.start()].replace(r"\$", "$")))
        parts.append(latex_to_html(match.group(0)))
        last = match.end()
    if last < len(text):
        parts.append(html.escape(text[last:].replace(r"\$", "$")))
    return "".join(parts)


def render_inline_math_plain(text: str) -> str:
    """Render mixed prose and inline LaTeX math into readable Unicode plain text."""
    if not text:
        return ""
    rendered = MATH_PATTERNS.sub(lambda m: latex_to_plain(m.group(0)), text)
    return rendered.replace(r"\$", "$")


def render_markdown_for_email(md_text: str) -> str:
    """Convert Markdown with LaTeX math into self-contained, JS-free email HTML."""
    placeholders: dict[str, tuple[str, bool]] = {}

    def _stash(match: re.Match[str]) -> str:
        token = f"@@MATHTOKEN{len(placeholders)}@@"
        raw = match.group(0)
        _, is_display = _strip_delimiters(raw)
        placeholders[token] = (latex_to_html(raw), is_display)
        return token

    protected = MATH_PATTERNS.sub(_stash, md_text or "")
    rendered = markdown.markdown(
        protected,
        extensions=["extra", "tables", "fenced_code", "sane_lists"],
    )
    for token, (html_math, is_display) in placeholders.items():
        if is_display:
            rendered = rendered.replace(f"<p>{token}</p>", html_math)
        rendered = rendered.replace(token, html_math)
    return rendered


_SEC0_HEADING_RE = re.compile(r"^\s*(?:\*\*|#{1,4}\s*)?0\.\s*论文标题\**\s*$")
_SEC0_META_RE = re.compile(
    r"^\s*[-*]\s*(?:\*\*)?(英文标题|中文标题|研究机构|一句话总结)(?:\*\*)?\s*[:：]\s*(.*)$"
)
_NEXT_SECTION_RE = re.compile(r"^\s*(?:\*\*|#{1,4}\s*)?[1-9]\.\s+")
_EMAIL_SECTION_HEADING_RE = re.compile(r"<p><strong>(\d+\.[^<]+)</strong></p>")


def prepare_body_for_display(md_text: str, fallback_one_liner: str = "") -> tuple[str, str]:
    """Extract the one-liner summary and strip duplicated title/affiliation header lines."""
    tldr = ""
    kept_lines: list[str] = []
    in_body = False
    for line in (md_text or "").splitlines():
        if not in_body:
            if _NEXT_SECTION_RE.match(line):
                in_body = True
                kept_lines.append(line)
                continue
            if _SEC0_HEADING_RE.match(line):
                continue
            meta_m = _SEC0_META_RE.match(line)
            if meta_m:
                key, val = meta_m.group(1), meta_m.group(2).strip()
                if key == "一句话总结" and val and not tldr:
                    tldr = val
                continue
            if not line.strip() and not kept_lines:
                continue
            if line.strip():
                in_body = True
        kept_lines.append(line)

    if not tldr and fallback_one_liner:
        tldr = fallback_one_liner.strip()
    return tldr, "\n".join(kept_lines).strip()


def style_email_markdown_html(html_body: str) -> str:
    """Apply minimalist inline styles suitable for email clients that strip CSS blocks."""
    styled = _EMAIL_SECTION_HEADING_RE.sub(
        '<div style="margin:16px 0 6px;padding-bottom:4px;border-bottom:1px solid #f1f5f9;'
        'font-size:13.5px;font-weight:700;color:#0f172a;letter-spacing:0.01em;">\\1</div>',
        html_body or "",
    )
    styled = styled.replace(
        "<p>",
        '<p style="margin:8px 0;font-size:13.8px;line-height:1.72;color:#334155;">',
    )
    styled = styled.replace(
        "<ul>",
        '<ul style="margin:6px 0 12px;padding-left:18px;color:#334155;">',
    )
    styled = styled.replace(
        "<li>",
        '<li style="margin:5px 0;font-size:13.8px;line-height:1.72;color:#334155;">',
    )
    styled = styled.replace(
        "<code>",
        '<code style="background:#f1f5f9;padding:1px 5px;border-radius:4px;font-size:12.5px;color:#0f172a;">',
    )
    return styled
