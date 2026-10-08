from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from berkeleypulse.textutil import sentences

PARSER_VERSION = "2"
TZ = ZoneInfo("America/Los_Angeles")

MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

DATE_RE = re.compile(
    r"\b("
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
    r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?"
    r"(?:(?:\s+at|,)\s*|\s+)(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
    re.IGNORECASE,
)

DATE_ONLY_RE = re.compile(
    r"\b("
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
    r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?\b",
    re.IGNORECASE,
)

WEIGHT_PATTERNS = [
    re.compile(
        r"(?P<name>[A-Za-z][A-Za-z0-9 /&'()-]{1,60}?):\s*\((?P<weight>\d{1,3})\s*(?:%|percent)\)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<name>[A-Za-z][A-Za-z0-9 /&'-]{1,40}?) (?:is|are) worth (?P<weight>\d{1,3})\s*(?:%|percent)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<name>[A-Za-z][A-Za-z0-9 /&'-]{1,40}?)\s*\((?P<weight>\d{1,3})\s*(?:%|percent)\)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![\d.])(?P<weight>\d{1,3})\s*(?:%|percent)\s*(?:of the grade is |for |–|-|:)\s*(?P<name>[A-Za-z][A-Za-z0-9 /&'-]{1,40})",
        re.IGNORECASE,
    ),
]


def empty_model() -> Dict:
    return {"grading": [], "late_policy": "", "drop_rules": [], "exams": [], "deadlines": []}


def _exam_datetimes(text: str, default_year: int, default_hour: int) -> List[datetime]:
    found = []
    for match in DATE_ONLY_RE.finditer(text):
        prefix = text[max(0, match.start() - 24):match.start()].lower()
        if "due" in prefix:
            continue
        timed = DATE_RE.match(text, match.start())
        parsed = _datetime_from(timed, default_year, timed=True) if timed else None
        if parsed is None:
            parsed = _datetime_from(match, default_year, timed=False, default_hour=default_hour)
        if parsed is not None:
            found.append(parsed)
    return found


def _datetime_from(match, default_year: int, timed: bool, default_hour: int = 12) -> Optional[datetime]:
    month = MONTHS[match.group(1).lower()]
    day = int(match.group(2))
    year = int(match.group(3) or default_year)
    if timed:
        hour = int(match.group(4))
        minute = int(match.group(5) or 0)
        ampm = (match.group(6) or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        return _safe_dt(year, month, day, hour, minute)
    return _safe_dt(year, month, day, default_hour, 0)


def find_datetime(text: str, default_year: int, default_hour: int = 12) -> Optional[datetime]:
    timed = DATE_RE.search(text)
    if timed:
        month = MONTHS[timed.group(1).lower()]
        day = int(timed.group(2))
        year = int(timed.group(3) or default_year)
        hour = int(timed.group(4))
        minute = int(timed.group(5) or 0)
        ampm = (timed.group(6) or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        return _safe_dt(year, month, day, hour, minute)
    plain = DATE_ONLY_RE.search(text)
    if plain:
        month = MONTHS[plain.group(1).lower()]
        day = int(plain.group(2))
        year = int(plain.group(3) or default_year)
        return _safe_dt(year, month, day, default_hour, 0)
    return None


def _safe_dt(year: int, month: int, day: int, hour: int, minute: int) -> Optional[datetime]:
    try:
        return datetime(year, month, day, hour, minute, tzinfo=TZ)
    except ValueError:
        return None


def _clean_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", name).strip(" -–:()[]")
    cleaned = re.sub(r"^(the|a|an)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+(is|are|grade|grades|score|scores)$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _grading(text: str) -> List[Dict]:
    found = []
    for line in text.splitlines():
        lowered = line.lower()
        if any(
            word in lowered
            for word in ("late", "penalty", "per day", "per hour", "letter grade", "rounded", "become a")
        ):
            continue
        for pattern in WEIGHT_PATTERNS:
            for match in pattern.finditer(line):
                name = _clean_name(match.group("name"))
                if not name or len(name) < 3:
                    continue
                weight = int(match.group("weight"))
                if weight <= 0 or weight > 100:
                    continue
                key = name.lower()
                if key in {"grade", "grading", "total", "score", "course", "final grade"} or key.startswith("of "):
                    continue
                found.append({"name": name[:1].upper() + name[1:], "weight": weight})
    deduped = []
    seen = set()
    for item in found:
        key = item["name"].lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    deduped = _drop_parent_weights(deduped)
    total = sum(item["weight"] for item in deduped)
    if len(deduped) >= 2 and not 90 <= total <= 110:
        return []
    return deduped


def _name_inside(parent: str, child: str) -> bool:
    words = [word for word in re.findall(r"[a-z]+", child.lower()) if len(word) > 3]
    return any(word in parent for word in words)


def _drop_parent_weights(items: List[Dict]) -> List[Dict]:
    """Drop a heading whose percent is just the sum of the lines under it."""
    drop = set()
    for index, item in enumerate(items):
        label = item["name"].lower()
        if " and " not in label and "&" not in label:
            continue
        others = [other for other_index, other in enumerate(items) if other_index != index]
        for left in range(len(others)):
            for right in range(left + 1, len(others)):
                if others[left]["weight"] + others[right]["weight"] != item["weight"]:
                    continue
                if _name_inside(label, others[left]["name"]) and _name_inside(label, others[right]["name"]):
                    drop.add(index)
    return [item for index, item in enumerate(items) if index not in drop]


_DUE_RE = re.compile(
    r"(?P<name>.{3,80}?) is due (?P<when>.+)"
    r"|(?P<name2>[A-Za-z][^:]{2,60}?)\s*(?:\(\d{1,3}\s*%\))?\s*:\s*Due[:\s]+(?P<when2>.+)",
    re.IGNORECASE,
)


def _exam_name(sentence: str) -> Optional[str]:
    lowered = sentence.lower()
    if "midterm" in lowered:
        return "Midterm"
    if re.search(r"\bfinal\b", lowered):
        return "Final"
    if re.search(r"\bexam\b", lowered):
        return "Exam"
    return None


def heuristic_parse(text: str, now: Optional[datetime] = None) -> Dict:
    model = empty_model()
    if not text.strip():
        return model
    default_year = (now or datetime.now(TZ)).year
    pieces = sentences(text)
    model["grading"] = _grading(text)
    late = [piece for piece in pieces if re.search(r"\blate\b", piece, re.IGNORECASE)]
    specific = [
        piece
        for piece in late
        if re.search(r"\b(penalty|penalties|percent|per day|letter)\b|%", piece, re.IGNORECASE)
    ]
    model["late_policy"] = " ".join((specific or late)[:2])
    model["drop_rules"] = [
        piece
        for piece in pieces
        if re.search(r"\b(drop|lowest)\b", piece, re.IGNORECASE)
        and re.search(r"\b(quiz|homework|assignment|score|grade|lab|midterm)\b", piece, re.IGNORECASE)
    ]
    seen_exams = set()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for piece in lines:
        name = _exam_name(piece)
        if not name:
            continue
        if re.search(r"\bworth\b|\bpercent\b|%", piece) and not DATE_RE.search(piece) and not DATE_ONLY_RE.search(piece):
            continue
        hour = 8 if name == "Final" else 19
        if re.search(r"\bin class\b", piece, re.IGNORECASE) and not DATE_RE.search(piece):
            hour = 17
        whens = _exam_datetimes(piece, default_year, hour)
        if not whens:
            continue
        number = re.search(r"#\s*(\d+)", piece)
        if len(whens) == 1 and number and name == "Midterm":
            labels = ["Midterm %s" % number.group(1)]
        elif len(whens) == 1:
            labels = [name]
        else:
            labels = ["%s %d" % (name, index) for index in range(1, len(whens) + 1)]
        for label, when in zip(labels, whens):
            if label in seen_exams:
                continue
            seen_exams.add(label)
            model["exams"].append({"name": label, "starts_at": when.isoformat()})
    last_component = ""
    for piece in lines:
        for pattern in WEIGHT_PATTERNS[:3]:
            found = pattern.search(piece)
            if found:
                last_component = _clean_name(found.group("name"))
                break
        match = _DUE_RE.search(piece)
        when_text = ""
        name = ""
        if match:
            when_text = match.group("when") or match.group("when2") or ""
            name = _clean_name(match.group("name") or match.group("name2") or "")
        elif re.sub(r"^[^\w]+", "", piece).lower().startswith("due"):
            when_text = piece
            name = last_component
        else:
            continue
        when = find_datetime(when_text, default_year, 23)
        if when is None:
            when = find_datetime(piece, default_year, 23)
        if when is None or not name or name.lower() == "due":
            continue
        model["deadlines"].append({"name": name, "due_at": when.isoformat()})
    return model


def coerce_model(data: object) -> Optional[Dict]:
    if not isinstance(data, dict):
        return None
    model = empty_model()
    try:
        for item in data.get("grading") or []:
            if not isinstance(item, dict):
                continue
            name = _clean_name(str(item.get("name", "")))
            weight = float(item.get("weight"))
            if name and 0 < weight <= 100:
                model["grading"].append({"name": name, "weight": weight})
        late = data.get("late_policy") or ""
        model["late_policy"] = str(late).strip()[:1000]
        for rule in data.get("drop_rules") or []:
            text = str(rule).strip()
            if text:
                model["drop_rules"].append(text[:500])
        for exam in data.get("exams") or []:
            if not isinstance(exam, dict):
                continue
            name = _clean_name(str(exam.get("name", ""))) or "Exam"
            starts = str(exam.get("starts_at", "")).strip()
            if starts:
                model["exams"].append({"name": name, "starts_at": starts})
        for item in data.get("deadlines") or []:
            if not isinstance(item, dict):
                continue
            name = _clean_name(str(item.get("name", "")))
            due = str(item.get("due_at", "")).strip()
            if name and due:
                model["deadlines"].append({"name": name, "due_at": due})
    except (TypeError, ValueError):
        return None
    if not any([model["grading"], model["late_policy"], model["drop_rules"], model["exams"], model["deadlines"]]):
        return None
    return model
