"""Static course files. A folder is opened only when its own date changed."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Callable, Dict, List

from berkeleypulse.syllabus import PARSER_VERSION, empty_model, heuristic_parse

READ_WORDS = ("syllabus", "schedule", "calendar", "policy", "outline")
COURSE_NAME_WORDS = READ_WORDS + (
    "study",
    "guide",
    "slide",
    "lecture",
    "section",
    "week",
    "topic",
    "homework",
    "lab",
    "exam",
    "midterm",
    "final",
    "rubric",
    "notes",
    "handout",
    "assignment",
    "blank",
)
CALENDAR_WORDS = (
    "syllabus",
    "schedule",
    "calendar",
    "policy",
    "outline",
    "study guide",
    "study-guide",
)
SKIP_SUFFIXES = (".ppt", ".pptx", ".mp4", ".png", ".jpg", ".jpeg", ".zip", ".mov", ".mp3")
ARTICLE_MARKERS = (
    re.compile(r"\babstract\b", re.IGNORECASE),
    re.compile(r"\bdoi\s*[:/]", re.IGNORECASE),
    re.compile(r"\bjournal of\b", re.IGNORECASE),
    re.compile(r"\bforthcoming in\b", re.IGNORECASE),
    re.compile(r"\bvol\.\s*\d+", re.IGNORECASE),
)
COURSE_TEXT = ("this course", "office hour", "your grade", "syllabus", "attendance", "study guide")
MAX_DEPTH = 4


def worth_reading(name: str, size: int = 0) -> bool:
    """PDFs are course material unless the filename is an author-year article."""
    if size and int(size) > 8_000_000:
        return False
    lowered = (name or "").lower()
    if lowered.endswith(SKIP_SUFFIXES):
        return False
    if lowered.endswith(".pdf"):
        return not article_filename(lowered)
    return any(word in lowered for word in READ_WORDS)


def article_filename(name: str) -> bool:
    lowered = (name or "").lower()
    if any(word in lowered for word in COURSE_NAME_WORDS):
        return False
    if re.search(r"\b[a-z]{3,},\s+[a-z]", lowered):
        return True
    if re.search(r"\b(?:fall|spring|summer|winter|week|topic|ch|chapter)\s*[_-]?\s*(?:19|20)\d{2}\b", lowered):
        return False
    return bool(re.search(r"\b[a-z][a-z'’-]{2,}[\s,_-]+(?:19|20)\d{2}\b", lowered))


def is_assigned_article(name: str, text: str) -> bool:
    """A downloaded PDF that reads as a paper, not as class information."""
    sample = (text or "")[:4000]
    lowered = sample.lower()
    if any(word in lowered for word in COURSE_TEXT) or any(word in (name or "").lower() for word in COURSE_NAME_WORDS):
        return False
    hits = sum(1 for pattern in ARTICLE_MARKERS if pattern.search(sample))
    return hits >= 2


def for_calendar(name: str) -> bool:
    lowered = (name or "").lower()
    return any(word in lowered for word in CALENDAR_WORDS)


def nodes_to_open(stored: Dict[str, str], nodes: List[dict]) -> List[dict]:
    """Keep nodes whose updated_at is new. An unchanged date is left closed."""
    opened = []
    for node in nodes:
        stamp = node.get("updated_at") or ""
        if stamp and stored.get(node.get("id") or "") == stamp:
            continue
        opened.append(node)
    return opened


def sync_course_files(conn, course_id: int, root_id: str, fetch_children: Callable, read_file: Callable) -> tuple:
    """Return (folders left closed, folders opened, text worth parsing)."""
    stamp_rows = conn.execute(
        "SELECT external_id, name, kind, updated_at FROM file_stamps WHERE course_id = ?",
        (course_id,),
    ).fetchall()
    stored = {row["external_id"]: row["updated_at"] for row in stamp_rows}
    names = {row["external_id"]: row["name"] for row in stamp_rows}
    have_body = {
        row["external_id"]
        for row in conn.execute(
            "SELECT external_id FROM file_bodies WHERE course_id = ?",
            (course_id,),
        )
    }
    pending = {
        row["external_id"]
        for row in stamp_rows
        if row["kind"] == "file" and row["external_id"] not in have_body and worth_reading(row["name"] or "")
    }
    skipped = 0
    opened = 0
    kept = False

    def walk(folder_id: str, depth: int) -> None:
        nonlocal skipped, opened, kept
        children = fetch_children(folder_id) or []
        changed = nodes_to_open(stored, children)
        changed_ids = {node.get("id") for node in changed}
        for node in children:
            if node.get("kind") != "folder":
                continue
            if node.get("id") in changed_ids or pending:
                if depth < MAX_DEPTH:
                    opened += 1
                    walk(node["canvas_id"], depth + 1)
                    _save_stamp(conn, course_id, node)
                    stored[node["id"]] = node.get("updated_at") or ""
            else:
                skipped += 1
        for node in children:
            if node.get("kind") != "file":
                continue
            name = node.get("name") or ""
            wanted = worth_reading(name, int(node.get("size") or 0))
            unseen = node.get("id") not in have_body or node.get("id") in changed_ids
            if wanted and unseen:
                text = (read_file(node) or "").strip()
                if is_assigned_article(name, text):
                    text = ""
                conn.execute(
                    """
                    INSERT INTO file_bodies(course_id, external_id, body)
                    VALUES(?, ?, ?)
                    ON CONFLICT(course_id, external_id) DO UPDATE SET body = excluded.body
                    """,
                    (course_id, node["id"], text[:200000]),
                )
                have_body.add(node["id"])
                pending.discard(node["id"])
                if text:
                    kept = True
            if node.get("id") in changed_ids or node.get("id") not in stored:
                _save_stamp(conn, course_id, node)
                stored[node["id"]] = node.get("updated_at") or ""
                names[node["id"]] = name

    walk(root_id, 0)
    if kept:
        rows = conn.execute(
            """
            SELECT file_stamps.name AS name, file_bodies.body AS body
            FROM file_bodies
            JOIN file_stamps
              ON file_stamps.course_id = file_bodies.course_id
             AND file_stamps.external_id = file_bodies.external_id
            WHERE file_bodies.course_id = ?
            ORDER BY file_bodies.external_id
            """,
            (course_id,),
        ).fetchall()
        body = "\n\n".join(row["body"] for row in rows if row["body"].strip() and for_calendar(row["name"]))[:200000]
        conn.execute("UPDATE courses SET file_text = ? WHERE id = ?", (body, course_id))
    return skipped, opened, kept


def _save_stamp(conn, course_id: int, node: dict) -> None:
    conn.execute(
        """
        INSERT INTO file_stamps(course_id, external_id, name, kind, updated_at)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(course_id, external_id) DO UPDATE SET
          name = excluded.name,
          kind = excluded.kind,
          updated_at = excluded.updated_at
        """,
        (
            course_id,
            node["id"],
            node.get("name") or "",
            node.get("kind") or "file",
            node.get("updated_at") or "",
        ),
    )


def apply_file_models(conn, now) -> None:
    """Fold a changed syllabus file into the calendar model. Unchanged text is skipped."""
    rows = conn.execute(
        "SELECT id, file_text, model_json FROM courses WHERE hidden = 0 AND file_text != ''"
    ).fetchall()
    for row in rows:
        text = row["file_text"] or ""
        digest = hashlib.sha256((PARSER_VERSION + "\0" + text).encode()).hexdigest()
        key = "file_text:%s" % row["id"]
        current = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if current is not None and current["value"] == digest:
            continue
        parsed = heuristic_parse(text, now=now)
        try:
            model = json.loads(row["model_json"] or "{}")
        except json.JSONDecodeError:
            model = {}
        if not isinstance(model, dict):
            model = {}
        for field in empty_model():
            model.setdefault(field, empty_model()[field])
        exams = {item.get("name", "").lower(): item for item in model.get("exams") or [] if isinstance(item, dict)}
        for exam in parsed["exams"]:
            exams[exam["name"].lower()] = exam
        model["exams"] = list(exams.values())
        deadlines = {
            item.get("name", "").lower(): item for item in model.get("deadlines") or [] if isinstance(item, dict)
        }
        for item in parsed["deadlines"]:
            deadlines[item["name"].lower()] = item
        model["deadlines"] = list(deadlines.values())
        if not model.get("late_policy"):
            model["late_policy"] = parsed["late_policy"]
        if not model.get("grading") and parsed["grading"]:
            model["grading"] = parsed["grading"]
        if not model.get("drop_rules") and parsed["drop_rules"]:
            model["drop_rules"] = parsed["drop_rules"]
        conn.execute("UPDATE courses SET model_json = ? WHERE id = ?", (json.dumps(model), row["id"]))
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, digest))
