from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx

from berkeleypulse.config import Settings
from berkeleypulse.db import meta_bump
from berkeleypulse.syllabus import coerce_model, heuristic_parse
from berkeleypulse.textutil import sentences, tokenize


@dataclass
class Citation:
    course_code: str
    source: str
    locator: str
    quote: str
    stability: str = "static"


@dataclass
class Answer:
    text: str
    citations: List[Citation] = field(default_factory=list)
    model: Optional[str] = None


def index_documents(conn: sqlite3.Connection, course_id: int, code: str, syllabus: str, assignments: List[sqlite3.Row]) -> None:
    conn.execute("DELETE FROM documents WHERE course_id = ?", (course_id,))
    syllabus_source = "%s syllabus" % code
    for index, sentence in enumerate(sentences(syllabus), start=1):
        _insert(conn, course_id, syllabus_source, "sentence %d" % index, sentence, "static")
    for assignment in assignments:
        source = "%s · %s" % (code, assignment["name"])
        for index, sentence in enumerate(sentences(assignment["description"] or ""), start=1):
            _insert(conn, course_id, source, "description sentence %d" % index, sentence, "live")


def _insert(conn: sqlite3.Connection, course_id: int, source: str, locator: str, text: str, stability: str) -> None:
    cleaned = text.strip()
    if len(cleaned) < 8:
        return
    conn.execute(
        "INSERT INTO documents(course_id, source, locator, text, stability) VALUES(?, ?, ?, ?, ?)",
        (course_id, source, locator, cleaned[:2000], stability),
    )


def answer_question(conn: sqlite3.Connection, question: str, settings: Settings, course_id: Optional[int] = None) -> Answer:
    question = question.strip()
    if not question:
        return Answer(text="Ask about a deadline, a grading rule, or a late policy.")
    rows = _documents(conn, course_id)
    if not rows:
        return Answer(text="No course documents are ingested yet. Connect Canvas or add a syllabus first.")
    hits = _rank(question, rows)
    if not hits:
        return Answer(text="Nothing in the ingested syllabi or assignments answers that.")
    citations = [
        Citation(
            course_code=hit["code"],
            source=hit["source"],
            locator=hit["locator"],
            quote=hit["quote"],
            stability=hit["stability"],
        )
        for hit in hits
    ]
    extractive = citations[0].quote
    if not settings.llm_ready:
        return Answer(text=extractive, citations=citations)
    drafted = _draft(question, citations, settings)
    if not drafted:
        return Answer(text=extractive, citations=citations)
    meta_bump(conn, "llm_calls")
    return Answer(text=drafted, citations=citations, model=settings.llm_model)


def _documents(conn: sqlite3.Connection, course_id: Optional[int]) -> List[sqlite3.Row]:
    if course_id is None:
        return list(
            conn.execute(
                """
                SELECT documents.*, courses.code AS code
                FROM documents
                JOIN courses ON courses.id = documents.course_id
                WHERE courses.hidden = 0
                """
            )
        )
    return list(
        conn.execute(
            """
            SELECT documents.*, courses.code AS code
            FROM documents
            JOIN courses ON courses.id = documents.course_id
            WHERE documents.course_id = ? AND courses.hidden = 0
            """,
            (course_id,),
        )
    )


def _rank(question: str, rows: List[sqlite3.Row]) -> List[Dict]:
    query_tokens = tokenize(question)
    if not query_tokens:
        return []
    token_sets = [set(tokenize(row["text"])) for row in rows]
    document_frequency: Dict[str, int] = {}
    for tokens in token_sets:
        for token in tokens:
            document_frequency[token] = document_frequency.get(token, 0) + 1
    total = max(1, len(rows))
    idf = {
        token: math.log((total + 1) / (count + 1)) + 1
        for token, count in document_frequency.items()
    }
    ranked = []
    for row, tokens in zip(rows, token_sets):
        title_tokens = set(tokenize(row["source"]))
        score = 0.0
        for token in query_tokens:
            weight = idf.get(token, 0.0)
            if token in tokens:
                score += weight
            if token in title_tokens:
                score += max(weight, 1.5) * 1.5
        if score <= 0:
            continue
        ranked.append(
            {
                "score": score,
                "code": row["code"],
                "source": row["source"],
                "locator": row["locator"],
                "quote": _best_sentence(row["text"], query_tokens, idf),
                "stability": _stability(row),
            }
        )
    ranked.sort(key=lambda item: item["score"], reverse=True)
    if not ranked:
        return []
    needed = _query_hits(ranked[0]["quote"], query_tokens)
    kept = [ranked[0]]
    for item in ranked[1:]:
        if item["score"] < kept[0]["score"] * 0.55:
            break
        if _query_hits(item["quote"], query_tokens) < needed:
            continue
        kept.append(item)
        if len(kept) == 3:
            break
    return kept


def _stability(row) -> str:
    locator = row["locator"] or ""
    if locator.startswith("description"):
        return "live"
    names = row.keys()
    if "stability" in names and row["stability"]:
        return str(row["stability"])
    return "static"


def _query_hits(quote: str, query_tokens: List[str]) -> int:
    tokens = set(tokenize(quote))
    return sum(1 for token in query_tokens if token in tokens)


def _best_sentence(text: str, query_tokens: List[str], idf: Dict[str, float]) -> str:
    parts = sentences(text) or [text]
    best = parts[0]
    best_score = -1.0
    for part in parts:
        tokens = set(tokenize(part))
        score = sum(idf.get(token, 1.0) for token in query_tokens if token in tokens)
        if score > best_score:
            best = part
            best_score = score
    return best[:500]


def _draft(question: str, citations: List[Citation], settings: Settings) -> Optional[str]:
    sources = "\n".join(
        "[%d] %s, %s: %s" % (index, item.source, item.locator, item.quote)
        for index, item in enumerate(citations, start=1)
    )
    payload = {
        "model": settings.llm_model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Answer the question using only the numbered sources. "
                    "Return JSON with an 'answer' string under 80 words. "
                    "If the sources do not say, set answer to exactly: The course materials do not say."
                ),
            },
            {"role": "user", "content": "Question: %s\n\nSources:\n%s" % (question, sources)},
        ],
    }
    try:
        response = httpx.post(
            settings.llm_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer %s" % settings.llm_api_key.strip()},
            json=payload,
            timeout=30.0,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        answer = str(parsed.get("answer", "")).strip()
    except Exception:
        return None
    if not answer:
        return None
    return answer[:1200]


def parse_with_optional_model(text: str, settings: Settings, now=None):
    """Parse a syllabus. Returns (model, used_llm)."""
    baseline = heuristic_parse(text, now=now)
    if not settings.llm_ready or len(text.strip()) < 40:
        return baseline, False
    try:
        response = httpx.post(
            settings.llm_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer %s" % settings.llm_api_key.strip()},
            json={
                "model": settings.llm_model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Extract a course policy model from the syllabus. Return JSON with keys "
                            "grading (list of {name, weight}), late_policy (string), drop_rules (list of strings), "
                            "exams (list of {name, starts_at ISO8601}), deadlines (list of {name, due_at ISO8601}). "
                            "Use America/Los_Angeles when a timezone is missing. Omit anything the text does not say."
                        ),
                    },
                    {"role": "user", "content": text[:12000]},
                ],
            },
            timeout=45.0,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        coerced = coerce_model(json.loads(content))
    except Exception:
        return baseline, False
    if coerced is None:
        return baseline, False
    return coerced, True
