#!/usr/bin/env python3
"""Convert a self-contained HTML report to PDF via headless Chromium (Playwright).

The strategy docs are single-column HTML with inline <svg> charts, so this prints
faithfully with backgrounds on and no JS/canvas concerns. Used by
`render_strategy_html.py --pdf` and runnable standalone:

    python scripts/html_to_pdf.py <input.html> [output.pdf]
"""
from __future__ import annotations

import sys
from pathlib import Path


def html_to_pdf(html_path: str | Path, pdf_path: str | Path | None = None) -> Path:
    html_path = Path(html_path).resolve()
    if not html_path.exists():
        raise FileNotFoundError(f"html not found: {html_path}")
    pdf_path = Path(pdf_path) if pdf_path else html_path.with_suffix(".pdf")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.as_uri(), wait_until="load")
        # Keep the on-screen look (themed backgrounds/colors), then let webfonts +
        # inline SVG settle before printing.
        page.emulate_media(media="screen")
        page.wait_for_timeout(1200)
        page.pdf(
            path=str(pdf_path),
            format="Letter",
            print_background=True,
            margin={"top": "0.45in", "bottom": "0.45in", "left": "0.4in", "right": "0.4in"},
        )
        browser.close()
    return pdf_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: html_to_pdf.py <input.html> [output.pdf]", file=sys.stderr)
        raise SystemExit(2)
    out = html_to_pdf(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    print(f"wrote {out}")
