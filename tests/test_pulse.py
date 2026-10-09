from datetime import datetime, date, time, timedelta

from stripe import InvalidRequestError
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from berkeleypulse.app import create_app
from berkeleypulse.billing import apply_event, billing_state, checkout_params, push_catalog
from berkeleypulse.canvas import next_url
from berkeleypulse.cli import main
from berkeleypulse.config import load_settings
from berkeleypulse.db import db, meta_get
from berkeleypulse.demo import seed_demo
from berkeleypulse.mail import score_email
from berkeleypulse.qa import answer_question
from berkeleypulse.schedule import estimate_effort_minutes, plan_blocks
from berkeleypulse.syllabus import heuristic_parse
from berkeleypulse.sync import add_manual_course, reindex_course, reschedule
from berkeleypulse.textutil import html_to_text

SYLLABUS = """
Homework is worth 20%.
Projects are worth 30%.
Quizzes are worth 10%.
The midterm is worth 15%.
The final is worth 25%.

The late penalty for homework is 10% when it is up to 24 hours late.
Projects cannot be submitted late.

We will drop the lowest quiz score at the end of the semester.

The midterm is on October 15, 2026 at 7:00pm.
The final is on December 16, 2026 at 8:00am.
Project 2 is due October 3, 2026 at 11:59pm.
"""

TZ = ZoneInfo("America/Los_Angeles")


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSE_DATA_DIR", str(tmp_path))
    for key in (
        "PULSE_AUTH_TOKEN",
        "PULSE_CANVAS_TOKEN",
        "PULSE_IMAP_PASSWORD",
        "PULSE_IMAP_USER",
        "PULSE_LLM_API_KEY",
        "PULSE_ALLOW_OPEN",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PULSE_POSTHOG_KEY", "")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "")
    monkeypatch.setenv("STRIPE_PRICE_ID", "")
    monkeypatch.setenv("STRIPE_PORTAL_CONFIGURATION", "")
    monkeypatch.setenv("STRIPE_BILLING", "")
    return tmp_path


def test_scores_deadline_mail_and_skips_newsletters():
    due, _ = score_email(
        "CS 61A: Project 2 due tomorrow",
        "cs61a@berkeley.edu",
        "Submit on bCourses.",
    )
    grade, _ = score_email(
        "Grade posted: Data C8 Lab 3",
        "notifications@instructure.com",
        "A grade was posted.",
    )
    room, _ = score_email(
        "MATH 54 midterm room change",
        "math54-staff@berkeley.edu",
        "The room changed.",
    )
    news, reason = score_email(
        "Campus events newsletter",
        "news@berkeley.edu",
        "Unsubscribe here. A few meetings are due to be announced.",
    )
    assert due >= 60
    assert grade >= 60
    assert room >= 40
    assert news < 40
    assert "newsletter" in reason
    chat, chat_reason = score_email(
        "Coffee chat Thursday",
        "advisor@berkeley.edu",
        "Want to meet this week?",
    )
    assert chat >= 40
    assert "coffee chat" in chat_reason
    canceled, canceled_reason = score_email(
        "CS 61A class canceled Friday",
        "cs61a@berkeley.edu",
        "No lecture. The discussion is also cancelled.",
    )
    assert canceled >= 60
    reasons = [part.strip() for part in canceled_reason.split(",")]
    assert "class canceled" in reasons
    assert "canceled" not in reasons
    meeting, meeting_reason = score_email(
        "Quick note",
        "ta@berkeley.edu",
        "Can we set a meeting tomorrow afternoon?",
    )
    assert meeting >= 40
    assert "meeting" in meeting_reason


def test_ratings_teach_later_mail_where_to_sit():
    from berkeleypulse.mail import apply_email_rating, one_sentence, place_mail

    assert one_sentence("First line of the note. The rest stays out of the way.") == "First line of the note."
    assert place_mail(80, "", 0, []) == "show"
    assert place_mail(10, "", 0, []) == "quiet"
    assert place_mail(80, "less", 0, []) == "quiet"
    assert place_mail(10, "more", 0, []) == "show"
    assert place_mail(80, "", -2, []) == "ignore"
    assert place_mail(80, "", 0, [-1]) == "quiet"
    assert place_mail(80, "", 0, [-4]) == "ignore"
    with db() as conn:
        conn.execute(
            """
            INSERT INTO emails(
              message_id, from_addr, from_name, subject, sent_at, snippet, body,
              score, reason, demo, dismissed, created_at
            ) VALUES('learn-1', 'club@berkeley.edu', 'Club', 'Weekly mixer', ?, 'First line of the note. Bring a friend.', 'First line of the note. Bring a friend.', 70, 'School sender', 0, 0, ?)
            """,
            ("2026-10-01T12:00:00-07:00", "2026-10-01T12:00:00-07:00"),
        )
        email_id = conn.execute("SELECT id FROM emails WHERE message_id = 'learn-1'").fetchone()["id"]
        apply_email_rating(conn, email_id, "ignore")
    client = TestClient(create_app())
    page = client.get("/")
    assert "Weekly mixer" in page.text
    assert "Marked ignored" in page.text
    assert "Bring a friend" not in page.text
    email_id = None
    with db() as conn:
        email_id = conn.execute("SELECT id FROM emails WHERE message_id = 'learn-1'").fetchone()["id"]
    gone = client.post("/emails/%s/dismiss" % email_id, follow_redirects=False)
    assert gone.status_code == 303
    assert "deleted=email-" in gone.headers["location"]
    deleted = client.get(gone.headers["location"])
    assert "Deleted!" in deleted.text
    assert "Weekly mixer" not in deleted.text
    client.post("/emails/%s/restore" % email_id)
    restored = client.get("/")
    assert "Weekly mixer" in restored.text


def test_syllabus_parser_extracts_policy_and_dates():
    model = heuristic_parse(SYLLABUS, now=datetime(2026, 10, 1, tzinfo=TZ))
    weights = {item["name"].lower(): item["weight"] for item in model["grading"]}
    assert weights["homework"] == 20
    assert weights["final"] == 25
    assert sum(weights.values()) == 100
    assert "10%" in model["late_policy"]
    assert any("lowest quiz" in rule.lower() for rule in model["drop_rules"])
    exams = {item["name"]: item["starts_at"] for item in model["exams"]}
    assert exams["Midterm"].startswith("2026-10-15T19:00:00")
    assert exams["Final"].startswith("2026-12-16T08:00:00")
    assert model["deadlines"][0]["name"] == "Project 2"
    assert model["deadlines"][0]["due_at"].startswith("2026-10-03T23:59:00")


def test_bullet_syllabus_keeps_both_midterms_and_the_grade_lines():
    text = """
Your grade will be based on five components:
Course Statements (Beginning and Ending): (10%)
Beginning statement (5%): Due Friday, September 4, 2026 at 11:59PM
Ending statement (5%): Due Sunday, December 13, 2026 at 11:59PM
Survey Research Analysis: (15%)
Due: Friday, December 11, 2026 at 11:59PM
Midterm exams: (25%)
Two midterm exams: September 30, 2026 & October 28, 2026 in class
Final exam: (30%)
Date: Friday, December 18, 2026 during the Final Exam Period
Attendance and Participation: (20%)
Late work will be accepted with penalties. For each day that an assignment is late, it will lose half of a letter grade (5%).
An 89.51% will become a 90%.
"""
    model = heuristic_parse(text, now=datetime(2026, 10, 1, tzinfo=TZ))
    weights = {item["name"].lower(): item["weight"] for item in model["grading"]}
    assert weights["beginning statement"] == 5
    assert weights["survey research analysis"] == 15
    assert weights["midterm exams"] == 25
    assert weights["final exam"] == 30
    assert weights["attendance and participation"] == 20
    assert "course statements" not in weights
    assert sum(weights.values()) == 100
    assert "51" not in {item["name"] for item in model["grading"]}
    exams = {item["name"]: item["starts_at"] for item in model["exams"]}
    assert exams["Midterm 1"].startswith("2026-09-30T17:00:00")
    assert exams["Midterm 2"].startswith("2026-10-28T17:00:00")
    assert exams["Final"].startswith("2026-12-18T08:00:00")
    dues = {item["name"].lower(): item["due_at"] for item in model["deadlines"]}
    assert dues["beginning statement"].startswith("2026-09-04T23:59:00")
    assert dues["ending statement"].startswith("2026-12-13T23:59:00")
    assert dues["survey research analysis"].startswith("2026-12-11T23:59:00")
    assert "letter grade" in model["late_policy"].lower()


def test_html_and_canvas_link_header():
    assert "Homework" in html_to_text("<p>Homework</p><script>secret</script>")
    assert "secret" not in html_to_text("<p>Homework</p><script>secret</script>")
    header = '<https://bcourses.berkeley.edu/api/v1/courses?page=2>; rel="next", <https://bcourses.berkeley.edu/api/v1/courses?page=1>; rel="prev"'
    assert next_url(header).endswith("page=2")
    assert next_url(None) is None


def test_crammer_studies_the_evening_before_and_spacer_spreads():
    now = datetime(2026, 10, 1, 9, 0, tzinfo=TZ)
    due = datetime(2026, 10, 8, 23, 59, tzinfo=TZ)
    crammer = plan_blocks(due, now, 150, "crammer", 50, TZ)
    assert crammer == [
        (
            datetime(2026, 10, 7, 18, 0, tzinfo=TZ),
            datetime(2026, 10, 7, 20, 30, tzinfo=TZ),
        )
    ]
    morning = datetime(2026, 10, 2, 8, 0, tzinfo=TZ)
    before_morning = plan_blocks(morning, now, 150, "crammer", 50, TZ)
    assert before_morning[0][0] == datetime(2026, 10, 1, 18, 0, tzinfo=TZ)

    spacer = plan_blocks(due, now, 150, "spacer", 50, TZ)
    assert [block[0].date() for block in spacer] == [
        date(2026, 10, 2),
        date(2026, 10, 4),
        date(2026, 10, 7),
    ]
    assert all(block[0].hour == 18 for block in spacer)
    assert all((block[1] - block[0]).total_seconds() == 50 * 60 for block in spacer)

    late = datetime(2026, 10, 7, 19, 30, tzinfo=TZ)
    shifted = plan_blocks(datetime(2026, 10, 8, 23, 59, tzinfo=TZ), late, 60, "crammer", 50, TZ)
    assert shifted[0][0] == datetime(2026, 10, 7, 20, 0, tzinfo=TZ)
    assert plan_blocks(datetime(2026, 10, 8, 23, 30, tzinfo=TZ), datetime(2026, 10, 8, 23, 0, tzinfo=TZ), 60, "crammer", 50, TZ) == []
    stacked_now = datetime(2026, 10, 1, 16, 11, tzinfo=TZ)
    stacked = plan_blocks(datetime(2026, 10, 2, 23, 59, tzinfo=TZ), stacked_now, 180, "spacer", 50, TZ)
    assert [block[0] for block in stacked] == [
        datetime(2026, 10, 1, 17, 0, tzinfo=TZ),
        datetime(2026, 10, 1, 17, 50, tzinfo=TZ),
        datetime(2026, 10, 1, 18, 40, tzinfo=TZ),
        datetime(2026, 10, 1, 19, 30, tzinfo=TZ),
    ]
    assert estimate_effort_minutes("Project 2", 20) == 180
    assert estimate_effort_minutes("Quiz 3", 10) == 45
    assert estimate_effort_minutes("Homework 1", 10) == 60


def test_syllabus_is_parsed_once():
    add_manual_course("CS 61A", "Structure and Interpretation", SYLLABUS)
    with db() as conn:
        course_id = conn.execute("SELECT id FROM courses").fetchone()["id"]
        assert reindex_course(conn, load_settings(), course_id) == "cached"
        assert meta_get(conn, "parses") == "1"
        assert int(meta_get(conn, "cache_hits")) >= 1


def test_unchanged_file_folder_stays_closed_and_a_cancelation_is_kept_once():
    from berkeleypulse.filescan import nodes_to_open, sync_course_files, worth_reading
    from berkeleypulse.notices import classify_update, store_notices
    from berkeleypulse.sync import rebuild_schedule

    assert worth_reading("Week 1 syllabus.pdf", 1200)
    assert worth_reading("Midterm 1 Study Guide.pdf", 1200)
    assert worth_reading("Topic 1 - blank.pdf", 1200)
    assert not worth_reading("Week 1 lecture.pptx", 1200)
    assert not worth_reading("Zaller 1992 The Nature of Opinion.pdf", 4000)
    from berkeleypulse.filescan import is_assigned_article

    assert is_assigned_article(
        "reading.pdf",
        "Abstract\nThis paper appears in the Journal of Politics.\nDOI: 10.1000/example",
    )
    assert not is_assigned_article(
        "reading.pdf",
        "This course meets Monday. Your grade is based on the midterm. Office hours are Tuesday.",
    )
    stored = {"folder:1": "2026-08-22T00:00:00Z"}
    weeks = [
        {"id": "folder:1", "canvas_id": "1", "name": "Week 1", "kind": "folder", "updated_at": "2026-08-22T00:00:00Z"},
        {"id": "folder:2", "canvas_id": "2", "name": "Week 2", "kind": "folder", "updated_at": "2026-09-21T00:00:00Z"},
    ]
    assert [item["name"] for item in nodes_to_open(stored, weeks)] == ["Week 2"]

    now = datetime(2026, 10, 2, 12, tzinfo=TZ)
    canceled = classify_update(
        "Section canceled Friday",
        "Class canceled on October 3, 2026 at 2:00pm.",
        now,
    )
    assert canceled["kind"] == "cancel"
    assert canceled["starts_at"].startswith("2026-10-03T14:00:00")
    moved = classify_update("Room change", "Discussion moved to October 4, 2026 at 3:00pm.", now)
    assert moved["kind"] == "move"

    calls = []

    def fetch(folder_id):
        calls.append(folder_id)
        if folder_id == "root":
            return weeks
        if folder_id == "2":
            return [
                {
                    "id": "file:9",
                    "canvas_id": "9",
                    "name": "syllabus.pdf",
                    "kind": "file",
                    "updated_at": "2026-09-21T00:00:00Z",
                    "size": 10,
                }
            ]
        raise AssertionError(folder_id)

    def read(_node):
        return "The project is due October 8, 2026 at 11:59pm."

    course_id = add_manual_course("DATA C8", "Foundations of Data Science", SYLLABUS)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO file_stamps(course_id, external_id, name, kind, updated_at)
            VALUES(?, 'folder:1', 'Week 1', 'folder', '2026-08-22T00:00:00Z')
            """,
            (course_id,),
        )
        skipped, opened, fresh = sync_course_files(conn, course_id, "root", fetch, read)
        assert skipped == 1 and opened == 1 and fresh
        assert calls == ["root", "2"]
        calls.clear()
        skipped, opened, fresh = sync_course_files(conn, course_id, "root", fetch, read)
        assert calls == ["root"]
        assert skipped == 2 and opened == 0 and not fresh
        item = {
            "id": 5,
            "title": "Section canceled Friday",
            "message": "Class canceled on October 3, 2026 at 2:00pm.",
            "posted_at": "2026-10-02T18:00:00Z",
            "updated_at": "2026-10-02T18:00:00Z",
        }
        assert store_notices(conn, course_id, [item], now=now) == 1
        assert store_notices(conn, course_id, [item], now=now) == 0
        reindex_course(conn, load_settings(), course_id, now=now)
        from berkeleypulse.filescan import apply_file_models

        apply_file_models(conn, now)
        apply_file_models(conn, now)
        rebuild_schedule(conn, load_settings(), now=now)
        titles = [row["title"] for row in conn.execute("SELECT title, kind FROM events")]
        assert any(row["kind"] == "cancel" for row in conn.execute("SELECT kind FROM events"))
        assert "DATA C8 · Class canceled" in titles
        deadlines = conn.execute("SELECT starts_at FROM events WHERE kind = 'deadline'").fetchall()
        assert any(row["starts_at"].startswith("2026-10-08") for row in deadlines)


def test_an_unread_pdf_is_opened_even_when_the_folder_date_is_unchanged():
    from berkeleypulse.filescan import sync_course_files

    course_id = add_manual_course("POLSCI 161", "Public Opinion", SYLLABUS)
    calls = []

    def fetch(folder_id):
        calls.append(folder_id)
        if folder_id == "root":
            return [
                {
                    "id": "folder:syllabus",
                    "canvas_id": "syllabus",
                    "name": "Syllabus",
                    "kind": "folder",
                    "updated_at": "2026-08-22T00:00:00Z",
                }
            ]
        return [
            {
                "id": "file:guide",
                "canvas_id": "guide",
                "name": "Midterm 1 Study Guide.pdf",
                "kind": "file",
                "updated_at": "2026-09-17T00:00:00Z",
                "size": 20,
            }
        ]

    def read(_node):
        return "Midterm 2 is on October 28, 2026 at 5:00pm."

    with db() as conn:
        conn.execute(
            """
            INSERT INTO file_stamps(course_id, external_id, name, kind, updated_at)
            VALUES(?, 'folder:syllabus', 'Syllabus', 'folder', '2026-08-22T00:00:00Z'),
                   (?, 'file:guide', 'Midterm 1 Study Guide.pdf', 'file', '2026-09-17T00:00:00Z')
            """,
            (course_id, course_id),
        )
        skipped, opened, fresh = sync_course_files(conn, course_id, "root", fetch, read)
        assert calls == ["root", "syllabus"]
        assert opened == 1 and fresh
        assert skipped == 0
        body = conn.execute("SELECT body FROM file_bodies WHERE external_id = 'file:guide'").fetchone()["body"]
        assert "October 28" in body
        calls.clear()
        sync_course_files(conn, course_id, "root", fetch, read)
        assert calls == ["root"]


def test_questions_cite_the_matching_policy_line():
    seed_demo(now=datetime(2026, 10, 1, 9, tzinfo=TZ))
    with db() as conn:
        dropped = answer_question(conn, "Can I drop the lowest quiz?", load_settings(), None)
        penalty = answer_question(conn, "What's the late penalty on PS4?", load_settings(), None)
        project = answer_question(conn, "Can I submit Project 2 late?", load_settings(), None)
    assert "lowest quiz" in dropped.text.lower()
    assert "CS 61A" in dropped.citations[0].source
    assert all("covers trees" not in item.quote.lower() for item in dropped.citations)
    assert all("lab" not in item.quote.lower() for item in dropped.citations)
    assert "15%" in penalty.text
    assert "PS4" in penalty.citations[0].source
    assert "cannot be submitted late" in project.text.lower()


def test_demo_digest_hides_newsletters_and_serves_calendar():
    seed_demo(now=datetime(2026, 10, 1, 9, tzinfo=TZ))
    with db() as conn:
        conn.execute(
            """
            INSERT INTO emails(
              message_id, from_addr, from_name, subject, sent_at, snippet, body,
              score, reason, course_id, demo, dismissed, created_at
            ) VALUES('demo-quiet', 'reader@berkeley.edu', 'Library', 'Reading group notes', ?, 'Notes from the group.', 'Notes from the group.', 0, 'no priority signals', NULL, 1, 0, ?)
            """,
            ("2026-10-01T16:00:00-07:00", "2026-10-01T16:00:00-07:00"),
        )
    client = TestClient(create_app())
    page = client.get("/")
    assert page.status_code == 200
    assert "Project 2 due tomorrow" in page.text
    assert "Grade posted" in page.text
    assert "room change" in page.text
    assert "Campus events newsletter" not in page.text
    assert "Quieter" in page.text
    assert "Reading group notes" in page.text
    assert "Session" in page.text
    assert "Download calendar" in page.text
    assert page.text.count("calendar.ics") == 1
    opened = client.get("/calendar.ics", headers={"accept": "text/html"}, follow_redirects=False)
    assert opened.status_code == 303
    assert opened.headers["location"] == "/"
    asked = client.get("/", params={"q": "Can I drop the lowest quiz?"})
    assert "lowest quiz" in asked.text.lower()
    assert "sentence" in asked.text
    calendar = client.get("/calendar.ics")
    assert "BEGIN:VEVENT" in calendar.text
    digest = client.get("/digest.json").json()
    assert any("Project 2" in item["subject"] for item in digest["important"])
    assert all("newsletter" not in item["subject"].lower() for item in digest["important"])
    month = client.get("/", params={"month": "2026-10", "day": "2026-10-02"})
    assert "October 2026" in month.text
    assert "month=2026-09" in month.text
    assert "month=2026-11" in month.text
    assert "Recent" in month.text
    assert "Project 2 due tomorrow" in month.text
    assert month.text.count("calendar.ics") == 1
    assert client.get("/", params={"month": "nope"}).status_code == 200
    week = client.get("/", params={"view": "week", "week": "2026-10-09"})
    assert "October 2026" in week.text
    assert "W41" in week.text
    assert "Someday" in week.text
    assert 'data-day="2026-10-05"' in week.text
    assert week.text.count('class="week-line"') == 60
    saved = client.post(
        "/notes",
        data={"day": "2026-10-09", "slot": "0", "body": "Remember to save"},
        headers={"Accept": "application/json"},
    )
    assert saved.json()["ok"] is True
    again = client.post(
        "/notes",
        data={"day": "2026-10-09", "slot": "0", "body": "Remember to save"},
        headers={"Accept": "application/json"},
    )
    assert again.json()["body"] == "Remember to save"
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM day_notes").fetchone()["n"] == 1
    shown = client.get("/", params={"view": "week", "week": "2026-10-05"})
    assert "Remember to save" in shown.text


def test_crammer_setting_rebuilds_study_blocks():
    seed_demo(now=datetime(2026, 10, 1, 9, tzinfo=TZ))
    client = TestClient(create_app())
    saved = client.post(
        "/settings",
        data={
            "canvas_base_url": "https://bcourses.berkeley.edu",
            "imap_host": "imap.gmail.com",
            "imap_port": "993",
            "imap_folder": "INBOX",
            "work_style": "crammer",
            "session_minutes": "50",
            "poll_minutes": "15",
            "llm_base_url": "https://api.openai.com/v1",
            "llm_model": "gpt-4o-mini",
            "timezone_name": "America/Los_Angeles",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    page = client.get("/", params={"view": "month"})
    assert "One study block the day before" in page.text
    assert load_settings().work_style == "crammer"


def test_manual_course_from_the_form():
    client = TestClient(create_app())
    created = client.post(
        "/courses",
        data={"code": "CS 61A", "name": "SICP", "syllabus": SYLLABUS},
        follow_redirects=True,
    )
    assert created.status_code == 200
    assert "lowest quiz" in created.text.lower()
    assert "Homework" in created.text


def test_auth_token_locks_a_public_server():
    client = TestClient(create_app())
    assert main(["serve", "--host", "0.0.0.0"]) == 2


def test_login_page_takes_a_berkeley_email_only():
    client = TestClient(create_app())
    page = client.get("/login")
    assert "Your email, schoolwork," in page.text
    assert "And calendar" in page.text
    assert "All in one place." in page.text
    assert "Enough browser hopping: school's hard enough." in page.text
    assert "By Berkeley. For Berkeley." in page.text
    assert "@berkeley.edu only" in page.text
    assert "Gradescope" in page.text
    assert "Data 8" in page.text
    refused = client.post("/login/email", data={"email": "oski@gmail.com"})
    assert refused.status_code == 200
    assert "Use your @berkeley.edu address." in refused.text
    subdomain = client.post("/login/email", data={"email": "ada@eecs.berkeley.edu"})
    assert "Use your @berkeley.edu address." in subdomain.text
    saved = client.post("/login/email", data={"email": "Ada.Lovelace@Berkeley.edu"})
    assert 'name="code"' in saved.text
    assert "Enter the code for ada.lovelace@berkeley.edu." in saved.text
    with db() as conn:
        row = conn.execute("SELECT email, created_at FROM interest").fetchone()
    assert row["email"] == "ada.lovelace@berkeley.edu"
    again = client.post(
        "/login/email",
        data={"email": "ada.lovelace@berkeley.edu"},
        headers={"Accept": "application/json"},
    )
    assert again.json() == {"ok": True, "email": "ada.lovelace@berkeley.edu"}
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM interest").fetchone()["n"] == 1
        assert conn.execute("SELECT created_at FROM interest").fetchone()["created_at"] == row["created_at"]


def test_login_wall(monkeypatch):
    monkeypatch.setenv("PULSE_AUTH_TOKEN", "secret-token")
    client = TestClient(create_app())
    blocked = client.get("/", follow_redirects=False)
    assert blocked.status_code == 303
    assert blocked.headers["location"] == "/login"
    interest = client.post("/login/email", data={"email": "oski@berkeley.edu"})
    assert interest.status_code == 200
    assert "Enter the code for oski@berkeley.edu." in interest.text
    assert client.get("/health").status_code == 200
    calendar = client.get("/calendar.ics", params={"token": "secret-token"})
    assert calendar.status_code == 200
    bad = client.post("/login", data={"token": "nope"})
    assert "does not match" in bad.text
    good = client.post("/login", data={"token": "secret-token"}, follow_redirects=False)
    assert good.status_code == 303
    assert client.get("/").status_code == 200


def test_live_assignment_text_does_not_reparse_static_policy():
    add_manual_course("CS 61A", "Structure and Interpretation", SYLLABUS)
    with db() as conn:
        course_id = conn.execute("SELECT id FROM courses").fetchone()["id"]
        conn.execute(
            """
            INSERT INTO assignments(
              course_id, external_id, name, due_at, points, description, group_name, group_weight
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (course_id, "ps4", "PS4", None, 10, "The late penalty on PS4 is 15% per day.", "Homework", 20),
        )
        assert reindex_course(conn, load_settings(), course_id) == "cached"
        assert meta_get(conn, "parses") == "1"
        row = conn.execute(
            "SELECT stability FROM documents WHERE locator LIKE 'description%'"
        ).fetchone()
        assert row["stability"] == "live"
        static = conn.execute(
            "SELECT stability FROM documents WHERE source LIKE '%syllabus' LIMIT 1"
        ).fetchone()
        assert static["stability"] == "static"


def test_scan_labels_live_and_static_and_drops_the_old_name():
    seed_demo(now=datetime(2026, 10, 1, 9, tzinfo=TZ))
    client = TestClient(create_app())
    page = client.get("/")
    assert "Berkeley Pulse" not in page.text
    assert "Scan now" in page.text
    assert "Live" in page.text
    assert "Static" in page.text
    scanned = client.post("/scan", follow_redirects=True)
    assert "Scan finished" in scanned.text
    assert "Static:" in scanned.text
    assert "Mail is not connected" in scanned.text
    calendar = client.get("/calendar.ics")
    assert "Pulse" in calendar.text
    assert "Berkeley Pulse" not in calendar.text


def test_posthog_stays_off_until_a_key_is_set():
    page = TestClient(create_app()).get("/")
    assert "posthog.init" not in page.text


def test_posthog_ignores_a_secret_key(monkeypatch):
    monkeypatch.setenv("PULSE_POSTHOG_KEY", "phs_secret")
    page = TestClient(create_app()).get("/")
    assert "posthog.init" not in page.text
    assert "phs_secret" not in page.text


def test_posthog_counts_pageviews_without_recording_the_page(monkeypatch):
    monkeypatch.setenv("PULSE_POSTHOG_KEY", "phc_test")
    monkeypatch.setenv("PULSE_POSTHOG_HOST", "https://us.i.posthog.com")
    page = TestClient(create_app()).get("/")
    assert "phc_test" in page.text
    assert "capture_pageview: true" in page.text
    assert "autocapture: false" in page.text
    assert "disable_session_recording: true" in page.text
    assert "posthog.capture('scan')" in page.text


def test_sign_in_page_is_the_click_through(monkeypatch):
    monkeypatch.setattr("berkeleypulse.app.open_desk", lambda target: "Window ready for %s." % target)
    monkeypatch.setattr("berkeleypulse.app.read_page_text", lambda: SYLLABUS)
    client = TestClient(create_app())
    page = client.get("/sign-in")
    assert page.status_code == 200
    assert "New Access Token" in page.text
    assert "Generate Token" in page.text
    assert "90 days" in page.text
    assert 'action="/sign-in/canvas"' in page.text
    assert "Allow bCourses" not in page.text
    assert "does not store your password" in page.text
    assert "Berkeley Pulse" not in page.text
    opened = client.post("/sign-in/open", data={"target": "calcentral"}, follow_redirects=True)
    assert "Window ready for calcentral" in opened.text
    saved = client.post(
        "/sign-in/page",
        data={"code": "CS 61A", "name": "SICP"},
        follow_redirects=True,
    )
    assert "lowest quiz" in saved.text.lower()
    assert "Static" in saved.text


def test_canvas_token_is_checked_before_it_is_saved(monkeypatch):
    monkeypatch.setattr("berkeleypulse.app.verify_canvas_token", lambda base, token: "Ada Lovelace")
    client = TestClient(create_app())
    saved = client.post("/sign-in/canvas", data={"canvas_token": "1042~secret"}, follow_redirects=True)
    assert "Ada Lovelace" in saved.text
    assert load_settings().canvas_token == "1042~secret"


def test_rejected_canvas_token_is_not_saved(monkeypatch):
    from berkeleypulse.canvas import CanvasError

    def reject(base, token):
        raise CanvasError("bCourses rejected that token. Generate a new one and paste it again.")

    monkeypatch.setattr("berkeleypulse.app.verify_canvas_token", reject)
    client = TestClient(create_app())
    saved = client.post("/sign-in/canvas", data={"canvas_token": "not-a-token"}, follow_redirects=True)
    assert "rejected" in saved.text
    assert load_settings().canvas_token == ""


def test_canvas_token_with_spaces_is_refused(monkeypatch):
    def explode(base, token):
        raise AssertionError(token)

    monkeypatch.setattr("berkeleypulse.app.verify_canvas_token", explode)
    client = TestClient(create_app())
    saved = client.post("/sign-in/canvas", data={"canvas_token": "1042~two words"}, follow_redirects=True)
    assert "no spaces" in saved.text
    assert load_settings().canvas_token == ""


def test_canvas_token_verification_reads_the_account_name(monkeypatch):
    from berkeleypulse.canvas import verify_canvas_token

    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        assert url == "https://bcourses.berkeley.edu/api/v1/users/self"
        assert headers["Authorization"] == "Bearer 1042~abc"
        assert follow_redirects is False

        class Response:
            status_code = 200
            is_redirect = False

            def json(self):
                return {"name": "Ada Lovelace"}

        return Response()

    monkeypatch.setattr("berkeleypulse.canvas.httpx.get", fake_get)
    assert verify_canvas_token("https://bcourses.berkeley.edu", "1042~abc") == "Ada Lovelace"


def test_saved_cookies_are_limited_to_school_sites():
    from berkeleypulse.desk import keep_cookie

    assert keep_cookie(".bcourses.berkeley.edu")
    assert keep_cookie("calcentral.berkeley.edu")
    assert keep_cookie(".google.com")
    assert not keep_cookie("evil.example")


def test_sign_in_reader_speaks_to_the_browser_socket():
    import base64
    import hashlib
    import json
    import socket
    import threading

    from berkeleypulse.desk import _cdp

    guid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    ready = threading.Event()
    holder = {}

    def serve():
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        holder["port"] = server.getsockname()[1]
        ready.set()
        conn, _ = server.accept()
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(4096)
            key = ""
            for line in data.decode().split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            accept = base64.b64encode(hashlib.sha1((key + guid).encode()).digest()).decode()
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    "Sec-WebSocket-Accept: %s\r\n"
                    "\r\n" % accept
                ).encode()
            )
            header = conn.recv(2)
            length = header[1] & 0x7F
            mask = conn.recv(4)
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(conn.recv(length)))
            assert json.loads(payload)["method"] == "Network.getAllCookies"
            body = json.dumps(
                {"id": 1, "result": {"cookies": [{"name": "canvas_session", "domain": ".bcourses.berkeley.edu"}]}}
            ).encode()
            conn.sendall(bytes([0x81, len(body)]) + body)
        finally:
            conn.close()
            server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    assert ready.wait(2)
    result = _cdp("ws://127.0.0.1:%d/devtools/browser/abc" % holder["port"], "Network.getAllCookies")
    thread.join(2)
    assert result["result"]["cookies"][0]["name"] == "canvas_session"


def test_gmail_feed_parses_without_a_password():
    from berkeleypulse.mail import parse_gmail_atom

    messages = parse_gmail_atom(
        """<?xml version="1.0"?>
        <feed xmlns="http://purl.org/atom/ns#">
          <entry>
            <title>CS 61A: Project 2 due tomorrow</title>
            <summary>Submit before the deadline.</summary>
            <issued>2026-10-01T19:00:00Z</issued>
            <id>tag:gmail:project-2</id>
            <author><name>CS 61A Staff</name><email>cs61a@berkeley.edu</email></author>
          </entry>
        </feed>
        """
    )
    assert messages[0].subject.startswith("CS 61A")
    assert messages[0].from_addr == "cs61a@berkeley.edu"
    assert messages[0].message_id == "tag:gmail:project-2"


def test_calcentral_counts_only_a_logged_in_status():
    from berkeleypulse.desk import calcentral_logged_in

    assert calcentral_logged_in({"isLoggedIn": True}) is True
    assert calcentral_logged_in({"isLoggedIn": False}) is False
    assert calcentral_logged_in("<html>CalCentral</html>") is False


def test_connection_state_is_green_only_after_a_passing_check():
    from berkeleypulse.desk import connection_state

    assert connection_state(False, False) == "off"
    assert connection_state(True, True) == "connected"
    assert connection_state(True, False) == "failed"


def test_connect_page_marks_a_failed_check(monkeypatch):
    monkeypatch.setattr(
        "berkeleypulse.app.access_report",
        lambda: {"canvas": "connected", "calcentral": "failed", "mail": "off", "window_open": False},
    )
    page = TestClient(create_app()).get("/sign-in")
    assert "pill ok" in page.text
    assert "Connected" in page.text
    assert "pill bad" in page.text
    assert "Failed" in page.text
    assert "was rejected" in page.text


def test_sign_in_opens_a_window_when_chrome_has_no_page(monkeypatch):
    from berkeleypulse.desk import _navigate

    calls = []

    def fake_get(path):
        if path == "/json/list":
            return []
        if path == "/json/version":
            return {"webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/x"}
        raise AssertionError(path)

    def fake_cdp(socket_url, method, params=None):
        calls.append((socket_url, method, params))
        if method == "Target.createTarget":
            return {"id": 1, "result": {"targetId": "tab-1"}}
        if method == "Browser.getWindowForTarget":
            return {"id": 1, "result": {"windowId": 4}}
        return {"id": 1, "result": {}}

    monkeypatch.setattr("berkeleypulse.desk._get_json", fake_get)
    monkeypatch.setattr("berkeleypulse.desk._cdp", fake_cdp)
    _navigate("https://bcourses.berkeley.edu")
    assert calls[0][1:] == (
        "Target.createTarget",
        {"url": "https://bcourses.berkeley.edu", "newWindow": True},
    )
    assert calls[1][1] == "Target.activateTarget"
    assert calls[2][1] == "Browser.getWindowForTarget"
    assert calls[3][1] == "Browser.setWindowBounds"
    assert calls[3][2]["bounds"]["windowState"] == "normal"


def test_sign_in_puts_the_url_on_a_blank_page(monkeypatch):
    from berkeleypulse.desk import _navigate

    calls = []

    def fake_get(path):
        if path == "/json/list":
            return [
                {
                    "type": "page",
                    "id": "page-1",
                    "url": "about:blank",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/page/page-1",
                }
            ]
        if path == "/json/version":
            return {"webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/x"}
        raise AssertionError(path)

    def fake_cdp(socket_url, method, params=None):
        calls.append((socket_url, method, params))
        if method == "Browser.getWindowForTarget":
            return {"id": 1, "result": {"windowId": 2}}
        return {"id": 1, "result": {}}

    monkeypatch.setattr("berkeleypulse.desk._get_json", fake_get)
    monkeypatch.setattr("berkeleypulse.desk._cdp", fake_cdp)
    _navigate("https://bcourses.berkeley.edu")
    assert calls[0] == (
        "ws://127.0.0.1:9333/devtools/page/page-1",
        "Page.navigate",
        {"url": "https://bcourses.berkeley.edu"},
    )


def test_save_reads_cookies_from_the_open_page(monkeypatch):
    from berkeleypulse.desk import DeskError, _all_cookies

    def fake_get(path):
        if path == "/json/list":
            return [
                {
                    "type": "page",
                    "url": "https://bcourses.berkeley.edu/",
                    "webSocketDebuggerUrl": "ws://page",
                }
            ]
        if path == "/json/version":
            return {"webSocketDebuggerUrl": "ws://browser"}
        raise AssertionError(path)

    def fake_cdp(socket_url, method, params=None):
        if socket_url == "ws://browser":
            raise DeskError("The sign-in window could not be read.")
        assert socket_url == "ws://page"
        assert method == "Network.getAllCookies"
        return {"id": 1, "result": {"cookies": [{"name": "canvas_session", "domain": "bcourses.berkeley.edu"}]}}

    monkeypatch.setattr("berkeleypulse.desk._get_json", fake_get)
    monkeypatch.setattr("berkeleypulse.desk._cdp", fake_cdp)
    cookies = _all_cookies()
    assert cookies[0]["name"] == "canvas_session"


def test_login_renders_when_the_project_data_dir_is_read_only(monkeypatch, tmp_path):
    from berkeleypulse.config import data_dir

    root = tmp_path / "app"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname = 'pulse'\n")
    (root / "src" / "berkeleypulse").mkdir(parents=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.delenv("PULSE_DATA_DIR", raising=False)
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr("berkeleypulse.config.project_root", lambda: root)
    root.chmod(0o555)
    try:
        page = TestClient(create_app()).get("/login")
        stored = data_dir()
    finally:
        root.chmod(0o755)
    assert page.status_code == 200
    assert stored == scratch / "pulse"
    assert (stored / "pulse.db").exists()


def test_permissions_are_read_from_the_local_file():
    from berkeleypulse.config import data_dir
    from berkeleypulse.desk import session_status

    (data_dir() / "session.json").write_text(
        '{"grants": {"canvas": true, "calcentral": false, "mail": true}, "cookies": []}'
    )
    status = session_status()
    assert status["canvas"] is True
    assert status["mail"] is True
    assert status["calcentral"] is False


def test_edit_reorders_classes_and_removes_their_dates():
    first = add_manual_course("ZZZ 1", "Later clock", SYLLABUS)
    second = add_manual_course("AAA 1", "Earlier clock", SYLLABUS)
    third = add_manual_course("MMM 1", "Also removed", SYLLABUS)
    now = datetime.now(TZ)
    day = (now + timedelta(days=3)).date()
    late = datetime.combine(day, time(18, 0), tzinfo=TZ)
    early = datetime.combine(day, time(9, 0), tzinfo=TZ)
    with db() as conn:
        conn.execute("DELETE FROM assignments")
        conn.execute(
            """
            INSERT INTO assignments(course_id, external_id, name, due_at, points, description)
            VALUES(?, 'essay', 'Essay', ?, 10, '')
            """,
            (first, late.isoformat()),
        )
        conn.execute(
            """
            INSERT INTO assignments(course_id, external_id, name, due_at, points, description)
            VALUES(?, 'quiz', 'Quiz', ?, 10, '')
            """,
            (second, early.isoformat()),
        )
        conn.execute(
            """
            INSERT INTO assignments(course_id, external_id, name, due_at, points, description)
            VALUES(?, 'paper', 'Paper', ?, 10, '')
            """,
            (third, early.isoformat()),
        )
        conn.execute(
            """
            INSERT INTO emails(
              message_id, from_addr, from_name, subject, sent_at, snippet, body,
              score, reason, course_id, demo, dismissed, created_at
            ) VALUES('zzz-mail', 'zzz@berkeley.edu', 'ZZZ', 'ZZZ 1 note', ?, 'Note', 'Note', 80, 'deadline', ?, 0, 0, ?)
            """,
            (now.isoformat(), first, now.isoformat()),
        )
    reschedule(now=now)
    client = TestClient(create_app())
    page = client.get("/", params={"view": "month"})
    courses_at = page.text.find('<ul class="courses">')
    assert page.text.find('href="/?editing=courses"') < courses_at
    assert page.text.find(">ZZZ 1<", courses_at) < page.text.find(">AAA 1<", courses_at)
    assert _deadline_at(page.text, "ZZZ 1 · Essay") < _deadline_at(page.text, "AAA 1 · Quiz")

    moved = client.post(
        "/courses/%s/move" % second,
        data={"direction": "up", "next_path": "/?view=month&editing=courses"},
        follow_redirects=True,
    )
    assert "Delete selected" in moved.text
    assert moved.text.find('aria-label="Move AAA 1 up"') < moved.text.find('aria-label="Move ZZZ 1 up"')
    assert _deadline_at(moved.text, "AAA 1 · Quiz") < _deadline_at(moved.text, "ZZZ 1 · Essay")

    detail = client.get("/courses/%s" % second)
    assert 'href="/courses/%s?editing=courses"' % second in detail.text

    empty = client.post("/courses/remove", data={"next_path": "/?editing=courses"}, follow_redirects=True)
    assert "Select the classes to delete." in empty.text

    removed = client.post(
        "/courses/remove",
        data={"delete": [str(first), str(third)], "next_path": "/?view=month&editing=courses"},
        follow_redirects=True,
    )
    assert "Removed 2 classes" in removed.text
    assert "ZZZ 1 · Essay" not in removed.text
    assert "MMM 1 · Paper" not in removed.text
    assert "AAA 1 · Quiz" in removed.text
    assert ">ZZZ 1<" not in removed.text
    assert ">MMM 1<" not in removed.text
    missing = client.get("/courses/%s" % first)
    assert missing.status_code == 404
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM assignments WHERE course_id = ?", (first,)).fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM documents WHERE course_id = ?", (first,)).fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM events WHERE title LIKE 'ZZZ 1%'").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM events WHERE title LIKE 'MMM 1%'").fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE title = 'AAA 1 · Quiz' AND kind = 'deadline'"
        ).fetchone()["n"] == 1
        hidden = conn.execute(
            "SELECT hidden, syllabus_text, model_json FROM courses WHERE id = ?",
            (first,),
        ).fetchone()
        assert hidden["hidden"] == 1
        assert hidden["syllabus_text"] == ""
        assert hidden["model_json"] is None
        mail = conn.execute("SELECT course_id FROM emails WHERE message_id = 'zzz-mail'").fetchone()
        assert mail["course_id"] is None


DATA8_PAGE = """
<div class="announcement" data-week="6" data-date="2026-09-28">
  <div class="announcement-body"><p>Project 1 checkpoint is due Friday.</p></div>
</div>
<div class="syllabus-container">
<h2 class="week-label">Week 1</h2>
<table><tbody>
<tr>
  <td>Wed, Aug 26</td>
  <td>
    <div class="syllabus-item"><strong class="label label-lab">Lab 1</strong><a href="/fa26/lab/1">Lab 1 (due 8/28)</a></div>
    <div class="syllabus-item"><strong class="label label-lecture">Lecture 1</strong><a href="/fa26/lectures/intro/">Introduction</a></div>
  </td>
  <td>
    <div class="syllabus-item"><strong class="label label-reading">Reading</strong><span class="readings-list"><a href="https://inferentialthinking.com/1">1.1</a>, <a href="https://inferentialthinking.com/2">1.2</a></span></div>
  </td>
</tr>
</tbody></table>
<h2 class="week-label">Week 5</h2>
<table><tbody>
<tr>
  <td>Mon, Sep 21</td>
  <td><div class="syllabus-item"><strong class="label label-exam">Exam</strong><span>Midterm 1, 8-10pm</span></div></td>
</tr>
<tr>
  <td>Thu, Sep 24</td>
  <td><div class="syllabus-item"><strong class="label label-project">Project 1</strong><a href="/fa26/proj">Project 1 (Checkpoint due 10/2, Entire project due 10/9)</a></div></td>
</tr>
</tbody></table>
</div>
"""


def test_data8_calendar_is_its_own_page_and_is_not_reread(monkeypatch):
    from berkeleypulse.sites import match_site, refresh_course_sites

    calls = {"n": 0}

    def fake(url):
        calls["n"] += 1
        assert url == "https://data8.org/fa26/"
        return DATA8_PAGE

    monkeypatch.setattr("berkeleypulse.sites.fetch_site", fake)
    monkeypatch.setattr("berkeleypulse.app.fetch_site", fake)
    assert match_site("POLSCI 161 Fall 2026", "Voting", "Fall 2026") is None
    course_id = add_manual_course(
        "Data C8 FA26",
        "Foundations of Data Science (Fall 2026)",
        "Homework is worth 20%. Projects are worth 30%. Quizzes are worth 10%. The midterm is worth 15%. The final is worth 25%.",
    )
    client = TestClient(create_app())
    page = client.get("/courses/%s/site" % course_id)
    assert page.status_code == 200
    assert "Week 1" in page.text
    assert "Lab 1 (due 8/28)" in page.text
    assert "Introduction" in page.text
    assert "Midterm 1, 8-10pm" in page.text
    assert "https://data8.org/fa26/" in page.text
    home = client.get("/")
    assert 'href="/courses/%s/site"' % course_id in home.text
    connect = client.get("/sign-in")
    assert 'href="/courses/%s/site"' % course_id in connect.text
    again = client.get("/courses/%s/site" % course_id)
    assert again.status_code == 200
    assert calls["n"] == 1
    with db() as conn:
        same, read = refresh_course_sites(conn)
        assert same == 1 and read == 0
        row = conn.execute(
            "SELECT starts_at FROM events WHERE title = ? AND kind = 'deadline'",
            ("Data C8 FA26 · Lab 1",),
        ).fetchone()
        assert row is not None
        assert row["starts_at"].startswith("2026-08-28T17:00:00")
        exam = conn.execute(
            "SELECT starts_at, ends_at FROM events WHERE title LIKE '%Midterm 1%' AND kind = 'exam'"
        ).fetchone()
        assert exam["starts_at"].startswith("2026-09-21T20:00:00")
        assert exam["ends_at"].startswith("2026-09-21T22:00:00")
        checkpoint = conn.execute(
            "SELECT starts_at FROM events WHERE title LIKE '%Checkpoint%'"
        ).fetchone()
        assert checkpoint["starts_at"].startswith("2026-10-02")
    assert calls["n"] == 2


def test_billing_stays_hidden_until_stripe_is_configured():
    client = TestClient(create_app())
    assert "Subscribe" not in client.get("/").text


def test_subscribe_stays_hidden_until_billing_is_turned_on(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "rk_live_local")
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_123")
    client = TestClient(create_app())
    assert "Subscribe" not in client.get("/").text


def test_subscribe_opens_hosted_checkout(monkeypatch):
    monkeypatch.setenv("STRIPE_BILLING", "1")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "rk_test_local")
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_123")
    monkeypatch.setattr(
        "berkeleypulse.app.checkout_url",
        lambda origin: "https://checkout.stripe.com/c/pay/cs_test_abc",
    )
    client = TestClient(create_app())
    assert 'action="/billing/checkout"' in client.get("/").text
    response = client.post("/billing/checkout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "https://checkout.stripe.com/c/pay/cs_test_abc"


def test_checkout_params_use_flexible_billing_and_skip_payment_method_types():
    params = checkout_params("http://127.0.0.1:8787", "", "price_123")
    assert params["mode"] == "subscription"
    assert "payment_method_types" not in params
    assert "automatic_tax" not in params
    assert params["subscription_data"]["billing_mode"]["type"] == "flexible"
    assert params["integration_identifier"].startswith("lockincal_checkout_")
    assert "{CHECKOUT_SESSION_ID}" in params["success_url"]


def test_paid_checkout_activates_and_cancel_removes_access(monkeypatch):
    apply_event({
        "type": "checkout.session.completed",
        "data": {"object": {
            "payment_status": "paid",
            "customer": "cus_123",
            "subscription": "sub_123",
        }},
    })
    assert billing_state()["active"] is True
    apply_event({
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_123", "customer": "cus_123", "status": "canceled"}},
    })
    state = billing_state()
    assert state["active"] is False
    assert state["manage"] is False
    monkeypatch.setenv("STRIPE_BILLING", "1")
    assert billing_state()["manage"] is True


def test_unpaid_checkout_does_not_activate():
    apply_event({
        "type": "checkout.session.completed",
        "data": {"object": {
            "payment_status": "unpaid",
            "customer": "cus_123",
            "subscription": "sub_123",
        }},
    })
    assert billing_state()["active"] is False


def test_invoice_events_follow_the_subscription():
    apply_event({
        "type": "invoice.paid",
        "data": {"object": {
            "customer": "cus_1",
            "parent": {"subscription_details": {"subscription": "sub_1"}},
        }},
    })
    assert billing_state()["active"] is True
    apply_event({
        "type": "invoice.payment_failed",
        "data": {"object": {"customer": "cus_1", "subscription": "sub_1"}},
    })
    assert billing_state()["status"] == "past_due"
    apply_event({
        "type": "customer.subscription.updated",
        "data": {"object": {"id": "sub_1", "customer": "cus_1", "status": "active"}},
    })
    assert billing_state()["active"] is True
    apply_event({
        "type": "charge.dispute.created",
        "data": {"object": {"customer": "cus_1"}},
    })
    assert billing_state()["status"] == "review"


def test_product_push_updates_the_same_product_and_skips_prices(monkeypatch):
    store = {}

    class Products:
        def retrieve(self, product_id):
            if product_id not in store:
                raise InvalidRequestError("missing", "id", code="resource_missing")
            return store[product_id]

        def create(self, params):
            assert "unit_amount" not in params
            assert "default_price_data" not in params
            store[params["id"]] = dict(params)
            return store[params["id"]]

        def update(self, product_id, params):
            store[product_id].update(params)
            return store[product_id]

    class Client:
        def __init__(self):
            self.v1 = self
            self.products = Products()

    monkeypatch.setenv("STRIPE_SECRET_KEY", "rk_live_local")
    monkeypatch.setattr("berkeleypulse.billing._client", lambda: Client())
    assert push_catalog() == ["live account", "created Lock In Cal"]
    assert store["lockincal"]["name"] == "Lock In Cal"
    store["lockincal"]["name"] = "old"
    assert push_catalog() == ["live account", "updated Lock In Cal"]
    assert store["lockincal"]["name"] == "Lock In Cal"


def test_webhook_rejects_a_bad_signature(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "rk_test_local")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    client = TestClient(create_app())
    response = client.post(
        "/billing/webhook",
        content=b"{}",
        headers={"stripe-signature": "t=1,v1=nope"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "Stripe signature did not match."


def test_webhook_stays_reachable_when_the_server_is_locked(monkeypatch):
    monkeypatch.setenv("PULSE_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "rk_test_local")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    client = TestClient(create_app())
    response = client.post(
        "/billing/webhook",
        content=b"{}",
        headers={"stripe-signature": "t=1,v1=nope"},
    )
    assert response.status_code == 400


def _deadline_at(html: str, title: str) -> int:
    at = html.find(title)
    if at < 0:
        raise AssertionError(title)
    return at
