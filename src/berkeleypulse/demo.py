from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from berkeleypulse.config import load_settings
from berkeleypulse.db import db
from berkeleypulse.mail import score_email
from berkeleypulse.sync import _replace_assignments, _upsert_course, rebuild_schedule, reindex_course

MONTHS = [
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def seed_demo(now: Optional[datetime] = None) -> bool:
    settings = load_settings()
    tz = ZoneInfo(settings.timezone)
    now = (now or datetime.now(tz)).astimezone(tz)
    with db() as conn:
        existing = conn.execute("SELECT COUNT(*) AS n FROM courses WHERE origin = 'demo'").fetchone()["n"]
        if existing:
            return False
        midterm = _at(now, 12, 19, 0)
        final = _at(now, 70, 8, 0)
        courses = [
            _cs61a(midterm, final),
            _data8(),
            _math54(),
        ]
        for spec in courses:
            course_id = _upsert_course(
                conn,
                origin="demo",
                external_id=spec["external_id"],
                code=spec["code"],
                name=spec["name"],
                term=_term(now),
                syllabus=spec["syllabus"],
                now=now,
            )
            _replace_assignments(conn, course_id, spec["assignments"](now))
            reindex_course(conn, settings, course_id, now=now)
        rebuild_schedule(conn, settings, now=now)
        _emails(conn, now)
        return True


def clear_demo() -> None:
    with db() as conn:
        conn.execute("DELETE FROM courses WHERE origin = 'demo'")
        conn.execute("DELETE FROM emails WHERE demo = 1")


def _term(now: datetime) -> str:
    if now.month >= 8:
        season = "Fall"
    elif now.month >= 5:
        season = "Summer"
    else:
        season = "Spring"
    return "%s %s" % (season, now.year)


def _at(now: datetime, days: int, hour: int, minute: int) -> datetime:
    day = now.date() + timedelta(days=days)
    return datetime.combine(day, time(hour, minute), tzinfo=now.tzinfo)


def _spoken(moment: datetime, clock: str) -> str:
    return "%s %s, %s at %s" % (MONTHS[moment.month], moment.day, moment.year, clock)


def _cs61a(midterm: datetime, final: datetime) -> dict:
    syllabus = """
CS 61A grading

Homework is worth 20%.
Projects are worth 30%.
Quizzes are worth 10%.
The midterm is worth 15%.
The final is worth 25%.

The late penalty for homework is 10% when it is up to 24 hours late.
Projects cannot be submitted late.

We will drop the lowest quiz score at the end of the semester.

The midterm is on __MIDTERM__.
The final is on __FINAL__.
""".strip().replace("__MIDTERM__", _spoken(midterm, "7:00pm")).replace("__FINAL__", _spoken(final, "8:00am"))
    return {
        "external_id": "demo:cs61a",
        "code": "CS 61A",
        "name": "Structure and Interpretation of Computer Programs",
        "syllabus": syllabus,
        "assignments": lambda now: [
            {
                "external_id": "demo:cs61a:project2",
                "name": "Project 2",
                "due_at": _at(now, 1, 23, 59).isoformat(),
                "points": 20,
                "description": "This project cannot be submitted late. The syllabus late window does not apply to Project 2.",
                "group_name": "Projects",
                "group_weight": 30,
            },
            {
                "external_id": "demo:cs61a:quiz3",
                "name": "Quiz 3",
                "due_at": _at(now, 6, 23, 59).isoformat(),
                "points": 10,
                "description": "Quiz 3 covers trees.",
                "group_name": "Quizzes",
                "group_weight": 10,
            },
        ],
    }


def _data8() -> dict:
    syllabus = """
Data C8 grading

Labs are worth 10%.
Homeworks are worth 20%.
Projects are worth 25%.
The midterm is worth 20%.
The final is worth 25%.

Labs close at the deadline. There is no late submission for a lab.
We will drop the lowest lab score.
""".strip()
    return {
        "external_id": "demo:data8",
        "code": "DATA C8",
        "name": "Foundations of Data Science",
        "syllabus": syllabus,
        "assignments": lambda now: [
            {
                "external_id": "demo:data8:lab3",
                "name": "Lab 3",
                "due_at": _at(now, 2, 23, 59).isoformat(),
                "points": 10,
                "description": "Labs are scored on effort. There is no late penalty because labs close at the deadline.",
                "group_name": "Labs",
                "group_weight": 10,
            }
        ],
    }


def _math54() -> dict:
    syllabus = """
Math 54 grading

Homework is worth 20%.
Quizzes are worth 15%.
The midterm is worth 25%.
The final is worth 40%.

Homework submitted late loses 10% per day.
""".strip()
    return {
        "external_id": "demo:math54",
        "code": "MATH 54",
        "name": "Linear Algebra and Differential Equations",
        "syllabus": syllabus,
        "assignments": lambda now: [
            {
                "external_id": "demo:math54:ps4",
                "name": "Problem Set 4 (PS4)",
                "due_at": _at(now, 4, 23, 59).isoformat(),
                "points": 15,
                "description": "The late penalty on Problem Set 4 (PS4) is 15% per day. No credit is given after 48 hours.",
                "group_name": "Homework",
                "group_weight": 20,
            }
        ],
    }


def _emails(conn, now: datetime) -> None:
    specs = [
        (
            "demo-mail-project",
            "cs61a@berkeley.edu",
            "CS 61A Staff",
            "CS 61A: Project 2 due tomorrow",
            "Submit Project 2 on bCourses before the deadline tomorrow night.",
            -2,
        ),
        (
            "demo-mail-grade",
            "notifications@instructure.com",
            "bCourses",
            "Grade posted: Data C8 Lab 3",
            "A grade was posted for Lab 3.",
            -5,
        ),
        (
            "demo-mail-room",
            "math54-staff@berkeley.edu",
            "Math 54 Staff",
            "MATH 54 midterm room change",
            "The midterm room is now Hearst Field Annex A1.",
            -26,
        ),
        (
            "demo-mail-news",
            "news@berkeley.edu",
            "Campus Events",
            "Campus events newsletter",
            "Unsubscribe here. A few club meetings are due to be announced this week.",
            -30,
        ),
    ]
    courses = {
        row["code"]: row["id"]
        for row in conn.execute("SELECT id, code FROM courses WHERE origin = 'demo'")
    }
    for message_id, addr, name, subject, body, hours in specs:
        score, reason = score_email(subject, addr, body)
        course_id = None
        blob = subject + body
        for code, course_id_value in courses.items():
            if code.lower() in blob.lower() or code.replace(" ", "").lower() in blob.replace(" ", "").lower():
                course_id = course_id_value
                break
        sent = now + timedelta(hours=hours)
        conn.execute(
            """
            INSERT INTO emails(
              message_id, from_addr, from_name, subject, sent_at, snippet, body,
              score, reason, course_id, demo, dismissed, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?)
            """,
            (
                message_id,
                addr,
                name,
                subject,
                sent.isoformat(),
                body[:240],
                body,
                score,
                reason,
                course_id,
                now.isoformat(),
            ),
        )
