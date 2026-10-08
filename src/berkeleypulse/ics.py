from __future__ import annotations

from icalendar import Calendar, Event

from berkeleypulse.sync import parse_dt


def build_calendar(rows) -> bytes:
    calendar = Calendar()
    calendar.add("prodid", "-//Pulse//EN")
    calendar.add("version", "2.0")
    calendar.add("x-wr-calname", "Pulse")
    for row in rows:
        start = parse_dt(row["starts_at"])
        end = parse_dt(row["ends_at"])
        if start is None or end is None:
            continue
        event = Event()
        event.add("uid", "pulse-%s@pulse.local" % row["id"])
        event.add("summary", row["title"])
        event.add("dtstart", start)
        event.add("dtend", end)
        description = row["details"] or ""
        if row["code"]:
            description = ("%s\n%s" % (row["code"], description)).strip()
        event.add("description", description)
        calendar.add_component(event)
    return calendar.to_ical()
