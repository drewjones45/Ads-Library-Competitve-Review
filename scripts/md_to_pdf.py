#!/usr/bin/env python3
"""md_to_pdf.py — render a Markdown document to a print-quality PDF.

Built for the long technical specs in this repo (PERFORMANCE_DASHBOARD_SPEC.md and
friends): wide comparison tables, multi-hundred-character SQL/field-list code
blocks, and deep heading nesting. Those three are exactly what a default
Markdown→PDF pipeline gets wrong — tables overflow the page box, code blocks clip
their right edge instead of wrapping, and headings orphan at the foot of a page.

Pipeline: python-markdown → styled single-file HTML → headless Chromium print
(reusing scripts/html_to_pdf.py's Playwright path, but with its own page setup so
the header/footer and page numbering can differ).

    python scripts/md_to_pdf.py SPEC.md [out.pdf] [--title "..."] [--keep-html]
"""
from __future__ import annotations

import argparse
import html as html_mod
import re
from pathlib import Path

import markdown


# Print-first stylesheet. Light only — a dark PDF wastes toner and reads badly on
# paper, so this deliberately ignores the dark theme the HTML dashboards use.
CSS = """
@page { size: Letter; margin: 0.62in 0.6in 0.7in 0.6in; }

:root {
  --ink:        #14171c;
  --ink-soft:   #4a525e;
  --ink-faint:  #77808e;
  --rule:       #d8dde5;
  --rule-soft:  #e8ecf2;
  --accent:     #1f4fd8;
  --code-bg:    #f5f7fa;
  --code-ink:   #1d2530;
  --quote-bg:   #fff8e6;
  --quote-edge: #e0a93b;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  font: 10pt/1.55 "Charter", "Iowan Old Style", "Palatino Linotype", Georgia, serif;
  color: var(--ink);
  -webkit-font-smoothing: antialiased;
}

/* ---------------------------------------------------------------- cover --- */
.cover { padding: 2.6in 0 0; text-align: left; page-break-after: always; }
.cover .eyebrow {
  font: 600 8.5pt/1 ui-sans-serif, -apple-system, "Helvetica Neue", sans-serif;
  letter-spacing: .16em; text-transform: uppercase; color: var(--accent);
}
.cover h1 {
  font: 700 30pt/1.15 ui-sans-serif, -apple-system, "Helvetica Neue", sans-serif;
  margin: .28in 0 .16in; letter-spacing: -.015em; border: 0; padding: 0;
}
.cover .lede { font-size: 11.5pt; color: var(--ink-soft); max-width: 5.2in; }
.cover .meta {
  margin-top: .42in; padding-top: .16in; border-top: 1px solid var(--rule);
  font: 9pt/1.7 ui-monospace, "SF Mono", Menlo, monospace; color: var(--ink-faint);
}

/* ------------------------------------------------------------------ toc --- */
.toc { page-break-after: always; }
.toc h2 { border: 0; margin: 0 0 .18in; font-size: 15pt; }
.toc ul { list-style: none; margin: 0; padding: 0; }
.toc > ul > li { margin: 0 0 3pt; }
.toc a { color: var(--ink); text-decoration: none; }
.toc > ul > li > a {
  font: 600 10.5pt/1.5 ui-sans-serif, -apple-system, sans-serif;
}
/* Second level: indented, lighter, and only one level deep — a spec with 60
   sub-headings produces an unreadable four-page contents otherwise. */
.toc ul ul { margin: 1pt 0 6pt .28in; }
.toc ul ul li { font-size: 9.5pt; color: var(--ink-soft); }
.toc ul ul ul { display: none; }

/* -------------------------------------------------------------- headings --- */
h1, h2, h3, h4 {
  font-family: ui-sans-serif, -apple-system, "Helvetica Neue", sans-serif;
  letter-spacing: -.01em; color: var(--ink);
  /* Never let a heading sit alone at the foot of a page. */
  page-break-after: avoid; break-after: avoid-page;
}
/* Each top-level section starts a fresh page: these are reference sections
   people jump to, not prose read end to end. */
h2 {
  font-size: 16pt; font-weight: 700; margin: 0 0 .18in;
  padding-bottom: 6pt; border-bottom: 2px solid var(--ink);
  page-break-before: always; break-before: page;
}
h2:first-of-type { page-break-before: avoid; }
h3 { font-size: 12pt; font-weight: 650; margin: 20pt 0 7pt; }
h4 { font-size: 10.5pt; font-weight: 650; margin: 15pt 0 5pt; color: var(--ink-soft); }

p { margin: 0 0 8pt; orphans: 2; widows: 2; }
strong { font-weight: 650; }

a { color: var(--accent); text-decoration: none; }

/* Section separators are stripped before render (see _strip_rules): every h2
   already starts its own page, so an <hr> only ever lands stranded at the top or
   foot of one. */
hr { display: none; }

/* ---------------------------------------------------------------- lists --- */
ul, ol { margin: 0 0 9pt; padding-left: 17pt; }
li { margin: 0 0 3.5pt; }
li > ul, li > ol { margin-top: 3.5pt; }

/* Checklist items render as `[ ] text` from python-markdown; give them a real box. */
li.task { list-style: none; margin-left: -14pt; padding-left: 16pt; position: relative; }
li.task::before {
  content: ""; position: absolute; left: 0; top: 3.2pt;
  width: 8pt; height: 8pt; border: 1px solid var(--ink-faint); border-radius: 1.5pt;
}

/* ----------------------------------------------------------------- code --- */
code {
  font: 8.6pt/1.4 ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  background: var(--code-bg); color: var(--code-ink);
  padding: .5pt 3pt; border-radius: 2.5pt;
  /* Long identifiers (omni_initiated_checkout, effective_object_story_id) must
     break rather than push the table or paragraph past the page box. */
  word-break: break-word; overflow-wrap: anywhere;
}
pre {
  background: var(--code-bg); border: 1px solid var(--rule-soft);
  border-left: 2.5pt solid var(--accent);
  border-radius: 3pt; padding: 8pt 10pt; margin: 0 0 10pt;
  /* Chromium will happily clip a 200-char SQL line off the right edge of the
     page. Wrapping is the only thing that keeps the DDL blocks complete. */
  white-space: pre-wrap; word-break: break-word; overflow-wrap: anywhere;
  page-break-inside: auto;
}
pre code {
  background: none; padding: 0; font-size: 8.2pt; line-height: 1.45;
}

/* --------------------------------------------------------------- tables --- */
table {
  width: 100%; border-collapse: collapse; margin: 0 0 12pt;
  font-size: 8.8pt; line-height: 1.4;
  page-break-inside: auto;
}
thead { display: table-header-group; }   /* repeat headers across page breaks */
tr { page-break-inside: avoid; break-inside: avoid; }
th {
  font-family: ui-sans-serif, -apple-system, sans-serif; font-weight: 650;
  text-align: left; background: #eef1f6;
  border-bottom: 1.2pt solid var(--ink-faint);
  padding: 5pt 7pt; vertical-align: bottom;
}
td {
  padding: 5pt 7pt; border-bottom: .6pt solid var(--rule-soft);
  vertical-align: top; word-break: normal; overflow-wrap: anywhere;
}
tbody tr:nth-child(even) { background: #fafbfd; }
td code, th code { font-size: 8pt; background: #e9edf3; }

/* --------------------------------------------------------- blockquotes --- */
blockquote {
  margin: 0 0 11pt; padding: 8pt 11pt;
  background: var(--quote-bg); border-left: 2.5pt solid var(--quote-edge);
  border-radius: 3pt; color: #4a3a12;
}
blockquote p:last-child { margin-bottom: 0; }
"""

HEADER = """
<div style="width:100%;font:7pt ui-sans-serif,-apple-system,sans-serif;
            color:#98a1af;padding:0 .6in;">
  <span>{title}</span>
</div>
"""

FOOTER = """
<div style="width:100%;font:7pt ui-sans-serif,-apple-system,sans-serif;
            color:#98a1af;padding:0 .6in;display:flex;
            justify-content:space-between;">
  <span>{subtitle}</span>
  <span class="pageNumber"></span>
</div>
"""


def _split_front_matter(md: str) -> tuple[str, str, str]:
    """Pull the H1 and the first paragraph off the top for the cover page.

    Returns (title, lede, remaining_markdown). The lede is whatever prose sits
    between the H1 and the next block-level element, joined into one line.
    """
    lines = md.splitlines()
    title, lede, start = "", [], 0
    for i, ln in enumerate(lines):
        if ln.startswith("# "):
            title = ln[2:].strip()
            start = i + 1
            break
    j = start
    while j < len(lines) and not lines[j].strip():
        j += 1
    while j < len(lines) and lines[j].strip() and not lines[j].startswith(("#", "|", "```", ">", "-", "*")):
        lede.append(lines[j].strip())
        j += 1
    return title, " ".join(lede), "\n".join(lines[j:])


def _tasklists(body: str) -> str:
    """Turn python-markdown's literal `[ ]` / `[x]` list text into styled items."""
    body = re.sub(r"<li>\[ \]\s*", '<li class="task">', body)
    body = re.sub(r"<li>\[[xX]\]\s*", '<li class="task done">', body)
    return body


def _strip_rules(md: str) -> str:
    """Drop `---` section separators.

    They read as separators on screen, but in print every `##` already starts a
    new page, so the rule is guaranteed to land alone at the top or bottom of one.
    Removed from the source rather than hidden in CSS so the surrounding blank
    lines collapse too.
    """
    return re.sub(r"^\s*---\s*$\n?", "", md, flags=re.MULTILINE)


def render_html(md_text: str, *, title_override: str | None = None,
                subtitle: str = "") -> tuple[str, str]:
    title, lede, body_md = _split_front_matter(md_text)
    title = title_override or title or "Document"

    conv = markdown.Markdown(extensions=[
        "tables", "fenced_code", "attr_list", "sane_lists", "toc", "md_in_html",
    ], extension_configs={"toc": {"toc_depth": "2-3", "permalink": False}})
    body = _tasklists(conv.convert(_strip_rules(body_md)))
    toc = conv.toc

    cover = (
        '<div class="cover">'
        '<div class="eyebrow">Technical specification</div>'
        f"<h1>{html_mod.escape(title)}</h1>"
        + (f'<div class="lede">{html_mod.escape(lede)}</div>' if lede else "")
        + (f'<div class="meta">{html_mod.escape(subtitle)}</div>' if subtitle else "")
        + "</div>"
    )
    toc_block = f'<div class="toc"><h2>Contents</h2>{toc}</div>' if toc.strip() else ""

    return title, (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{html_mod.escape(title)}</title>"
        f"<style>{CSS}</style></head><body>"
        f"{cover}{toc_block}{body}</body></html>"
    )


def to_pdf(html_path: Path, pdf_path: Path, *, title: str, subtitle: str) -> Path:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.as_uri(), wait_until="load")
        page.emulate_media(media="print")
        page.wait_for_timeout(600)
        page.pdf(
            path=str(pdf_path),
            format="Letter",
            print_background=True,
            display_header_footer=True,
            header_template=HEADER.format(title=html_mod.escape(title)),
            footer_template=FOOTER.format(subtitle=html_mod.escape(subtitle)),
            margin={"top": "0.72in", "bottom": "0.62in", "left": "0.6in", "right": "0.6in"},
        )
        browser.close()
    return pdf_path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="markdown file")
    ap.add_argument("output", nargs="?", default=None, help="output pdf (default: alongside input)")
    ap.add_argument("--title", default=None, help="override the cover title")
    ap.add_argument("--subtitle", default="", help="cover meta line + footer left text")
    ap.add_argument("--keep-html", action="store_true", help="keep the intermediate HTML")
    args = ap.parse_args()

    src = Path(args.input).resolve()
    out = Path(args.output).resolve() if args.output else src.with_suffix(".pdf")
    title, html_doc = render_html(
        src.read_text(encoding="utf-8"),
        title_override=args.title, subtitle=args.subtitle,
    )
    tmp_html = out.with_suffix(".print.html")
    tmp_html.write_text(html_doc, encoding="utf-8")
    try:
        to_pdf(tmp_html, out, title=title, subtitle=args.subtitle or title)
    finally:
        if not args.keep_html:
            tmp_html.unlink(missing_ok=True)
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
