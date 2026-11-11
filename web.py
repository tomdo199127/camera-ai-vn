"""Web quản lý: biển số và lịch trình, cảnh báo vùng, khuôn mặt người quen, cài đặt Telegram."""

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta

import cv2
import httpx
import numpy as np

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from common import (BASE_DIR, GO2RTC_API, IMAGES_DIR, REC_DIR, REC_STATUS, RTSP_HOST, go2rtc_sources, NOTIFY_MODES, connect,
                    get_settings, init_db, merge_visitors, notify_matrix, rec_limit_gb)
import ai
import search
from detector import GROUP_LABELS
from backfill import SEGMENT, file_start, note_skip
from recorder import LIVE_AUDIO_FOLLOW, audio_args, probe

# thử link camera đọc RTSP giống worker: qua TCP, tắt cảnh báo HEVC lúc chờ keyframe
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")
from notify import telegram_call
from plates import normalize_plate, plate_key

WEB_USER = os.environ.get("WEB_USER", "admin")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")  # chỉ dùng tạo admin đầu tiên (bootstrap_admin)

PLATE = "COALESCE(s.corrected_plate, s.plate)"

init_db()
# Mỗi request một kết nối SQLite riêng (mở khi cần, đóng khi xong). Dùng chung một kết nối giữa nhiều thread
# từng làm web kẹt ở một "ảnh chụp" DB cũ: trang hiện số liệu cũ hàng giờ và mọi lần ghi báo "database is locked".
_request_conn: ContextVar[dict | None] = ContextVar("request_conn", default=None)


class RequestDB:
    def _conn(self) -> sqlite3.Connection:
        holder = _request_conn.get()
        if holder is None:
            raise RuntimeError("db chỉ dùng được trong request")
        if "conn" not in holder:
            holder["conn"] = connect()
        return holder["conn"]

    def __getattr__(self, name):
        return getattr(self._conn(), name)


db = RequestDB()
app = FastAPI(title="Quản lý biển số")


@app.middleware("http")
async def request_db(request: Request, call_next):
    holder: dict = {}
    token = _request_conn.set(holder)
    try:
        return await call_next(request)
    finally:
        _request_conn.reset(token)
        conn = holder.pop("conn", None)
        if conn is not None:
            if conn.in_transaction:  # request lỗi giữa chừng: bỏ phần chưa commit, không giữ khoá
                conn.rollback()
            conn.close()


SESSION_COOKIE = "lpr_session"
# phiên gần như không hết hạn: admin xem được thiết bị đang đăng nhập ở đâu, đăng xuất hoặc khoá từ xa
SESSION_DAYS = int(os.environ.get("WEB_SESSION_DAYS", "3650"))
COOKIE_DAYS = 400  # trình duyệt không giữ cookie lâu hơn mức này nên phát lại mỗi lần gia hạn
# trang/tệp mở được khi chưa đăng nhập (không chứa dữ liệu camera)
PUBLIC_PATHS = {"/login", "/api/login", "/manifest.webmanifest", "/sw.js", "/favicon.ico"}
PUBLIC_PREFIXES = ["/static/login.", "/static/icons/", "/hls/"]  # /hls/ tự kiểm tra bằng mã trong đường dẫn
LOGIN_FAILS: dict[str, list[float]] = {}  # ip -> thời điểm các lần sai gần đây
MAX_FAILS, FAIL_WINDOW = 5, 600

# các trang trong menu; thành viên được mở trang nào thì có quyền tương ứng ("users" = quản lý thành viên)
PAGES = {"playback": "Xem, xem lại", "detections": "Nhận diện", "events": "Cảnh báo",
         "people": "Khuôn mặt", "cameras": "Camera", "settings": "Cài đặt",
         "users": "Người dùng"}
# đầu đường dẫn -> (quyền để đọc, quyền để sửa): có một trong các quyền là được. Một trang hay dùng dữ liệu
# của trang khác (vd Cảnh báo cần danh sách người quen) nên một API có thể thuộc nhiều trang.
# Đường dẫn /api/ không có ở đây và cũng không trong OPEN_PATHS thì chỉ admin dùng được.
PATH_PERMS = [
    ("/api/stats", {"detections", "events"}, set()),
    ("/api/sightings", {"detections"}, {"detections"}),
    ("/api/plates", {"detections"}, {"detections"}),
    ("/api/events", {"events", "people"}, {"events", "people"}),
    ("/api/people", {"people", "events", "settings"}, {"people"}),
    ("/api/faces", {"people"}, {"people"}),
    ("/api/visitor", {"visitors"}, {"visitors"}),  # /api/visitors, /api/visitor-search
    ("/api/visits", {"visitors"}, {"visitors"}),
    ("/api/recordings", {"playback", "cameras", "settings"}, set()),
    ("/api/live", {"playback"}, set()),
    ("/rec/", {"playback"}, set()),
    ("/api/cameras", {"cameras"}, {"cameras"}),
    ("/api/zones", {"cameras"}, {"cameras"}),
    ("/api/settings", {"settings"}, {"settings"}),
    ("/api/users", {"users"}, {"users"}),
]
# ai đăng nhập cũng dùng được (ảnh chụp đi kèm dữ liệu của nhiều trang)
OPEN_PATHS = {"/api/me", "/api/me/password", "/api/me/camera-order", "/api/logout", "/api/sessions", "/api/version", "/api/health"}
OPEN_PREFIXES = ("/api/sessions/", "/images/")
PW_ITERS = 240_000
USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]{2,32}")


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    # qua ngrok (agent chạy trên chính server) thì IP thật là phần tử cuối do ngrok thêm vào X-Forwarded-For;
    # các phần tử trước do người gửi tự đặt được nên không tin
    fwd = request.headers.get("x-forwarded-for", "")
    if peer in ("127.0.0.1", "::1") and fwd:
        return fwd.split(",")[-1].strip()
    return peer


def client_location(request: Request, ip: str) -> str:
    """Vị trí gần đúng của IP, cỡ thành phố, theo header của Cloudflare. Không so request.client với localhost được:
    qua cloudflared thì uvicorn đã đổi nó thành IP thật; máy trong LAN tự gửi header thì đã dừng ở nhánh mạng nội bộ."""
    try:
        if ipaddress.ip_address(ip).is_private:
            return "Mạng nội bộ"
    except ValueError:
        return ""
    country = request.headers.get("cf-ipcountry", "")
    if country == "T1":
        return "Mạng ẩn danh Tor"
    parts = []
    for v in (request.headers.get("cf-ipcity", ""), request.headers.get("cf-region", ""), "" if country == "XX" else country):
        v = urllib.parse.unquote(v.encode("latin-1", "replace").decode("utf-8", "replace")).strip()
        if v and not any(v.startswith(p) for p in parts):  # "Ho Chi Minh City" rồi "Ho Chi Minh City (HCMC)"
            parts.append(v)
    return ", ".join(parts)[:80]


def set_session_cookie(resp, request: Request, token: str):
    https = request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"
    resp.set_cookie(SESSION_COOKIE, token, max_age=COOKIE_DAYS * 86400, httponly=True, samesite="lax", secure=https, path="/")


@contextmanager
def auth_db():
    """Kết nối riêng, ngắn cho phiên đăng nhập. Dùng chung `db` giữa middleware và các thread
    dễ còn treo một giao dịch đọc cũ, khi ghi SQLite báo "database is locked" ngay lập tức."""
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PW_ITERS)
    return f"pbkdf2_sha256${PW_ITERS}${salt.hex()}${dk.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, iters, salt, want = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(iters))
    except ValueError:
        return False
    return secrets.compare_digest(dk.hex(), want)


DUMMY_HASH = hash_password(secrets.token_urlsafe())  # tên không có vẫn tốn thời gian như tên có, khỏi dò tên


def bootstrap_admin():
    """Bảng users còn trống mà .env có WEB_PASSWORD: tạo admin từ WEB_USER/WEB_PASSWORD (cách đăng nhập cũ).
    Sau đó mật khẩu đổi trên web, sửa .env không còn tác dụng."""
    with auth_db() as c:
        if WEB_PASSWORD and not c.execute("SELECT 1 FROM users").fetchone():
            c.execute("INSERT OR IGNORE INTO users(username, pw_hash, is_admin, created_at) VALUES (?, ?, 1, ?)",
                      (WEB_USER, hash_password(WEB_PASSWORD), time.time()))


bootstrap_admin()
# chưa có tài khoản nào (không đặt WEB_PASSWORD): web mở, ai vào cũng như admin
OPEN_USER = {"id": 0, "username": "", "is_admin": True, "perms": set(PAGES), "cams": None, "open": True}
_user: ContextVar[dict | None] = ContextVar("user", default=None)  # middleware đặt cho từng request


def user_info(r) -> dict:
    perms = set(PAGES) if r["is_admin"] else {p for p in r["perms"].split(",") if p in PAGES}
    cams = None if r["is_admin"] or not r["cameras"] else set(r["cameras"].split(","))
    return {"id": r["id"], "username": r["username"], "is_admin": bool(r["is_admin"]), "perms": perms, "cams": cams}


def visible_cams() -> set[str] | None:
    """Các camera tài khoản đang dùng được xem; None = tất cả."""
    u = _user.get()
    return u["cams"] if u else None


def cam_in(col: str) -> str:
    """Điều kiện SQL thêm vào WHERE để chỉ lấy dữ liệu của camera được xem ('' nếu xem được tất cả).
    ID camera ghi thẳng vào câu SQL (đã thoát dấu ') để khỏi phải xếp thứ tự tham số trong các câu lồng nhau."""
    cams = visible_cams()
    if cams is None:
        return ""
    quoted = ",".join("'" + c.replace("'", "''") + "'" for c in sorted(cams))
    return f" AND {col} IN ({quoted or 'NULL'})"


def check_cam(cam: str):
    cams = visible_cams()
    if cams is not None and cam not in cams:
        raise HTTPException(404, "Không có camera này")


def check_row_cam(table: str, rid: int):
    """Bản ghi (biển số, cảnh báo, vùng, lượt qua lại) của camera không được xem: coi như không có."""
    if visible_cams() is not None:
        r = db.execute(f"SELECT camera FROM {table} WHERE id = ?", (rid,)).fetchone()
        if r:
            check_cam(r["camera"])


def path_cam_ok(user: dict, path: str) -> bool:
    """Video /rec/<camera>/... và ảnh <camera>_....jpg trong /images/ chỉ mở được khi xem được camera đó."""
    cams = user["cams"]
    if cams is None:
        return True
    if path.startswith("/rec/"):
        return path.split("/")[2] in cams
    name = path.rsplit("/", 1)[-1]
    if any(name.startswith(c + "_") for c in cams):
        return True
    with auth_db() as c:  # ID camera có thể chứa "_": đã khớp camera được xem ở trên thì thôi
        return not any(name.startswith(r[0] + "_") for r in c.execute("SELECT id FROM cameras"))


def session_username(c, request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    now = time.time()
    row = c.execute("SELECT username, expires_at, last_seen, ip, location FROM sessions WHERE token_hash = ?",
                    (token_hash(token),)).fetchone()
    if not row or row["expires_at"] < now:
        return None
    ip = client_ip(request)
    location = client_location(request, ip)
    # gia hạn trượt; đổi mạng hoặc đổi vị trí thì admin thấy ngay chỗ mới
    if now - row["last_seen"] > 3600 or ip != row["ip"] or (location and location != row["location"]):
        c.execute("UPDATE sessions SET last_seen = ?, expires_at = ?, ip = ?, location = ? WHERE token_hash = ?",
                  (now, now + SESSION_DAYS * 86400, ip, location, token_hash(token)))
        request.state.renew_session = token
    return row["username"]


def basic_username(c, request) -> str | None:
    """Vẫn nhận HTTP Basic cho script/curl, nhưng trình duyệt không còn bị hỏi bằng hộp thoại nữa."""
    header = request.headers.get("authorization", "")
    if not header.startswith("Basic "):
        return None
    try:
        user, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
    except Exception:
        return None
    r = c.execute("SELECT username, pw_hash FROM users WHERE username = ?", (user,)).fetchone()
    return r["username"] if r and check_password(pwd, r["pw_hash"]) else None


def current_user(request) -> dict | None:
    """Tài khoản đang dùng (cookie phiên hoặc HTTP Basic); None = chưa đăng nhập hoặc tài khoản đã khoá."""
    with auth_db() as c:
        if not c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return OPEN_USER
        name = session_username(c, request) or basic_username(c, request)
        r = name and c.execute("SELECT * FROM users WHERE username = ? AND disabled = 0", (name,)).fetchone()
        return user_info(r) if r else None


def allowed(user: dict, method: str, path: str) -> bool:
    if user["is_admin"] or not path.startswith(("/api/", "/rec/")):
        return True
    if path in OPEN_PATHS or path.startswith(OPEN_PREFIXES) or (path == "/api/cameras" and method == "GET"):
        return True  # danh sách camera: trang nào cũng cần tên camera
    for prefix, read, write in PATH_PERMS:
        if path.startswith(prefix):
            return bool((read if method in ("GET", "HEAD") else write) & user["perms"])
    return False


@app.middleware("http")
async def static_revalidate(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/static/") and "cache-control" not in resp.headers:
        resp.headers["cache-control"] = "no-cache"
    return resp


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Bảo vệ toàn bộ web bằng phiên đăng nhập (cookie), mỗi tài khoản chỉ mở được các trang được cấp."""
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith(tuple(PUBLIC_PREFIXES)):
        return await call_next(request)
    user = current_user(request)
    if not user:
        if path.startswith(("/api/", "/images/", "/rec/")):
            return JSONResponse({"detail": "Chưa đăng nhập"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    if not allowed(user, request.method, path) or \
            (path.startswith(("/rec/", "/images/")) and not path_cam_ok(user, path)):
        return JSONResponse({"detail": "Tài khoản của bạn không được mở mục này"}, status_code=403)
    request.state.user = user
    token = _user.set(user)
    try:
        response = await call_next(request)
    finally:
        _user.reset(token)
    renew = getattr(request.state, "renew_session", None)
    if renew and path != "/api/logout":
        set_session_cookie(response, request, renew)
    return response


class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginIn, request: Request):
    ip, now = client_ip(request), time.time()
    fails = [t for t in LOGIN_FAILS.get(ip, []) if now - t < FAIL_WINDOW]
    if len(fails) >= MAX_FAILS:
        wait = int(FAIL_WINDOW - (now - fails[0])) // 60 + 1
        raise HTTPException(429, f"Sai mật khẩu quá nhiều lần, thử lại sau {wait} phút")
    with auth_db() as c:
        r = c.execute("SELECT username, pw_hash, disabled FROM users WHERE username = ?",
                      (body.username.strip(),)).fetchone()
    ok = check_password(body.password, r["pw_hash"] if r else DUMMY_HASH) and r is not None
    if not ok:
        fails.append(now)
        LOGIN_FAILS[ip] = fails
        time.sleep(1)  # làm chậm dò mật khẩu
        raise HTTPException(401, "Sai tên đăng nhập hoặc mật khẩu")
    if r["disabled"]:
        raise HTTPException(403, "Tài khoản đang bị khoá")
    LOGIN_FAILS.pop(ip, None)
    token = secrets.token_urlsafe(32)
    with auth_db() as c:
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        c.execute("INSERT INTO sessions(token_hash, username, created_at, expires_at, last_seen, ip, location, agent)"
                  " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  (token_hash(token), r["username"], now, now + SESSION_DAYS * 86400, now, ip,
                   client_location(request, ip), request.headers.get("user-agent", "")[:200]))
    resp = JSONResponse({"ok": True})
    set_session_cookie(resp, request, token)
    return resp


@app.post("/api/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with auth_db() as c:
            c.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


def sees_all_sessions(user: dict) -> bool:
    return "users" in user["perms"]


@app.get("/api/sessions")
def list_sessions(request: Request):
    user = request.state.user
    mine = token_hash(request.cookies.get(SESSION_COOKIE, ""))
    sql = ("SELECT s.token_hash, s.username, s.created_at, s.last_seen, s.expires_at, s.ip, s.location, s.agent,"
           " u.id AS user_id, u.is_admin FROM sessions s LEFT JOIN users u ON u.username = s.username WHERE s.expires_at > ?")
    args: tuple = (time.time(),)
    if not sees_all_sessions(user):  # thành viên thường chỉ thấy thiết bị của mình
        sql, args = sql + " AND s.username = ?", args + (user["username"],)
    with auth_db() as c:
        out = [dict(r) for r in c.execute(sql + " ORDER BY s.last_seen DESC", args)]
    for r in out:
        r["current"] = r["token_hash"] == mine
        r["id"] = r.pop("token_hash")[:16]
    return out


@app.delete("/api/sessions/{sid}")
def revoke_session(sid: str, request: Request):
    if not re.fullmatch(r"[0-9a-f]{16}", sid):
        raise HTTPException(400, "ID không hợp lệ")
    user = request.state.user
    with auth_db() as c:
        if sees_all_sessions(user):
            c.execute("DELETE FROM sessions WHERE substr(token_hash, 1, 16) = ?", (sid,))
        else:
            c.execute("DELETE FROM sessions WHERE substr(token_hash, 1, 16) = ? AND username = ?",
                      (sid, user["username"]))
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    u = request.state.user
    return {"username": u["username"], "is_admin": u["is_admin"], "open": u.get("open", False),
            "perms": [p for p in PAGES if p in u["perms"]], "pages": PAGES,
            "cameras": None if u["cams"] is None else sorted(u["cams"])}


def check_new_password(pw: str):
    if len(pw) < 8:
        raise HTTPException(400, "Mật khẩu cần ít nhất 8 ký tự")


class MyPasswordIn(BaseModel):
    old: str
    new: str


@app.post("/api/me/password")
def change_my_password(body: MyPasswordIn, request: Request):
    u = request.state.user
    if u.get("open"):
        raise HTTPException(400, "Chưa có tài khoản nào, hãy tạo ở trang Người dùng")
    check_new_password(body.new)
    with auth_db() as c:
        r = c.execute("SELECT pw_hash FROM users WHERE id = ?", (u["id"],)).fetchone()
        if not check_password(body.old, r["pw_hash"]):
            time.sleep(1)
            raise HTTPException(400, "Mật khẩu hiện tại không đúng")
        c.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (hash_password(body.new), u["id"]))
        # các thiết bị khác đang đăng nhập bằng mật khẩu cũ phải đăng nhập lại
        c.execute("DELETE FROM sessions WHERE username = ? AND token_hash != ?",
                  (u["username"], token_hash(request.cookies.get(SESSION_COOKIE, ""))))
    return {"ok": True}


class UserIn(BaseModel):
    username: str | None = None
    password: str | None = None
    is_admin: bool | None = None
    perms: list[str] | None = None
    cameras: list[str] | None = None  # [] = mọi camera
    disabled: bool | None = None


def clean_perms(actor: dict, perms: list[str], is_admin: bool, keep: set = frozenset()) -> str:
    if set(perms) - set(PAGES):
        raise HTTPException(400, "Quyền không hợp lệ")
    # thành viên quản lý người dùng không tự nâng quyền qua tài khoản khác được; quyền người kia đang có thì giữ được
    if not actor["is_admin"] and (is_admin or not set(perms) <= actor["perms"] | keep):
        raise HTTPException(403, "Bạn chỉ cấp được những quyền mà chính bạn đang có")
    return ",".join(p for p in PAGES if p in perms)


def clean_cams(c, actor: dict, cams: list[str], had: set | None = frozenset()) -> str:
    """'' = xem mọi camera. had: camera người kia đang được xem (None = tất cả; tài khoản mới: rỗng), giữ lại được."""
    if set(cams) - {r[0] for r in c.execute("SELECT id FROM cameras")}:
        raise HTTPException(400, "Có camera không còn tồn tại, tải lại trang rồi thử lại")
    limit = actor["cams"]
    if limit is not None:
        keeps_all = not cams and had is None
        if not keeps_all and (not cams or not set(cams) <= limit | (had or set())):
            raise HTTPException(403, "Bạn chỉ cấp được những camera mà chính bạn xem được")
    return ",".join(sorted(set(cams)))


def editable_user(c, uid: int, actor: dict):
    r = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not r:
        raise HTTPException(404, "Không có tài khoản này")
    if r["id"] == actor["id"]:
        raise HTTPException(400, "Không sửa hay xoá tài khoản của chính mình ở đây (đổi mật khẩu bằng nút Đổi mật khẩu)")
    if r["is_admin"] and not actor["is_admin"]:
        raise HTTPException(403, "Chỉ admin sửa được tài khoản admin")
    return r


@app.get("/api/users")
def list_users():
    with auth_db() as c:
        last = {r["username"]: r["last"] for r in c.execute(
            "SELECT username, MAX(last_seen) AS last FROM sessions GROUP BY username")}
        out = [dict(r) for r in c.execute(
            "SELECT id, username, is_admin, perms, cameras, disabled, created_at FROM users"
            " ORDER BY is_admin DESC, username")]
    for r in out:
        r["perms"] = list(PAGES) if r["is_admin"] else [p for p in r["perms"].split(",") if p in PAGES]
        r["cameras"] = [] if r["is_admin"] else [x for x in r["cameras"].split(",") if x]
        r["is_admin"], r["disabled"] = bool(r["is_admin"]), bool(r["disabled"])
        r["last_seen"] = last.get(r["username"])
    return out


@app.post("/api/users")
def create_user(body: UserIn, request: Request):
    actor = request.state.user
    name = (body.username or "").strip()
    if not USERNAME_RE.fullmatch(name):
        raise HTTPException(400, "Tên đăng nhập 2–32 ký tự, chỉ gồm chữ không dấu, số và . _ -")
    check_new_password(body.password or "")
    perms = clean_perms(actor, body.perms or [], bool(body.is_admin))
    with auth_db() as c:
        cams = clean_cams(c, actor, body.cameras or [])
        first = not c.execute("SELECT 1 FROM users").fetchone()  # tài khoản đầu tiên luôn là admin
        try:
            cur = c.execute("INSERT INTO users(username, pw_hash, is_admin, perms, cameras, created_at)"
                            " VALUES (?, ?, ?, ?, ?, ?)", (name, hash_password(body.password),
                                                          int(first or bool(body.is_admin)), perms, cams, time.time()))
        except sqlite3.IntegrityError:
            raise HTTPException(400, "Tên đăng nhập này đã có") from None
    return {"ok": True, "id": cur.lastrowid}


@app.patch("/api/users/{uid}")
def update_user(uid: int, body: UserIn, request: Request):
    actor = request.state.user
    with auth_db() as c:
        r = editable_user(c, uid, actor)
        sets: dict = {}
        if body.perms is not None or body.is_admin is not None:
            is_admin = bool(r["is_admin"]) if body.is_admin is None else body.is_admin
            had = {p for p in r["perms"].split(",") if p}
            sets["perms"] = clean_perms(actor, list(had) if body.perms is None else body.perms, is_admin, had)
            sets["is_admin"] = int(is_admin)
        if body.cameras is not None:
            had = {x for x in r["cameras"].split(",") if x} or None
            sets["cameras"] = clean_cams(c, actor, body.cameras, had)
        if body.password:
            check_new_password(body.password)
            sets["pw_hash"] = hash_password(body.password)
        if body.disabled is not None:
            sets["disabled"] = int(body.disabled)
        if sets:
            c.execute(f"UPDATE users SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?", (*sets.values(), uid))
        if body.password or body.disabled:  # đổi mật khẩu / khoá: đăng xuất mọi thiết bị của người đó
            c.execute("DELETE FROM sessions WHERE username = ?", (r["username"],))
    return {"ok": True}


@app.delete("/api/users/{uid}")
def delete_user(uid: int, request: Request):
    with auth_db() as c:
        r = editable_user(c, uid, request.state.user)
        c.execute("DELETE FROM users WHERE id = ?", (uid,))
        c.execute("DELETE FROM sessions WHERE username = ?", (r["username"],))
    return {"ok": True}


def rows(sql, args=()):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def day_range(date_from: str | None, date_to: str | None):
    """Chuyển 'YYYY-MM-DD' (giờ server) thành khoảng unix time [start, end)."""
    start = datetime.strptime(date_from, "%Y-%m-%d").timestamp() if date_from else 0
    end = (datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)).timestamp() if date_to else 1e12
    return start, end


SIGHTING_COLS = f"""s.id, {PLATE} AS plate, s.plate AS ocr_plate, s.corrected_plate, s.camera,
    c.name AS camera_name, c.location AS camera_location, s.first_seen, s.last_seen, s.reads,
    s.ocr_score, s.raw_text, s.plate_img, s.frame_img, s.ai_status, s.ai_plate, s.ai_conf"""


@app.get("/api/stats")
def stats():
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    sc, ec = cam_in("s.camera"), cam_in("camera")
    r = db.execute(
        f"SELECT COUNT(*) AS sightings, COUNT(DISTINCT {PLATE}) AS plates FROM sightings s WHERE first_seen >= ?{sc}",
        (today,),
    ).fetchone()
    total = db.execute(f"SELECT COUNT(*), COUNT(DISTINCT {PLATE}) FROM sightings s WHERE 1{sc}").fetchone()
    events = dict(db.execute(f"SELECT kind, COUNT(*) FROM events WHERE ts >= ?{ec} GROUP BY kind", (today,)).fetchall())
    hourly = rows(
        "SELECT CAST(strftime('%H', first_seen, 'unixepoch', 'localtime') AS INTEGER) AS hour, COUNT(*) AS n"
        f" FROM sightings s WHERE first_seen >= ?{sc} GROUP BY hour",
        (today,),
    )
    return {
        "today_sightings": r["sightings"], "today_plates": r["plates"],
        "total_sightings": total[0], "total_plates": total[1], "hourly": hourly,
        "today_zone_events": events.get("zone", 0), "today_face_events": events.get("face", 0),
    }


@app.get("/api/sightings")
def list_sightings(q: str = "", camera: str = "", date_from: str | None = None, date_to: str | None = None,
                   sort: str = "", limit: int = 60, offset: int = 0):
    """sort: "" = mới nhất trước, "oldest" = cũ nhất trước,
    "count" = biển xuất hiện nhiều lần nhất trước, mỗi biển một thẻ (lần gần nhất trong bộ lọc)."""
    start, end = day_range(date_from, date_to)
    where, args = ["s.first_seen >= ?", "s.first_seen < ?"], [start, end]
    if q:
        where.append(f"REPLACE(REPLACE({PLATE}, '-', ''), '.', '') LIKE ?")
        args.append("%" + q.upper().replace("-", "").replace(".", "").replace(" ", "") + "%")
    if camera:
        where.append("s.camera = ?")
        args.append(camera)
    sql_where = " AND ".join(where) + cam_in("s.camera")
    # plate_count: tổng số lần biển này xuất hiện (mọi lúc, mọi camera được xem), hiện ngay trên thẻ
    counts = (f"(SELECT COALESCE(s2.corrected_plate, s2.plate) AS p, COUNT(*) AS n FROM sightings s2"
              f" WHERE 1{cam_in('s2.camera')} GROUP BY p)")
    select = (f"SELECT {SIGHTING_COLS}, pc.n AS plate_count FROM sightings s LEFT JOIN cameras c ON c.id = s.camera"
              f" LEFT JOIN {counts} pc ON pc.p = {PLATE}")
    if sort == "count":
        total = db.execute(f"SELECT COUNT(DISTINCT {PLATE}) FROM sightings s WHERE {sql_where}", args).fetchone()[0]
        latest = (f"SELECT id FROM (SELECT s.id, ROW_NUMBER() OVER (PARTITION BY {PLATE} ORDER BY s.first_seen DESC)"
                  f" AS rn FROM sightings s WHERE {sql_where}) WHERE rn = 1")
        sql = f"{select} WHERE s.id IN ({latest}) ORDER BY pc.n DESC, s.first_seen DESC LIMIT ? OFFSET ?"
    else:
        total = db.execute(f"SELECT COUNT(*) FROM sightings s WHERE {sql_where}", args).fetchone()[0]
        sql = (f"{select} WHERE {sql_where} ORDER BY s.first_seen {'ASC' if sort == 'oldest' else 'DESC'}"
               f" LIMIT ? OFFSET ?")
    items = rows(sql, (*args, min(limit, 500), offset))
    return {"total": total, "items": items}


class SightingUpdate(BaseModel):
    plate: str | None = None  # None hoặc "" -> bỏ sửa, quay về kết quả OCR


@app.patch("/api/sightings/{sid}")
def update_sighting(sid: int, body: SightingUpdate):
    check_row_cam("sightings", sid)
    corrected = None
    if body.plate and body.plate.strip():
        corrected = normalize_plate(body.plate) or body.plate.strip().upper()
    if not db.execute("UPDATE sightings SET corrected_plate = ? WHERE id = ?", (corrected, sid)).rowcount:
        raise HTTPException(404, "Không tìm thấy")
    db.commit()
    return rows(f"SELECT {SIGHTING_COLS} FROM sightings s LEFT JOIN cameras c ON c.id = s.camera WHERE s.id = ?",
                (sid,))[0]


@app.delete("/api/sightings/{sid}")
def delete_sighting(sid: int):
    check_row_cam("sightings", sid)
    r = db.execute("SELECT plate_img, frame_img FROM sightings WHERE id = ?", (sid,)).fetchone()
    if not r:
        raise HTTPException(404, "Không tìm thấy")
    for rel in r:
        (IMAGES_DIR / rel).unlink(missing_ok=True)
    db.execute("DELETE FROM sightings WHERE id = ?", (sid,))
    db.commit()
    return {"ok": True}


@app.get("/api/plates/{plate}/timeline")
def plate_timeline(plate: str, date_from: str | None = None, date_to: str | None = None):
    start, end = day_range(date_from, date_to)
    items = rows(
        f"SELECT {SIGHTING_COLS} FROM sightings s LEFT JOIN cameras c ON c.id = s.camera"
        f" WHERE {PLATE} = ? AND s.last_seen >= ? AND s.first_seen < ?{cam_in('s.camera')} ORDER BY s.first_seen",
        (plate, start, end),
    )
    summary = db.execute(
        f"SELECT COUNT(*) AS n, MIN(first_seen) AS first_seen, MAX(last_seen) AS last_seen,"
        f" SUM(last_seen - first_seen) AS dwell FROM sightings s WHERE {PLATE} = ?{cam_in('s.camera')}",
        (plate,),
    ).fetchone()
    watched = plate_key(plate) in get_settings(db).get("watch_plates", "").split(",")
    return {"plate": plate, "summary": dict(summary), "items": items, "watched": watched}


class WatchIn(BaseModel):
    watch: bool


@app.post("/api/plates/{plate}/watch")
def watch_plate(plate: str, body: WatchIn):
    """Thêm / bỏ biển số khỏi danh sách theo dõi (báo Telegram mỗi khi xe xuất hiện)."""
    keys = [k for k in get_settings(db).get("watch_plates", "").split(",") if k and k != plate_key(plate)]
    if body.watch:
        keys.append(plate_key(plate))
    put_setting("watch_plates", ",".join(keys))
    db.commit()
    return {"watched": body.watch}


@app.get("/api/cameras")
def list_cameras():
    streams = go2rtc_streams()
    if streams:  # luồng có trong go2rtc.yaml mà chưa có trong danh sách (cài mới, sửa tay file cấu hình)
        known = {r[0] for r in db.execute("SELECT id FROM cameras")}
        order = db.execute("SELECT COALESCE(MAX(sort_order), -1) FROM cameras").fetchone()[0]
        for name in streams.keys() - known:
            order += 1
            db.execute("INSERT INTO cameras(id, name, sort_order) VALUES (?, ?, ?)", (name, name, order))
        db.commit()
    cams = rows(
        "SELECT c.*, st.connected, st.last_frame_at, st.fps, st.updated_at,"
        " (SELECT COUNT(*) FROM sightings s WHERE s.camera = c.id) AS sightings,"
        " (SELECT COUNT(*) FROM zones z WHERE z.camera = c.id AND z.enabled = 1) AS zones"
        f" FROM cameras c LEFT JOIN camera_status st ON st.camera = c.id WHERE 1{cam_in('c.id')}"
        " ORDER BY c.sort_order, c.id"
    )
    for c in cams:
        src = (streams.get(c["id"]) or [""])[0]
        c["source"] = mask_source(src)
        c["source_kind"] = src.split(":", 1)[0].lower() if src else ""
    u = _user.get()
    saved = u and db.execute("SELECT cam_order FROM users WHERE id = ?", (u["id"],)).fetchone()
    if saved and saved[0]:  # thứ tự riêng của tài khoản; camera thêm sau xếp cuối, theo thứ tự chung
        rank = {cam: i for i, cam in enumerate(saved[0].split(","))}
        cams.sort(key=lambda c: rank.get(c["id"], len(rank)))
    return cams


class CameraOrderIn(BaseModel):
    cameras: list[str]  # rỗng = về thứ tự chung


@app.put("/api/me/camera-order")
def set_camera_order(body: CameraOrderIn, request: Request):
    u = request.state.user
    if not u["id"]:
        raise HTTPException(400, "Cần đăng nhập bằng tài khoản để lưu thứ tự camera")
    with auth_db() as c:
        known = {r[0] for r in c.execute("SELECT id FROM cameras")}
        cams = [x for x in dict.fromkeys(body.cameras) if x in known and (u["cams"] is None or x in u["cams"])]
        c.execute("UPDATE users SET cam_order = ? WHERE id = ?", (",".join(cams), u["id"]))
    return {"ok": True}


class CameraUpdate(BaseModel):
    name: str | None = None
    location: str | None = None
    lpr_enabled: bool | None = None
    face_enabled: bool | None = None
    track_enabled: bool | None = None
    record_enabled: bool | None = None
    record_audio: bool | None = None
    talk_enabled: bool | None = None


@app.patch("/api/cameras/{cam}")
def update_camera(cam: str, body: CameraUpdate):
    check_cam(cam)
    for field, value in body.model_dump(exclude_none=True).items():
        db.execute(f"UPDATE cameras SET {field} = ? WHERE id = ?", (int(value) if isinstance(value, bool) else value, cam))
    db.commit()
    return {"ok": True}


@app.get("/api/cameras/{cam}/snapshot")
def camera_snapshot(cam: str):
    """Ảnh hiện tại của camera, lấy qua go2rtc."""
    check_cam(cam)
    if not db.execute("SELECT 1 FROM cameras WHERE id = ?", (cam,)).fetchone():
        raise HTTPException(404, "Không có camera này")
    try:
        with urllib.request.urlopen(f"{GO2RTC_API}/api/frame.jpeg?src={cam}", timeout=20) as r:
            return Response(r.read(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    except Exception as e:
        error = e
    # go2rtc tạo ảnh bằng ffmpeg, hay lỗi với H.265 khi chưa có ai xem luồng: đọc thẳng một khung qua RTSP như worker
    frame = grab_frame(f"rtsp://{RTSP_HOST}/{cam}")
    if frame is None:
        return JSONResponse({"error": f"Không lấy được ảnh: {error}"}, status_code=502)
    return Response(frame_jpeg(frame), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ---------- thêm / sửa / xoá camera: mỗi camera là một luồng trong go2rtc ----------
# Chỉ nhận nguồn camera thật. go2rtc còn có exec: / ffmpeg: … chạy được lệnh trên máy chủ, không cho nhập từ web.
SOURCE_SCHEMES = ("rtsp", "rtsps", "rtmp", "http", "https", "onvif")
PROBE_PREFIX = "_probe_"
SD_PREFIX = "_sd_"  # luồng phụ 640x360 (Xiaomi subtype=sd) cho trang Xem nhiều camera, tạo trong go2rtc khi cần


def mask_source(url: str) -> str:
    """Ẩn mật khẩu trong link camera trước khi gửi ra trình duyệt."""
    return re.sub(r"(://[^:/@?#]*:)[^@/]*@", r"\1•••@", url)


def go2rtc_streams() -> dict[str, list[str]]:
    """{tên luồng: [link nguồn]} trong go2rtc (bỏ luồng thử); {} nếu go2rtc không chạy."""
    return {name: src for name, src in (go2rtc_sources() or {}).items()
            if not name.startswith((PROBE_PREFIX, SD_PREFIX, TALK_PREFIX))}


def go2rtc_put(name: str, source: str):
    """Thêm luồng; go2rtc tự ghi vào go2rtc.yaml nên còn nguyên sau khi khởi động lại."""
    httpx.put(f"{GO2RTC_API}/api/streams", params={"name": name, "src": source}, timeout=10).raise_for_status()


def go2rtc_delete(name: str):
    httpx.delete(f"{GO2RTC_API}/api/streams", params={"src": name}, timeout=10).raise_for_status()


def sd_stream(cam: str) -> str:
    """Tên luồng phụ chất lượng thấp của camera Xiaomi (lần đầu thì tạo trong go2rtc); camera khác: luồng chính."""
    sources = go2rtc_sources() or {}
    name = SD_PREFIX + cam
    if name in sources:
        return name
    src = next((x for x in sources.get(cam, []) if x.startswith("xiaomi:")), None)
    if not src:
        return cam
    go2rtc_put(name, src + ("&" if "?" in src else "?") + "subtype=sd")
    return name


def drop_sd_stream(cam: str):
    live_probe.pop(cam, None)  # link mới: đọc lại mã hoá lần xem sau
    live_probe.pop(SD_PREFIX + cam, None)
    sources = go2rtc_sources() or {}
    for name in (SD_PREFIX + cam, TALK_PREFIX + cam):  # luồng nói qua loa: tạo lại khi cần
        if name in sources:
            go2rtc_delete(name)


def check_source(source: str) -> str:
    source = source.strip()
    scheme = source.split("://", 1)[0].lower() if "://" in source else ""
    if scheme not in SOURCE_SCHEMES:
        raise HTTPException(400, "Link camera phải bắt đầu bằng rtsp:// (hoặc rtsps://, onvif://, http://)")
    if any(c in source for c in " \n\r\t"):
        raise HTTPException(400, "Link camera không được có dấu cách")
    return source


def probe_error(text: str) -> str:
    t = text.lower()
    if "401" in t or "unauthorized" in t or "wrong user" in t:
        return "Sai tên đăng nhập hoặc mật khẩu camera"
    if "404" in t or "not found" in t or "describe" in t:
        return ("Camera trả lời nhưng không cho xem luồng: sai tài khoản / mật khẩu, hoặc sai đường dẫn"
                " (kiểm tra hãng, kênh, luồng)")
    if "refused" in t:
        return "Camera từ chối kết nối ở cổng này: kiểm tra cổng (thường 554) và đã bật RTSP trong app camera chưa"
    if "timeout" in t or "no route" in t or "unreachable" in t or "deadline" in t:
        return "Không kết nối được tới camera: kiểm tra IP, camera có cùng mạng với máy chủ không"
    return f"Không lấy được hình: {text.strip()[:200]}"


def grab_frame(url: str, seconds: float = 15):
    """Đọc một khung hình bằng OpenCV, giống worker (H.265 có thể mất vài giây chờ keyframe)."""
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000,
                                                 cv2.CAP_PROP_READ_TIMEOUT_MSEC, 10000])
    try:
        end = time.time() + seconds
        while cap.isOpened() and time.time() < end:
            ok, frame = cap.read()
            if ok and frame is not None:
                return frame
        return None
    finally:
        cap.release()


def frame_jpeg(frame) -> bytes:
    scale = min(1.0, 1280 / frame.shape[1])
    if scale < 1:
        frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()


def probe_source(source: str) -> bytes:
    """Thử link camera qua go2rtc rồi đọc một khung hình từ RTSP của go2rtc: đúng đường worker sẽ dùng.
    Trả về ảnh JPEG; lỗi thì HTTPException kèm lời giải thích dễ hiểu."""
    name = f"{PROBE_PREFIX}{uuid.uuid4().hex[:8]}"
    try:
        go2rtc_put(name, source)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Không gọi được go2rtc: {e}")
    try:
        try:  # go2rtc nối thử tới camera và báo lỗi cụ thể (sai IP, sai mật khẩu…)
            r = httpx.get(f"{GO2RTC_API}/api/streams", params={"src": name, "video": "all"}, timeout=25)
        except httpx.TimeoutException:
            raise HTTPException(400, probe_error("timeout"))
        if r.status_code != 200:
            raise HTTPException(400, probe_error(r.text))
        frame = grab_frame(f"rtsp://{RTSP_HOST}/{name}")
        if frame is None:
            raise HTTPException(400, "Kết nối được camera nhưng không đọc được hình. Thử luồng phụ, hoặc đổi mã hoá"
                                     " video sang H.264 trong cài đặt camera.")
        return frame_jpeg(frame)
    finally:
        try:
            go2rtc_delete(name)
        except httpx.HTTPError:
            pass


def camera_id(name: str) -> str:
    """Tên hiển thị -> tên luồng go2rtc (không dấu, chữ thường, gạch dưới), không trùng camera đã có."""
    base = unicodedata.normalize("NFD", name.replace("đ", "d").replace("Đ", "D"))
    base = re.sub(r"[^a-z0-9]+", "_", "".join(c for c in base if not unicodedata.combining(c)).lower()).strip("_")
    base = (base or "cam")[:30]
    taken = {r[0] for r in db.execute("SELECT id FROM cameras")} | go2rtc_streams().keys()
    cid, n = base, 2
    while cid in taken:
        cid, n = f"{base}_{n}", n + 1
    return cid


class SourceIn(BaseModel):
    source: str


class CameraIn(BaseModel):
    name: str
    location: str = ""
    source: str
    lpr_enabled: bool = False
    face_enabled: bool = False
    track_enabled: bool = False


@app.post("/api/cameras/test")
def test_camera(body: SourceIn):
    return Response(probe_source(check_source(body.source)), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/cameras")
def add_camera(body: CameraIn):
    if visible_cams() is not None:
        raise HTTPException(403, "Chỉ tài khoản xem được mọi camera mới thêm được camera")
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Chưa đặt tên camera")
    source = check_source(body.source)
    probe_source(source)  # chỉ lưu khi đã lấy được hình
    cid = camera_id(name)
    try:
        go2rtc_put(cid, source)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Không lưu được vào go2rtc: {e}")
    order = db.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM cameras").fetchone()[0]
    db.execute("INSERT INTO cameras(id, name, location, lpr_enabled, face_enabled, track_enabled, sort_order)"
               " VALUES (?, ?, ?, ?, ?, ?, ?)", (cid, name, body.location.strip(), int(body.lpr_enabled),
                                                 int(body.face_enabled), int(body.track_enabled), order))
    db.commit()
    return {"id": cid}


@app.put("/api/cameras/{cam}/source")
def change_camera_source(cam: str, body: SourceIn):
    """Đổi link (vd đổi mật khẩu camera, camera đổi IP)."""
    check_cam(cam)
    if not db.execute("SELECT 1 FROM cameras WHERE id = ?", (cam,)).fetchone():
        raise HTTPException(404, "Không có camera này")
    source = check_source(body.source)
    probe_source(source)
    try:
        if cam in go2rtc_streams():
            go2rtc_delete(cam)
        go2rtc_put(cam, source)
        drop_sd_stream(cam)  # luồng phụ tạo lại theo link mới lần xem sau
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Không lưu được vào go2rtc: {e}")
    return {"ok": True}


@app.delete("/api/cameras/{cam}")
def delete_camera(cam: str):
    """Xoá camera khỏi go2rtc và danh sách; lịch sử biển số / người qua lại của camera vẫn giữ."""
    check_cam(cam)
    if not db.execute("SELECT 1 FROM cameras WHERE id = ?", (cam,)).fetchone():
        raise HTTPException(404, "Không có camera này")
    try:
        if cam in go2rtc_streams():
            go2rtc_delete(cam)
        drop_sd_stream(cam)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Không xoá được khỏi go2rtc: {e}")
    db.execute("DELETE FROM zones WHERE camera = ?", (cam,))
    db.execute("DELETE FROM camera_status WHERE camera = ?", (cam,))
    db.execute("DELETE FROM cameras WHERE id = ?", (cam,))
    db.commit()
    return {"ok": True}

# ---------- vùng cảnh báo ----------
ZONE_CLASSES = ("person", "car", "bike", "animal")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ZoneIn(BaseModel):
    camera: str | None = None
    name: str | None = None
    points: list[list[float]] | None = None
    classes: list[str] | None = None
    start_time: str | None = None
    end_time: str | None = None
    cooldown: int | None = None
    notify: bool | None = None
    enabled: bool | None = None


def zone_out(r):
    z = dict(r)
    z["points"] = json.loads(z["points"])
    z["classes"] = [c for c in z["classes"].split(",") if c]
    return z


def zone_values(body: ZoneIn):
    """Kiểm tra và chuyển dữ liệu vùng sang dạng lưu trong DB."""
    v = body.model_dump(exclude_none=True)
    if "points" in v:
        if len(v["points"]) < 3:
            raise HTTPException(400, "Vùng cần ít nhất 3 điểm")
        v["points"] = json.dumps([[round(min(1, max(0, x)), 4), round(min(1, max(0, y)), 4)] for x, y in v["points"]])
    if "classes" in v:
        v["classes"] = ",".join(c for c in ZONE_CLASSES if c in v["classes"])
        if not v["classes"]:
            raise HTTPException(400, "Chọn ít nhất một loại đối tượng")
    for k in ("start_time", "end_time"):
        if k in v and v[k] and not TIME_RE.match(v[k]):
            raise HTTPException(400, "Giờ phải có dạng HH:MM")
    if "cooldown" in v:
        v["cooldown"] = min(86400, max(0, v["cooldown"]))
    if "name" in v:
        v["name"] = v["name"].strip() or "Vùng cảnh báo"
    for k in ("notify", "enabled"):
        if k in v:
            v[k] = int(v[k])
    return v


@app.get("/api/zones")
def list_zones(camera: str = ""):
    sql, args = "SELECT * FROM zones WHERE 1" + cam_in("camera"), ()
    if camera:
        sql, args = sql + " AND camera = ?", (camera,)
    return [zone_out(r) for r in db.execute(sql + " ORDER BY id", args)]


@app.post("/api/zones")
def create_zone(body: ZoneIn):
    check_cam(body.camera or "")
    if not body.camera or not db.execute("SELECT 1 FROM cameras WHERE id = ?", (body.camera,)).fetchone():
        raise HTTPException(400, "Không có camera này")
    if not body.points:
        raise HTTPException(400, "Chưa vẽ vùng")
    v = {"name": "Vùng cảnh báo", "classes": ["person"], **body.model_dump(exclude_none=True)}
    v = zone_values(ZoneIn(**v))
    cols = ", ".join(v)
    cur = db.execute(f"INSERT INTO zones({cols}) VALUES ({', '.join('?' * len(v))})", tuple(v.values()))
    db.commit()
    return zone_out(db.execute("SELECT * FROM zones WHERE id = ?", (cur.lastrowid,)).fetchone())


@app.patch("/api/zones/{zid}")
def update_zone(zid: int, body: ZoneIn):
    check_row_cam("zones", zid)
    v = zone_values(body)
    v.pop("camera", None)
    if v:
        sets = ", ".join(f"{k} = ?" for k in v)
        if not db.execute(f"UPDATE zones SET {sets} WHERE id = ?", (*v.values(), zid)).rowcount:
            raise HTTPException(404, "Không tìm thấy vùng")
        db.commit()
    return zone_out(db.execute("SELECT * FROM zones WHERE id = ?", (zid,)).fetchone())


@app.delete("/api/zones/{zid}")
def delete_zone(zid: int):
    check_row_cam("zones", zid)
    db.execute("DELETE FROM zones WHERE id = ?", (zid,))
    db.commit()
    return {"ok": True}


# ---------- sự kiện (cảnh báo vùng + khuôn mặt) ----------
EVENT_COLS = """e.id, e.kind, e.camera, c.name AS camera_name, e.ts, e.last_seen, e.label, e.zone_id,
    z.name AS zone_name, e.person_id, e.score, e.img, e.frame_img, e.notified, e.note,
    e.embedding IS NOT NULL AS has_face"""
EVENT_FROM = "events e LEFT JOIN cameras c ON c.id = e.camera LEFT JOIN zones z ON z.id = e.zone_id"


@app.get("/api/events")
def list_events(kind: str = "", camera: str = "", person_id: int | None = None, unknown: bool = False,
                date_from: str | None = None, date_to: str | None = None, limit: int = 60, offset: int = 0,
                q: str = ""):
    start, end = day_range(date_from, date_to)
    where, args = ["e.ts >= ?", "e.ts < ?"], [start, end]
    if kind:
        where.append("e.kind = ?")
        args.append(kind)
    if camera:
        where.append("e.camera = ?")
        args.append(camera)
    if person_id is not None:
        where.append("e.person_id = ?")
        args.append(person_id)
    if unknown:
        where.append("e.kind = 'face' AND e.person_id IS NULL")
    sql_where = " AND ".join(where) + cam_in("e.camera")
    if q.strip():
        return search_events(q.strip(), sql_where, args, min(limit, 500), max(0, offset))
    total = db.execute(f"SELECT COUNT(*) FROM events e WHERE {sql_where}", args).fetchone()[0]
    items = rows(f"SELECT {EVENT_COLS} FROM {EVENT_FROM} WHERE {sql_where} ORDER BY e.ts DESC LIMIT ? OFFSET ?",
                 (*args, min(limit, 500), offset))
    return {"total": total, "items": items}


def search_events(q, sql_where, args, limit, offset):
    """Cảnh báo vùng xếp theo mức giống mô tả (vector CLIP + màu của ảnh cắt), giống nhất trước. Mô tả nói rõ
    loại ("ô tô trắng", "chó") thì chỉ tìm trong cảnh báo loại đó."""
    prompts, constraints, groups, contrast = search.describe(q)
    labels = [GROUP_LABELS[g] for g in sorted(groups) if g in GROUP_LABELS]
    like = f" AND ({' OR '.join('e.label LIKE ?' for _ in labels)})" if labels else ""
    found = db.execute(f"SELECT e.id, e.clip_emb, e.colors FROM events e WHERE {sql_where} AND e.kind = 'zone'"
                       f" AND length(e.clip_emb) = {search.DIM * 2}{like}",
                       (*args, *(f"%{l}%" for l in labels))).fetchall()
    if not found:
        return {"total": 0, "items": []}
    text = clip_text().embed_text(list(prompts) + list(contrast))
    embs = np.frombuffer(b"".join(r["clip_emb"] for r in found), np.float16).reshape(len(found), -1).astype(np.float32)
    n_bins = len(search.REGIONS) * len(search.BINS)
    colors = np.stack([np.frombuffer(r["colors"], np.float16) if r["colors"] and len(r["colors"]) == n_bins * 2
                       else np.zeros(n_bins, np.float16) for r in found]).astype(np.float32)
    sims = embs @ text.T
    pos = sims[:, :len(prompts)].max(1)
    base = pos + search.color_score(colors.reshape(len(found), len(search.REGIONS), -1), constraints)
    ok = base >= base.max() - SEARCH_SPREAD
    scores = base
    if contrast:  # nam / nữ, trẻ em: bỏ ảnh giống phía ngược lại hơn, xếp theo mức chênh
        margin = pos - sims[:, len(prompts):].max(1)
        ok &= margin > 0
        scores = base + margin
    order = np.argsort(-scores)
    keep = order[ok[order]][:SEARCH_MAX]
    page = [(found[i]["id"], float(scores[i])) for i in keep[offset:offset + limit]]
    if not page:
        return {"total": len(keep), "items": []}
    info = {r["id"]: r for r in rows(f"SELECT {EVENT_COLS} FROM {EVENT_FROM} WHERE e.id IN ({','.join('?' * len(page))})",
                                     [eid for eid, _ in page])}
    return {"total": len(keep), "items": [{**info[eid], "score": round(sc, 3)} for eid, sc in page if eid in info]}


@app.get("/api/events/search-status")
def event_search_status():
    """Số ảnh cảnh báo vùng chưa tính vector CLIP (service lpr-search đang chạy bù) và chưa tìm được."""
    pending = db.execute("SELECT COUNT(*) FROM events WHERE clip_emb IS NULL AND kind = 'zone'").fetchone()[0]
    return {"pending": pending}


@app.delete("/api/events/{eid}")
def delete_event(eid: int):
    check_row_cam("events", eid)
    r = db.execute("SELECT img, frame_img FROM events WHERE id = ?", (eid,)).fetchone()
    if not r:
        raise HTTPException(404, "Không tìm thấy")
    for rel in r:
        (IMAGES_DIR / rel).unlink(missing_ok=True)
    db.execute("DELETE FROM events WHERE id = ?", (eid,))
    db.commit()
    return {"ok": True}


class AssignIn(BaseModel):
    person_id: int | None = None
    name: str | None = None  # tạo người mới với tên này


@app.post("/api/events/{eid}/assign")
def assign_event(eid: int, body: AssignIn):
    """Gán khuôn mặt trong sự kiện cho một người quen: thêm làm ảnh mẫu, gắn tên cho các lần thấy giống hệt."""
    from faces import FACE_MATCH, from_blob

    check_row_cam("events", eid)
    ev = db.execute("SELECT img, embedding FROM events WHERE id = ? AND kind = 'face'", (eid,)).fetchone()
    if not ev or ev["embedding"] is None:
        raise HTTPException(404, "Sự kiện không có khuôn mặt")
    pid = body.person_id
    if body.name and body.name.strip():
        pid = db.execute("INSERT INTO people(name, created_at) VALUES (?, ?)", (body.name.strip(), time.time())).lastrowid
    person = db.execute("SELECT id, name FROM people WHERE id = ?", (pid,)).fetchone()
    if not person:
        raise HTTPException(400, "Chọn người hoặc nhập tên mới")
    rel = f"faces/{pid}/{uuid.uuid4().hex[:12]}.jpg"
    (IMAGES_DIR / rel).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(IMAGES_DIR / ev["img"], IMAGES_DIR / rel)
    db.execute("INSERT INTO faces(person_id, embedding, img, created_at) VALUES (?, ?, ?, ?)",
               (pid, ev["embedding"], rel, time.time()))
    # các lần "người lạ" khác có khuôn mặt giống -> gắn luôn cho người này
    emb = from_blob(ev["embedding"])
    same = [eid] + [r["id"] for r in db.execute(
        "SELECT id, embedding FROM events WHERE kind = 'face' AND person_id IS NULL AND id != ? AND embedding IS NOT NULL",
        (eid,)) if float(from_blob(r["embedding"]) @ emb) >= FACE_MATCH]
    db.executemany("UPDATE events SET person_id = ?, label = ? WHERE id = ?", [(pid, person["name"], i) for i in same])
    db.commit()
    return {"person_id": pid, "name": person["name"], "relabeled": len(same)}


# ---------- người quen ----------
_analyzer = None
_analyzer_lock = threading.Lock()


def analyzer():
    """Nạp model khuôn mặt lần đầu cần dùng (tải ảnh mẫu)."""
    global _analyzer
    with _analyzer_lock:
        if _analyzer is None:
            from faces import FaceAnalyzer
            _analyzer = FaceAnalyzer(threads=1)
        return _analyzer


class PersonIn(BaseModel):
    name: str | None = None
    note: str | None = None
    notify: bool | None = None


@app.get("/api/people")
def list_people():
    ec = cam_in("e.camera")
    return rows(
        f"""SELECT p.*, (SELECT COUNT(*) FROM faces f WHERE f.person_id = p.id) AS faces,
                  (SELECT img FROM faces f WHERE f.person_id = p.id ORDER BY f.id DESC LIMIT 1) AS face_img,
                  (SELECT MAX(e.last_seen) FROM events e WHERE e.person_id = p.id{ec}) AS last_seen,
                  (SELECT COUNT(*) FROM events e WHERE e.person_id = p.id{ec}) AS events
           FROM people p ORDER BY p.name COLLATE NOCASE"""
    )


@app.post("/api/people")
def create_person(body: PersonIn):
    if not body.name or not body.name.strip():
        raise HTTPException(400, "Nhập tên")
    pid = db.execute("INSERT INTO people(name, note, notify, created_at) VALUES (?, ?, ?, ?)",
                     (body.name.strip(), body.note or "", int(bool(body.notify)), time.time())).lastrowid
    db.commit()
    return {"id": pid}


@app.patch("/api/people/{pid}")
def update_person(pid: int, body: PersonIn):
    v = body.model_dump(exclude_none=True)
    if "name" in v:
        v["name"] = v["name"].strip()
        if not v["name"]:
            raise HTTPException(400, "Nhập tên")
        db.execute("UPDATE events SET label = ? WHERE person_id = ?", (v["name"], pid))
    if "notify" in v:
        v["notify"] = int(v["notify"])
    for k, val in v.items():
        db.execute(f"UPDATE people SET {k} = ? WHERE id = ?", (val, pid))
    db.commit()
    return {"ok": True}


@app.delete("/api/people/{pid}")
def delete_person(pid: int):
    db.execute("DELETE FROM faces WHERE person_id = ?", (pid,))
    db.execute("UPDATE events SET person_id = NULL, label = 'Người lạ' WHERE person_id = ?", (pid,))
    db.execute("DELETE FROM people WHERE id = ?", (pid,))
    db.commit()
    shutil.rmtree(IMAGES_DIR / "faces" / str(pid), ignore_errors=True)
    return {"ok": True}


@app.get("/api/people/{pid}/faces")
def list_faces(pid: int):
    return rows("SELECT id, img, created_at FROM faces WHERE person_id = ? ORDER BY id DESC", (pid,))


@app.post("/api/people/{pid}/faces")
async def upload_face(pid: int, request: Request):
    """Nhận một ảnh (body là file ảnh), lấy khuôn mặt lớn nhất làm ảnh mẫu."""
    from faces import face_crop, to_blob

    if not db.execute("SELECT 1 FROM people WHERE id = ?", (pid,)).fetchone():
        raise HTTPException(404, "Không có người này")
    data = await request.body()
    if len(data) > 20_000_000:
        raise HTTPException(413, "Ảnh quá lớn (tối đa 20 MB)")
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "Không đọc được ảnh (dùng JPG hoặc PNG)")
    scale = min(1.0, 1600 / max(img.shape[:2]))
    if scale < 1:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    fa = analyzer()
    faces = fa.detect(img, 640)
    if not faces:
        raise HTTPException(422, "Không tìm thấy khuôn mặt trong ảnh")
    _, box, kps = faces[0]
    emb = fa.embed(img, kps)
    rel = f"faces/{pid}/{uuid.uuid4().hex[:12]}.jpg"
    (IMAGES_DIR / rel).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(IMAGES_DIR / rel), face_crop(img, box), [cv2.IMWRITE_JPEG_QUALITY, 90])
    fid = db.execute("INSERT INTO faces(person_id, embedding, img, created_at) VALUES (?, ?, ?, ?)",
                     (pid, to_blob(emb), rel, time.time())).lastrowid
    db.commit()
    return {"id": fid, "img": rel, "faces_in_photo": len(faces)}


@app.delete("/api/faces/{fid}")
def delete_face(fid: int):
    r = db.execute("SELECT img FROM faces WHERE id = ?", (fid,)).fetchone()
    if r:
        (IMAGES_DIR / r["img"]).unlink(missing_ok=True)
        db.execute("DELETE FROM faces WHERE id = ?", (fid,))
        db.commit()
    return {"ok": True}


# ---------- người qua lại ----------
# gợi ý "có thể là cùng người" của visitor p.id, theo cả 2 chiều (lượt của p giống người khác, hoặc ngược lại)
SUGGEST_SQL = """SELECT maybe_visitor AS other, maybe_sim AS sim FROM visits
                 WHERE visitor_id = p.id AND maybe_visitor IN (SELECT id FROM visitors)
                 UNION ALL
                 SELECT visitor_id AS other, maybe_sim AS sim FROM visits WHERE maybe_visitor = p.id"""
# {cams}: cam_in("v2.camera"), ảnh đại diện lấy từ camera được xem
VISITOR_COLS = f"""p.id, p.label, p.person_id, pe.name AS person_name, COUNT(v.id) AS visits,
                   MIN(v.ts) AS first_seen, MAX(v.last_seen) AS last_seen, GROUP_CONCAT(DISTINCT v.camera) AS cameras,
                   (SELECT img FROM visits v2 WHERE v2.visitor_id = p.id{{cams}} ORDER BY v2.ts DESC LIMIT 1) AS img,
                   (SELECT COUNT(*) FROM ({SUGGEST_SQL})) AS suggestions"""
VISITOR_FROM = "visits v JOIN visitors p ON p.id = v.visitor_id LEFT JOIN people pe ON pe.id = p.person_id"
SEARCH_SPREAD = 0.08   # chỉ giữ người có điểm cách người khớp nhất không quá chừng này
SEARCH_MAX = 300
_clip, _clip_lock = None, threading.Lock()


def visitor_cols() -> str:
    return VISITOR_COLS.replace("{cams}", cam_in("v2.camera"))


def check_visitor(vid: int):
    """Người qua lại chỉ xuất hiện ở camera không được xem: coi như không có."""
    if visible_cams() is not None and not db.execute(
            f"SELECT 1 FROM visits WHERE visitor_id = ?{cam_in('camera')} LIMIT 1", (vid,)).fetchone():
        raise HTTPException(404, "Không tìm thấy")


def clip_text():
    """Phần đọc chữ của CLIP (~65 MB), chỉ nạp ở lần tìm đầu tiên."""
    global _clip
    with _clip_lock:
        if _clip is None:
            _clip = search.ClipModel(vision=False)
        return _clip


@app.get("/api/visitors")
def list_visitors(date_from: str | None = None, date_to: str | None = None, camera: str = "", q: str = "",
                  limit: int = 60, offset: int = 0):
    """Những người xuất hiện trong khoảng ngày, nhiều lượt nhất trước; có q thì xếp theo mức giống mô tả.
    Trang web tải dần từng trang khi cuộn."""
    start, end = day_range(date_from, date_to)
    where, args = ["v.ts >= ?", "v.ts < ?"], [start, end]
    if camera:
        where.append("v.camera = ?")
        args.append(camera)
    where.append("1" + cam_in("v.camera"))
    limit, offset = min(limit, 500), max(0, offset)
    if q.strip():
        return search_visitors(q.strip(), where, args, limit, offset)
    return rows(
        f"""SELECT {visitor_cols()} FROM {VISITOR_FROM}
            WHERE {" AND ".join(where)} GROUP BY p.id ORDER BY visits DESC, last_seen DESC, p.id LIMIT ? OFFSET ?""",
        (*args, limit, offset),
    )


def search_visitors(q, where, args, limit, offset):
    """So câu mô tả với vector CLIP + màu của từng lượt; mỗi người lấy lượt khớp nhất (ảnh của lượt đó)."""
    found = db.execute(f"SELECT v.visitor_id, v.img, v.clip_emb, v.colors FROM visits v WHERE {' AND '.join(where)}"
                       f" AND length(v.clip_emb) = {search.DIM * 2}", args).fetchall()
    if not found:
        return []
    prompts, constraints = search.translate(q)
    text = clip_text().embed_text(list(prompts))  # ONNX Runtime cho phép nhiều thread gọi cùng lúc
    embs = np.frombuffer(b"".join(r["clip_emb"] for r in found), np.float16).reshape(len(found), -1).astype(np.float32)
    n_bins = len(search.REGIONS) * len(search.BINS)
    colors = np.stack([np.frombuffer(r["colors"], np.float16) if r["colors"] and len(r["colors"]) == n_bins * 2
                       else np.zeros(n_bins, np.float16) for r in found]).astype(np.float32)
    scores = (embs @ text.T).max(1) + search.color_score(colors.reshape(len(found), len(search.REGIONS), -1), constraints)
    best = {}
    for r, sc in zip(found, scores.tolist()):
        if r["visitor_id"] not in best or sc > best[r["visitor_id"]][0]:
            best[r["visitor_id"]] = (sc, r["img"])
    ranked = sorted(best.items(), key=lambda kv: -kv[1][0])
    top = ranked[0][1][0]
    ranked = [kv for kv in ranked if kv[1][0] >= top - SEARCH_SPREAD][:SEARCH_MAX][offset:offset + limit]
    if not ranked:
        return []
    ids = [vid for vid, _ in ranked]
    info = {r["id"]: r for r in rows(
        f"""SELECT {visitor_cols()} FROM {VISITOR_FROM}
            WHERE {" AND ".join(where)} AND p.id IN ({",".join("?" * len(ids))}) GROUP BY p.id""", (*args, *ids))}
    return [{**info[vid], "img": img, "score": round(sc, 3)} for vid, (sc, img) in ranked if vid in info]


@app.get("/api/visitor-search/status")
def visitor_search_status():
    """Số lượt chưa tính vector CLIP (service lpr-search đang chạy bù)."""
    pending = db.execute("SELECT COUNT(*) FROM visits WHERE clip_emb IS NULL").fetchone()[0]
    return {"pending": pending}


@app.get("/api/visitors/{vid}")
def visitor_detail(vid: int, date_from: str | None = None, date_to: str | None = None):
    v = db.execute("SELECT p.id, p.label, p.person_id, pe.name AS person_name, p.first_seen, p.last_seen"
                   " FROM visitors p LEFT JOIN people pe ON pe.id = p.person_id WHERE p.id = ?", (vid,)).fetchone()
    if not v:
        raise HTTPException(404, "Không tìm thấy")
    check_visitor(vid)
    start, end = day_range(date_from, date_to)
    visits = rows("SELECT v.id, v.camera, c.name AS camera_name, v.ts, v.last_seen, v.similarity, v.img, v.frame_img,"
                  " v.ai_verdict, v.ai_conf, v.ai_reason"
                  " FROM visits v LEFT JOIN cameras c ON c.id = v.camera"
                  f" WHERE v.visitor_id = ? AND v.ts >= ? AND v.ts < ?{cam_in('v.camera')} ORDER BY v.ts DESC",
                  (vid, start, end))
    suggestions = rows(
        f"""SELECT s.id, s.sim, p.label, pe.name AS person_name,
                   (SELECT json_object('verdict', ai_verdict, 'conf', ai_conf, 'reason', ai_reason) FROM visits
                    WHERE ai_verdict IS NOT NULL AND ((visitor_id = ? AND maybe_visitor = s.id)
                          OR (visitor_id = s.id AND maybe_visitor = ?)) ORDER BY ts DESC LIMIT 1) AS ai,
                   (SELECT COUNT(*) FROM visits WHERE visitor_id = s.id) AS visits,
                   lv.img, lv.frame_img, lv.ts, c.name AS camera_name
            FROM (SELECT other AS id, MAX(sim) AS sim FROM ({SUGGEST_SQL.replace("p.id", "?")}) GROUP BY other) s
            JOIN visitors p ON p.id = s.id
            LEFT JOIN people pe ON pe.id = p.person_id
            JOIN visits lv ON lv.id = (SELECT id FROM visits WHERE visitor_id = s.id{cam_in("camera")}
                                       ORDER BY ts DESC LIMIT 1)
            LEFT JOIN cameras c ON c.id = lv.camera
            ORDER BY s.sim DESC""", (vid, vid, vid, vid))
    for g in suggestions:
        g["ai"] = json.loads(g["ai"]) if g["ai"] else None
    return {**dict(v), "visits": visits, "suggestions": suggestions}


class VisitorIn(BaseModel):
    label: str | None = None
    merge_into: int | None = None  # gộp mọi lượt của người này vào người khác (hệ thống nhận nhầm là 2 người)
    dismiss: int | None = None     # "không phải cùng người": bỏ gợi ý trùng với người này


@app.patch("/api/visitors/{vid}")
def update_visitor(vid: int, body: VisitorIn):
    check_visitor(vid)
    if body.merge_into:
        check_visitor(body.merge_into)
    if body.dismiss:
        db.execute("UPDATE visits SET maybe_visitor = NULL, maybe_sim = NULL WHERE (visitor_id = ? AND maybe_visitor = ?)"
                   " OR (visitor_id = ? AND maybe_visitor = ?)", (vid, body.dismiss, body.dismiss, vid))
    elif body.merge_into:
        if not db.execute("SELECT 1 FROM visitors WHERE id = ?", (body.merge_into,)).fetchone():
            raise HTTPException(404, "Không có người cần gộp vào")
        merge_visitors(db, vid, body.merge_into)
    elif body.label is not None:
        db.execute("UPDATE visitors SET label = ? WHERE id = ?", (body.label.strip(), vid))
    db.commit()
    return {"ok": True}


@app.post("/api/visits/{visit_id}/split")
def split_visit(visit_id: int):
    """Lượt bị gộp nhầm (bởi ReID hoặc AI): tách ra thành một người mới, giữ nguyên ảnh."""
    check_row_cam("visits", visit_id)
    r = db.execute("SELECT visitor_id, img, embedding, ts, last_seen FROM visits WHERE id = ?", (visit_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Không tìm thấy")
    if db.execute("SELECT COUNT(*) FROM visits WHERE visitor_id = ?", (r["visitor_id"],)).fetchone()[0] < 2:
        raise HTTPException(400, "Người này chỉ có một lượt")
    new = db.execute("INSERT INTO visitors(descriptor, img, first_seen, last_seen) VALUES (?, ?, ?, ?)",
                     (r["embedding"] or b"", r["img"], r["ts"], r["last_seen"])).lastrowid
    # không gợi ý / để AI gộp lại vào người cũ nữa
    db.execute("UPDATE visits SET visitor_id = ?, similarity = 0, maybe_visitor = NULL, maybe_sim = NULL,"
               " ai_verdict = 'split' WHERE id = ?", (new, visit_id))
    db.commit()
    return {"visitor_id": new}


@app.delete("/api/visits/{visit_id}")
def delete_visit(visit_id: int):
    """Xoá một lượt (nhận nhầm người). Người không còn lượt nào cũng bị xoá."""
    check_row_cam("visits", visit_id)
    r = db.execute("SELECT visitor_id, img, frame_img FROM visits WHERE id = ?", (visit_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Không tìm thấy")
    for rel in (r["img"], r["frame_img"]):
        (IMAGES_DIR / rel).unlink(missing_ok=True)
    db.execute("DELETE FROM visits WHERE id = ?", (visit_id,))
    db.execute("DELETE FROM visitors WHERE id = ? AND NOT EXISTS (SELECT 1 FROM visits WHERE visitor_id = ?)",
               (r["visitor_id"], r["visitor_id"]))
    db.commit()
    return {"ok": True}


# ---------- ghi hình / xem lại ----------
REC_FILE = re.compile(r"^\d{2}-\d{2}-\d{2}\.mp4$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REC_SEGMENT = 60  # recorder.py cắt file mỗi phút


def rec_cam_dir(cam: str):
    if not re.match(r"^[\w-]+$", cam):
        raise HTTPException(404, "Không có camera này")
    check_cam(cam)
    return REC_DIR / cam


def mp4_complete(path) -> bool:
    """File đã ghi xong: moov nằm ngay sau ftyp (faststart). File đang ghi, hoặc bị ngắt giữa chừng, chưa có moov."""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        return False
    if head[4:8] != b"ftyp":
        return False
    n = int.from_bytes(head[:4], "big")
    return head[n + 4:n + 8] == b"moov"


@app.get("/api/recordings")
def recordings_status():
    """Dung lượng video và trạng thái ghi của từng camera (recorder.py cập nhật mỗi phút)."""
    try:
        st = json.loads(REC_STATUS.read_text())
    except (OSError, ValueError):
        st = {}
    cams = visible_cams()
    if cams is not None:
        st["cameras"] = {k: v for k, v in st.get("cameras", {}).items() if k in cams}
    st["limit_gb"] = rec_limit_gb(get_settings(db))
    st["recorder_alive"] = bool(st.get("updated")) and time.time() - st["updated"] < 180
    return st


@app.get("/api/recordings/{cam}/days")
def recording_days(cam: str):
    d = rec_cam_dir(cam)
    if not d.is_dir():
        return []
    days = []
    for day in sorted((x for x in os.scandir(d) if x.is_dir() and DATE_RE.match(x.name)), key=lambda x: x.name):
        n = sum(1 for f in os.scandir(day.path) if REC_FILE.match(f.name))
        if n:
            days.append({"date": day.name, "files": n})
    return days


@app.get("/api/recordings/{cam}/{day}")
def recording_day(cam: str, day: str):
    """Các đoạn video trong ngày: t = giờ bắt đầu (unix), d = độ dài (giây, ước tính), f = tên file,
    m = % khung hình thay đổi trong phút (recorder.py tính, chưa có thì null), th = đã có ảnh thu nhỏ <tên>.jpg."""
    if not DATE_RE.match(day):
        raise HTTPException(400, "Ngày phải có dạng YYYY-MM-DD")
    d = rec_cam_dir(cam) / day
    if not d.is_dir():
        return []
    present = {f.name for f in os.scandir(d)}
    names = sorted(n for n in present if REC_FILE.match(n))
    try:
        motion = json.loads((d / "motion.json").read_text())
    except (OSError, ValueError):
        motion = {}
    starts = [datetime.strptime(f"{day} {n[:8]}", "%Y-%m-%d %H-%M-%S").timestamp() for n in names]
    segs = []
    for i, (name, t) in enumerate(zip(names, starts)):
        gap = starts[i + 1] - t if i + 1 < len(starts) else None
        if gap is not None and gap <= REC_SEGMENT + 30:
            dur = gap
        # trước chỗ ngắt quãng (hoặc file cuối) có thể là file đang ghi / ghi dở: chỉ lấy khi đã ghi xong
        elif mp4_complete(d / name):
            dur = REC_SEGMENT
        else:
            continue
        stem = name[:-4]
        segs.append({"t": t, "d": dur, "f": name, "m": motion.get(stem), "th": f"{stem}.jpg" in present})
    return segs


class RecordingDelete(BaseModel):
    files: list[str]  # tên file trong ngày, vd "13-05-00.mp4"


@app.delete("/api/recordings/{cam}/{day}")
def delete_recordings(cam: str, day: str, body: RecordingDelete, request: Request):
    """Xoá hẳn các đoạn video đã chọn (kèm ảnh thu nhỏ). Chỉ admin: PATH_PERMS không cho ai sửa /api/recordings.
    Bỏ qua file vừa ghi trong 90 s gần đây (recorder có thể vẫn đang ghi vào)."""
    if not DATE_RE.match(day):
        raise HTTPException(400, "Ngày phải có dạng YYYY-MM-DD")
    d = rec_cam_dir(cam) / day
    deleted = skipped = 0
    for name in set(body.files):
        if not REC_FILE.match(name):
            raise HTTPException(400, f"Tên file không hợp lệ: {name}")
        path = d / name
        try:
            if time.time() - path.stat().st_mtime < 90:
                skipped += 1
                continue
            path.unlink()
        except FileNotFoundError:
            continue
        (d / f"{name[:-4]}.jpg").unlink(missing_ok=True)
        start = file_start(day, name)
        note_skip(cam, start, start + SEGMENT + 5)  # người dùng cố ý xoá: không tự lấy lại từ thẻ nhớ
        deleted += 1
    if deleted:
        print(f"{request.state.user['username'] or 'admin'} xoá {deleted} video {cam}/{day}", flush=True)
    return {"deleted": deleted, "skipped": skipped}


# ---------- xem trực tiếp ----------
# go2rtc chỉ nghe trên máy chủ nên luồng đi qua web: mỗi người xem một ffmpeg đọc RTSP từ go2rtc, chép nguyên hình,
# sửa tiếng như recorder (Xiaomi khai sai tần số), xuất MP4 phân mảnh qua WebSocket; trình duyệt phát bằng
# Media Source Extensions (iPhone: ManagedMediaSource).
# Camera xem trực tiếp không kèm tiếng (vẫn ghi tiếng nếu bật "Kèm tiếng"): camera Wi-Fi yếu, tiếng về không đều làm
# trình phát phải giữ đệm tới 3 s; chỉ hình thì về đều, trễ như các camera khác.
LIVE_NO_AUDIO = set(filter(None, os.environ.get("LPR_LIVE_NO_AUDIO", "").split(",")))
LIVE_MAX = 16                                  # số luồng xem cùng lúc tối đa (mỗi luồng ~1% CPU; trang Xem mở mọi camera)
live_viewers = 0
live_probe: dict[str, tuple[float, dict]] = {}  # camera -> (lúc đọc, probe()) cho camera không bật ghi hình
LIVE_PROBE_TTL = 12 * 3600  # mã hoá chỉ đổi khi đổi link camera (khi đó xoá khỏi live_probe)


def live_info(cam: str) -> dict:
    """Mã hoá hình / tiếng của camera: lấy của recorder nếu đang ghi, không thì đọc thử luồng (~8 s, nhớ 12 giờ)."""
    if cam.startswith(SD_PREFIX):  # luồng phụ: tiếng như luồng chính, chỉ cần biết mã hoá hình (đọc nhanh)
        main = live_info(cam[len(SD_PREFIX):])
        t, info = live_probe.get(cam, (0.0, {}))
        if not info or time.time() - t > LIVE_PROBE_TTL:
            try:
                out = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0",
                                      "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                                      f"rtsp://{RTSP_HOST}/{cam}"], capture_output=True, text=True, timeout=20)
                info = {"video": out.stdout.strip()}
            except subprocess.TimeoutExpired:
                info = {}
            if info.get("video"):
                live_probe[cam] = (time.time(), info)
        return {**main, **info} if info.get("video") else {}
    try:
        c = json.loads(REC_STATUS.read_text()).get("cameras", {}).get(cam, {})
    except (OSError, ValueError):
        c = {}
    if c.get("recording") and (c.get("probe") or {}).get("video"):
        return c["probe"]
    t, info = live_probe.get(cam, (0.0, {}))
    if info and time.time() - t < LIVE_PROBE_TTL:
        return info
    info = probe(f"rtsp://{RTSP_HOST}/{cam}")
    if info.get("video"):
        live_probe[cam] = (time.time(), info)
    return info


def warm_live_info():
    """Đọc sẵn mã hoá các camera không ghi hình khi web khởi động, để lần xem trực tiếp đầu không chờ ~8 s."""
    for cam in go2rtc_streams():
        try:
            live_info(cam)
        except Exception:
            pass


threading.Thread(target=warm_live_info, daemon=True).start()


def mp4_init_end(buf: bytes) -> int | None:
    """Vị trí hết phần đầu (ftyp + moov) của MP4 phân mảnh; None nếu chưa đủ dữ liệu."""
    i = 0
    while i + 8 <= len(buf):
        size, kind = int.from_bytes(buf[i:i + 4], "big"), buf[i + 4:i + 8]
        if size < 8 or i + size > len(buf):
            return None
        i += size
        if kind == b"moov":
            return i
    return None


def mse_mime(init: bytes) -> str:
    """Chuỗi codecs cho MediaSource, đọc từ cấu hình bộ giải mã trong moov (avcC / hvcC)."""
    codecs = []
    if (k := init.find(b"hvcC")) > 0:
        c = init[k + 4:k + 17]
        space, tier, idc = c[1] >> 6, (c[1] >> 5) & 1, c[1] & 0x1F
        compat = int(f"{int.from_bytes(c[2:6], 'big'):032b}"[::-1], 2)  # đảo thứ tự bit theo ISO 14496-15
        cons = list(c[6:12])
        while cons and not cons[-1]:
            cons.pop()
        codecs.append(f"hvc1.{' ABC'[space].strip()}{idc}.{compat:X}.{'LH'[tier]}{c[12]}"
                      + "".join(f".{x:X}" for x in cons))
    elif (k := init.find(b"avcC")) > 0:
        c = init[k + 4:k + 8]
        codecs.append(f"avc1.{c[1]:02X}{c[2]:02X}{c[3]:02X}")
    if b"mp4a" in init:
        codecs.append("mp4a.40.2")
    return f'video/mp4; codecs="{",".join(codecs)}"'


@app.websocket("/api/live/{cam}")
async def live_stream(ws: WebSocket, cam: str, q: str = ""):
    """Tin nhắn đầu: {"type": "init", "mime"} hoặc {"type": "error", "msg"}; sau đó là các khối nhị phân fMP4.
    q=sd: luồng phụ chất lượng thấp (nhẹ ~10 lần, cho trang xem nhiều camera cùng lúc)."""
    global live_viewers
    origin = ws.headers.get("origin", "")
    # middleware đăng nhập không chạy cho WebSocket: kiểm tra phiên ở đây; chặn trang web khác mở hộ (cùng cookie)
    user = current_user(ws)
    if not user or not allowed(user, "GET", ws.url.path) or (user["cams"] is not None and cam not in user["cams"]) or \
            (origin and origin.split("://", 1)[-1] != ws.headers.get("host")):
        await ws.close(code=4401)
        return
    with auth_db() as c:
        known = c.execute("SELECT 1 FROM cameras WHERE id = ?", (cam,)).fetchone()
    await ws.accept()
    if not known:
        await ws.send_json({"type": "error", "msg": "Không có camera này"})
        return await ws.close()
    if live_viewers >= LIVE_MAX:
        await ws.send_json({"type": "error", "msg": f"Đang có {LIVE_MAX} luồng xem trực tiếp (tối đa), thử lại sau."})
        return await ws.close()
    live_viewers += 1
    proc = None
    try:
        stream = cam
        if q == "sd":
            try:
                stream = await run_in_threadpool(sd_stream, cam)
            except httpx.HTTPError:
                pass  # không tạo được luồng phụ: xem luồng chính
        info = await run_in_threadpool(live_info, stream)
        if not info.get("video"):
            await ws.send_json({"type": "error", "msg": "Không đọc được luồng từ camera (camera offline?)"})
            return
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
               "-stimeout", "10000000", "-i", f"rtsp://{RTSP_HOST}/{stream}", "-map", "0:v:0", "-c:v", "copy"]
        if info["video"] == "hevc":
            cmd += ["-tag:v", "hvc1"]
        if cam not in LIVE_NO_AUDIO:
            cmd += audio_args(info, LIVE_AUDIO_FOLLOW)
        # phân mảnh mỗi 0,2 s (không chờ khung khoá) để độ trễ thấp
        cmd += ["-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                "-frag_duration", "200000", "pipe:1"]
        proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        buf, end = b"", None
        while end is None:
            chunk = await asyncio.wait_for(proc.stdout.read(1 << 16), 25)
            if not chunk:
                await ws.send_json({"type": "error", "msg": "Camera không gửi hình, thử lại sau."})
                return
            buf += chunk
            end = mp4_init_end(buf)
        await ws.send_json({"type": "init", "mime": mse_mime(buf[:end])})
        await ws.send_bytes(buf)

        async def pump():
            while chunk := await proc.stdout.read(1 << 16):
                await ws.send_bytes(chunk)

        async def until_closed():
            while (await ws.receive())["type"] != "websocket.disconnect":
                pass

        tasks = [asyncio.create_task(pump()), asyncio.create_task(until_closed())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)  # người xem đóng giữa chừng: lỗi gửi là bình thường
    except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError, OSError):
        pass
    finally:
        live_viewers -= 1
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()
        try:
            await ws.close()
        except RuntimeError:
            pass


# ---------- xem trực tiếp bằng HLS (iPhone dưới iOS 17.1 không có MediaSource) ----------
# Trình phát HLS của iPhone không chắc gửi cookie cho mọi yêu cầu, và phần tiếp theo của luồng là đường dẫn tương đối
# nên mã xem nằm ngay trong đường dẫn. go2rtc chỉ nghe nội bộ: web chuyển tiếp, mỗi lần đều kiểm tra lại quyền.
HLS_TTL = 6 * 3600
hls_tokens: dict[str, dict] = {}  # mã -> {cam, username, session, until, ids (phiên HLS của go2rtc)}
HLS_TYPES = {"playlist.m3u8": "application/vnd.apple.mpegurl", "init.mp4": "video/mp4", "segment.m4s": "video/iso.segment"}


@app.get("/api/live/{cam}/hls")
def hls_start(cam: str, request: Request):
    check_cam(cam)
    now = time.time()
    for k in [k for k, t in hls_tokens.items() if t["until"] < now]:
        del hls_tokens[k]
    tk = secrets.token_urlsafe(24)
    cookie = request.cookies.get(SESSION_COOKIE)
    hls_tokens[tk] = {"cam": cam, "username": request.state.user["username"], "until": now + HLS_TTL, "ids": set(),
                      "session": token_hash(cookie) if cookie else None}
    return {"url": f"/hls/{tk}/stream.m3u8"}


def hls_entry(tk: str) -> dict:
    """Mã còn hạn, phiên chưa đăng xuất, tài khoản chưa bị khoá và vẫn được xem camera đó."""
    t = hls_tokens.get(tk)
    if t and t["until"] >= time.time():
        with auth_db() as c:
            r = c.execute("SELECT * FROM users WHERE username = ? AND disabled = 0", (t["username"],)).fetchone()
            alive = not t["session"] or c.execute("SELECT 1 FROM sessions WHERE token_hash = ?", (t["session"],)).fetchone()
        if r and alive:
            user = user_info(r)
            if allowed(user, "GET", f"/api/live/{t['cam']}") and (user["cams"] is None or t["cam"] in user["cams"]):
                return t
    raise HTTPException(404, "Không có luồng này")


def hls_forward(path: str, params: dict, media_type: str) -> Response:
    try:
        r = httpx.get(f"{GO2RTC_API}/api/{path}", params=params, timeout=20)
    except httpx.HTTPError:
        raise HTTPException(502, "Không đọc được luồng từ camera") from None
    return Response(r.content, status_code=r.status_code, media_type=media_type, headers={"Cache-Control": "no-store"})


@app.get("/hls/{tk}/stream.m3u8")
def hls_master(tk: str):
    t = hls_entry(tk)
    # mp4=flac: tiếng G.711 của camera đổi sang FLAC, loại iPhone phát được trong fMP4
    resp = hls_forward("stream.m3u8", {"src": t["cam"], "mp4": "flac"}, HLS_TYPES["playlist.m3u8"])
    if m := re.search(rb"id=(\w+)", resp.body):
        t["ids"].add(m[1].decode())
    return resp


@app.get("/hls/{tk}/hls/{name}")
def hls_part(tk: str, name: str, id: str, n: int | None = None):
    t = hls_entry(tk)
    if name not in HLS_TYPES or id not in t["ids"]:
        raise HTTPException(404, "Không có luồng này")
    return hls_forward(f"hls/{name}", {"id": id, **({} if n is None else {"n": n})}, HLS_TYPES[name])


# ---------- nói qua loa camera ----------
# Trình duyệt gửi tiếng micro (PCM 16 bit, 16 kHz, một kênh) qua WebSocket. ffmpeg đổi sang G.711 rồi đẩy (RTSP)
# vào luồng _talk_<camera> của go2rtc, go2rtc nối luồng đó vào kênh loa của camera (camera có kênh tiếng "sendonly",
# vd Xiaomi). go2rtc gửi xuống camera ngay khi nhận nên ffmpeg phải giãn đều theo thời gian thực (arealtime), không
# thì camera nhận từng cục, phát ngắt quãng. Camera Xiaomi khai 8 kHz nhưng loa phát 16 kHz (như tiếng thu về).
TALK_PREFIX = "_talk_"
TALK_RATE = 16000
TALK_MAX = 300                    # giây nói liền tối đa mỗi lần (quên tắt micro thì tự ngắt)
TALK_CODECS = {"PCMA": "pcm_alaw", "PCMU": "pcm_mulaw"}
talking: dict[str, str] = {}      # camera -> người đang nói


def talk_codec(cam: str) -> tuple[str, int] | None:
    """(mã hoá ffmpeg, tần số khai báo) của loa camera; None nếu không có loa hoặc go2rtc chưa nối camera."""
    try:
        r = httpx.get(f"{GO2RTC_API}/api/streams", params={"src": cam}, timeout=5)
        medias = [m for p in r.json().get("producers") or [] for m in p.get("medias") or []]
    except (httpx.HTTPError, ValueError):
        return None
    for m in medias:
        kind, direction, codec = (x.strip() for x in (m.split(",") + ["", ""])[:3])
        name, _, rate = codec.partition("/")
        if kind == "audio" and direction == "sendonly" and name in TALK_CODECS:
            return TALK_CODECS[name], int(rate or 8000)
    return None


def talk_enabled(cam: str) -> bool:
    """Đã bật "Nói qua loa" cho camera này ở trang Camera (camera ngoài đường thì để tắt)."""
    with auth_db() as c:
        r = c.execute("SELECT talk_enabled FROM cameras WHERE id = ?", (cam,)).fetchone()
    return bool(r and r[0])


@app.get("/api/live/{cam}/talk")
def talk_info(cam: str):
    check_cam(cam)
    return {"ok": talk_enabled(cam) and talk_codec(cam) is not None, "busy": cam in talking}


@app.websocket("/api/talk/{cam}")
async def talk(ws: WebSocket, cam: str):
    """Nhận các khối nhị phân PCM s16le 16 kHz một kênh, phát ra loa camera. Ai xem trực tiếp được thì nói được.
    Gửi lại {"type": "ready"} khi loa đã nối, {"type": "error", "msg"} nếu không được."""
    origin = ws.headers.get("origin", "")
    user = current_user(ws)
    if not user or not allowed(user, "GET", f"/api/live/{cam}") or \
            (user["cams"] is not None and cam not in user["cams"]) or \
            (origin and origin.split("://", 1)[-1] != ws.headers.get("host")):
        await ws.close(code=4401)
        return
    with auth_db() as c:
        known = c.execute("SELECT 1 FROM cameras WHERE id = ?", (cam,)).fetchone()
    await ws.accept()

    async def fail(text: str):
        try:
            await ws.send_json({"type": "error", "msg": text})
            await ws.close()
        except (RuntimeError, WebSocketDisconnect):
            pass

    if not known:
        return await fail("Không có camera này")
    if not talk_enabled(cam):
        return await fail("Camera này chưa bật “Nói qua loa” (trang Camera).")
    if cam in talking:
        return await fail(f"{talking[cam] or 'Người khác'} đang nói qua camera này, thử lại sau.")
    talking[cam] = user["username"]
    proc, started = None, time.time()
    try:
        codec = await run_in_threadpool(talk_codec, cam)
        if not codec:
            return await fail("Camera này không có loa (hoặc chưa nối được camera).")
        enc, rate = codec
        info = await run_in_threadpool(live_info, cam)
        double = 1.7 <= (info.get("audio_ratio") or 1) <= 2.3  # Xiaomi: tần số thật gấp đôi tần số khai báo
        real = rate * 2 if double else rate
        name = TALK_PREFIX + cam
        if name not in (go2rtc_sources() or {}):
            # luồng phải có sẵn thì ffmpeg mới đẩy vào được; nguồn giả (cổng 9 đóng) chỉ để giữ chỗ
            await run_in_threadpool(go2rtc_put, name, "rtsp://127.0.0.1:9/talk")
        af = f"aresample={real},asetnsamples=n={real // 50}:p=0,arealtime" + (f",asetrate={rate}" if double else "")
        proc = await asyncio.create_subprocess_exec(
            # không dò đầu vào (tiếng tới theo thời gian thực, dò thì chờ thêm 2–3 s)
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-probesize", "32", "-analyzeduration", "0",
            "-f", "s16le", "-ar", str(TALK_RATE),
            "-ac", "1", "-i", "pipe:0", "-af", af, "-c:a", enc, "-ac", "1",
            "-rtsp_transport", "tcp", "-f", "rtsp", f"rtsp://{RTSP_HOST}/{name}",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)

        def published() -> bool:
            # nguồn giả không bao giờ nối được, nên có nguồn đang nối = ffmpeg đã đẩy lên
            try:
                r = httpx.get(f"{GO2RTC_API}/api/streams", params={"src": name}, timeout=5)
                return any(p.get("format_name") for p in r.json().get("producers") or [])
            except (httpx.HTTPError, ValueError):
                return False

        async def link():
            # ffmpeg chỉ đẩy lên khi đã nhận tiếng. Nối loa khi go2rtc chưa thấy luồng thì lỗi, mỗi lần hụt mất
            # 0,3–4 s, nên chờ thấy luồng rồi mới nối (lệnh nối mất ~1,5 s: go2rtc chờ loa camera 1 s)
            for _ in range(40):
                await asyncio.sleep(0.15)
                if proc.returncode is not None:
                    break
                if not await run_in_threadpool(published):
                    continue
                try:
                    r = await run_in_threadpool(lambda: httpx.post(
                        f"{GO2RTC_API}/api/streams", params={"dst": cam, "src": f"rtsp://{RTSP_HOST}/{name}"},
                        timeout=10))
                except httpx.HTTPError:
                    continue
                if r.status_code == 200:
                    return await ws.send_json({"type": "ready"})
            await fail("Không nối được loa camera, thử lại sau.")

        async def pump():
            while (m := await ws.receive())["type"] != "websocket.disconnect":
                if m.get("bytes"):
                    proc.stdin.write(m["bytes"])
                    await proc.stdin.drain()

        linker = asyncio.create_task(link())
        try:
            await asyncio.wait_for(pump(), TALK_MAX)
        except asyncio.TimeoutError:
            await fail(f"Đã nói {TALK_MAX // 60} phút liền, tự tắt micro.")
        finally:
            linker.cancel()
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass  # trình duyệt đóng / ffmpeg dừng giữa chừng
    finally:
        talking.pop(cam, None)
        if proc:
            print(f"{user['username'] or 'admin'} nói qua loa {cam} {time.time() - started:.0f} s", flush=True)
            if proc.returncode is None:
                try:
                    proc.stdin.close()  # ffmpeg đẩy nốt rồi dừng; go2rtc tự ngắt loa khi luồng hết
                    await asyncio.wait_for(proc.wait(), 3)
                except (OSError, asyncio.TimeoutError):
                    proc.kill()
                    await proc.wait()
        try:
            await ws.close()
        except (RuntimeError, WebSocketDisconnect):
            pass


# ---------- cài đặt ----------
class SettingsIn(BaseModel):
    telegram_token: str | None = None  # None = giữ nguyên, "" = xoá
    telegram_chat_id: str | None = None
    # camera -> loại thông báo gửi Telegram: zone (vào vùng đã vẽ), camera (xuất hiện ở bất kỳ đâu
    # trong khung hình), unknown (người lạ), watch (người quen / biển số trong danh sách theo dõi)
    notify_matrix: dict[str, list[str]] | None = None
    camera_alert_classes: list[str] | None = None
    # khung giờ gửi (HH:MM, rỗng = cả ngày) và thời gian chờ giữa 2 tin của từng loại
    camera_alert_start: str | None = None
    camera_alert_end: str | None = None
    camera_alert_cooldown: int | None = None
    unknown_start: str | None = None
    unknown_end: str | None = None
    unknown_cooldown: int | None = None
    watch_start: str | None = None
    watch_end: str | None = None
    watch_cooldown: int | None = None
    scan_interval: int | None = None           # quét định kỳ khi không có chuyển động (giây)
    reid_window_hours: int | None = None       # người qua lại: chỉ so với người gặp trong bấy nhiêu giờ
    watch_people: list[int] | None = None
    watch_plates: list[str] | None = None
    ai_api_key: str | None = None      # None = giữ nguyên, "" = xoá
    ai_base_url: str | None = None
    ai_model: str | None = None
    ai_plate_model: str | None = None  # model riêng cho biển số ("" = dùng chung ai_model)
    ai_visits: str | None = None       # off / suggest (chỉ ghi ý kiến) / auto (tự gộp khi AI chắc chắn)
    ai_plates: str | None = None       # kiểm tra lại biển OCR đã đọc
    ai_read_missed: bool | None = None # đọc biển OCR trên máy thấy nhưng không đọc ra
    ai_min_ocr: float | None = None    # biển OCR đọc được dưới mức này: quá mờ, không gửi AI
    rec_max_gb: float | None = None    # giới hạn dung lượng video ghi hình, đầy thì xoá file cũ nhất


def put_setting(key, value):
    db.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
               (key, value))


def split(v):
    return [x for x in v.split(",") if x]


@app.get("/api/settings")
def read_settings():
    s = get_settings(db)
    token = s.get("telegram_token", "")
    return {
        "telegram_token_set": bool(token),
        "telegram_token_hint": f"…{token[-4:]}" if token else "",
        "telegram_chat_id": s.get("telegram_chat_id", ""),
        "notify_matrix": {cam: sorted(m) for cam, m in
                          notify_matrix(s, [r[0] for r in db.execute("SELECT id FROM cameras")]).items()},
        "camera_alert_classes": split(s.get("camera_alert_classes", "person")),
        "times": {mode: {"start": s.get(f"{prefix}_start", ""), "end": s.get(f"{prefix}_end", ""),
                         "cooldown": int(s.get(f"{prefix}_cooldown") or default)}
                  for mode, prefix, default in (("camera", "camera_alert", 120), ("unknown", "unknown", 0),
                                                ("watch", "watch", 300))},
        "scan_interval": int(float(s.get("scan_interval") or 10)),
        "reid_window_hours": int(float(s.get("reid_window_hours") or 12)),
        "watch_people": [r[0] for r in db.execute("SELECT id FROM people WHERE notify = 1")],
        "watch_plates": [normalize_plate(k) or k for k in split(s.get("watch_plates", ""))],
        "rec_max_gb": rec_limit_gb(s),
        **ai_settings(s),
    }


def ai_settings(s):
    key = s.get("ai_api_key", "")
    try:
        state = json.loads(s.get("ai_state") or "{}")
    except ValueError:
        state = {}
    if state.get("day") != datetime.now().strftime("%Y-%m-%d"):
        state.update(tokens=0, calls=0, cost=0.0)
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    visits = dict(db.execute("SELECT ai_verdict, COUNT(*) FROM visits WHERE ts >= ? AND ai_verdict IS NOT NULL"
                             " GROUP BY ai_verdict", (today,)).fetchall())
    merged = db.execute("SELECT COUNT(*) FROM visits WHERE ts >= ? AND ai_verdict = 'same' AND maybe_visitor IS NULL"
                        " AND ai_conf >= ?", (today, ai.MERGE_CONF)).fetchone()[0]
    plates = dict(db.execute("SELECT ai_status, COUNT(*) FROM sightings WHERE first_seen >= ? AND ai_status IS NOT NULL"
                             " GROUP BY ai_status", (today,)).fetchall())
    cands = dict(db.execute("SELECT COALESCE(status, 'pending'), COUNT(*) FROM plate_candidates WHERE ts >= ?"
                            " GROUP BY 1", (today,)).fetchall())
    pending = db.execute("SELECT COUNT(*) FROM visits WHERE maybe_visitor IS NOT NULL AND ai_verdict IS NULL"
                         " AND maybe_visitor IN (SELECT id FROM visitors)").fetchone()[0]
    return {
        "ai_key_set": bool(key), "ai_key_hint": f"…{key[-4:]}" if key else "",
        "ai_base_url": s.get("ai_base_url") or ai.DEFAULT_BASE_URL, "ai_model": s.get("ai_model", ""),
        "ai_plate_model": s.get("ai_plate_model", ""),
        "ai_visits": s.get("ai_visits", "off"), "ai_plates": s.get("ai_plates", "off"),
        "ai_read_missed": s.get("ai_read_missed", "1") == "1",
        "ai_min_ocr": float(s.get("ai_min_ocr") or ai.DEFAULT_MIN_OCR),
        "ai_state": state, "ai_pending": pending,
        "ai_today": {"visits": visits, "merged": merged, "plates": plates, "candidates": cands},
    }


@app.put("/api/settings")
def save_settings(body: SettingsIn):
    v = body.model_dump(exclude_none=True)
    if "watch_people" in v:
        ids = set(v.pop("watch_people"))
        for (pid,) in db.execute("SELECT id FROM people").fetchall():
            db.execute("UPDATE people SET notify = ? WHERE id = ?", (int(pid in ids), pid))
    if "watch_plates" in v:
        keys = dict.fromkeys(plate_key(p) for p in v.pop("watch_plates") if plate_key(p))
        put_setting("watch_plates", ",".join(keys))
    if "notify_matrix" in v:
        cams = {r[0] for r in db.execute("SELECT id FROM cameras")}
        clean = {cam: [m for m in NOTIFY_MODES if m in modes] for cam, modes in v.pop("notify_matrix").items() if cam in cams}
        put_setting("notify_matrix", json.dumps(clean))
    if "camera_alert_classes" in v:
        v["camera_alert_classes"] = [c for c in ZONE_CLASSES if c in v["camera_alert_classes"]] or ["person"]
    for k in [k for k in v if k.endswith("_cooldown")]:
        v[k] = min(86400, max(0, v[k]))
    for k in [k for k in v if k.endswith(("_start", "_end"))]:
        if v[k] and not TIME_RE.match(v[k]):
            raise HTTPException(400, "Giờ phải có dạng HH:MM")
    if "scan_interval" in v:
        v["scan_interval"] = min(60, max(2, v["scan_interval"]))
    if "reid_window_hours" in v:
        v["reid_window_hours"] = min(168, max(1, v["reid_window_hours"]))
    for k in ("ai_visits", "ai_plates"):
        if k in v and v[k] not in ai.MODES:
            raise HTTPException(400, "Chế độ AI không hợp lệ")
    if "ai_min_ocr" in v:
        v["ai_min_ocr"] = min(0.9, max(0.0, v["ai_min_ocr"]))
    if "rec_max_gb" in v:
        REC_DIR.mkdir(parents=True, exist_ok=True)
        disk_gb = shutil.disk_usage(REC_DIR).total / 1e9
        if not 1 <= v["rec_max_gb"] <= disk_gb * 0.95:
            raise HTTPException(400, f"Giới hạn video phải từ 1 tới {disk_gb * 0.95:.0f} GB (ổ chứa video có {disk_gb:.0f} GB)")
        v["rec_max_gb"] = round(v["rec_max_gb"])
    if v.get("ai_base_url") and not v["ai_base_url"].startswith(("https://", "http://")):
        raise HTTPException(400, "Địa chỉ API phải bắt đầu bằng https://")
    for k, val in v.items():
        if isinstance(val, bool):
            val = "1" if val else "0"
        elif isinstance(val, list):
            val = ",".join(val)
        put_setting(k, str(val).strip())
    db.commit()
    return read_settings()


def _token():
    token = get_settings(db).get("telegram_token")
    if not token:
        raise HTTPException(400, "Chưa nhập token bot Telegram")
    return token


@app.post("/api/settings/telegram/test")
def telegram_test():
    chat_id = get_settings(db).get("telegram_chat_id")
    if not chat_id:
        raise HTTPException(400, "Chưa có chat ID")
    try:
        telegram_call(_token(), "sendMessage", {"chat_id": chat_id, "text": "✅ Đã kết nối hệ thống camera. Cảnh báo sẽ được gửi vào đây."})
    except Exception as e:
        hint = ""
        if "chat not found" in str(e).lower():
            hint = (" — ID của nhóm Telegram luôn có dấu trừ phía trước (vd -1001234567890), và bot phải được thêm vào nhóm."
                    " Gửi một tin trong nhóm rồi bấm “Tự lấy chat ID” để lấy đúng ID.")
        raise HTTPException(400, f"Telegram báo lỗi: {e}{hint}")
    return {"ok": True}


@app.post("/api/settings/telegram/detect-chat")
def telegram_detect_chat():
    """Lấy chat ID từ tin nhắn gần nhất người dùng gửi cho bot."""
    try:
        updates = telegram_call(_token(), "getUpdates", {"limit": 50}).get("result", [])
    except Exception as e:
        raise HTTPException(400, f"Telegram báo lỗi: {e}")
    for u in reversed(updates):
        chat = (u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}).get("chat")
        if chat:
            title = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("username", "")
            db.execute("INSERT INTO settings(key, value) VALUES ('telegram_chat_id', ?)"
                       " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(chat["id"]),))
            db.commit()
            return {"chat_id": str(chat["id"]), "title": title}
    raise HTTPException(404, "Chưa thấy tin nhắn nào. Mở bot trong Telegram, bấm Start hoặc gửi một tin bất kỳ rồi thử lại.")


def _ai_conn():
    s = get_settings(db)
    if not s.get("ai_api_key"):
        raise HTTPException(400, "Chưa nhập API key")
    return s.get("ai_base_url") or ai.DEFAULT_BASE_URL, s["ai_api_key"], s


@app.get("/api/settings/ai/models")
def ai_models():
    """Các model của nhà cung cấp, model đọc được ảnh lên trước."""
    base, key, _ = _ai_conn()
    try:
        models = ai.list_models(base, key)
    except (ai.ApiError, httpx.HTTPError) as e:
        raise HTTPException(400, f"Không lấy được danh sách model: {e}")
    # nhà cung cấp báo rõ model không đọc được ảnh thì bỏ; không báo (None) thì vẫn cho chọn rồi bấm "Thử AI"
    return sorted([m for m in models if m["vision"] is not False], key=lambda m: m["id"].lower())


@app.post("/api/settings/ai/test")
def ai_test():
    """Gửi một ảnh biển số gần đây (hoặc ảnh camera) để chắc model đọc được ảnh."""
    base, key, s = _ai_conn()
    if not s.get("ai_model"):
        raise HTTPException(400, "Chưa chọn model")
    r = db.execute("SELECT plate_img FROM sightings ORDER BY first_seen DESC LIMIT 1").fetchone()
    content = [{"type": "text", "text": "Đọc biển số xe trong ảnh. Trả lời ngắn gọn bằng tiếng Việt."}]
    if r:
        content.append(ai.image_part(r[0], min_w=480))
    t = time.time()
    try:
        text, tokens, cost = ai.chat(base, key, s["ai_model"], [{"role": "user", "content": content}], timeout=60)
    except (ai.ApiError, httpx.HTTPError) as e:
        raise HTTPException(400, f"AI báo lỗi: {e}")
    return {"reply": text.strip()[:300], "tokens": tokens, "cost": cost, "seconds": round(time.time() - t, 1),
            "with_image": bool(r)}


# ---------- route riêng của từng máy (tuỳ chọn) ----------
# local_routes.py không nằm trong project (đã .gitignore): định nghĩa register(app, web) để thêm route riêng.
try:
    import local_routes
except ImportError:
    local_routes = None
if local_routes:
    local_routes.register(app, sys.modules[__name__])


@app.get("/api/health")
def health():
    return {"ok": True, "time": time.time()}


app.mount("/images", StaticFiles(directory=IMAGES_DIR), name="images")
REC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/rec", StaticFiles(directory=REC_DIR), name="rec")  # video ghi hình, trình duyệt tua bằng Range
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


NO_CACHE = {"Cache-Control": "no-cache"}


def asset_version() -> str:
    # đổi mỗi lần triển khai code giao diện mới, để trình duyệt / app điện thoại không dùng bản cũ đã lưu
    return str(max(int((BASE_DIR / "static" / f).stat().st_mtime) for f in ("app.js", "style.css")))


@app.get("/")
def index():
    html = (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")
    v = asset_version()
    html = html.replace("/static/style.css", f"/static/style.css?v={v}").replace("/static/app.js", f"/static/app.js?v={v}")
    return Response(html, media_type="text/html; charset=utf-8", headers=NO_CACHE)


@app.get("/api/version")
def version():
    return {"version": asset_version()}


@app.get("/login")
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return FileResponse(BASE_DIR / "static" / "login.html", headers=NO_CACHE)


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(BASE_DIR / "static" / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    # service worker phải nằm ở gốc để điều khiển cả trang
    return FileResponse(BASE_DIR / "static" / "sw.js", media_type="text/javascript", headers=NO_CACHE)


@app.get("/favicon.ico")
def favicon():
    return FileResponse(BASE_DIR / "static" / "icons" / "icon-192.png", media_type="image/png")
