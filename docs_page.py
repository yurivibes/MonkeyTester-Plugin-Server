"""Render a plugin's README.md as a self-contained HTML documentation page.

Stdlib only, no external assets. Shared verbatim by every MonkeyTester plugin
(copy this file; keep the copies identical). Raw HTML in the markdown is
escaped, never passed through; link targets are limited to http(s), mailto and
in-page anchors, so the page is safe to serve under a strict CSP:
    default-src 'none'; style-src 'unsafe-inline'

    from docs_page import docs_html
    body = docs_html("esp32-sensor", readme_text)   # -> bytes (UTF-8 HTML)

Supported markdown: ATX headings, paragraphs, fenced code blocks, indented
code blocks, unordered/ordered lists (nesting by indentation), blockquotes,
GFM pipe tables, horizontal rules, inline code, **bold**, *italic*, links,
<autolinks>. Images render as their alt text. Relative links (other repo
files) render as plain text because they do not resolve on the plugin.
"""
from __future__ import annotations

import html
import re

__all__ = ["render_markdown", "docs_html"]

_FENCE = re.compile(r"^(\s{0,3})(`{3,}|~{3,})\s*([\w+-]*)")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_HR = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_LIST = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")
_SAFE_URL = re.compile(r"^(https?://|mailto:|#)", re.I)


def _slug(text: str, used: dict) -> str:
    base = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    base = re.sub(r"[\s]+", "-", base) or "section"
    n = used.get(base, 0)
    used[base] = n + 1
    return base if n == 0 else f"{base}-{n}"


def _inline(text: str) -> str:
    """Escape text and apply inline markdown. Code spans are protected first."""
    codes: list[str] = []

    def hold_code(m: "re.Match") -> str:
        codes.append("<code>" + html.escape(m.group(2).strip(" ")) + "</code>")
        return f"\x01{len(codes) - 1}\x01"

    text = re.sub(r"(`+)(.+?)\1", hold_code, text)
    out = _inline_plain(text)
    return re.sub(r"\x01(\d+)\x01", lambda m: codes[int(m.group(1))], out)


def _link(label_html: str, url: str) -> str:
    url = url.strip()
    if url.startswith("<") and url.endswith(">"):
        url = url[1:-1]
    url = url.split()[0] if url.split() else ""
    if _SAFE_URL.match(url):
        return ('<a href="' + html.escape(url, quote=True) + '"'
                + ("" if url.startswith("#") else ' target="_blank" rel="noopener noreferrer"')
                + ">" + label_html + "</a>")
    return label_html


def _inline_plain(text: str) -> str:
    # Work on escaped text; markdown punctuation survives html.escape except
    # '<' '>' '&' '"' "'", which only matter for autolinks (handled below).
    holders: list[str] = []

    def hold(s: str) -> str:
        holders.append(s)
        return f"\x00{len(holders) - 1}\x00"

    # Autolinks <https://...>
    text = re.sub(r"<((?:https?://|mailto:)[^\s<>]+)>",
                  lambda m: hold(_link(html.escape(m.group(1)), m.group(1))), text)
    # Images ![alt](url) -> alt text
    text = re.sub(r"!\[([^\]]*)\]\(([^)]*)\)", lambda m: hold(html.escape(m.group(1))), text)
    # Links [label](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]*)\)",
                  lambda m: hold(_link(_emph(html.escape(m.group(1))), m.group(2))), text)
    text = _emph(html.escape(text, quote=False))
    return re.sub(r"\x00(\d+)\x00", lambda m: holders[int(m.group(1))], text)


def _emph(s: str) -> str:
    s = re.sub(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", r"<strong>\2</strong>", s)
    s = re.sub(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])", r"<em>\1</em>", s)
    s = re.sub(r"(?<![\w_])_(?=\S)(.+?)(?<=\S)_(?![\w_])", r"<em>\1</em>", s)
    return s


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row)]


def _render_list(items: list[tuple[int, bool, str]]) -> str:
    """items: (indent, ordered, text). Builds nested lists by indentation."""
    out: list[str] = []
    stack: list[tuple[int, str]] = []  # (indent, tag)
    for indent, ordered, text in items:
        tag = "ol" if ordered else "ul"
        while stack and (indent < stack[-1][0] or (indent == stack[-1][0] and tag != stack[-1][1])):
            out.append(f"</li></{stack.pop()[1]}>")
        if not stack or indent > stack[-1][0]:
            out.append(f"<{tag}><li>")
            stack.append((indent, tag))
        else:
            out.append("</li><li>")
        out.append(_inline(text))
    while stack:
        out.append(f"</li></{stack.pop()[1]}>")
    return "".join(out)


def render_markdown(md: str) -> str:
    """Return the HTML body fragment for markdown text."""
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    used_ids: dict = {}
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = _FENCE.match(line)
        if m:
            fence = m.group(2)
            lang = m.group(3)
            i += 1
            code: list[str] = []
            while i < n and not lines[i].strip().startswith(fence):
                code.append(lines[i])
                i += 1
            i += 1  # closing fence (or EOF)
            cls = f' class="language-{html.escape(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>" + html.escape("\n".join(code)) + "</code></pre>")
            continue
        m = _HEADING.match(line)
        if m:
            level = len(m.group(1))
            text = m.group(2)
            hid = _slug(re.sub(r"[`*_\[\]()]", "", text), used_ids)
            out.append(f'<h{level} id="{hid}">' + _inline(text) + f"</h{level}>")
            i += 1
            continue
        if _HR.match(line):
            out.append("<hr>")
            i += 1
            continue
        if line.startswith("    ") or line.startswith("\t"):
            code = []
            while i < n and (lines[i].startswith("    ") or lines[i].startswith("\t") or not lines[i].strip()):
                code.append(lines[i][4:] if lines[i].startswith("    ") else lines[i][1:])
                i += 1
            while code and not code[-1].strip():
                code.pop()
            out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
            continue
        if line.lstrip().startswith(">"):
            quote: list[str] = []
            while i < n and lines[i].lstrip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>" + render_markdown("\n".join(quote)) + "</blockquote>")
            continue
        if "|" in line and i + 1 < n and _TABLE_SEP.match(lines[i + 1]) and "-" in lines[i + 1]:
            head = _cells(line)
            i += 2
            rows: list[list[str]] = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(_cells(lines[i]))
                i += 1
            t = ["<table><thead><tr>"]
            t += ["<th>" + _inline(c) + "</th>" for c in head]
            t.append("</tr></thead><tbody>")
            for r in rows:
                r = (r + [""] * len(head))[:len(head)]
                t.append("<tr>" + "".join("<td>" + _inline(c) + "</td>" for c in r) + "</tr>")
            t.append("</tbody></table>")
            out.append("".join(t))
            continue
        if _LIST.match(line):
            items: list[tuple[int, bool, str]] = []
            while i < n:
                lm = _LIST.match(lines[i])
                if lm:
                    items.append((len(lm.group(1).expandtabs(4)), lm.group(2)[0].isdigit(), lm.group(3)))
                    i += 1
                elif lines[i].strip() and lines[i].startswith((" ", "\t")) and items:
                    ind, o, t = items[-1]
                    items[-1] = (ind, o, t + " " + lines[i].strip())  # continuation line
                    i += 1
                elif not lines[i].strip() and i + 1 < n and _LIST.match(lines[i + 1]):
                    i += 1  # loose list: blank line between items
                else:
                    break
            out.append(_render_list(items))
            continue
        para: list[str] = []
        while i < n and lines[i].strip() and not (
                _FENCE.match(lines[i]) or _HEADING.match(lines[i]) or _HR.match(lines[i])
                or lines[i].lstrip().startswith(">") or _LIST.match(lines[i])):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>" + _inline(" ".join(para)) + "</p>")
    return "\n".join(out)


_CSS = """
:root { color-scheme: light dark; --fg:#1d1d1f; --bg:#ffffff; --muted:#6e6e73;
  --code-bg:#f2f2f4; --border:#d9d9de; --link:#0a66c2; }
@media (prefers-color-scheme: dark) { :root { --fg:#e8e8ea; --bg:#151517;
  --muted:#9a9aa0; --code-bg:#232327; --border:#38383d; --link:#6cb4ff; } }
html { background: var(--bg); color: var(--fg); }
body { font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  max-width: 880px; margin: 0 auto; padding: 24px 20px 64px; }
header.docs-head { color: var(--muted); font-size: 13px; border-bottom: 1px solid var(--border);
  padding-bottom: 8px; margin-bottom: 16px; }
h1, h2, h3, h4 { line-height: 1.25; margin: 1.6em 0 0.5em; }
h1 { font-size: 1.8em; } h2 { font-size: 1.4em; border-bottom: 1px solid var(--border); padding-bottom: .2em; }
a { color: var(--link); }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .88em;
  background: var(--code-bg); padding: .1em .35em; border-radius: 4px; }
pre { background: var(--code-bg); padding: 12px 14px; border-radius: 6px; overflow-x: auto; }
pre code { background: none; padding: 0; font-size: .85em; }
table { border-collapse: collapse; margin: 1em 0; display: block; overflow-x: auto; }
th, td { border: 1px solid var(--border); padding: 6px 10px; text-align: left; vertical-align: top; }
th { background: var(--code-bg); }
blockquote { margin: 1em 0; padding: 0 1em; color: var(--muted); border-left: 3px solid var(--border); }
hr { border: 0; border-top: 1px solid var(--border); margin: 2em 0; }
"""


def docs_html(title: str, md: str, version: str | None = None) -> bytes:
    """Full UTF-8 HTML page: '<title> documentation' + the rendered markdown."""
    label = html.escape(title) + (" " + html.escape(version) if version else "")
    page = ("<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{html.escape(title)} documentation</title><style>{_CSS}</style></head>"
            f"<body><header class=\"docs-head\">{label} &middot; plugin documentation</header>"
            f"<main>{render_markdown(md)}</main></body></html>\n")
    return page.encode("utf-8")
