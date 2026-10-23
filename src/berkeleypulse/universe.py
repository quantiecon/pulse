"""Turn a typed line or a schedule image into a calendar proposal."""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import httpx

from berkeleypulse.config import Settings

Event = Dict[str, datetime]
Chip = Dict[str, str]

_WEEKDAYS = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}

_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

_MEALS = {
    "breakfast": time(8, 0),
    "brunch": time(10, 30),
    "lunch": time(12, 0),
    "dinner": time(18, 0),
    "coffee": time(10, 0),
}

_RANGE = re.compile(
    r"(?P<h1>\d{1,2})(?::(?P<m1>\d{2}))?\s*(?P<p1>am|pm)?"
    r"\s*(?:-|–|—|to)\s*"
    r"(?P<h2>\d{1,2})(?::(?P<m2>\d{2}))?\s*(?P<p2>am|pm)?",
    re.IGNORECASE,
)
_CLOCK = re.compile(
    r"(?<!\d)(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<p>am|pm)\b|\b(?P<h24>\d{1,2}):(?P<m24>\d{2})\b",
    re.IGNORECASE,
)
_NAMED_DAY = re.compile(
    r"\b(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(?P<day>\d{1,2})(?:,?\s+(?P<year>\d{4}))?",
    re.IGNORECASE,
)
_SLASH_DAY = re.compile(r"\b(?P<month>\d{1,2})/(?P<day>\d{1,2})(?:/(?P<year>\d{2,4}))?\b")
_WORD = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def interpret(prompt: str, now: datetime) -> Tuple[List[Event], str]:
    """Read calendar additions out of a typed line. Nothing is guessed past a day and a time."""
    events: List[Event] = []
    reason = ""
    for chunk in _chunks(prompt):
        event, why = _one(chunk, now)
        if event:
            events.append(event)
        elif why and not reason:
            reason = why
    if events:
        return events, ""
    return [], reason or "Name the day and what to add."


def propose(incoming: Sequence[Event], existing: Sequence[Event]) -> Dict:
    """Old schedule beside the new one. Added items are marked, and overlaps are conflicts."""
    used = set()
    added: List[Event] = []
    changed: List[Tuple[Event, Event]] = []
    for event in incoming:
        match = _match(event, existing, used)
        if match is None:
            added.append(event)
            continue
        used.add(id(match))
        if _same_span(event, match):
            continue
        changed.append((match, event))
    moved = {id(old) for old, _ in changed}
    conflicts: List[Event] = []
    seen = set()
    for event in added + [new for _, new in changed]:
        for item in existing:
            if id(item) in moved or id(item) in seen:
                continue
            if _overlaps(event, item):
                seen.add(id(item))
                conflicts.append(item)
    added_count = len(added)
    changed_count = len(changed)
    conflict_count = len(conflicts)
    return {
        "added": added_count,
        "changed": changed_count,
        "conflicts": conflict_count,
        "summary": _summary(added_count, changed_count, conflict_count),
        "days": _days(existing, added, changed, seen),
    }


def read_schedule_image(images: Sequence[Tuple[bytes, str]], settings: Settings, now: datetime) -> Tuple[List[Event], str]:
    """Read events a screenshot actually shows. Missing times are dropped, not filled in."""
    if not images:
        return [], ""
    if not settings.llm_ready:
        return [], "Could not read a schedule from that image."
    content: List[dict] = [
        {
            "type": "text",
            "text": (
                "Read the schedule in the image. Return JSON with an events list. "
                "Each event needs title, date as YYYY-MM-DD, start as HH:MM, and end as HH:MM, 24-hour. "
                "Include an event only when the title, date, and both times are visible. "
                "Do not infer a missing time or date. Today is %s."
            )
            % now.date().isoformat(),
        }
    ]
    for data, media in images[:10]:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": "data:%s;base64,%s" % (media, base64.b64encode(data).decode("ascii"))},
            }
        )
    try:
        response = httpx.post(
            settings.llm_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer %s" % settings.llm_api_key.strip()},
            json={
                "model": settings.llm_model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": content}],
            },
            timeout=30.0,
        )
        response.raise_for_status()
        parsed = json.loads(response.json()["choices"][0]["message"]["content"])
    except Exception:
        return [], "Could not read a schedule from that image."
    events = []
    for item in parsed.get("events") or []:
        event = _from_reading(item, now)
        if event:
            events.append(event)
    if not events:
        return [], "Could not read a schedule from that image."
    return events, ""


def _chunks(prompt: str) -> List[str]:
    parts = [part.strip(" \t-•") for part in re.split(r"[\n;]+", prompt) if part.strip()]
    return parts or [prompt.strip()]


def _one(chunk: str, now: datetime) -> Tuple[Optional[Event], str]:
    day = _date(chunk, now)
    weekday = _weekday(chunk)
    start, end, time_text = _times(chunk)
    if start is None:
        meal = _meal(chunk)
        if meal is not None:
            start = meal
            end = _plus_hour(meal)
    if day is None and weekday is None:
        if re.search(r"\btoday\b", chunk, re.IGNORECASE):
            day = now.date()
        elif re.search(r"\btomorrow\b", chunk, re.IGNORECASE):
            day = now.date() + timedelta(days=1)
    if day is None and weekday is None:
        return None, "Add a day, like Friday."
    if start is None or end is None:
        return None, "Add a time, like 2–3pm."
    if day is None:
        day = _coming(now, weekday, start)
    elif weekday is not None and day.weekday() != weekday and not _has_explicit_date(chunk):
        day = _coming(now, weekday, start)
    start_at = datetime.combine(day, start, tzinfo=now.tzinfo)
    end_at = datetime.combine(day, end, tzinfo=now.tzinfo)
    if end_at <= start_at:
        end_at += timedelta(days=1)
    if not _has_explicit_date(chunk) and start_at <= now:
        start_at += timedelta(days=7)
        end_at += timedelta(days=7)
    title = _title(chunk, time_text)
    if not title:
        return None, "Name what to add."
    return {"title": title, "start": start_at, "end": end_at}, ""


def _times(chunk: str) -> Tuple[Optional[time], Optional[time], str]:
    match = _RANGE.search(chunk)
    if match:
        start = _clock_time(match.group("h1"), match.group("m1"), match.group("p1"), match.group("p2"), match.group("h2"))
        end = _clock_time(match.group("h2"), match.group("m2"), match.group("p2"), match.group("p1"), match.group("h1"))
        if start and end:
            return start, end, match.group(0)
    named = re.search(r"\b(noon|midnight)\b", chunk, re.IGNORECASE)
    if named:
        start = time(12, 0) if named.group(1).lower() == "noon" else time(0, 0)
        return start, _plus_hour(start), named.group(0)
    clock = _CLOCK.search(chunk)
    if not clock:
        return None, None, ""
    if clock.group("h24"):
        hour = int(clock.group("h24"))
        minute = int(clock.group("m24"))
        if hour > 23 or minute > 59:
            return None, None, ""
        start = time(hour, minute)
        return start, _plus_hour(start), clock.group(0)
    start = _clock_time(clock.group("h"), clock.group("m"), clock.group("p"), None, None)
    if start is None:
        return None, None, ""
    return start, _plus_hour(start), clock.group(0)


def _clock_time(hour_text, minute_text, meridian, other, other_hour) -> Optional[time]:
    if hour_text is None:
        return None
    hour = int(hour_text)
    minute = int(minute_text or 0)
    if minute > 59 or hour > 24:
        return None
    mark = (meridian or "").lower()
    if not mark and other:
        other_mark = other.lower()
        if other_hour is not None and int(other_hour) < hour and other_mark == "pm" and hour != 12:
            mark = "am"
        else:
            mark = other_mark
    if mark == "am":
        if hour == 12:
            hour = 0
        elif hour > 12:
            return None
    elif mark == "pm":
        if hour < 12:
            hour += 12
        elif hour > 12:
            return None
    elif hour == 24:
        return None
    elif hour < 8:
        hour += 12
    elif hour > 23:
        return None
    if hour > 23:
        return None
    return time(hour, minute)


def _plus_hour(start: time) -> time:
    moment = datetime(2000, 1, 1, start.hour, start.minute) + timedelta(hours=1)
    return time(moment.hour, moment.minute)


def _weekday(chunk: str) -> Optional[int]:
    for word in _WORD.findall(chunk.lower()):
        if word in _WEEKDAYS:
            return _WEEKDAYS[word]
    return None


def _date(chunk: str, now: datetime):
    named = _NAMED_DAY.search(chunk)
    if named:
        return _safe_date(
            int(named.group("year") or now.year),
            _month_number(named.group("month")),
            int(named.group("day")),
            now,
        )
    slash = _SLASH_DAY.search(chunk)
    if not slash:
        return None
    year_text = slash.group("year")
    if year_text and len(year_text) == 2:
        year = 2000 + int(year_text)
    elif year_text:
        year = int(year_text)
    else:
        year = now.year
    return _safe_date(year, int(slash.group("month")), int(slash.group("day")), now)


def _safe_date(year: int, month: int, day: int, now: datetime):
    try:
        found = datetime(year, month, day).date()
    except ValueError:
        return None
    if found < now.date() and (now.date() - found).days > 1 and year == now.year:
        try:
            return datetime(year + 1, month, day).date()
        except ValueError:
            return None
    return found


def _has_explicit_date(chunk: str) -> bool:
    return bool(_NAMED_DAY.search(chunk) or _SLASH_DAY.search(chunk) or re.search(r"\b(today|tomorrow)\b", chunk, re.IGNORECASE))


def _coming(now: datetime, weekday: int, start: time):
    today = now.date()
    delta = (weekday - today.weekday()) % 7
    candidate = today + timedelta(days=delta)
    if datetime.combine(candidate, start, tzinfo=now.tzinfo) <= now:
        candidate += timedelta(days=7)
    return candidate


def _meal(chunk: str) -> Optional[time]:
    for word in _WORD.findall(chunk.lower()):
        if word in _MEALS:
            return _MEALS[word]
    return None


_SKIP = {
    "a",
    "an",
    "am",
    "at",
    "add",
    "block",
    "calendar",
    "from",
    "next",
    "on",
    "please",
    "pm",
    "put",
    "schedule",
    "the",
    "this",
    "today",
    "tomorrow",
}


def _title(chunk: str, time_text: str) -> str:
    text = chunk
    if time_text:
        text = text.replace(time_text, " ")
    text = _NAMED_DAY.sub(" ", text)
    text = _SLASH_DAY.sub(" ", text)
    words = []
    for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9'.+-]*", text):
        token = word.lower().strip(".")
        if token in _SKIP or token in _WEEKDAYS or token in _MONTHS:
            continue
        words.append(word.strip("."))
    title = " ".join(words).strip()
    if title and title[0].isalpha() and title[0].islower():
        title = title[0].upper() + title[1:]
    return title[:120]


def _month_number(name: str) -> int:
    token = name.lower().rstrip(".")
    if token in _MONTHS:
        return _MONTHS[token]
    return _MONTHS[token[:3]]


def _match(event: Event, existing: Sequence[Event], used: set) -> Optional[Event]:
    wanted = _compact(event["title"])
    if len(wanted) < 3:
        return None
    found = [item for item in existing if id(item) not in used and _compact(item["title"]) == wanted]
    if not found:
        return None
    found.sort(key=lambda item: abs((item["start"] - event["start"]).total_seconds()))
    return found[0]


def _compact(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", title.lower())


def _same_span(left: Event, right: Event) -> bool:
    return _minute(left["start"]) == _minute(right["start"]) and _minute(left["end"]) == _minute(right["end"]) and left["start"].date() == right["start"].date()


def _minute(moment: datetime) -> int:
    return moment.hour * 60 + moment.minute


def _overlaps(left: Event, right: Event) -> bool:
    return left["start"].date() == right["start"].date() and left["start"] < right["end"] and right["start"] < left["end"]


def _days(existing: Sequence[Event], added: List[Event], changed: List[Tuple[Event, Event]], conflict_ids: set) -> List[dict]:
    grouped: Dict[str, dict] = {}

    def bucket(moment: datetime) -> dict:
        key = moment.date().isoformat()
        if key not in grouped:
            grouped[key] = {"label": _day_label(moment), "sort": moment.date().toordinal(), "rows": {}}
        return grouped[key]

    def place(moment: datetime, old_items: List[Chip], new_items: List[Chip]) -> None:
        row = bucket(moment)["rows"].setdefault(_minute(moment), {"time": _clock(moment), "sort": _minute(moment), "old": [], "new": []})
        _extend(row["old"], old_items)
        _extend(row["new"], new_items)

    for event in added:
        clashes = _clash_chips(event, existing, conflict_ids, skip=None)
        place(event["start"], clashes, clashes + [_chip(event, "added")])
    for old, new in changed:
        clashes = _clash_chips(new, existing, conflict_ids, skip=id(old))
        same_slot = _minute(old["start"]) == _minute(new["start"]) and old["start"].date() == new["start"].date()
        if not same_slot:
            place(old["start"], [_chip(old, "")], [])
        old_side = ([_chip(old, "")] + clashes) if same_slot else list(clashes)
        place(new["start"], old_side, clashes + [_chip(new, "changed")])
    days = []
    for item in sorted(grouped.values(), key=lambda entry: entry["sort"]):
        rows = []
        for row in sorted(item["rows"].values(), key=lambda entry: entry["sort"]):
            row["old"] = _ordered(row["old"])
            row["new"] = _ordered(row["new"])
            rows.append({"time": row["time"], "old": row["old"], "new": row["new"]})
        days.append({"label": item["label"], "rows": rows})
    return days


def _clash_chips(event: Event, existing: Sequence[Event], conflict_ids: set, skip: Optional[int]) -> List[Chip]:
    chips = []
    for item in existing:
        if id(item) == skip or id(item) not in conflict_ids:
            continue
        if _overlaps(event, item):
            chips.append(_chip(item, "conflict"))
    return chips


def _extend(items: List[Chip], extra: List[Chip]) -> None:
    seen = {(item["title"], item["tone"]) for item in items}
    for item in extra:
        key = (item["title"], item["tone"])
        if key in seen:
            continue
        seen.add(key)
        items.append(item)


def _ordered(items: List[Chip]) -> List[Chip]:
    rank = {"": 1, "conflict": 0, "added": 2, "changed": 2}
    return sorted(items, key=lambda item: rank.get(item["tone"], 1))


def _chip(event: Event, tone: str) -> Chip:
    return {"title": event["title"], "tone": tone, "span": _span(event["start"], event["end"])}


def _summary(added: int, changed: int, conflicts: int) -> str:
    conflict = "1 conflict" if conflicts == 1 else "%d conflicts" % conflicts
    return "%d added · %d changed · %s" % (added, changed, conflict)


def _from_reading(item, now: datetime) -> Optional[Event]:
    if not isinstance(item, dict):
        return None
    title = " ".join(str(item.get("title") or "").split())
    if not title or len(title) > 120:
        return None
    day_parts = str(item.get("date") or "").split("-")
    if len(day_parts) != 3 or not all(part.isdigit() for part in day_parts):
        return None
    start = _hhmm(str(item.get("start") or ""))
    end = _hhmm(str(item.get("end") or ""))
    if start is None or end is None:
        return None
    try:
        day = datetime(int(day_parts[0]), int(day_parts[1]), int(day_parts[2])).date()
    except ValueError:
        return None
    start_at = datetime.combine(day, start, tzinfo=now.tzinfo)
    end_at = datetime.combine(day, end, tzinfo=now.tzinfo)
    if end_at <= start_at:
        return None
    return {"title": title, "start": start_at, "end": end_at}


def _hhmm(value: str) -> Optional[time]:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def _day_label(moment: datetime) -> str:
    return moment.strftime("%A, %b ") + str(moment.day)


def _clock(moment: datetime) -> str:
    hour = str(int(moment.strftime("%I")))
    return "%s:%s %s" % (hour, moment.strftime("%M"), moment.strftime("%p"))


def _span(start: datetime, end: datetime) -> str:
    return "%s–%s" % (_clock(start), _clock(end))
