"""Shared PDF → text extraction (pdfplumber). Used by the agendacenter
adapter (agenda PDFs) and the municode adapter (minutes PDFs)."""

from __future__ import annotations

import io

import requests


def fetch_pdf_text(url: str, timeout: int = 30) -> str | None:
    """Download a PDF and extract its text. Returns None if the URL isn't a
    PDF or yields under 100 chars of text."""
    try:
        import pdfplumber
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"},
                         timeout=timeout)
        if r.status_code != 200 or b"%PDF" not in r.content[:10]:
            return None
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        return text.strip() if len(text) > 100 else None
    except Exception as e:
        print(f"       PDF text extraction failed: {e}")
        return None
