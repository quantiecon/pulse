from __future__ import annotations

import email
import imaplib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import List, Optional, Tuple

import httpx

from berkeleypulse.config import Settings
from berkeleypulse.textutil import html_to_text, sentences

IMPORTANT = 40
URGENT = 60

_SUBJECT_RULES = [
    ("due", 50, "due date"),
    ("deadline", 50, "deadline"),
    ("overdue", 50, "overdue"),
    ("action required", 45, "action required"),
    ("enrollment", 40, "enrollment"),
    ("waitlist", 40, "enrollment"),
    ("grade", 35, "grades"),
    ("midterm", 30, "exam"),
    ("exam", 30, "exam"),
    ("final", 25, "exam"),
    ("quiz", 20, "quiz"),
    ("coffee chat", 45, "coffee chat"),
    ("coffee", 40, "coffee chat"),
    ("class canceled", 70, "class canceled"),
    ("class cancelled", 70, "class canceled"),
    ("class cancellation", 70, "class canceled"),
    ("canceled", 60, "canceled"),
    ("cancelled", 60, "canceled"),
    ("cancellation", 60, "canceled"),
    ("meeting", 40, "meeting"),
    ("invitation", 40, "invitation"),
    ("invite", 40, "invitation"),
    ("rsvp", 40, "invitation"),
    ("office hours", 40, "office hours"),
]

_BODY_RULES = [
    ("due", 15, "due date"),
    ("deadline", 15, "deadline"),
    ("grade", 15, "grades"),
    ("midterm", 15, "exam"),
    ("exam", 15, "exam"),
    ("action required", 20, "action required"),
    ("enrollment", 15, "enrollment"),
    ("coffee chat", 40, "coffee chat"),
    ("meeting", 40, "meeting"),
    ("class canceled", 50, "class canceled"),
    ("class cancelled", 50, "class canceled"),
    ("class cancellation", 50, "class canceled"),
    ("canceled", 35, "canceled"),
    ("cancelled", 35, "canceled"),
    ("cancellation", 35, "canceled"),
]


@dataclass
class RawEmail:
    uid: int
    message_id: str
    from_addr: str
    from_name: str
    subject: str
    sent_at: Optional[datetime]
    body: str


def parse_gmail_atom(payload: str) -> List[RawEmail]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise RuntimeError("Mail sent something Pulse could not read.") from exc
    messages = []
    for entry in root.iter():
        if _local(entry.tag) != "entry":
            continue
        subject = _child_text(entry, "title") or "(no subject)"
        body = html_to_text(_child_text(entry, "summary"))
        message_id = _child_text(entry, "id") or subject
        author = ""
        email_addr = ""
        for child in list(entry):
            if _local(child.tag) != "author":
                continue
            author = _child_text(child, "name")
            email_addr = _child_text(child, "email")
        sent = _atom_time(_child_text(entry, "issued") or _child_text(entry, "modified"))
        messages.append(
            RawEmail(
                uid=0,
                message_id=message_id[:300],
                from_addr=email_addr or author,
                from_name=author,
                subject=subject[:500],
                sent_at=sent,
                body=body[:8000],
            )
        )
    return messages


def fetch_gmail_atom(cookies: httpx.Cookies) -> List[RawEmail]:
    response = httpx.get(
        "https://mail.google.com/mail/feed/atom",
        cookies=cookies,
        timeout=20,
        follow_redirects=True,
        headers={"User-Agent": "Pulse"},
    )
    if response.status_code != 200 or b"<feed" not in response.content[:800]:
        raise RuntimeError("Mail did not accept the saved sign-in. Open mail and allow it again.")
    return parse_gmail_atom(response.text)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(node, name: str) -> str:
    for child in list(node):
        if _local(child.tag) == name:
            return "".join(child.itertext()).strip()
    return ""


def _atom_time(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


RATING_WEIGHT = {"more": 1, "less": -1, "ignore": -2}


def one_sentence(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if not cleaned:
        return ""
    line = sentences(cleaned)[0]
    if len(line) > 140:
        return line[:137].rstrip() + "..."
    return line


def preference_keys(from_addr: str, reason: str):
    keys = []
    addr = (from_addr or "").strip().lower()
    if addr:
        keys.append(("sender", addr))
    for part in (reason or "").split(","):
        tag = part.strip().lower()
        if tag and tag not in {"no priority signals", "newsletter"}:
            keys.append(("reason", tag))
    return keys


def place_mail(base_score: int, explicit: str, sender_weight: int, reason_weights: List[int]) -> str:
    if explicit == "ignore":
        return "ignore"
    if explicit == "less":
        return "quiet"
    if explicit == "more":
        return "show"
    if sender_weight <= -2:
        return "ignore"
    if sender_weight < 0:
        return "quiet"
    if sender_weight > 0:
        return "show"
    if any(weight <= -4 for weight in reason_weights):
        return "ignore"
    if any(weight < 0 for weight in reason_weights):
        return "quiet"
    if base_score >= IMPORTANT:
        return "show"
    return "quiet"


def apply_email_rating(conn, email_id: int, rating: str) -> None:
    if rating not in RATING_WEIGHT:
        raise ValueError(rating)
    row = conn.execute("SELECT from_addr, reason, user_rating FROM emails WHERE id = ?", (email_id,)).fetchone()
    if row is None:
        return
    keys = preference_keys(row["from_addr"], row["reason"])
    previous = row["user_rating"] or ""
    if previous in RATING_WEIGHT:
        _shift_prefs(conn, keys, -RATING_WEIGHT[previous], -1)
    _shift_prefs(conn, keys, RATING_WEIGHT[rating], 1)
    conn.execute("UPDATE emails SET user_rating = ? WHERE id = ?", (rating, email_id))


def _shift_prefs(conn, keys, weight_delta: int, sample_delta: int) -> None:
    for kind, key in keys:
        current = conn.execute(
            "SELECT weight, samples FROM mail_prefs WHERE kind = ? AND key = ?",
            (kind, key),
        ).fetchone()
        if current is None:
            if sample_delta > 0:
                conn.execute(
                    "INSERT INTO mail_prefs(kind, key, weight, samples) VALUES(?, ?, ?, ?)",
                    (kind, key, weight_delta, sample_delta),
                )
            continue
        weight = int(current["weight"]) + weight_delta
        samples = int(current["samples"]) + sample_delta
        if samples <= 0:
            conn.execute("DELETE FROM mail_prefs WHERE kind = ? AND key = ?", (kind, key))
        else:
            conn.execute(
                "UPDATE mail_prefs SET weight = ?, samples = ? WHERE kind = ? AND key = ?",
                (weight, samples, kind, key),
            )


def band(score: int) -> str:
    if score >= URGENT:
        return "Urgent"
    if score >= IMPORTANT:
        return "Important"
    return "Quiet"


def _has(text: str, phrase: str) -> bool:
    return re.search(r"\b%s\b" % re.escape(phrase), text, re.IGNORECASE) is not None


def score_email(subject: str, from_addr: str, body: str) -> Tuple[int, str]:
    score = 0
    reasons: List[str] = []
    seen = set()
    blob = "%s\n%s" % (subject, body)
    addr = (from_addr or "").lower()

    def add(points: int, reason: str) -> None:
        nonlocal score
        if reason in seen:
            return
        if reason == "canceled" and "class canceled" in seen:
            return
        seen.add(reason)
        reasons.append(reason)
        score += points

    if any(host in addr for host in ("instructure.com", "bcourses.berkeley.edu")):
        add(30, "Canvas notification")
    if addr.endswith("@berkeley.edu") or addr.endswith(".berkeley.edu"):
        add(10, "School sender")
    for word, points, reason in _SUBJECT_RULES:
        if _has(subject, word):
            add(points, reason)
    for word, points, reason in _BODY_RULES:
        if _has(blob, word):
            add(points, reason)
    if _has(subject, "newsletter") or _has(blob, "unsubscribe"):
        score = min(score, 15)
        if "newsletter" not in seen:
            reasons.append("newsletter")
    score = max(0, min(100, score))
    if not reasons:
        reasons.append("no priority signals")
    return score, ", ".join(reasons)


def decode_mime(value: Optional[str]) -> str:
    if not value:
        return ""
    chunks = []
    for text, encoding in decode_header(value):
        if isinstance(text, bytes):
            chunks.append(text.decode(encoding or "utf-8", errors="replace"))
        else:
            chunks.append(text)
    return "".join(chunks)


def extract_body(message: Message) -> str:
    if message.is_multipart():
        plain: List[str] = []
        html_parts: List[str] = []
        for part in message.walk():
            disposition = str(part.get("Content-Disposition") or "")
            if "attachment" in disposition.lower():
                continue
            content_type = part.get_content_type()
            if content_type not in {"text/plain", "text/html"}:
                continue
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if content_type == "text/plain":
                plain.append(text)
            else:
                html_parts.append(html_to_text(text))
        if plain:
            return "\n".join(plain).strip()
        return "\n".join(html_parts).strip()
    payload = message.get_payload(decode=True) or b""
    if isinstance(payload, str):
        text = payload
    else:
        text = payload.decode(message.get_content_charset() or "utf-8", errors="replace")
    if message.get_content_type() == "text/html":
        return html_to_text(text)
    return text.strip()


def _address(message: Message) -> Tuple[str, str]:
    raw = decode_mime(message.get("From"))
    match = re.search(r"^(?P<name>.*?)\s*<(?P<addr>[^>]+)>$", raw)
    if match:
        return match.group("addr").strip(), match.group("name").strip().strip('"')
    return raw.strip(), ""


def _sent_at(message: Message) -> Optional[datetime]:
    raw = message.get("Date")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def imap_login_ok(settings: Settings) -> bool:
    try:
        client = imaplib.IMAP4_SSL(settings.imap_host, int(settings.imap_port), timeout=8)
    except Exception:
        return False
    try:
        client.login(settings.imap_user, settings.imap_password)
        return True
    except Exception:
        return False
    finally:
        try:
            client.logout()
        except Exception:
            pass


def fetch_new_mail(settings: Settings, last_uid: int) -> List[RawEmail]:
    client = imaplib.IMAP4_SSL(settings.imap_host, int(settings.imap_port), timeout=20)
    try:
        client.login(settings.imap_user, settings.imap_password)
        status, _ = client.select(settings.imap_folder, readonly=True)
        if status != "OK":
            raise RuntimeError("Could not open the mailbox.")
        if last_uid > 0:
            status, data = client.uid("SEARCH", None, "UID %d:*" % (last_uid + 1))
        else:
            since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%d-%b-%Y")
            status, data = client.uid("SEARCH", None, "(SINCE %s)" % since)
        if status != "OK" or not data or not data[0]:
            return []
        uids = sorted(int(item) for item in data[0].split())
        uids = [uid for uid in uids if uid > last_uid][:40]
        messages: List[RawEmail] = []
        for uid in uids:
            status, fetched = client.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if status != "OK" or not fetched:
                continue
            raw = _payload_bytes(fetched)
            if not raw:
                continue
            message = email.message_from_bytes(raw)
            from_addr, from_name = _address(message)
            subject = decode_mime(message.get("Subject")) or "(no subject)"
            message_id = (message.get("Message-ID") or "").strip() or "uid-%s" % uid
            body = extract_body(message)[:8000]
            messages.append(
                RawEmail(
                    uid=uid,
                    message_id=message_id,
                    from_addr=from_addr,
                    from_name=from_name,
                    subject=subject[:500],
                    sent_at=_sent_at(message),
                    body=body,
                )
            )
        return messages
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _payload_bytes(fetched) -> bytes:
    for item in fetched:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    return b""
