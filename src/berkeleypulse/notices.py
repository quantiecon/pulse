"""Announcements that move or cancel a class. A saved announcement is not parsed again."""

from __future__ import annotations

import re
from typing import Optional

from berkeleypulse.syllabus import find_datetime
from berkeleypulse.textutil import html_to_text

CANCEL_RE = re.compile(r"\b(?:class |section |lecture )?(?:cancell?ed|cancellation)\b", re.IGNORECASE)
MOVE_RE = re.compile(
    r"\b(?:moved|reschedul\w*|new time|new room|new location|room change|location change)\b",
    re.IGNORECASE,
)


def classify_update(title: str, body: str, now=None) -> Optional[dict]:
    text = html_to_text("%s\n%s" % (title or "", body or ""))
    if CANCEL_RE.search(text):
        kind = "cancel"
    elif MOVE_RE.search(text):
        kind = "move"
    else:
        return None
    when = find_datetime(text, (now.year if now is not None else 2026))
    return {
        "kind": kind,
        "starts_at": when.isoformat() if when else "",
        "title": (title or "Announcement").strip()[:300],
    }


def store_notices(conn, course_id: int, announcements: list, now=None) -> int:
    """Store new or edited announcements. Return how many cancellations or moves were new."""
    flagged = 0
    for item in announcements:
        external_id = str(item.get("id") or "")
        updated = item.get("updated_at") or item.get("posted_at") or item.get("delayed_post_at") or ""
        if not external_id or not updated:
            continue
        prior = conn.execute(
            "SELECT updated_at FROM notices WHERE course_id = ? AND external_id = ?",
            (course_id, external_id),
        ).fetchone()
        if prior is not None and prior["updated_at"] == updated:
            continue
        title = item.get("title") or "Announcement"
        body = html_to_text(item.get("message") or "")[:8000]
        found = classify_update(title, body, now=now)
        kind = found["kind"] if found else ""
        starts = found["starts_at"] if found else ""
        if not starts:
            starts = item.get("posted_at") or ""
        conn.execute(
            """
            INSERT INTO notices(course_id, external_id, title, body, posted_at, updated_at, kind, starts_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(course_id, external_id) DO UPDATE SET
              title = excluded.title,
              body = excluded.body,
              posted_at = excluded.posted_at,
              updated_at = excluded.updated_at,
              kind = excluded.kind,
              starts_at = excluded.starts_at
            """,
            (
                course_id,
                external_id,
                title[:300],
                body,
                item.get("posted_at"),
                updated,
                kind,
                starts if kind else "",
            ),
        )
        if kind and (prior is None or prior["updated_at"] != updated):
            flagged += 1
    return flagged
