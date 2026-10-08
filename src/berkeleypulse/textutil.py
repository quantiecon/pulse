from __future__ import annotations

import io
import re
from html.parser import HTMLParser
from typing import List

STOPWORDS = {
    "a", "an", "the", "of", "to", "for", "on", "in", "is", "are", "was", "were",
    "what", "whats", "can", "i", "do", "does", "my", "we", "our", "you", "your",
    "and", "or", "if", "it", "this", "that", "how", "much", "many", "there",
    "with", "from", "about", "be", "at", "by", "as", "will", "please", "me",
}


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []
        self.skip = False

    def handle_starttag(self, tag, attrs):  # type: ignore[override]
        if tag in {"script", "style"}:
            self.skip = True
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):  # type: ignore[override]
        if tag in {"script", "style"}:
            self.skip = False
        if tag in {"p", "div", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):  # type: ignore[override]
        if not self.skip:
            self.parts.append(data)


def html_to_text(value: str) -> str:
    if not value:
        return ""
    if "<" not in value:
        return value.strip()
    parser = _HTMLText()
    parser.feed(value)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def sentences(text: str) -> List[str]:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return []
    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    return [part.strip() for part in parts if part.strip()]


def tokenize(text: str) -> List[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [word for word in words if word not in STOPWORDS and len(word) > 1]


def compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def pdf_to_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise ValueError("That PDF is encrypted. Export an unlocked copy and upload it again.")
    pages = [(page.extract_text() or "") for page in reader.pages]
    text = "\n".join(pages).strip()
    if not text:
        raise ValueError("No text could be read from that PDF.")
    return text
