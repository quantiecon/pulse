"""A browser window the user clicks. Pulse never types passwords or reads screenshots."""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from berkeleypulse.config import data_dir, load_settings

PORT = 9333
TARGETS = {
    "canvas": "https://bcourses.berkeley.edu",
    "calcentral": "https://calcentral.berkeley.edu",
    "mail": "https://mail.google.com/mail/u/0/",
}
KEEP_SUFFIXES = (
    "berkeley.edu",
    "instructure.com",
    "google.com",
    "googlemail.com",
)


class DeskError(RuntimeError):
    pass


def keep_cookie(domain: str) -> bool:
    host = (domain or "").lower().lstrip(".")
    return any(host == suffix or host.endswith("." + suffix) for suffix in KEEP_SUFFIXES)


def browser_base() -> str:
    raw = os.environ.get("PULSE_BROWSER_URL", "http://127.0.0.1:%d" % PORT).rstrip("/")
    return raw


def session_status() -> Dict[str, Any]:
    grants = _read_grants()
    return {
        "window_open": debugger_open(),
        "canvas": bool(grants.get("canvas")),
        "calcentral": bool(grants.get("calcentral")),
        "mail": bool(grants.get("mail")),
        "saved": any(bool(grants.get(name)) for name in ("canvas", "calcentral", "mail")),
    }


def connection_state(saved: bool, ok: bool) -> str:
    if not saved:
        return "off"
    return "connected" if ok else "failed"


_ACCESS_CACHE: Dict[str, Any] = {"at": 0.0, "report": None}


def access_report() -> Dict[str, Any]:
    now = time.time()
    cached = _ACCESS_CACHE.get("report")
    if cached and now - float(_ACCESS_CACHE.get("at") or 0) < 20:
        report = dict(cached)
        report["window_open"] = debugger_open()
        return report
    report = _probe_access()
    _ACCESS_CACHE["at"] = now
    _ACCESS_CACHE["report"] = report
    return dict(report)


def _clear_access_cache() -> None:
    _ACCESS_CACHE["at"] = 0.0
    _ACCESS_CACHE["report"] = None


def debugger_open() -> bool:
    try:
        httpx.get(browser_base() + "/json/version", timeout=0.4)
    except httpx.HTTPError:
        return False
    return True


def open_desk(target: str) -> str:
    url = TARGETS.get(target)
    if not url:
        raise DeskError("Choose Canvas, CalCentral, or mail.")
    if not debugger_open():
        if os.environ.get("PULSE_BROWSER_URL"):
            raise DeskError("The sign-in browser is not running. Start it on the computer you want to click on.")
        _launch_chrome(url)
        if not _wait_for_debugger():
            raise DeskError("The sign-in window did not start.")
    _navigate(url)
    return "The sign-in window is open on %s. Finish any text message or Duo prompt there, then save." % _label(target)


def _launch_chrome(url: str) -> None:
    binary = chrome_path()
    if not binary:
        raise DeskError("Google Chrome is not installed on this computer, so there is no window to click in.")
    profile = data_dir() / "chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            binary,
            "--user-data-dir=%s" % profile,
            "--remote-debugging-port=%d" % PORT,
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            "--window-position=80,60",
            "--window-size=1200,840",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _wait_for_debugger() -> bool:
    for _ in range(40):
        if debugger_open():
            return True
        time.sleep(0.25)
    return False


def save_session() -> str:
    if not debugger_open():
        raise DeskError("The sign-in window is not open. Open it, finish the prompt, then save.")
    cookies = [item for item in _all_cookies() if keep_cookie(item.get("domain", ""))]
    usable = []
    for item in cookies:
        name = item.get("name") or ""
        value = item.get("value") or ""
        domain = item.get("domain") or ""
        if not name or not value or not domain:
            continue
        usable.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": item.get("path") or "/",
                "secure": bool(item.get("secure", True)),
            }
        )
    if not usable:
        raise DeskError("Finish the prompt in the window, then save. Pulse has not kept a key yet.")
    grants = {
        "canvas": _canvas_accepts(usable),
        "calcentral": _calcentral_accepts(usable),
        "mail": _mail_accepts(usable),
    }
    if not any(grants.values()):
        raise DeskError("The prompt is still open. Finish it in the window, then save. Pulse keeps a key only after that site accepts you.")
    kept = [item for item in usable if _cookie_for_grants(item, grants)]
    path = data_dir() / "session.json"
    path.write_text(
        json.dumps(
            {
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "grants": grants,
                "cookies": kept,
            },
            indent=2,
        )
        + "\n"
    )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _clear_access_cache()
    return _describe(grants)


def read_page_text() -> str:
    if not debugger_open():
        raise DeskError("Open the sign-in window and go to the syllabus page first.")
    pages = _get_json("/json/list")
    page = None
    for item in pages:
        if item.get("type") == "page" and item.get("webSocketDebuggerUrl"):
            page = item
            break
    if page is None:
        raise DeskError("The sign-in window has no page yet.")
    result = _cdp(
        page["webSocketDebuggerUrl"],
        "Runtime.evaluate",
        {"expression": "document.body ? document.body.innerText : ''", "returnByValue": True},
    )
    value = (((result.get("result") or {}).get("result") or {}).get("value")) or ""
    text = str(value).strip()
    if len(text) < 40:
        raise DeskError("That page did not have enough text. Open the syllabus, then save the page.")
    return text[:200000]


def load_cookie_jar(service: str = "") -> httpx.Cookies:
    jar = httpx.Cookies()
    for item in _read_saved():
        domain = item.get("domain") or ""
        if service == "canvas" and not _school_domain(domain):
            continue
        if service == "mail" and not _google_domain(domain):
            continue
        if service == "calcentral" and "calcentral" not in domain and "berkeley.edu" not in domain:
            continue
        jar.set(item["name"], item["value"], domain=domain, path=item.get("path") or "/")
    return jar


def chrome_path() -> Optional[str]:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _probe_access() -> Dict[str, Any]:
    from berkeleypulse.mail import imap_login_ok

    settings = load_settings()
    grants = _read_grants()
    cookies = _read_saved()
    canvas_saved = bool(settings.canvas_token.strip() or grants.get("canvas"))
    canvas_ok = _canvas_live(settings, cookies) if canvas_saved else False
    cal_saved = bool(grants.get("calcentral"))
    cal_ok = _calcentral_accepts(cookies) if cal_saved else False
    if grants.get("mail"):
        mail_saved = True
        mail_ok = _mail_accepts(cookies)
    elif settings.mail_ready:
        mail_saved = True
        mail_ok = imap_login_ok(settings)
    else:
        mail_saved = False
        mail_ok = False
    return {
        "canvas": connection_state(canvas_saved, canvas_ok),
        "calcentral": connection_state(cal_saved, cal_ok),
        "mail": connection_state(mail_saved, mail_ok),
        "window_open": debugger_open(),
    }


def _canvas_live(settings, cookies: List[Dict[str, Any]]) -> bool:
    if settings.canvas_token.strip():
        try:
            response = httpx.get(
                settings.canvas_base_url + "/api/v1/users/self",
                headers={"Authorization": "Bearer %s" % settings.canvas_token.strip(), "Accept": "application/json"},
                timeout=8,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            return False
        return response.status_code == 200
    return _canvas_accepts(cookies)


def _canvas_accepts(cookies: List[Dict[str, str]]) -> bool:
    jar = httpx.Cookies()
    for item in cookies:
        domain = item.get("domain") or ""
        if "bcourses" not in domain and "instructure" not in domain:
            continue
        jar.set(item["name"], item["value"], domain=domain, path=item.get("path") or "/")
    if not list(jar.jar):
        return False
    try:
        response = httpx.get(
            load_settings().canvas_base_url + "/api/v1/users/self",
            cookies=jar,
            headers={"Accept": "application/json"},
            timeout=8,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def _label(target: str) -> str:
    return {"canvas": "bCourses", "calcentral": "CalCentral", "mail": "mail"}.get(target, "the site")


def _describe(grants: Dict[str, bool]) -> str:
    names = []
    if grants.get("canvas"):
        names.append("bCourses")
    if grants.get("calcentral"):
        names.append("CalCentral")
    if grants.get("mail"):
        names.append("mail")
    joined = ", ".join(names)
    return "Saved %s on this computer. The keys stay in the Pulse folder. Your password was not stored." % joined


def _school_domain(domain: str) -> bool:
    host = domain.lower()
    return "berkeley.edu" in host or "instructure.com" in host


def _google_domain(domain: str) -> bool:
    host = domain.lower()
    return "google.com" in host or "googlemail.com" in host


def _cookie_for_grants(cookie: Dict[str, str], grants: Dict[str, bool]) -> bool:
    domain = cookie.get("domain") or ""
    if _google_domain(domain):
        return bool(grants.get("mail"))
    if _school_domain(domain):
        return bool(grants.get("canvas") or grants.get("calcentral"))
    return False


def _calcentral_accepts(cookies: List[Dict[str, str]]) -> bool:
    if not any("calcentral" in (item.get("domain") or "") for item in cookies):
        return False
    jar = httpx.Cookies()
    for item in cookies:
        if _school_domain(item.get("domain") or ""):
            jar.set(item["name"], item["value"], domain=item.get("domain"), path=item.get("path") or "/")
    try:
        response = httpx.get(
            "https://calcentral.berkeley.edu/api/my/status",
            cookies=jar,
            timeout=8,
            follow_redirects=False,
            headers={"User-Agent": "Pulse", "Accept": "application/json"},
        )
    except httpx.HTTPError:
        return False
    if response.status_code != 200:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return calcentral_logged_in(payload)


def calcentral_logged_in(payload) -> bool:
    return isinstance(payload, dict) and payload.get("isLoggedIn") is True


def _mail_accepts(cookies: List[Dict[str, str]]) -> bool:
    if not any(_google_domain(item.get("domain") or "") for item in cookies):
        return False
    jar = httpx.Cookies()
    for item in cookies:
        if _google_domain(item.get("domain") or ""):
            jar.set(item["name"], item["value"], domain=item.get("domain"), path=item.get("path") or "/")
    try:
        response = httpx.get(
            "https://mail.google.com/mail/feed/atom",
            cookies=jar,
            timeout=8,
            follow_redirects=True,
            headers={"User-Agent": "Pulse"},
        )
    except httpx.HTTPError:
        return False
    return response.status_code == 200 and b"<feed" in response.content[:800]


def _read_session() -> Dict[str, Any]:
    path = data_dir() / "session.json"
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _read_grants() -> Dict[str, bool]:
    grants = _read_session().get("grants")
    if not isinstance(grants, dict):
        return {}
    return {key: bool(grants.get(key)) for key in ("canvas", "calcentral", "mail")}


def _read_saved() -> List[Dict[str, Any]]:
    cookies = _read_session().get("cookies")
    if not isinstance(cookies, list):
        return []
    return [item for item in cookies if isinstance(item, dict)]


def _navigate(url: str) -> None:
    version = _get_json("/json/version")
    browser = version.get("webSocketDebuggerUrl")
    if not browser:
        raise DeskError("The sign-in window did not accept a new page.")
    pages = [
        item
        for item in _get_json("/json/list")
        if item.get("type") == "page" and item.get("webSocketDebuggerUrl")
    ]
    if not pages:
        created = _cdp(browser, "Target.createTarget", {"url": url, "newWindow": True})
        target_id = (created.get("result") or {}).get("targetId")
        if not target_id:
            raise DeskError("The sign-in window did not open a page.")
        _place_window(browser, target_id)
        return
    page = pages[0]
    _cdp(page["webSocketDebuggerUrl"], "Page.navigate", {"url": url})
    _place_window(browser, page.get("id"))


def _place_window(browser: str, target_id: Optional[str]) -> None:
    if not target_id:
        return
    try:
        _cdp(browser, "Target.activateTarget", {"targetId": target_id})
        found = _cdp(browser, "Browser.getWindowForTarget", {"targetId": target_id})
        window_id = (found.get("result") or {}).get("windowId")
        if window_id is None:
            return
        _cdp(
            browser,
            "Browser.setWindowBounds",
            {
                "windowId": window_id,
                "bounds": {
                    "left": 80,
                    "top": 60,
                    "width": 1200,
                    "height": 840,
                    "windowState": "normal",
                },
            },
        )
    except DeskError:
        return


def _all_cookies() -> List[Dict[str, Any]]:
    sockets = []
    for item in _get_json("/json/list"):
        if item.get("type") == "page" and item.get("webSocketDebuggerUrl"):
            sockets.append(item["webSocketDebuggerUrl"])
    version = _get_json("/json/version")
    browser = version.get("webSocketDebuggerUrl")
    if browser:
        sockets.append(browser)
    if not sockets:
        raise DeskError("The sign-in window is not open. Open it, finish the prompt, then save.")
    last_error = None
    for socket_url in sockets:
        for method in ("Network.getAllCookies", "Storage.getCookies"):
            try:
                result = _cdp(socket_url, method)
            except DeskError as exc:
                last_error = exc
                continue
            cookies = (result.get("result") or {}).get("cookies") or []
            if isinstance(cookies, list):
                return cookies
    if last_error is not None:
        raise last_error
    return []


def _get_json(path: str):
    try:
        response = httpx.get(browser_base() + path, timeout=3.0)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        raise DeskError("The sign-in window could not be reached.") from exc


def _cdp(socket_url: str, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    parsed = urlparse(socket_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path = path + "?" + parsed.query
    try:
        sock = socket.create_connection((host, port), 5)
    except OSError as exc:
        raise DeskError("The sign-in window could not be reached.") from exc
    sock.settimeout(8)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%s\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ) % (path, host, port, key)
        sock.sendall(request.encode())
        buffered = _read_until(sock, b"\r\n\r\n")
        head, _, rest = buffered.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise DeskError("The sign-in window refused the connection.")
        payload = json.dumps({"id": 1, "method": method, "params": params or {}}).encode()
        sock.sendall(_client_frame(payload, opcode=1))
        pending = rest
        while True:
            message, pending = _next_message(sock, pending)
            if message.get("id") == 1:
                if message.get("error"):
                    raise DeskError("The sign-in window could not be read.")
                return message
    except socket.timeout as exc:
        raise DeskError("The sign-in window did not answer.") from exc
    finally:
        sock.close()


def _read_until(sock: socket.socket, marker: bytes) -> bytes:
    data = b""
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise DeskError("The sign-in window closed.")
        data += chunk
    return data


def _client_frame(payload: bytes, opcode: int = 1) -> bytes:
    mask = os.urandom(4)
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(length.to_bytes(2, "big"))
    else:
        header.append(0x80 | 127)
        header.extend(length.to_bytes(8, "big"))
    header.extend(mask)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + masked


def _next_message(sock: socket.socket, pending: bytes):
    pieces = []
    while True:
        frame, pending = _read_frame(sock, pending)
        opcode = frame["opcode"]
        if opcode == 8:
            raise DeskError("The sign-in window disconnected.")
        if opcode == 9:
            sock.sendall(_client_frame(frame["data"], opcode=10))
            continue
        if opcode in (0, 1):
            pieces.append(frame["data"])
            if frame["fin"]:
                try:
                    return json.loads(b"".join(pieces).decode()), pending
                except json.JSONDecodeError as exc:
                    raise DeskError("The sign-in window sent something Pulse could not read.") from exc


def _read_frame(sock: socket.socket, pending: bytes):
    pending = _fill(sock, pending, 2)
    first = pending[0]
    second = pending[1]
    offset = 2
    length = second & 0x7F
    if length == 126:
        pending = _fill(sock, pending, 4)
        length = int.from_bytes(pending[2:4], "big")
        offset = 4
    elif length == 127:
        pending = _fill(sock, pending, 10)
        length = int.from_bytes(pending[2:10], "big")
        offset = 10
    masked = bool(second & 0x80)
    if masked:
        pending = _fill(sock, pending, offset + 4 + length)
        mask = pending[offset : offset + 4]
        raw = pending[offset + 4 : offset + 4 + length]
        data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(raw))
        offset = offset + 4 + length
    else:
        pending = _fill(sock, pending, offset + length)
        data = pending[offset : offset + length]
        offset += length
    return {"fin": bool(first & 0x80), "opcode": first & 0x0F, "data": data}, pending[offset:]


def _fill(sock: socket.socket, pending: bytes, size: int) -> bytes:
    while len(pending) < size:
        chunk = sock.recv(65536)
        if not chunk:
            raise DeskError("The sign-in window closed.")
        pending += chunk
    return pending
