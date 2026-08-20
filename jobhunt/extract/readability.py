"""Rung 2: main-content extraction with trafilatura.

Strips nav, cookie banners, footers, and related-jobs sidebars, and returns the
content block. The fallback when JSON-LD is absent or carries a stub.
"""
from __future__ import annotations

import trafilatura

from jobhunt.pipeline import normalize as norm


def extract(html: str | None, url: str | None = None) -> tuple[str | None, str | None]:
    """Returns (markdown, plain text). Both None when nothing usable was found."""
    if not html:
        return None, None
    try:
        markdown = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            favor_recall=True,
        )
    except Exception:  # noqa: BLE001 - a parser blowing up is a failed rung, not a crash
        return None, None
    if not markdown:
        return None, None
    # House style, applied here as everywhere else that generates text.
    markdown = markdown.replace("—", ", ").replace("–", "-")
    return markdown, norm.html_to_text(f"<div>{markdown}</div>")
