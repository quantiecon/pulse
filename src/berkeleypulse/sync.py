from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from berkeleypulse.canvas import (
    CanvasError,
    fetch_announcements,
    fetch_assignment_groups,
    fetch_courses,
    fetch_file_bytes,
    fetch_folder_children,
    fetch_root_folder,
)
from berkeleypulse.filescan import apply_file_models, sync_course_files
from berkeleypulse.notices import store_notices
from berkeleypulse.config import Settings, load_settings
from berkeleypulse.db import db, meta_bump, meta_get, meta_set
from berkeleypulse.desk import load_cookie_jar, session_status
from berkeleypulse.mail import URGENT, fetch_gmail_atom, fetch_new_mail, score_email
from berkeleypulse.qa import index_documents, parse_with_optional_model
from berkeleypulse.schedule import estimate_effort_minutes, plan_blocks
from berkeleypulse.sites import refresh_course_sites, site_markers, stored_calendar
from berkeleypulse.syllabus import PARSER_VERSION
from berkeleypulse.textutil import compact, html_to_text, pdf_to_text

logger = logging.getLogger("berkeleypulse")
_LOCK = threading.Lock()


@dataclass
class SyncResult:
    message: str
    new_urgent: List[str] = field(default_factory=list)


def material_hash(syllabus: str, tag: str) -> str:
    """Hash of static policy text only. Due dates and mail are refreshed without this."""
    digest = hashlib.sha256()
    digest.update(PARSER_VERSION.encode())
    digest.update(b"\0")
    digest.update(tag.encode())
    digest.update(b"\0")
    digest.update(syllabus.encode())
    return digest.hexdigest()


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def current_term(now: datetime) -> str:
    if now.month >= 8:
        season = "Fall"
    elif now.month >= 5:
        season = "Summer"
    else:
        season = "Spring"
    return "%s %s" % (season, now.year)


def add_manual_course(code: str, name: str, syllabus: str, now: Optional[datetime] = None) -> int:
    settings = load_settings()
    tz = ZoneInfo(settings.timezone)
    now = (now or datetime.now(tz)).astimezone(tz)
    with db() as conn:
        course_id = _upsert_course(
            conn,
            origin="manual",
            external_id="manual-%s" % uuid.uuid4().hex[:12],
            code=code.strip(),
            name=name.strip(),
            term=current_term(now),
            syllabus=syllabus.strip()[:200000],
            now=now,
        )
        if course_id is None:
            raise RuntimeError("Could not save that course.")
        reindex_course(conn, settings, course_id, now=now)
        rebuild_schedule(conn, settings, now=now)
        return course_id


def hide_course(course_id: int) -> None:
    hide_courses([course_id])


def hide_courses(course_ids: List[int]) -> int:
    removed = 0
    with db() as conn:
        for course_id in course_ids:
            if _wipe_course(conn, int(course_id)):
                removed += 1
    return removed


def _wipe_course(conn, course_id: int) -> bool:
    row = conn.execute(
        "SELECT id FROM courses WHERE id = ? AND hidden = 0",
        (course_id,),
    ).fetchone()
    if row is None:
        return False
    conn.execute(
        """
        UPDATE courses
        SET hidden = 1,
            syllabus_text = '',
            model_json = NULL,
            material_hash = NULL,
            parsed_with = NULL
        WHERE id = ?
        """,
        (course_id,),
    )
    conn.execute("DELETE FROM assignments WHERE course_id = ?", (course_id,))
    conn.execute("DELETE FROM documents WHERE course_id = ?", (course_id,))
    conn.execute("DELETE FROM events WHERE course_id = ?", (course_id,))
    conn.execute("UPDATE emails SET course_id = NULL WHERE course_id = ?", (course_id,))
    return True


def move_course(course_id: int, direction: str) -> None:
    if direction not in {"up", "down"}:
        return
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id FROM courses
            WHERE hidden = 0
            ORDER BY sort_order, code, id
            """
        ).fetchall()
        ids = [int(row["id"]) for row in rows]
        if course_id not in ids:
            return
        index = ids.index(course_id)
        target = index - 1 if direction == "up" else index + 1
        if target < 0 or target >= len(ids):
            return
        ids[index], ids[target] = ids[target], ids[index]
        for position, item_id in enumerate(ids):
            conn.execute("UPDATE courses SET sort_order = ? WHERE id = ?", (position, item_id))


def reindex_course(conn, settings: Settings, course_id: int, now: Optional[datetime] = None) -> str:
    course = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    if course is None or course["hidden"]:
        return "skipped"
    assignments = conn.execute(
        "SELECT * FROM assignments WHERE course_id = ? ORDER BY id",
        (course_id,),
    ).fetchall()
    tag = settings.parser_tag
    digest = material_hash(course["syllabus_text"] or "", tag)
    if course["material_hash"] == digest and course["model_json"]:
        meta_bump(conn, "cache_hits")
        _fill_grading_from_groups(conn, course_id, assignments)
        index_documents(conn, course_id, course["code"], course["syllabus_text"] or "", assignments)
        return "cached"
    model, used_llm = parse_with_optional_model(course["syllabus_text"] or "", settings, now=now)
    if not model.get("grading"):
        model["grading"] = _grading_from_groups(assignments)
    if used_llm:
        meta_bump(conn, "llm_calls")
    meta_bump(conn, "parses")
    conn.execute(
        """
        UPDATE courses
        SET material_hash = ?, model_json = ?, parsed_with = ?, updated_at = ?
        WHERE id = ?
        """,
        (digest, json.dumps(model), tag, _iso(now), course_id),
    )
    index_documents(conn, course_id, course["code"], course["syllabus_text"] or "", assignments)
    return "parsed"


def rebuild_schedule(conn, settings: Settings, now: Optional[datetime] = None) -> int:
    tz = ZoneInfo(settings.timezone)
    now = (now or datetime.now(tz)).astimezone(tz)
    conn.execute("DELETE FROM events")
    courses = conn.execute("SELECT * FROM courses WHERE hidden = 0").fetchall()
    block_count = 0
    for course in courses:
        try:
            model = json.loads(course["model_json"] or "{}")
        except json.JSONDecodeError:
            model = {}
        assignments = conn.execute(
            "SELECT * FROM assignments WHERE course_id = ?",
            (course["id"],),
        ).fetchall()
        taken = set()
        work = []
        for assignment in assignments:
            due = parse_dt(assignment["due_at"])
            if due is None:
                continue
            taken.add(compact(assignment["name"]))
            _event(
                conn,
                course["id"],
                "deadline",
                "%s · %s" % (course["code"], assignment["name"]),
                due,
                due + timedelta(minutes=15),
                "Due",
            )
            if due > now:
                work.append(
                    (
                        assignment["name"],
                        due,
                        estimate_effort_minutes(assignment["name"], assignment["points"]),
                    )
                )
        for exam in model.get("exams") or []:
            starts = parse_dt(exam.get("starts_at"))
            if starts is None:
                continue
            _event(
                conn,
                course["id"],
                "exam",
                "%s · %s" % (course["code"], exam.get("name") or "Exam"),
                starts,
                starts + timedelta(minutes=90),
                "From the syllabus",
            )
            if starts > now:
                work.append((exam.get("name") or "Exam", starts, estimate_effort_minutes(exam.get("name") or "Exam", None, "exam")))
        for deadline in model.get("deadlines") or []:
            name = deadline.get("name") or ""
            if compact(name) in taken:
                continue
            due = parse_dt(deadline.get("due_at"))
            if due is None:
                continue
            _event(
                conn,
                course["id"],
                "deadline",
                "%s · %s" % (course["code"], name),
                due,
                due + timedelta(minutes=15),
                "From the syllabus",
            )
            if due > now:
                work.append((name, due, estimate_effort_minutes(name, None)))
        calendar = stored_calendar(conn, course["id"])
        if calendar:
            for marker in site_markers(calendar, tz):
                if compact(marker["name"]) in taken:
                    continue
                _event(
                    conn,
                    course["id"],
                    marker["kind"],
                    "%s · %s" % (course["code"], marker["name"]),
                    marker["start"],
                    marker["end"],
                    "From the course site",
                )
        for name, due, effort in work:
            blocks = plan_blocks(
                due,
                now,
                effort,
                settings.work_style,
                settings.session_minutes,
                tz,
            )
            total = len(blocks)
            for index, (start, end) in enumerate(blocks, start=1):
                if settings.work_style == "crammer":
                    details = "One study block the day before"
                else:
                    details = "Session %d of %d" % (index, total)
                _event(conn, course["id"], "study", "%s · %s" % (course["code"], name), start, end, details)
                block_count += 1
    for notice in conn.execute(
        """
        SELECT notices.*, courses.code AS code
        FROM notices
        JOIN courses ON courses.id = notices.course_id
        WHERE courses.hidden = 0 AND notices.kind IN ('cancel', 'move') AND notices.starts_at != ''
        """
    ):
        starts = parse_dt(notice["starts_at"])
        if starts is None:
            continue
        label = "Class canceled" if notice["kind"] == "cancel" else "Class moved"
        _event(
            conn,
            notice["course_id"],
            notice["kind"],
            "%s · %s" % (notice["code"], label),
            starts,
            starts + timedelta(minutes=30),
            notice["title"],
        )
    return block_count


def reschedule(now: Optional[datetime] = None) -> int:
    settings = load_settings()
    with db() as conn:
        return rebuild_schedule(conn, settings, now=now)


def sync_all(now: Optional[datetime] = None) -> SyncResult:
    with _LOCK:
        settings = load_settings()
        tz = ZoneInfo(settings.timezone)
        now = (now or datetime.now(tz)).astimezone(tz)
        live = []
        new_urgent: List[str] = []
        with db() as conn:
            desk = session_status()
            if settings.canvas_ready or desk["canvas"]:
                try:
                    count = pull_canvas(conn, settings, now)
                    live.append("Canvas updated %d courses" % count)
                except CanvasError as exc:
                    live.append(str(exc))
                except Exception:
                    logger.error("canvas sync failed")
                    live.append("Canvas could not be reached.")
            else:
                live.append("Canvas is not connected")
            cached = 0
            parsed = 0
            for row in conn.execute("SELECT id FROM courses WHERE hidden = 0"):
                outcome = reindex_course(conn, settings, row["id"], now=now)
                if outcome == "cached":
                    cached += 1
                elif outcome == "parsed":
                    parsed += 1
            site_same, site_read = refresh_course_sites(conn)
            if site_same:
                live.append("%d course sites unchanged" % site_same)
            if site_read:
                live.append("%d course sites read" % site_read)
            if settings.canvas_ready or desk["canvas"]:
                skipped, checked, flagged = _sync_canvas_static(conn, settings, now)
                apply_file_models(conn, now)
                if skipped:
                    live.append("%d file folders unchanged" % skipped)
                if checked:
                    live.append("%d file folders checked" % checked)
                if flagged:
                    live.append("%d class changes" % flagged)
            blocks = rebuild_schedule(conn, settings, now=now)
            if desk["mail"]:
                try:
                    added, new_urgent = pull_gmail(conn)
                    live.append("%d new emails" % added)
                except Exception as exc:
                    logger.error("mail sync failed")
                    live.append(str(exc) or "Mail could not be read from the saved sign-in.")
            elif settings.mail_ready:
                try:
                    added, new_urgent = pull_mail(conn, settings)
                    live.append("%d new emails" % added)
                except Exception as exc:
                    logger.error("mail sync failed")
                    live.append(_mail_failure(exc))
            else:
                live.append("Mail is not connected")
            message = "Scan finished. Live: %s. Static: %d unchanged, %d parsed. %d study blocks on the calendar." % (
                ". ".join(note.rstrip(".") for note in live),
                cached,
                parsed,
                blocks,
            )
            meta_set(conn, "last_sync_at", now.isoformat())
            meta_set(conn, "last_sync_note", message)
            return SyncResult(message=message, new_urgent=new_urgent)


def _sync_canvas_static(conn, settings: Settings, now: datetime):
    cookies = None if settings.canvas_ready else load_cookie_jar("canvas")
    skipped = 0
    checked = 0
    flagged = 0
    start = (now - timedelta(days=45)).date().isoformat()
    courses = conn.execute(
        """
        SELECT id, external_id FROM courses
        WHERE origin = 'canvas' AND hidden = 0 AND external_id IS NOT NULL AND external_id != ''
        """
    ).fetchall()
    for course in courses:
        try:
            root = fetch_root_folder(settings, course["external_id"], cookies)
            root_id = str(root.get("id") or "")
            if root_id:
                left, opened, _fresh = sync_course_files(
                    conn,
                    course["id"],
                    root_id,
                    lambda folder_id: fetch_folder_children(settings, folder_id, cookies),
                    lambda node: _read_canvas_file(settings, node, cookies),
                )
                skipped += left
                checked += opened
        except CanvasError:
            logger.error("file scan failed for course %s", course["external_id"])
        try:
            items = fetch_announcements(settings, course["external_id"], start, cookies)
            flagged += store_notices(conn, course["id"], items, now=now)
        except CanvasError:
            logger.error("announcement scan failed for course %s", course["external_id"])
    return skipped, checked, flagged


def _read_canvas_file(settings: Settings, node: dict, cookies) -> str:
    url = node.get("url") or ""
    if not url:
        return ""
    data = fetch_file_bytes(settings, url, cookies)
    name = (node.get("name") or "").lower()
    kind = (node.get("content_type") or "").lower()
    if "pdf" in kind or name.endswith(".pdf"):
        try:
            return pdf_to_text(data)
        except Exception:
            return ""
    return html_to_text(data.decode("utf-8", errors="replace"))


def _mail_failure(exc: Exception) -> str:
    raw = exc.args[0] if exc.args else ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    text = str(raw)
    if "AUTHENTICATIONFAILED" in text or "Invalid credentials" in text:
        return "Mail rejected the password. Use an app password from the mail account."
    return "Mail could not be read. Check the IMAP host, user, and app password."


def pull_canvas(conn, settings: Settings, now: datetime) -> int:
    cookies = None if settings.canvas_ready else load_cookie_jar("canvas")
    courses = fetch_courses(settings, cookies)
    count = 0
    for course in courses:
        external_id = str(course.get("id"))
        existing = conn.execute(
            "SELECT id, hidden FROM courses WHERE origin = 'canvas' AND external_id = ?",
            (external_id,),
        ).fetchone()
        if existing and existing["hidden"]:
            continue
        term = ""
        if isinstance(course.get("term"), dict):
            term = course["term"].get("name") or ""
        groups = fetch_assignment_groups(settings, external_id, cookies)
        assignments = []
        for group in groups:
            for item in group.get("assignments") or []:
                if not isinstance(item, dict):
                    continue
                assignments.append(
                    {
                        "external_id": str(item.get("id")),
                        "name": (item.get("name") or "Assignment")[:300],
                        "due_at": item.get("due_at"),
                        "points": item.get("points_possible"),
                        "description": html_to_text(item.get("description") or "")[:20000],
                        "group_name": group.get("name"),
                        "group_weight": group.get("group_weight"),
                    }
                )
        course_id = _upsert_course(
            conn,
            origin="canvas",
            external_id=external_id,
            code=(course.get("course_code") or course.get("name") or "Course")[:80],
            name=(course.get("name") or "Course")[:200],
            term=term[:80],
            syllabus=html_to_text(course.get("syllabus_body") or "")[:200000],
            now=now,
        )
        if course_id is None:
            continue
        _replace_assignments(conn, course_id, assignments)
        count += 1
    return count


def pull_gmail(conn):
    added, urgent, _uid = _store_mail(conn, fetch_gmail_atom(load_cookie_jar("mail")))
    return added, urgent


def pull_mail(conn, settings: Settings):
    last_uid = int(meta_get(conn, "imap_uid", "0") or "0")
    messages = fetch_new_mail(settings, last_uid)
    added, urgent, max_uid = _store_mail(conn, messages)
    if max_uid > last_uid:
        meta_set(conn, "imap_uid", str(max_uid))
    return added, urgent


def _store_mail(conn, messages):
    added = 0
    urgent = []
    max_uid = 0
    now = datetime.now(timezone.utc).isoformat()
    for item in messages:
        max_uid = max(max_uid, item.uid)
        exists = conn.execute("SELECT id FROM emails WHERE message_id = ?", (item.message_id,)).fetchone()
        if exists:
            continue
        score, reason = score_email(item.subject, item.from_addr, item.body)
        course_id = match_course_id(conn, item.subject, item.body)
        snippet = re.sub(r"\s+", " ", item.body).strip()[:240]
        conn.execute(
            """
            INSERT INTO emails(
              message_id, from_addr, from_name, subject, sent_at, snippet, body,
              score, reason, course_id, demo, dismissed, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
            """,
            (
                item.message_id,
                item.from_addr,
                item.from_name,
                item.subject,
                item.sent_at.isoformat() if item.sent_at else None,
                snippet,
                item.body,
                score,
                reason,
                course_id,
                now,
            ),
        )
        added += 1
        if score >= URGENT:
            urgent.append(item.subject)
    return added, urgent, max_uid


def match_course_id(conn, subject: str, body: str) -> Optional[int]:
    blob = compact("%s %s" % (subject, body))
    best = None
    best_len = 0
    for row in conn.execute("SELECT id, code FROM courses WHERE hidden = 0"):
        code = compact(row["code"] or "")
        if len(code) < 4:
            continue
        if code in blob and len(code) > best_len:
            best = row["id"]
            best_len = len(code)
    return best


def _upsert_course(conn, origin, external_id, code, name, term, syllabus, now) -> Optional[int]:
    row = conn.execute(
        "SELECT id, hidden FROM courses WHERE origin = ? AND external_id = ?",
        (origin, external_id),
    ).fetchone()
    if row and row["hidden"]:
        return None
    updated = _iso(now)
    if row:
        conn.execute(
            """
            UPDATE courses
            SET code = ?, name = ?, term = ?, syllabus_text = ?, updated_at = ?
            WHERE id = ?
            """,
            (code, name, term, syllabus, updated, row["id"]),
        )
        return int(row["id"])
    position = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM courses").fetchone()["n"]
    cursor = conn.execute(
        """
        INSERT INTO courses(
          origin, external_id, code, name, term, syllabus_text, updated_at, hidden, sort_order
        ) VALUES(?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (origin, external_id, code, name, term, syllabus, updated, position),
    )
    return int(cursor.lastrowid)


def _replace_assignments(conn, course_id: int, assignments: List[Dict]) -> None:
    conn.execute("DELETE FROM assignments WHERE course_id = ?", (course_id,))
    for item in assignments:
        conn.execute(
            """
            INSERT INTO assignments(
              course_id, external_id, name, due_at, points, description, group_name, group_weight
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                course_id,
                item.get("external_id"),
                item.get("name") or "Assignment",
                item.get("due_at"),
                item.get("points"),
                item.get("description") or "",
                item.get("group_name"),
                item.get("group_weight"),
            ),
        )


def _fill_grading_from_groups(conn, course_id: int, assignments) -> None:
    row = conn.execute("SELECT model_json FROM courses WHERE id = ?", (course_id,)).fetchone()
    if row is None:
        return
    try:
        model = json.loads(row["model_json"] or "{}")
    except json.JSONDecodeError:
        model = {}
    if model.get("grading"):
        return
    groups = _grading_from_groups(assignments)
    if not groups:
        return
    model["grading"] = groups
    conn.execute("UPDATE courses SET model_json = ? WHERE id = ?", (json.dumps(model), course_id))


def _grading_from_groups(assignments) -> List[Dict]:
    groups: Dict[str, float] = {}
    for row in assignments:
        if not row["group_name"] or row["group_weight"] is None:
            continue
        weight = float(row["group_weight"])
        if weight > 0:
            groups[row["group_name"]] = weight
    items = [{"name": name, "weight": weight} for name, weight in groups.items()]
    total = sum(item["weight"] for item in items)
    if len(items) >= 2 and 90 <= total <= 110:
        return items
    return []


def _event(conn, course_id, kind, title, start, end, details) -> None:
    conn.execute(
        """
        INSERT INTO events(course_id, kind, title, starts_at, ends_at, details)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (course_id, kind, title, start.isoformat(), end.isoformat(), details),
    )


def _iso(now: Optional[datetime]) -> str:
    if now is None:
        return datetime.now(timezone.utc).isoformat()
    return now.isoformat()
