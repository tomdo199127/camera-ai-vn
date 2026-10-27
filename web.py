"""Web quản lý: biển số và lịch trình, cảnh báo vùng, khuôn mặt người quen, cài đặt Telegram."""

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.request
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta

import cv2
import httpx
import numpy as np

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from common import (BASE_DIR, GO2RTC_API, IMAGES_DIR, RTSP_HOST, go2rtc_sources, NOTIFY_MODES, connect, get_settings, init_db, merge_visitors,
                    notify_matrix)
import ai
import search

# thử link camera đọc RTSP giống worker: qua TCP, tắt cảnh báo HEVC lúc chờ keyframe
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")
from notify import telegram_call
from plates import normalize_plate, plate_key

WEB_USER = os.environ.get("WEB_USER", "admin")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")

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
SESSION_DAYS = int(os.environ.get("WEB_SESSION_DAYS", "30"))
# trang/tệp mở được khi chưa đăng nhập (không chứa dữ liệu camera)
PUBLIC_PATHS = {"/login", "/api/login", "/manifest.webmanifest", "/sw.js", "/favicon.ico"}
PUBLIC_PREFIXES = ["/static/login.", "/static/icons/"]
LOGIN_FAILS: dict[str, list[float]] = {}  # ip -> thời điểm các lần sai gần đây
MAX_FAILS, FAIL_WINDOW = 5, 600


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    # qua ngrok (agent chạy trên chính server) thì IP thật là phần tử cuối do ngrok thêm vào X-Forwarded-For;
    # các phần tử trước do người gửi tự đặt được nên không tin
    fwd = request.headers.get("x-forwarded-for", "")
    if peer in ("127.0.0.1", "::1") and fwd:
        return fwd.split(",")[-1].strip()
    return peer


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


def session_user(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    now = time.time()
    with auth_db() as c:
        row = c.execute("SELECT username, expires_at, last_seen FROM sessions WHERE token_hash = ?",
                        (token_hash(token),)).fetchone()
        if not row or row["expires_at"] < now:
            return None
        if now - row["last_seen"] > 3600:  # gia hạn trượt: còn dùng thì còn đăng nhập
            c.execute("UPDATE sessions SET last_seen = ?, expires_at = ? WHERE token_hash = ?",
                      (now, now + SESSION_DAYS * 86400, token_hash(token)))
        return row["username"]


def basic_ok(request: Request) -> bool:
    """Vẫn nhận HTTP Basic cho script/curl, nhưng trình duyệt không còn bị hỏi bằng hộp thoại nữa."""
    header = request.headers.get("authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        user, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
    except Exception:
        return False
    return secrets.compare_digest(user, WEB_USER) and secrets.compare_digest(pwd, WEB_PASSWORD)


@app.middleware("http")
async def static_revalidate(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/static/") and "cache-control" not in resp.headers:
        resp.headers["cache-control"] = "no-cache"
    return resp


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Bảo vệ toàn bộ web bằng phiên đăng nhập (cookie) khi có đặt WEB_PASSWORD."""
    path = request.url.path
    if (not WEB_PASSWORD or path in PUBLIC_PATHS or path.startswith(tuple(PUBLIC_PREFIXES))
            or session_user(request) or basic_ok(request)):
        return await call_next(request)
    if path.startswith("/api/") or path.startswith("/images/"):
        return JSONResponse({"detail": "Chưa đăng nhập"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


class LoginIn(BaseModel):
    username: str
    password: str
    remember: bool = True


@app.post("/api/login")
def login(body: LoginIn, request: Request):
    ip, now = client_ip(request), time.time()
    fails = [t for t in LOGIN_FAILS.get(ip, []) if now - t < FAIL_WINDOW]
    if len(fails) >= MAX_FAILS:
        wait = int(FAIL_WINDOW - (now - fails[0])) // 60 + 1
        raise HTTPException(429, f"Sai mật khẩu quá nhiều lần, thử lại sau {wait} phút")
    ok = (bool(WEB_PASSWORD) and secrets.compare_digest(body.username.strip(), WEB_USER)
          and secrets.compare_digest(body.password, WEB_PASSWORD))
    if not ok:
        fails.append(now)
        LOGIN_FAILS[ip] = fails
        time.sleep(1)  # làm chậm dò mật khẩu
        raise HTTPException(401, "Sai tên đăng nhập hoặc mật khẩu")
    LOGIN_FAILS.pop(ip, None)
    token = secrets.token_urlsafe(32)
    days = SESSION_DAYS if body.remember else 1
    with auth_db() as c:
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        c.execute("INSERT INTO sessions(token_hash, username, created_at, expires_at, last_seen, ip, agent)"
                  " VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (token_hash(token), WEB_USER, now, now + days * 86400, now, ip,
                   request.headers.get("user-agent", "")[:200]))
    resp = JSONResponse({"ok": True})
    https = request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"
    resp.set_cookie(SESSION_COOKIE, token, max_age=days * 86400 if body.remember else None,
                    httponly=True, samesite="lax", secure=https, path="/")
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


@app.get("/api/sessions")
def list_sessions(request: Request):
    mine = token_hash(request.cookies.get(SESSION_COOKIE, ""))
    with auth_db() as c:
        out = [dict(r) for r in c.execute("SELECT token_hash, created_at, last_seen, expires_at, ip, agent"
                                          " FROM sessions WHERE expires_at > ? ORDER BY last_seen DESC",
                                          (time.time(),))]
    for r in out:
        r["current"] = r["token_hash"] == mine
        r["id"] = r.pop("token_hash")[:16]
    return out


@app.delete("/api/sessions/{sid}")
def revoke_session(sid: str):
    if not re.fullmatch(r"[0-9a-f]{16}", sid):
        raise HTTPException(400, "ID không hợp lệ")
    with auth_db() as c:
        c.execute("DELETE FROM sessions WHERE substr(token_hash, 1, 16) = ?", (sid,))
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
    r = db.execute(
        f"SELECT COUNT(*) AS sightings, COUNT(DISTINCT {PLATE}) AS plates FROM sightings s WHERE first_seen >= ?",
        (today,),
    ).fetchone()
    total = db.execute(f"SELECT COUNT(*), COUNT(DISTINCT {PLATE}) FROM sightings s").fetchone()
    events = dict(db.execute("SELECT kind, COUNT(*) FROM events WHERE ts >= ? GROUP BY kind", (today,)).fetchall())
    hourly = rows(
        "SELECT CAST(strftime('%H', first_seen, 'unixepoch', 'localtime') AS INTEGER) AS hour, COUNT(*) AS n"
        " FROM sightings WHERE first_seen >= ? GROUP BY hour",
        (today,),
    )
    return {
        "today_sightings": r["sightings"], "today_plates": r["plates"],
        "total_sightings": total[0], "total_plates": total[1], "hourly": hourly,
        "today_zone_events": events.get("zone", 0), "today_face_events": events.get("face", 0),
    }


@app.get("/api/sightings")
def list_sightings(q: str = "", camera: str = "", date_from: str | None = None, date_to: str | None = None,
                   limit: int = 60, offset: int = 0):
    start, end = day_range(date_from, date_to)
    where, args = ["s.first_seen >= ?", "s.first_seen < ?"], [start, end]
    if q:
        where.append(f"REPLACE(REPLACE({PLATE}, '-', ''), '.', '') LIKE ?")
        args.append("%" + q.upper().replace("-", "").replace(".", "").replace(" ", "") + "%")
    if camera:
        where.append("s.camera = ?")
        args.append(camera)
    sql_where = " AND ".join(where)
    total = db.execute(f"SELECT COUNT(*) FROM sightings s WHERE {sql_where}", args).fetchone()[0]
    items = rows(
        f"SELECT {SIGHTING_COLS} FROM sightings s LEFT JOIN cameras c ON c.id = s.camera"
        f" WHERE {sql_where} ORDER BY s.first_seen DESC LIMIT ? OFFSET ?",
        (*args, min(limit, 500), offset),
    )
    return {"total": total, "items": items}


class SightingUpdate(BaseModel):
    plate: str | None = None  # None hoặc "" -> bỏ sửa, quay về kết quả OCR


@app.patch("/api/sightings/{sid}")
def update_sighting(sid: int, body: SightingUpdate):
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
    r = db.execute("SELECT plate_img, frame_img FROM sightings WHERE id = ?", (sid,)).fetchone()
    if not r:
        raise HTTPException(404, "Không tìm thấy")
    for rel in r:
        (IMAGES_DIR / rel).unlink(missing_ok=True)
    db.execute("DELETE FROM sightings WHERE id = ?", (sid,))
    db.commit()
    return {"ok": True}


@app.get("/api/plates")
def list_plates(q: str = "", limit: int = 200):
    where, args = "", []
    if q:
        where = f"WHERE REPLACE(REPLACE({PLATE}, '-', ''), '.', '') LIKE ?"
        args.append("%" + q.upper().replace("-", "").replace(".", "").replace(" ", "") + "%")
    return rows(
        f"""SELECT {PLATE} AS plate, COUNT(*) AS sightings, MIN(s.first_seen) AS first_seen,
                   MAX(s.last_seen) AS last_seen, GROUP_CONCAT(DISTINCT s.camera) AS cameras,
                   (SELECT plate_img FROM sightings s2 WHERE COALESCE(s2.corrected_plate, s2.plate) = {PLATE}
                    ORDER BY s2.ocr_score DESC LIMIT 1) AS plate_img
            FROM sightings s {where} GROUP BY {PLATE} ORDER BY last_seen DESC LIMIT ?""",
        (*args, limit),
    )


@app.get("/api/plates/{plate}/timeline")
def plate_timeline(plate: str, date_from: str | None = None, date_to: str | None = None):
    start, end = day_range(date_from, date_to)
    items = rows(
        f"SELECT {SIGHTING_COLS} FROM sightings s LEFT JOIN cameras c ON c.id = s.camera"
        f" WHERE {PLATE} = ? AND s.last_seen >= ? AND s.first_seen < ? ORDER BY s.first_seen",
        (plate, start, end),
    )
    summary = db.execute(
        f"SELECT COUNT(*) AS n, MIN(first_seen) AS first_seen, MAX(last_seen) AS last_seen,"
        f" SUM(last_seen - first_seen) AS dwell FROM sightings s WHERE {PLATE} = ?",
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
        " FROM cameras c LEFT JOIN camera_status st ON st.camera = c.id ORDER BY c.sort_order, c.id"
    )
    for c in cams:
        src = (streams.get(c["id"]) or [""])[0]
        c["source"] = mask_source(src)
        c["source_kind"] = src.split(":", 1)[0].lower() if src else ""
    return cams


class CameraUpdate(BaseModel):
    name: str | None = None
    location: str | None = None
    lpr_enabled: bool | None = None
    face_enabled: bool | None = None
    track_enabled: bool | None = None


@app.patch("/api/cameras/{cam}")
def update_camera(cam: str, body: CameraUpdate):
    for field, value in body.model_dump(exclude_none=True).items():
        db.execute(f"UPDATE cameras SET {field} = ? WHERE id = ?", (int(value) if isinstance(value, bool) else value, cam))
    db.commit()
    return {"ok": True}


@app.get("/api/cameras/{cam}/snapshot")
def camera_snapshot(cam: str):
    """Ảnh hiện tại của camera, lấy qua go2rtc."""
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


def mask_source(url: str) -> str:
    """Ẩn mật khẩu trong link camera trước khi gửi ra trình duyệt."""
    return re.sub(r"(://[^:/@?#]*:)[^@/]*@", r"\1•••@", url)


def go2rtc_streams() -> dict[str, list[str]]:
    """{tên luồng: [link nguồn]} trong go2rtc (bỏ luồng thử); {} nếu go2rtc không chạy."""
    return {name: src for name, src in (go2rtc_sources() or {}).items() if not name.startswith(PROBE_PREFIX)}


def go2rtc_put(name: str, source: str):
    """Thêm luồng; go2rtc tự ghi vào go2rtc.yaml nên còn nguyên sau khi khởi động lại."""
    httpx.put(f"{GO2RTC_API}/api/streams", params={"name": name, "src": source}, timeout=10).raise_for_status()


def go2rtc_delete(name: str):
    httpx.delete(f"{GO2RTC_API}/api/streams", params={"src": name}, timeout=10).raise_for_status()


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
    if not db.execute("SELECT 1 FROM cameras WHERE id = ?", (cam,)).fetchone():
        raise HTTPException(404, "Không có camera này")
    source = check_source(body.source)
    probe_source(source)
    try:
        if cam in go2rtc_streams():
            go2rtc_delete(cam)
        go2rtc_put(cam, source)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Không lưu được vào go2rtc: {e}")
    return {"ok": True}


@app.delete("/api/cameras/{cam}")
def delete_camera(cam: str):
    """Xoá camera khỏi go2rtc và danh sách; lịch sử biển số / người qua lại của camera vẫn giữ."""
    if not db.execute("SELECT 1 FROM cameras WHERE id = ?", (cam,)).fetchone():
        raise HTTPException(404, "Không có camera này")
    try:
        if cam in go2rtc_streams():
            go2rtc_delete(cam)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Không xoá được khỏi go2rtc: {e}")
    db.execute("DELETE FROM zones WHERE camera = ?", (cam,))
    db.execute("DELETE FROM camera_status WHERE camera = ?", (cam,))
    db.execute("DELETE FROM cameras WHERE id = ?", (cam,))
    db.commit()
    return {"ok": True}

# ---------- vùng cảnh báo ----------
ZONE_CLASSES = ("person", "car", "bike")
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
    sql, args = "SELECT * FROM zones", ()
    if camera:
        sql, args = sql + " WHERE camera = ?", (camera,)
    return [zone_out(r) for r in db.execute(sql + " ORDER BY id", args)]


@app.post("/api/zones")
def create_zone(body: ZoneIn):
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
                date_from: str | None = None, date_to: str | None = None, limit: int = 60, offset: int = 0):
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
    sql_where = " AND ".join(where)
    total = db.execute(f"SELECT COUNT(*) FROM events e WHERE {sql_where}", args).fetchone()[0]
    items = rows(f"SELECT {EVENT_COLS} FROM {EVENT_FROM} WHERE {sql_where} ORDER BY e.ts DESC LIMIT ? OFFSET ?",
                 (*args, min(limit, 500), offset))
    return {"total": total, "items": items}


@app.delete("/api/events/{eid}")
def delete_event(eid: int):
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
    return rows(
        """SELECT p.*, (SELECT COUNT(*) FROM faces f WHERE f.person_id = p.id) AS faces,
                  (SELECT img FROM faces f WHERE f.person_id = p.id ORDER BY f.id DESC LIMIT 1) AS face_img,
                  (SELECT MAX(e.last_seen) FROM events e WHERE e.person_id = p.id) AS last_seen,
                  (SELECT COUNT(*) FROM events e WHERE e.person_id = p.id) AS events
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
VISITOR_COLS = f"""p.id, p.label, p.person_id, pe.name AS person_name, COUNT(v.id) AS visits,
                   MIN(v.ts) AS first_seen, MAX(v.last_seen) AS last_seen, GROUP_CONCAT(DISTINCT v.camera) AS cameras,
                   (SELECT img FROM visits v2 WHERE v2.visitor_id = p.id ORDER BY v2.ts DESC LIMIT 1) AS img,
                   (SELECT COUNT(*) FROM ({SUGGEST_SQL})) AS suggestions"""
VISITOR_FROM = "visits v JOIN visitors p ON p.id = v.visitor_id LEFT JOIN people pe ON pe.id = p.person_id"
SEARCH_SPREAD = 0.08   # chỉ giữ người có điểm cách người khớp nhất không quá chừng này
SEARCH_MAX = 300
_clip, _clip_lock = None, threading.Lock()


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
    limit, offset = min(limit, 500), max(0, offset)
    if q.strip():
        return search_visitors(q.strip(), where, args, limit, offset)
    return rows(
        f"""SELECT {VISITOR_COLS} FROM {VISITOR_FROM}
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
        f"""SELECT {VISITOR_COLS} FROM {VISITOR_FROM}
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
    start, end = day_range(date_from, date_to)
    visits = rows("SELECT v.id, v.camera, c.name AS camera_name, v.ts, v.last_seen, v.similarity, v.img, v.frame_img,"
                  " v.ai_verdict, v.ai_conf, v.ai_reason"
                  " FROM visits v LEFT JOIN cameras c ON c.id = v.camera"
                  " WHERE v.visitor_id = ? AND v.ts >= ? AND v.ts < ? ORDER BY v.ts DESC", (vid, start, end))
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
            LEFT JOIN visits lv ON lv.id = (SELECT id FROM visits WHERE visitor_id = s.id ORDER BY ts DESC LIMIT 1)
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
    if not WEB_PASSWORD or session_user(request):
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
