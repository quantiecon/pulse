"""Public course calendars, such as https://data8.org/fa26/."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from html import unescape
from datetime import date, datetime, time, timedelta
from typing import Callable, List, Optional
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from berkeleypulse.db import meta_get, meta_set
from berkeleypulse.textutil import html_to_text

logger = logging.getLogger("berkeleypulse")

PARSER_VERSION = "site-1"
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_DUE_RE = re.compile(
    r"(checkpoint|entire project|project|lab|homework)?\s*due\s+(\d{1,2})/(\d{1,2})",
    re.I,
)
_TIME_RE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*(?:-|–|to)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)",
    re.I,
)
_ROW_RE = re.compile(r"<tr\b[^>]*>([\s\S]*?)</tr>", re.I)
_CELL_RE = re.compile(r"<td\b[^>]*>([\s\S]*?)</td>", re.I)
_ITEM_RE = re.compile(r'<div class="syllabus-item"[\s\S]*?</div>', re.I)
_LINK_RE = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>', re.I)
_DATE_RE = re.compile(
    r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+([A-Za-z]{3})\s+(\d{1,2})",
    re.I,
)


def match_site(code: str, name: str, term: str = "") -> Optional[dict]:
    """Known public calendars. Data 8's fall site is the first one."""
    blob = re.sub(r"[^a-z0-9]+", "", ("%s %s %s" % (code or "", name or "", term or "")).lower())
    if "datac8" not in blob and "data8" not in blob:
        return None
    if "fa26" not in blob and "fall2026" not in blob:
        return None
    return {
        "url": "https://data8.org/fa26/",
        "label": "data8.org/fa26",
        "year": 2026,
    }


def fetch_site(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Pulse"})
    with urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", "replace")


def parse_course_site(html: str, year: int, page_url: str) -> dict:
    return {
        "url": page_url,
        "announcements": _announcements(html),
        "weeks": _weeks(html, year, page_url),
    }


def refresh_course_sites(conn, fetch: Optional[Callable[[str], str]] = None) -> tuple:
    """Read each matched course site once. An unchanged page is not parsed again."""
    fetch = fetch or fetch_site
    unchanged = 0
    updated = 0
    rows = conn.execute(
        "SELECT id, code, name, term FROM courses WHERE hidden = 0"
    ).fetchall()
    for row in rows:
        site = match_site(row["code"], row["name"], row["term"] or "")
        if site is None:
            continue
        status = refresh_site(conn, row["id"], site, fetch)
        if status == "cached":
            unchanged += 1
        elif status == "parsed":
            updated += 1
    return unchanged, updated


def refresh_site(conn, course_id: int, site: dict, fetch: Callable[[str], str]) -> str:
    body_key = "site_body:%s" % course_id
    hash_key = "site_hash:%s" % course_id
    try:
        html = fetch(site["url"])
    except Exception:
        logger.error("course site could not be read")
        return "cached" if meta_get(conn, body_key) else "failed"
    digest = hashlib.sha256((PARSER_VERSION + "\0" + html).encode()).hexdigest()
    if meta_get(conn, hash_key) == digest and meta_get(conn, body_key):
        return "cached"
    parsed = parse_course_site(html, int(site["year"]), site["url"])
    meta_set(conn, hash_key, digest)
    meta_set(conn, body_key, json.dumps(parsed))
    return "parsed"


def stored_calendar(conn, course_id: int) -> Optional[dict]:
    raw = meta_get(conn, "site_body:%s" % course_id)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def mark_current_week(calendar: dict, today: date) -> None:
    for week in calendar.get("weeks") or []:
        dates = []
        for day in week.get("days") or []:
            parsed = _iso_date(day.get("date") or "")
            if parsed is not None:
                dates.append(parsed)
        week["current"] = bool(dates) and min(dates) <= today <= max(dates)


def site_markers(calendar: dict, tz: ZoneInfo) -> List[dict]:
    """Dated labs, homework, projects, and exams. Lectures stay on the course page."""
    found: List[dict] = []
    seen = set()
    for week in calendar.get("weeks") or []:
        for day in week.get("days") or []:
            on = _iso_date(day.get("date") or "")
            if on is None:
                continue
            for item in day.get("items") or []:
                kind = item.get("kind") or ""
                if kind == "exam":
                    start, end = _exam_span(on, item, tz)
                    _add(found, seen, "exam", item.get("title") or "Exam", start, end)
                    continue
                if kind not in {"lab", "homework", "project"}:
                    continue
                for due in item.get("dues") or []:
                    due_day = _iso_date(due.get("date") or "")
                    if due_day is None:
                        continue
                    start = datetime.combine(due_day, time(17, 0), tzinfo=tz)
                    name = due.get("name") or item.get("title") or "Due"
                    _add(found, seen, "deadline", name, start, start + timedelta(minutes=15))
    return found


def _add(found, seen, kind, name, start, end) -> None:
    name = re.sub(r"\s+", " ", name).strip()
    key = (kind, name.lower(), start.date().isoformat())
    if not name or key in seen:
        return
    seen.add(key)
    found.append({"kind": kind, "name": name, "start": start, "end": end})


def _announcements(html: str) -> List[dict]:
    found = []
    seen = set()
    pattern = re.compile(
        r'<div class="announcement"[^>]*data-date="([^"]+)"[^>]*>[\s\S]*?'
        r'<div class="announcement-body"[^>]*>([\s\S]*?)</div>',
        re.I,
    )
    for match in pattern.finditer(html):
        text = html_to_text(match.group(2))
        text = re.sub(r"\s+", " ", text).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        found.append({"date": match.group(1)[:10], "text": text})
    found.sort(key=lambda item: item["date"], reverse=True)
    return found[:4]


def _weeks(html: str, year: int, page_url: str) -> List[dict]:
    start = html.find('class="syllabus-container"')
    body = html[start:] if start >= 0 else html
    chunks = re.split(r'<h2\b[^>]*class="week-label"', body, flags=re.I)
    weeks = []
    for chunk in chunks[1:]:
        title = _plain(re.split(r"</h2>", chunk, maxsplit=1, flags=re.I)[0])
        title = re.sub(r"^[^A-Za-z0-9]+", "", title).strip() or "Week"
        days = []
        for row in _ROW_RE.finditer(chunk):
            cells = _CELL_RE.findall(row.group(1))
            if len(cells) < 2:
                continue
            label = _plain(cells[0])
            on = _row_date(label, year)
            items = _items(cells[1], on, page_url)
            if len(cells) > 2:
                items.extend(_items(cells[2], on, page_url))
            if not label and not items:
                continue
            days.append(
                {
                    "label": label,
                    "date": on.isoformat() if on else "",
                    "items": items,
                }
            )
        if days:
            weeks.append({"name": title, "days": days, "current": False})
    return weeks


def _items(cell: str, on: Optional[date], page_url: str) -> List[dict]:
    items = []
    for block in _ITEM_RE.findall(cell):
        kind_match = re.search(r"label-([a-z]+)", block, re.I)
        kind = kind_match.group(1).lower() if kind_match else "note"
        label = ""
        label_match = re.search(r"<strong\b[^>]*>([\s\S]*?)</strong>", block, re.I)
        if label_match:
            label = _plain(label_match.group(1))
        links = []
        for href, inner in _LINK_RE.findall(block):
            links.append({"title": _plain(inner), "href": urljoin(page_url, unescape(href))})
        span_match = re.search(r"<span\b[^>]*>([\s\S]*?)</span>", block, re.I)
        span = _plain(span_match.group(1)) if span_match else ""
        title = links[0]["title"] if links else (span or label)
        if not title:
            continue
        dues = []
        if on is not None:
            short = re.sub(r"\s*\([^)]*\)", "", title).strip() or label or title
            for qualifier, month, day in _DUE_RE.findall(title):
                due = _due_on(int(month), int(day), on)
                name = short
                if qualifier.strip():
                    name = "%s · %s" % (short, qualifier.strip().title())
                dues.append({"date": due.isoformat(), "name": name})
        start_clock, end_clock = _clocks(title)
        item = {
            "kind": kind,
            "label": label,
            "title": title,
            "href": links[0]["href"] if links else "",
            "links": links[1:],
            "dues": dues,
            "start": start_clock,
            "end": end_clock,
        }
        items.append(item)
    return items


def _clocks(title: str):
    match = _TIME_RE.search(title)
    if not match:
        return "", ""
    start_hour = int(match.group(1))
    start_min = int(match.group(2) or "0")
    end_hour = int(match.group(3))
    end_min = int(match.group(4) or "0")
    suffix = match.group(5).lower()
    if suffix == "pm":
        if start_hour < 12:
            start_hour += 12
        if end_hour < 12:
            end_hour += 12
    elif suffix == "am":
        if start_hour == 12:
            start_hour = 0
        if end_hour == 12:
            end_hour = 0
    return "%02d:%02d" % (start_hour, start_min), "%02d:%02d" % (end_hour, end_min)


def _exam_span(on: date, item: dict, tz: ZoneInfo):
    start_clock = item.get("start") or "12:00"
    end_clock = item.get("end") or ""
    start = datetime.combine(on, _clock(start_clock), tzinfo=tz)
    if end_clock:
        end = datetime.combine(on, _clock(end_clock), tzinfo=tz)
        if end <= start:
            end = start + timedelta(hours=2)
    else:
        end = start + timedelta(hours=2)
    return start, end


def _clock(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _row_date(label: str, year: int) -> Optional[date]:
    match = _DATE_RE.search(label or "")
    if not match:
        return None
    month = _MONTHS.get(match.group(2).lower())
    if month is None:
        return None
    try:
        return date(year, month, int(match.group(3)))
    except ValueError:
        return None


def _due_on(month: int, day: int, row: date) -> date:
    try:
        due = date(row.year, month, day)
    except ValueError:
        return row
    if due < row and (row - due).days > 60:
        try:
            return date(row.year + 1, month, day)
        except ValueError:
            return row
    return due


def _iso_date(value: str) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _plain(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
        .replace("&nbsp;", " ")
    )
    return re.sub(r"\s+", " ", text).strip()
