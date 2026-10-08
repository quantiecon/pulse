from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import httpx

from berkeleypulse.config import Settings

LINK_RE = re.compile(r"<([^>]+)>\s*;\s*rel=\"?next\"?", re.IGNORECASE)


class CanvasError(RuntimeError):
    pass


def next_url(link_header: Optional[str]) -> Optional[str]:
    if not link_header:
        return None
    match = LINK_RE.search(link_header)
    if not match:
        return None
    return match.group(1)


def verify_canvas_token(base_url: str, token: str) -> str:
    """Confirm a token with Canvas and return the account name. The token is not logged."""
    url = base_url.rstrip("/") + "/api/v1/users/self"
    try:
        response = httpx.get(
            url,
            headers={"Authorization": "Bearer %s" % token, "Accept": "application/json"},
            timeout=12,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise CanvasError("bCourses could not be reached. Check the connection and try the token again.") from exc
    if response.status_code in {401, 403} or response.is_redirect:
        raise CanvasError("bCourses rejected that token. Generate a new one and paste it again.")
    if response.status_code != 200:
        raise CanvasError("bCourses did not accept that token (%s)." % response.status_code)
    try:
        payload = response.json()
    except Exception as exc:
        raise CanvasError("bCourses did not return an account for that token.") from exc
    name = ""
    if isinstance(payload, dict):
        name = str(payload.get("name") or payload.get("short_name") or "").strip()
    if not name:
        raise CanvasError("bCourses did not return an account for that token.")
    return name


def _error_message(response: httpx.Response, used_token: bool) -> str:
    if response.status_code in {401, 403}:
        if used_token:
            return "Canvas rejected the token. On Connect, generate a new access token and paste it again."
        return "Canvas did not accept the saved sign-in. Open the sign-in window and finish the prompt again."
    try:
        payload = response.json()
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors and isinstance(errors, list):
            message = errors[0].get("message")
            if message:
                return "Canvas request failed: %s" % message
    except Exception:
        pass
    return "Canvas request failed (%s)." % response.status_code


def canvas_get(
    settings: Settings,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    cookies: Optional[httpx.Cookies] = None,
) -> Any:
    headers = {"Accept": "application/json"}
    used_token = bool(settings.canvas_token.strip())
    if used_token:
        headers["Authorization"] = "Bearer %s" % settings.canvas_token.strip()
    elif cookies is None or not list(cookies.jar):
        raise CanvasError("Canvas is not connected. On Connect, paste an access token.")
    url = path if path.startswith("http") else settings.canvas_base_url + path
    collected: List[Any] = []
    first = True
    with httpx.Client(timeout=20.0, cookies=cookies) as client:
        while url:
            response = client.get(url, headers=headers, params=params if first else None)
            first = False
            if response.status_code >= 400:
                raise CanvasError(_error_message(response, used_token))
            data = response.json()
            if isinstance(data, list):
                collected.extend(data)
                url = next_url(response.headers.get("Link"))
                continue
            return data
    return collected


def fetch_courses(settings: Settings, cookies: Optional[httpx.Cookies] = None) -> List[Dict[str, Any]]:
    courses = canvas_get(
        settings,
        "/api/v1/courses",
        {
            "enrollment_state": "active",
            "state[]": "available",
            "include[]": ["syllabus_body", "term"],
            "per_page": 50,
        },
        cookies=cookies,
    )
    usable = []
    for course in courses:
        if course.get("access_restricted_by_date"):
            continue
        if not course.get("name"):
            continue
        usable.append(course)
    return usable


def fetch_root_folder(settings: Settings, course_id: str, cookies: Optional[httpx.Cookies] = None) -> Dict[str, Any]:
    folder = canvas_get(settings, "/api/v1/courses/%s/folders/root" % course_id, cookies=cookies)
    return folder if isinstance(folder, dict) else {}


def fetch_folder_children(
    settings: Settings,
    folder_id: str,
    cookies: Optional[httpx.Cookies] = None,
) -> List[Dict[str, Any]]:
    folders = canvas_get(
        settings,
        "/api/v1/folders/%s/folders" % folder_id,
        {"per_page": 50},
        cookies=cookies,
    )
    files = canvas_get(
        settings,
        "/api/v1/folders/%s/files" % folder_id,
        {"per_page": 50},
        cookies=cookies,
    )
    nodes = [_folder_node(item) for item in folders if isinstance(item, dict)]
    nodes.extend(_file_node(item) for item in files if isinstance(item, dict))
    return [node for node in nodes if node]


def fetch_file_bytes(settings: Settings, url: str, cookies: Optional[httpx.Cookies] = None) -> bytes:
    headers = {}
    used_token = bool(settings.canvas_token.strip())
    if used_token:
        headers["Authorization"] = "Bearer %s" % settings.canvas_token.strip()
    elif cookies is None or not list(cookies.jar):
        raise CanvasError("Canvas is not connected. On Connect, paste an access token.")
    with httpx.Client(timeout=20.0, cookies=cookies, follow_redirects=True) as client:
        response = client.get(url, headers=headers)
    if response.status_code >= 400:
        raise CanvasError(_error_message(response, used_token))
    return response.content


def fetch_announcements(
    settings: Settings,
    course_id: str,
    start_date: str,
    cookies: Optional[httpx.Cookies] = None,
) -> List[Dict[str, Any]]:
    items = canvas_get(
        settings,
        "/api/v1/announcements",
        {
            "context_codes[]": "course_%s" % course_id,
            "start_date": start_date,
            "active_only": True,
            "per_page": 30,
        },
        cookies=cookies,
    )
    return [item for item in items if isinstance(item, dict)]


def _folder_node(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not item.get("id"):
        return None
    return {
        "id": "folder:%s" % item["id"],
        "canvas_id": str(item["id"]),
        "name": (item.get("name") or "Folder")[:300],
        "kind": "folder",
        "updated_at": item.get("updated_at") or "",
    }


def _file_node(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not item.get("id"):
        return None
    return {
        "id": "file:%s" % item["id"],
        "canvas_id": str(item["id"]),
        "name": (item.get("display_name") or item.get("filename") or "File")[:300],
        "kind": "file",
        "updated_at": item.get("updated_at") or item.get("modified_at") or "",
        "content_type": item.get("content-type") or "",
        "url": item.get("url") or "",
        "size": item.get("size") or 0,
    }


def fetch_assignment_groups(
    settings: Settings,
    course_id: str,
    cookies: Optional[httpx.Cookies] = None,
) -> List[Dict[str, Any]]:
    return canvas_get(
        settings,
        "/api/v1/courses/%s/assignment_groups" % course_id,
        {"include[]": ["assignments"], "per_page": 50},
        cookies=cookies,
    )
