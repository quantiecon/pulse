from __future__ import annotations

import hmac
import json
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from berkeleypulse.billing import BillingError, billing_state, checkout_url, handle_webhook, portal_url, sync_session
from berkeleypulse.canvas import CanvasError, verify_canvas_token
from berkeleypulse.config import data_dir, load_settings, locked_fields, posthog_public, update_settings
from berkeleypulse.db import db, meta_get
from berkeleypulse.demo import clear_demo, seed_demo
from berkeleypulse.desk import DeskError, access_report, open_desk, read_page_text, save_session
from berkeleypulse.ics import build_calendar
from berkeleypulse.mail import IMPORTANT, apply_email_rating, band, one_sentence, place_mail, score_email
from berkeleypulse.notify import notify
from berkeleypulse.qa import answer_question
from berkeleypulse.sites import fetch_site, mark_current_week, match_site, refresh_site, stored_calendar
from berkeleypulse.sync import (
    add_manual_course,
    hide_course,
    hide_courses,
    move_course,
    parse_dt,
    rebuild_schedule,
    reschedule,
    sync_all,
)
from berkeleypulse.textutil import pdf_to_text

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


def create_app() -> FastAPI:
    app = FastAPI(title="Pulse")
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
    _install_auth(app)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, error: str = ""):
        return _render(request, "login.html", {"error": error})

    @app.post("/login", response_class=HTMLResponse)
    def login(request: Request, token: str = Form("")):
        expected = os.environ.get("PULSE_AUTH_TOKEN", "")
        if expected and _same_secret(token.strip(), expected):
            response = RedirectResponse("/", status_code=303)
            response.set_cookie("pulse_auth", expected, httponly=True, samesite="lax", path="/")
            return response
        return _render(request, "login.html", {"error": "That token does not match."})

    @app.post("/billing/checkout")
    def billing_checkout(request: Request):
        try:
            return RedirectResponse(checkout_url(_origin(request)), status_code=303)
        except BillingError as exc:
            return _redirect("/", str(exc), "error")

    @app.post("/billing/portal")
    def billing_portal(request: Request):
        try:
            return RedirectResponse(portal_url(_origin(request)), status_code=303)
        except BillingError as exc:
            return _redirect("/", str(exc), "error")

    @app.get("/billing/return")
    def billing_return(session_id: str = ""):
        try:
            payment = sync_session(session_id)
        except BillingError as exc:
            return _redirect("/", str(exc), "error")
        if payment in {"paid", "no_payment_required"}:
            return _redirect("/", "Subscription is active.")
        return _redirect("/", "Payment is still processing.", "error")

    @app.post("/billing/webhook")
    async def billing_webhook(request: Request):
        payload = await request.body()
        signature = request.headers.get("stripe-signature", "")
        try:
            handle_webhook(payload, signature)
        except BillingError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"received": True}

    @app.get("/", response_class=HTMLResponse)
    def home(
        request: Request,
        notice: str = "",
        level: str = "",
        q: str = "",
        course: str = "",
        deleted: str = "",
        editing: str = "",
        month: str = "",
        day: str = "",
    ):
        context = _home_context(notice, level, q, course, deleted, month, day)
        context["editing"] = editing == "courses"
        return _render(request, "home.html", context)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, notice: str = "", level: str = ""):
        return _render(request, "settings.html", _settings_context(notice, level))

    @app.post("/settings")
    def save_settings(
        canvas_base_url: str = Form(""),
        canvas_token: str = Form(""),
        imap_host: str = Form(""),
        imap_port: str = Form("993"),
        imap_user: str = Form(""),
        imap_password: str = Form(""),
        imap_folder: str = Form("INBOX"),
        work_style: str = Form("spacer"),
        session_minutes: str = Form("50"),
        poll_minutes: str = Form("15"),
        llm_base_url: str = Form(""),
        llm_api_key: str = Form(""),
        llm_model: str = Form(""),
        timezone_name: str = Form(""),
    ):
        current = load_settings()
        try:
            port = int(imap_port or current.imap_port)
            sessions = int(session_minutes or current.session_minutes)
            polls = int(poll_minutes or current.poll_minutes)
        except ValueError:
            return _redirect("/settings", "Ports and minutes need to be numbers.", "error")
        changes = {
            "canvas_base_url": canvas_base_url.strip() or current.canvas_base_url,
            "imap_host": imap_host.strip() or current.imap_host,
            "imap_port": port,
            "imap_user": imap_user.strip(),
            "imap_folder": imap_folder.strip() or "INBOX",
            "work_style": work_style if work_style in {"spacer", "crammer"} else current.work_style,
            "session_minutes": sessions,
            "poll_minutes": polls,
            "llm_base_url": llm_base_url.strip() or current.llm_base_url,
            "llm_model": llm_model.strip() or current.llm_model,
            "timezone": timezone_name.strip() or current.timezone,
        }
        if canvas_token.strip():
            changes["canvas_token"] = canvas_token.strip()
        if imap_password.strip():
            changes["imap_password"] = imap_password.strip()
        if llm_api_key.strip():
            changes["llm_api_key"] = llm_api_key.strip()
        try:
            ZoneInfo(changes["timezone"])
        except Exception:
            return _redirect("/settings", "That timezone was not recognized.", "error")
        update_settings(**changes)
        reschedule()
        return _redirect("/settings", "Settings saved. Study blocks were rebuilt for the way you work.")

    @app.post("/scan")
    @app.post("/sync")
    def run_scan():
        result = sync_all()
        notify(result.new_urgent)
        return _redirect("/", result.message)

    @app.get("/sign-in", response_class=HTMLResponse)
    def sign_in_page(request: Request, notice: str = "", level: str = ""):
        return _render(request, "signin.html", _sign_in_context(notice, level))

    @app.post("/sign-in/canvas")
    def save_canvas_token(canvas_token: str = Form("")):
        if "canvas_token" in locked_fields():
            return _redirect("/sign-in", "PULSE_CANVAS_TOKEN is set, so this page cannot replace it.", "error")
        token = canvas_token.strip()
        if not token or any(char.isspace() for char in token):
            return _redirect("/sign-in", "Paste the access token by itself, with no spaces.", "error")
        settings = load_settings()
        try:
            name = verify_canvas_token(settings.canvas_base_url, token)
        except CanvasError as exc:
            return _redirect("/sign-in", str(exc), "error")
        update_settings(canvas_token=token)
        return _redirect("/sign-in", "bCourses token saved for %s." % name)

    @app.post("/sign-in/open")
    def open_sign_in(target: str = Form("canvas")):
        try:
            message = open_desk(target)
        except DeskError as exc:
            return _redirect("/sign-in", str(exc), "error")
        return _redirect("/sign-in", message)

    @app.post("/sign-in/save")
    def save_sign_in():
        try:
            message = save_session()
        except DeskError as exc:
            return _redirect("/sign-in", str(exc), "error")
        return _redirect("/sign-in", message)

    @app.post("/sign-in/page")
    def save_open_page(code: str = Form(""), name: str = Form("")):
        if not code.strip() or not name.strip():
            return _redirect("/sign-in", "A saved page needs a course code and a name.", "error")
        try:
            text = read_page_text()
        except DeskError as exc:
            return _redirect("/sign-in", str(exc), "error")
        course_id = add_manual_course(code, name, text)
        return _redirect(
            "/courses/%s" % course_id,
            "Saved that page as static policy. Scan will not read it again unless the text changes.",
        )

    @app.post("/demo")
    def load_demo():
        created = seed_demo()
        if created:
            return _redirect("/", "Demo semester loaded. Nothing was fetched from Canvas or your inbox.")
        return _redirect("/", "Demo semester is already loaded.")

    @app.post("/demo/clear")
    def remove_demo():
        clear_demo()
        reschedule()
        return _redirect("/", "Demo semester removed.")

    @app.get("/courses/{course_id}", response_class=HTMLResponse)
    def course_page(
        request: Request,
        course_id: int,
        q: str = "",
        notice: str = "",
        level: str = "",
        editing: str = "",
    ):
        context = _course_context(course_id, q, notice, level)
        if context is None:
            raise HTTPException(status_code=404)
        context["editing"] = editing == "courses"
        return _render(request, "course.html", context)

    @app.get("/courses/{course_id}/site", response_class=HTMLResponse)
    def course_site_page(request: Request, course_id: int, notice: str = "", level: str = ""):
        context = _site_context(course_id, notice, level)
        if context is None:
            raise HTTPException(status_code=404)
        return _render(request, "course_site.html", context)

    @app.post("/courses")
    def create_course(
        code: str = Form(""),
        name: str = Form(""),
        syllabus: str = Form(""),
        pdf: Optional[UploadFile] = File(None),
    ):
        text = syllabus.strip()
        if pdf is not None and pdf.filename:
            data = pdf.file.read()
            if len(data) > 10_000_000:
                return _redirect("/", "That PDF is larger than 10 MB.", "error")
            try:
                text = (text + "\n\n" + pdf_to_text(data)).strip()
            except ValueError as exc:
                return _redirect("/", str(exc), "error")
            except Exception:
                return _redirect("/", "That PDF could not be read.", "error")
        if not code.strip() or not name.strip() or not text:
            return _redirect("/", "A course needs a code, a name, and syllabus text or a PDF.", "error")
        course_id = add_manual_course(code, name, text)
        return _redirect("/courses/%s" % course_id, "Course added. The syllabus was parsed once.")

    @app.post("/courses/{course_id}/hide")
    def remove_course(course_id: int):
        hide_course(course_id)
        reschedule()
        return _redirect("/", "Course removed on this machine. Canvas will not import it again.")

    @app.post("/courses/{course_id}/move")
    def move_course_route(
        course_id: int,
        direction: str = Form("up"),
        next_path: str = Form("/?editing=courses"),
    ):
        move_course(course_id, direction)
        return RedirectResponse(_editing_path(next_path), status_code=303)

    @app.post("/courses/remove")
    async def remove_courses(request: Request):
        form = await request.form()
        selected = [int(value) for value in form.getlist("delete") if str(value).isdigit()]
        next_path = str(form.get("next_path") or "/")
        if not selected:
            return _redirect(_editing_path(next_path), "Select the classes to delete.", "error")
        removed = hide_courses(selected)
        reschedule()
        if removed == 0:
            return _redirect(_editing_path(next_path), "Those classes are already gone.", "error")
        if removed == 1:
            message = "Class removed. Its assignments, tests, and due dates are gone."
        else:
            message = "Removed %d classes. Their assignments, tests, and due dates are gone." % removed
        return _redirect(_leave_edit(next_path, set(selected)), message)

    @app.post("/emails/{email_id}/dismiss")
    def dismiss_email(email_id: int, next_path: str = Form("/")):
        with db() as conn:
            conn.execute("UPDATE emails SET dismissed = 1 WHERE id = ?", (email_id,))
        return RedirectResponse("/?deleted=email-%s" % email_id, status_code=303)

    @app.post("/emails/{email_id}/restore")
    def restore_email(email_id: int):
        with db() as conn:
            conn.execute("UPDATE emails SET dismissed = 0 WHERE id = ?", (email_id,))
        return RedirectResponse("/", status_code=303)

    @app.post("/events/{event_id}/hide")
    def hide_event(event_id: int):
        with db() as conn:
            row = conn.execute("SELECT course_id, kind, title, starts_at FROM events WHERE id = ?", (event_id,)).fetchone()
            if row is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO hidden_events(signature) VALUES (?)",
                    (_event_signature(row),),
                )
        return RedirectResponse("/?deleted=event-%s" % event_id, status_code=303)

    @app.post("/events/{event_id}/restore")
    def restore_event(event_id: int):
        with db() as conn:
            row = conn.execute("SELECT course_id, kind, title, starts_at FROM events WHERE id = ?", (event_id,)).fetchone()
            if row is not None:
                conn.execute("DELETE FROM hidden_events WHERE signature = ?", (_event_signature(row),))
        return RedirectResponse("/", status_code=303)

    @app.post("/emails/{email_id}/rate")
    def rate_email(email_id: int, rating: str = Form(...), next_path: str = Form("/")):
        if rating not in ("more", "less", "ignore"):
            return _redirect(_safe_next(next_path))
        with db() as conn:
            apply_email_rating(conn, email_id, rating)
        return RedirectResponse(_safe_next(next_path), status_code=303)

    @app.get("/calendar.ics")
    def calendar(request: Request, download: str = ""):
        accept = request.headers.get("accept", "")
        opened_in_browser = "text/html" in accept and download != "1" and not request.query_params.get("token")
        if opened_in_browser:
            return RedirectResponse("/", status_code=303)
        with db() as conn:
            rows = conn.execute(
                """
                SELECT events.*, courses.code AS code
                FROM events
                JOIN courses ON courses.id = events.course_id
                WHERE courses.hidden = 0
                ORDER BY events.starts_at
                """
            ).fetchall()
        disposition = "attachment" if download else "inline"
        filename = "pulse.ics"
        return Response(
            content=build_calendar(rows),
            media_type="text/calendar; charset=utf-8",
            headers={"Content-Disposition": '%s; filename="%s"' % (disposition, filename)},
        )

    @app.get("/digest.json")
    def digest():
        settings = load_settings()
        tz = ZoneInfo(settings.timezone)
        now = datetime.now(tz)
        with db() as conn:
            emails, _, _ = _classify_mail(conn, tz)
            upcoming = _flat_events(conn, tz, now)
            stats = _stats(conn, tz)
        return JSONResponse(
            {
                "important": [
                    {
                        "subject": item["subject"],
                        "from": item["from_addr"],
                        "score": item["score"],
                        "band": item["band"],
                        "reason": item["reason"],
                        "sent_at": item["sent_at"],
                    }
                    for item in emails
                ],
                "upcoming": upcoming,
                "stats": stats,
            }
        )

    return app


def _install_auth(app: FastAPI) -> None:
    token = os.environ.get("PULSE_AUTH_TOKEN", "")
    if not token:
        return

    @app.middleware("http")
    async def check_auth(request: Request, call_next):
        path = request.url.path
        if path in {"/login", "/health", "/favicon.ico", "/billing/webhook"} or path.startswith("/static"):
            return await call_next(request)
        cookie = request.cookies.get("pulse_auth", "")
        if _same_secret(cookie, token):
            return await call_next(request)
        if path == "/calendar.ics" and _same_secret(request.query_params.get("token", ""), token):
            return await call_next(request)
        if path == "/login":
            return await call_next(request)
        return RedirectResponse("/login", status_code=303)


def _same_secret(given: str, expected: str) -> bool:
    if not given or not expected or len(given) != len(expected):
        return False
    return hmac.compare_digest(given, expected)


def _render(request: Request, name: str, context: dict):
    payload = dict(context)
    payload["request"] = request
    payload["posthog"] = posthog_public()
    payload["billing"] = billing_state()
    return templates.TemplateResponse(request, name, payload)


def _origin(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _redirect(path: str, notice: str = "", level: str = "ok"):
    query = {}
    if notice:
        query["notice"] = notice
    if level != "ok":
        query["level"] = level
    if query:
        joiner = "&" if "?" in path else "?"
        path = path + joiner + urlencode(query)
    return RedirectResponse(path, status_code=303)


def _safe_next(value: str) -> str:
    if value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def _editing_path(value: str) -> str:
    path = _safe_next(value)
    if "editing=courses" in path:
        return path
    joiner = "&" if "?" in path else "?"
    return path + joiner + "editing=courses"


def _leave_edit(value: str, removed: set) -> str:
    path = _safe_next(value)
    path = path.replace("?editing=courses", "").replace("&editing=courses", "")
    if not path:
        path = "/"
    if path.startswith("/courses/"):
        raw = path.split("?", 1)[0].rstrip("/").split("/")[-1]
        if raw.isdigit() and int(raw) in removed:
            return "/"
    return path


def _deleted_target(deleted: str):
    kind, _, raw = (deleted or "").partition("-")
    if kind in {"email", "event"} and raw.isdigit():
        return {"kind": kind, "id": raw}
    return None


def _event_signature(row) -> str:
    return "%s|%s|%s|%s" % (row["course_id"], row["kind"], row["title"], row["starts_at"])


def _home_context(
    notice: str,
    level: str,
    question: str,
    course: str,
    deleted: str = "",
    month: str = "",
    day: str = "",
) -> dict:
    settings = load_settings()
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    course_id = int(course) if str(course).isdigit() else None
    with db() as conn:
        answer = answer_question(conn, question, settings, course_id) if question.strip() else None
        rescore_emails(conn)
        emails, quiet, ignored = _classify_mail(conn, tz)
        grouped = _grouped_events(conn, tz, now)
        calendar, selected_day = _month_calendar(conn, tz, now, month, day)
        courses = _course_list(conn)
        demo = conn.execute(
            "SELECT COUNT(*) AS n FROM courses WHERE origin = 'demo' AND hidden = 0"
        ).fetchone()["n"]
        stats = _stats(conn, tz)
        recent = (_recent_notices(conn, tz, now) + _recent_flags(emails, tz, now))[:8]
    return {
        "notice": notice,
        "level": level,
        "question": question,
        "selected_course": course,
        "answer": answer,
        "emails": emails,
        "quiet": quiet,
        "ignored": ignored,
        "deleted": _deleted_target(deleted),
        "days": grouped,
        "calendar": calendar,
        "selected_day": selected_day,
        "recent": recent,
        "courses": courses,
        "site_courses": [item for item in courses if item.get("site_path")],
        "demo": demo,
        "settings": settings,
        "desk": access_report(),
        "stats": stats,
        "data_dir": str(data_dir()),
        "autorefresh": False,
    }


def _sign_in_context(notice: str, level: str) -> dict:
    settings = load_settings()
    tz = ZoneInfo(settings.timezone)
    with db() as conn:
        stats = _stats(conn, tz)
        courses = _course_list(conn)
        demo = conn.execute(
            "SELECT COUNT(*) AS n FROM courses WHERE origin = 'demo' AND hidden = 0"
        ).fetchone()["n"]
    return {
        "notice": notice,
        "level": level,
        "settings": settings,
        "desk": access_report(),
        "stats": stats,
        "demo": demo,
        "site_courses": [item for item in courses if item.get("site_path")],
        "data_dir": str(data_dir()),
        "canvas_locked": "canvas_token" in locked_fields(),
        "token_expires": _token_expires_label(),
        "token_expires_short": _token_expires_short(),
        "autorefresh": False,
    }


def _token_expires_on() -> date:
    return date.today() + timedelta(days=90)


def _token_expires_label() -> str:
    expires = _token_expires_on()
    return "%s %d, %s" % (expires.strftime("%B"), expires.day, expires.year)


def _token_expires_short() -> str:
    return _token_expires_on().strftime("%m/%d/%Y")


def _settings_context(notice: str, level: str) -> dict:
    settings = load_settings()
    tz = ZoneInfo(settings.timezone)
    with db() as conn:
        stats = _stats(conn, tz)
        demo = conn.execute(
            "SELECT COUNT(*) AS n FROM courses WHERE origin = 'demo' AND hidden = 0"
        ).fetchone()["n"]
    return {
        "notice": notice,
        "level": level,
        "settings": settings,
        "desk": access_report(),
        "locked": locked_fields(),
        "stats": stats,
        "demo": demo,
        "data_dir": str(data_dir()),
        "autorefresh": False,
    }


def _course_context(course_id: int, question: str, notice: str, level: str):
    settings = load_settings()
    tz = ZoneInfo(settings.timezone)
    with db() as conn:
        row = conn.execute("SELECT * FROM courses WHERE id = ? AND hidden = 0", (course_id,)).fetchone()
        if row is None:
            return None
        answer = answer_question(conn, question, settings, course_id) if question.strip() else None
        assignments = []
        for item in conn.execute(
            "SELECT * FROM assignments WHERE course_id = ? ORDER BY due_at IS NULL, due_at",
            (course_id,),
        ):
            assignments.append(
                {
                    "name": item["name"],
                    "due": _fmt_when(item["due_at"], tz) if item["due_at"] else "No due date",
                    "points": _points(item["points"]),
                    "description": item["description"],
                    "group_name": item["group_name"],
                }
            )
        courses = _course_list(conn)
        stats = _stats(conn, tz)
        demo = conn.execute(
            "SELECT COUNT(*) AS n FROM courses WHERE origin = 'demo' AND hidden = 0"
        ).fetchone()["n"]
    try:
        model = json.loads(row["model_json"] or "{}")
    except json.JSONDecodeError:
        model = {}
    exams = []
    for exam in model.get("exams") or []:
        exams.append({"name": exam.get("name") or "Exam", "when": _fmt_when(exam.get("starts_at"), tz)})
    deadlines = []
    for item in model.get("deadlines") or []:
        deadlines.append({"name": item.get("name") or "Deadline", "when": _fmt_when(item.get("due_at"), tz)})
    grading = []
    for item in model.get("grading") or []:
        weight = item.get("weight")
        label = str(int(weight)) if float(weight) == int(float(weight)) else str(weight)
        grading.append({"name": item.get("name"), "weight": label})
    return {
        "notice": notice,
        "level": level,
        "course": dict(row),
        "answer": answer,
        "question": question,
        "assignments": assignments,
        "grading": grading,
        "late_policy": model.get("late_policy") or "",
        "drop_rules": model.get("drop_rules") or [],
        "exams": exams,
        "deadlines": deadlines,
        "courses": courses,
        "site_path": _site_path(row["id"], row["code"], row["name"], row["term"] or ""),
        "settings": settings,
        "stats": stats,
        "demo": demo,
        "data_dir": str(data_dir()),
        "autorefresh": False,
        "parsed_with": row["parsed_with"] or "not parsed",
    }


def _course_list(conn):
    rows = conn.execute(
        """
        SELECT id, code, name, term, origin
        FROM courses
        WHERE hidden = 0
        ORDER BY sort_order, code, id
        """
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["site_path"] = _site_path(row["id"], row["code"], row["name"], row["term"] or "")
        item["site_label"] = ""
        site = match_site(row["code"], row["name"], row["term"] or "")
        if site:
            item["site_label"] = site["label"]
        items.append(item)
    return items


def _site_path(course_id: int, code: str, name: str, term: str) -> str:
    if match_site(code, name, term) is None:
        return ""
    return "/courses/%s/site" % course_id


def _site_context(course_id: int, notice: str, level: str):
    settings = load_settings()
    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).date()
    with db() as conn:
        row = conn.execute("SELECT * FROM courses WHERE id = ? AND hidden = 0", (course_id,)).fetchone()
        if row is None:
            return None
        site = match_site(row["code"], row["name"], row["term"] or "")
        if site is None:
            return None
        calendar = stored_calendar(conn, course_id)
        if calendar is None:
            status = refresh_site(conn, course_id, site, fetch_site)
            calendar = stored_calendar(conn, course_id) or {
                "url": site["url"],
                "announcements": [],
                "weeks": [],
            }
            if status == "parsed":
                rebuild_schedule(conn, settings)
        mark_current_week(calendar, today)
        courses = _course_list(conn)
        stats = _stats(conn, tz)
        demo = conn.execute(
            "SELECT COUNT(*) AS n FROM courses WHERE origin = 'demo' AND hidden = 0"
        ).fetchone()["n"]
    return {
        "notice": notice,
        "level": level,
        "course": dict(row),
        "site": site,
        "calendar": calendar,
        "courses": courses,
        "settings": settings,
        "stats": stats,
        "demo": demo,
        "data_dir": str(data_dir()),
        "autorefresh": False,
    }


def rescore_emails(conn) -> None:
    rows = conn.execute("SELECT id, subject, from_addr, body, score, reason FROM emails").fetchall()
    for row in rows:
        score, reason = score_email(row["subject"] or "", row["from_addr"] or "", row["body"] or "")
        if score == row["score"] and reason == row["reason"]:
            continue
        conn.execute("UPDATE emails SET score = ?, reason = ? WHERE id = ?", (score, reason, row["id"]))


def _load_prefs(conn):
    prefs = {}
    for row in conn.execute("SELECT kind, key, weight, samples FROM mail_prefs"):
        prefs[(row["kind"], row["key"])] = int(row["weight"])
    return prefs


def _classify_mail(conn, tz):
    prefs = _load_prefs(conn)
    rows = conn.execute(
        """
        SELECT emails.*, courses.code AS code
        FROM emails
        LEFT JOIN courses ON courses.id = emails.course_id
        WHERE emails.dismissed = 0 AND emails.reason NOT LIKE ?
        ORDER BY emails.score DESC, emails.sent_at DESC
        LIMIT 80
        """,
        ("%newsletter%",),
    ).fetchall()
    buckets = {"show": [], "quiet": [], "ignore": []}
    for row in rows:
        reason_weights = []
        sender_weight = prefs.get(("sender", (row["from_addr"] or "").strip().lower()), 0)
        for part in (row["reason"] or "").split(","):
            tag = part.strip().lower()
            if tag:
                reason_weights.append(prefs.get(("reason", tag), 0))
        explicit = row["user_rating"] or ""
        place = place_mail(row["score"], explicit, sender_weight, reason_weights)
        buckets[place].append(
            {
                "id": row["id"],
                "subject": row["subject"],
                "from_addr": row["from_addr"],
                "from_name": row["from_name"],
                "score": row["score"],
                "band": band(row["score"]),
                "reason": row["reason"],
                "sent_at": row["sent_at"],
                "when": _fmt_when(row["sent_at"], tz),
                "snippet": row["snippet"],
                "summary": one_sentence(row["snippet"] or row["subject"]),
                "code": row["code"],
                "place": place,
                "rating": explicit,
                "rating_label": _rating_label(explicit),
            }
        )
    return buckets["show"][:20], buckets["quiet"][:20], buckets["ignore"][:20]


def _rating_label(rating: str) -> str:
    return {
        "more": "Marked important",
        "less": "Marked less important",
        "ignore": "Marked ignored",
    }.get(rating, "")


def _flat_events(conn, tz, now):
    grouped = _grouped_events(conn, tz, now)
    flat = []
    for day in grouped:
        for item in day["items"]:
            flat.append(
                {
                    "day": day["label"],
                    "time": item["time"],
                    "title": item["title"],
                    "kind": item["kind"],
                    "details": item["details"],
                }
            )
    return flat


def _grouped_events(conn, tz, now):
    start = datetime.combine(now.astimezone(tz).date(), time(0, 0), tzinfo=tz)
    horizon = now.astimezone(tz) + timedelta(days=21)
    items = [
        item
        for item in _event_items(conn, tz)
        if start <= item["sort"] <= horizon
    ]
    items.sort(key=lambda item: (item["day_sort"], item["course_order"], item["sort"], item["title"]))
    grouped = []
    for item in items:
        if not grouped or grouped[-1]["label"] != item["day"]:
            grouped.append({"label": item["day"], "items": []})
        grouped[-1]["items"].append(item)
    return grouped


def _event_items(conn, tz):
    hidden = {item["signature"] for item in conn.execute("SELECT signature FROM hidden_events")}
    rows = conn.execute(
        """
        SELECT events.*, courses.code AS code, courses.sort_order AS sort_order
        FROM events
        JOIN courses ON courses.id = events.course_id
        WHERE courses.hidden = 0
        """
    ).fetchall()
    items = []
    for row in rows:
        if _event_signature(row) in hidden:
            continue
        starts = parse_dt(row["starts_at"])
        if starts is None:
            continue
        local = starts.astimezone(tz)
        items.append(
            {
                "id": row["id"],
                "sort": starts,
                "day_sort": local.date(),
                "course_order": int(row["sort_order"] or 0),
                "day": local.strftime("%A, %b ") + str(local.day),
                "time": _clock(local),
                "title": row["title"],
                "label": _chip_label(row["kind"], row["title"], row["details"]),
                "kind": row["kind"],
                "details": row["details"],
            }
        )
    return items


def _chip_label(kind: str, title: str, details: str) -> str:
    if kind == "study" and details:
        return details
    return title or details or "Event"


def _month_calendar(conn, tz, now, month_key: str, day_key: str):
    today = now.astimezone(tz).date()
    first = _parse_month(month_key, today)
    if first.month == 12:
        following = date(first.year + 1, 1, 1)
    else:
        following = date(first.year, first.month + 1, 1)
    if first.month == 1:
        previous = date(first.year - 1, 12, 1)
    else:
        previous = date(first.year, first.month - 1, 1)
    last = following - timedelta(days=1)
    grid_start = first - timedelta(days=(first.weekday() + 1) % 7)
    grid_end = last + timedelta(days=(5 - last.weekday()) % 7)
    by_day = {}
    for item in _event_items(conn, tz):
        if item["day_sort"] < grid_start or item["day_sort"] > grid_end:
            continue
        by_day.setdefault(item["day_sort"], []).append(item)
    for bucket in by_day.values():
        bucket.sort(key=lambda item: (item["course_order"], item["sort"], item["title"]))
    selected = _parse_day(day_key)
    if selected is None or selected < grid_start or selected > grid_end:
        selected = None
    weeks = []
    cursor = grid_start
    while cursor <= grid_end:
        week = []
        for _ in range(7):
            items = by_day.get(cursor, [])
            week.append(
                {
                    "iso": cursor.isoformat(),
                    "number": cursor.day,
                    "in_month": cursor.month == first.month,
                    "today": cursor == today,
                    "selected": cursor == selected,
                    "items": items[:3],
                    "extra": max(0, len(items) - 3),
                }
            )
            cursor += timedelta(days=1)
        weeks.append(week)
    selected_day = None
    if selected is not None:
        chosen = by_day.get(selected, [])
        local = datetime.combine(selected, time(0, 0), tzinfo=tz)
        selected_day = {
            "label": local.strftime("%A, %b ") + str(local.day),
            "items": chosen,
        }
    return (
        {
            "key": "%04d-%02d" % (first.year, first.month),
            "label": first.strftime("%B %Y"),
            "prev": "%04d-%02d" % (previous.year, previous.month),
            "next": "%04d-%02d" % (following.year, following.month),
            "weekdays": ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
            "weeks": weeks,
        },
        selected_day,
    )


def _parse_month(value: str, today: date) -> date:
    parts = (value or "").split("-")
    if len(parts) == 2 and all(part.isdigit() for part in parts):
        year, month = int(parts[0]), int(parts[1])
        if 1 <= month <= 12 and 2000 <= year <= 2100:
            return date(year, month, 1)
    return today.replace(day=1)


def _parse_day(value: str):
    parts = (value or "").split("-")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def _recent_notices(conn, tz, now):
    cutoff = now.astimezone(tz) - timedelta(days=14)
    items = []
    rows = conn.execute(
        """
        SELECT notices.title, notices.posted_at, notices.kind, courses.code AS code
        FROM notices
        JOIN courses ON courses.id = notices.course_id
        WHERE courses.hidden = 0 AND notices.kind IN ('cancel', 'move')
        ORDER BY notices.posted_at DESC
        """
    ).fetchall()
    for row in rows:
        posted = parse_dt(row["posted_at"])
        if posted is None or posted < cutoff:
            continue
        reason = "class canceled" if row["kind"] == "cancel" else "class moved"
        title = row["title"]
        if row["code"]:
            title = "%s · %s" % (row["code"], row["title"])
        items.append({"subject": title, "when": _fmt_when(row["posted_at"], tz), "reason": reason})
    return items[:8]


def _recent_flags(emails, tz, now):
    cutoff = now.astimezone(tz) - timedelta(days=14)
    recent = []
    for email in emails:
        sent = parse_dt(email.get("sent_at"))
        if sent is None:
            continue
        local = sent.astimezone(tz)
        if local > now.astimezone(tz) or local < cutoff:
            continue
        recent.append(email)
    recent.sort(key=lambda item: item.get("sent_at") or "", reverse=True)
    return recent[:8]


def _stats(conn, tz):
    last = meta_get(conn, "last_sync_at", "")
    return {
        "parses": meta_get(conn, "parses", "0"),
        "cache_hits": meta_get(conn, "cache_hits", "0"),
        "llm_calls": meta_get(conn, "llm_calls", "0"),
        "last_sync": _fmt_when(last, tz) if last else "not yet",
        "note": meta_get(conn, "last_sync_note", ""),
    }


def _fmt_when(value, tz) -> str:
    parsed = parse_dt(value)
    if parsed is None:
        return ""
    local = parsed.astimezone(tz)
    return "%s %s, %s" % (local.strftime("%b"), local.day, _clock(local))


def _points(value):
    if value is None:
        return None
    number = float(value)
    if number == int(number):
        return int(number)
    return number


def _clock(local: datetime) -> str:
    hour = str(int(local.strftime("%I")))
    return "%s:%s %s" % (hour, local.strftime("%M"), local.strftime("%p"))
