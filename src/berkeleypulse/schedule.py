from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

Block = Tuple[datetime, datetime]


def estimate_effort_minutes(name: str, points: Optional[float], kind: str = "deadline") -> int:
    tokens = set(re.findall(r"[a-z0-9]+", name.lower()))
    if kind == "exam" or tokens & {"midterm", "exam"}:
        return 180
    if "project" in tokens:
        return 180
    if "quiz" in tokens:
        return 45
    if points is None:
        return 60
    return max(30, min(240, int(round(float(points) * 6))))


def plan_blocks(
    due: datetime,
    now: datetime,
    effort_minutes: int,
    style: str,
    session_minutes: int,
    tz: ZoneInfo,
) -> List[Block]:
    due = due.astimezone(tz)
    now = now.astimezone(tz)
    cutoff = due - timedelta(hours=2)
    if cutoff <= now or effort_minutes <= 0:
        return []
    if style == "crammer":
        duration = max(45, min(int(effort_minutes), 240))
        start = datetime.combine(due.date() - timedelta(days=1), time(18, 0), tzinfo=tz)
        fitted = _fit_one(start, duration, now, cutoff)
        return [fitted] if fitted else []

    session = max(25, int(session_minutes))
    count = int((effort_minutes + session / 2) // session)
    count = max(1, min(8, count))
    lengths = _split(effort_minutes, count, session)
    today = now.date()
    last_day = due.date() - timedelta(days=1)
    if last_day < today:
        last_day = today
    window = min(10, max(count, count * 2))
    first_day = last_day - timedelta(days=window - 1)
    if first_day < today:
        first_day = today
    days = []
    day = first_day
    while day <= last_day:
        days.append(day)
        day += timedelta(days=1)
    if not days:
        return []
    starts = [datetime.combine(item, time(18, 0), tzinfo=tz) for item in days]
    if len(starts) > count:
        starts = _even(starts, count)
    elif len(starts) < count:
        extras: List[datetime] = []
        for item in reversed(days):
            for hour in (14, 10, 8):
                extras.append(datetime.combine(item, time(hour, 0), tzinfo=tz))
                if len(starts) + len(extras) >= count:
                    break
            if len(starts) + len(extras) >= count:
                break
        starts = sorted(starts + extras)[:count]
    fitted_blocks: List[Block] = []
    for start, length in zip(starts, lengths):
        if fitted_blocks and start < fitted_blocks[-1][1]:
            start = fitted_blocks[-1][1]
        block = _fit_one(start, length, now, cutoff)
        if block and fitted_blocks and block[0] < fitted_blocks[-1][1]:
            block = _fit_one(fitted_blocks[-1][1], length, now, cutoff)
        if block is None or (fitted_blocks and block[0] < fitted_blocks[-1][1]):
            continue
        fitted_blocks.append(block)
    return fitted_blocks


def _split(effort: int, count: int, session: int) -> List[int]:
    remaining = int(effort)
    lengths = []
    for _ in range(count - 1):
        chunk = min(session, remaining)
        lengths.append(chunk)
        remaining -= chunk
    lengths.append(max(25, remaining))
    return lengths


def _even(items: Sequence[datetime], count: int) -> List[datetime]:
    if count >= len(items):
        return list(items)
    if count == 1:
        return [items[-1]]
    chosen = []
    last = len(items) - 1
    for index in range(count):
        chosen.append(items[(index * last) // (count - 1)])
    return chosen


def _ceil_hour(moment: datetime) -> datetime:
    trimmed = moment.replace(minute=0, second=0, microsecond=0)
    if trimmed < moment:
        return trimmed + timedelta(hours=1)
    return trimmed


def _fit_one(start: datetime, duration: int, now: datetime, cutoff: datetime) -> Optional[Block]:
    end = start + timedelta(minutes=duration)
    if start < now:
        start = _ceil_hour(now)
        end = start + timedelta(minutes=duration)
    if end > cutoff:
        end = cutoff
        start = end - timedelta(minutes=duration)
    if start < now:
        start = now
    if end > cutoff or end <= start or (end - start) < timedelta(minutes=30):
        return None
    return start, end
