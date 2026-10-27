"""Cấu hình và cơ sở dữ liệu dùng chung cho worker và web."""

import json
import os
import sqlite3
import urllib.request
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("LPR_DATA_DIR", BASE_DIR / "data"))
IMAGES_DIR = DATA_DIR / "images"
DB_PATH = DATA_DIR / "lpr.db"
MODELS_DIR = Path(os.environ.get("LPR_MODELS_DIR", BASE_DIR / "models"))

GO2RTC_API = os.environ.get("GO2RTC_API", "http://127.0.0.1:1984")
RTSP_HOST = os.environ.get("RTSP_HOST", "127.0.0.1:8554")


def go2rtc_sources(timeout: float = 3) -> dict[str, list[str]] | None:
    """{tên luồng: [link nguồn]} đọc từ file cấu hình go2rtc; None nếu go2rtc không trả lời.
    Đọc cấu hình chứ không đọc /api/streams vì luồng Xiaomi đang chạy không còn báo link nguồn."""
    try:
        with urllib.request.urlopen(f"{GO2RTC_API}/api/config", timeout=timeout) as r:
            cfg = yaml.safe_load(r.read()) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return None
    streams = cfg.get("streams") or {}
    return {str(name): [str(s) for s in (src if isinstance(src, list) else [src]) if s]
            for name, src in streams.items()}

SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    id          TEXT PRIMARY KEY,           -- tên stream trong go2rtc
    name        TEXT NOT NULL,
    location    TEXT NOT NULL DEFAULT '',
    lpr_enabled INTEGER NOT NULL DEFAULT 0,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

-- Một lần "xuất hiện": biển số được thấy liên tục trên một camera
CREATE TABLE IF NOT EXISTS sightings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    plate           TEXT NOT NULL,          -- biển do OCR luận ra
    corrected_plate TEXT,                   -- biển người dùng sửa lại (nếu có)
    camera          TEXT NOT NULL,
    first_seen      REAL NOT NULL,          -- unix time
    last_seen       REAL NOT NULL,
    reads           INTEGER NOT NULL DEFAULT 1,
    ocr_score       REAL NOT NULL,
    raw_text        TEXT NOT NULL DEFAULT '',
    plate_img       TEXT NOT NULL,          -- đường dẫn tương đối trong IMAGES_DIR
    frame_img       TEXT NOT NULL,
    bbox            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sightings_first ON sightings(first_seen);
CREATE INDEX IF NOT EXISTS idx_sightings_plate ON sightings(COALESCE(corrected_plate, plate));
CREATE INDEX IF NOT EXISTS idx_sightings_camera ON sightings(camera, first_seen);

-- Trạng thái worker theo từng camera
CREATE TABLE IF NOT EXISTS camera_status (
    camera        TEXT PRIMARY KEY,
    connected     INTEGER NOT NULL DEFAULT 0,
    last_frame_at REAL,
    fps           REAL,
    updated_at    REAL
);

-- Vùng cảnh báo vẽ trên ảnh camera; toạ độ chuẩn hoá 0..1
CREATE TABLE IF NOT EXISTS zones (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    camera     TEXT NOT NULL,
    name       TEXT NOT NULL,
    points     TEXT NOT NULL,               -- JSON [[x, y], ...]
    classes    TEXT NOT NULL DEFAULT 'person', -- person,car,bike
    start_time TEXT NOT NULL DEFAULT '',    -- 'HH:MM', rỗng = cả ngày
    end_time   TEXT NOT NULL DEFAULT '',
    cooldown   INTEGER NOT NULL DEFAULT 60, -- giây tối thiểu giữa 2 cảnh báo của vùng
    notify     INTEGER NOT NULL DEFAULT 1,  -- gửi Telegram
    enabled    INTEGER NOT NULL DEFAULT 1
);

-- Sự kiện: có đối tượng vào vùng (kind='zone') hoặc thấy khuôn mặt (kind='face')
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,
    camera    TEXT NOT NULL,
    ts        REAL NOT NULL,
    last_seen REAL NOT NULL,
    label     TEXT NOT NULL,                -- 'Người' / tên người quen / 'Người lạ'
    zone_id   INTEGER,
    person_id INTEGER,
    score     REAL NOT NULL DEFAULT 0,      -- độ tin cậy phát hiện / độ giống khuôn mặt
    img       TEXT NOT NULL,                -- ảnh cắt đối tượng / khuôn mặt
    frame_img TEXT NOT NULL,
    embedding BLOB,                         -- vector khuôn mặt, để gán người lạ thành người quen
    notified  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, ts);
CREATE INDEX IF NOT EXISTS idx_events_person ON events(person_id, ts);

-- Người quen và các ảnh mặt mẫu của họ
CREATE TABLE IF NOT EXISTS people (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    notify     INTEGER NOT NULL DEFAULT 0,  -- gửi Telegram khi thấy người này
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS faces (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id  INTEGER NOT NULL,
    embedding  BLOB NOT NULL,
    img        TEXT NOT NULL,
    created_at REAL NOT NULL
);

-- Người qua lại: mỗi visitor là một người (nhận ra lại theo màu quần áo / dáng người),
-- mỗi visit là một lần người đó xuất hiện trên một camera
CREATE TABLE IF NOT EXISTS visitors (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    label      TEXT NOT NULL DEFAULT '',
    person_id  INTEGER,                     -- nếu nhận ra khuôn mặt người quen
    descriptor BLOB NOT NULL,
    img        TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_visitors_last ON visitors(last_seen);
CREATE TABLE IF NOT EXISTS visits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    visitor_id INTEGER NOT NULL,
    camera     TEXT NOT NULL,
    ts         REAL NOT NULL,
    last_seen  REAL NOT NULL,
    similarity REAL NOT NULL DEFAULT 0,     -- độ giống với người đã gặp trước (0 = người mới)
    img        TEXT NOT NULL,
    frame_img  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_visits_visitor ON visits(visitor_id, ts);
CREATE INDEX IF NOT EXISTS idx_visits_ts ON visits(ts);

-- Biển thấy được nhưng OCR không đọc chắc trong cả lần xe đi qua (xe đi nhanh, biển nhỏ / nghiêng):
-- giữ ảnh rõ nhất để AI (ai.py) đọc; đọc được thì thành một lần xuất hiện trong sightings
CREATE TABLE IF NOT EXISTS plate_candidates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    camera     TEXT NOT NULL,
    ts         REAL NOT NULL,
    last_seen  REAL NOT NULL,
    hits       INTEGER NOT NULL,            -- số khung thấy biển
    ocr_text   TEXT NOT NULL DEFAULT '',    -- OCR đọc được gì (có thể sai / thiếu)
    ocr_score  REAL NOT NULL DEFAULT 0,
    plate_img  TEXT NOT NULL,
    frame_img  TEXT NOT NULL,
    bbox       TEXT NOT NULL DEFAULT '',
    status     TEXT,                        -- NULL = chờ AI, unreadable, duplicate, error
    ai_plate   TEXT,
    ai_conf    REAL
);
CREATE INDEX IF NOT EXISTS idx_plate_candidates_ts ON plate_candidates(ts);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- phiên đăng nhập web: chỉ lưu sha256 của token trong cookie
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    username   TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_seen  REAL NOT NULL,
    ip         TEXT NOT NULL DEFAULT '',
    agent      TEXT NOT NULL DEFAULT ''
);
"""

# cột thêm sau lần triển khai đầu: (bảng, cột, kiểu)
MIGRATIONS = [
    ("cameras", "face_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("camera_status", "motion", "REAL"),
    ("cameras", "track_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("events", "note", "TEXT NOT NULL DEFAULT ''"),       # lý do không gửi Telegram
    ("visits", "embedding", "BLOB"),                       # vector ReID của lượt này
    ("visits", "maybe_visitor", "INTEGER"),                # người cũ có thể trùng (độ giống chưa đủ để tự gộp)
    ("visits", "maybe_sim", "REAL"),
    ("visits", "clip_emb", "BLOB"),                        # vector CLIP của ảnh (float16) để tìm theo mô tả
    ("visits", "colors", "BLOB"),                          # tỉ lệ màu theo vùng đầu / thân / dưới (search.py)
    # ý kiến của AI (ai.py) về gợi ý "có thể trùng": same / different / unsure / error
    ("visits", "ai_verdict", "TEXT"),
    ("visits", "ai_conf", "REAL"),
    ("visits", "ai_reason", "TEXT NOT NULL DEFAULT ''"),
    # AI đọc lại biển: ok (giống OCR) / fixed (đã tự sửa) / differs (khác, chờ người dùng) / unreadable / error
    ("sightings", "ai_status", "TEXT"),
    ("sightings", "ai_plate", "TEXT"),
    ("sightings", "ai_conf", "REAL"),
]


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    for table, col, decl in MIGRATIONS:
        if col not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    # cột thêm bằng migration nên index tạo sau; thiếu index này trang Qua lại mất ~30 s mỗi lần tải
    conn.execute("CREATE INDEX IF NOT EXISTS idx_visits_maybe ON visits(maybe_visitor)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_visits_noclip ON visits(ts) WHERE clip_emb IS NULL")
    conn.commit()
    conn.close()


def merge_visitors(conn, vid: int, into: int):
    """Gộp mọi lượt của visitor vid vào visitor into (hai visitor thực ra là một người). Không commit."""
    conn.execute("UPDATE visits SET visitor_id = ? WHERE visitor_id = ?", (into, vid))
    conn.execute("UPDATE visits SET maybe_visitor = ? WHERE maybe_visitor = ?", (into, vid))
    conn.execute("UPDATE visits SET maybe_visitor = NULL, maybe_sim = NULL WHERE maybe_visitor = visitor_id")
    conn.execute("UPDATE visitors SET first_seen = MIN(first_seen, (SELECT first_seen FROM visitors WHERE id = ?)),"
                 " last_seen = MAX(last_seen, (SELECT last_seen FROM visitors WHERE id = ?)) WHERE id = ?",
                 (vid, vid, into))
    conn.execute("DELETE FROM visitors WHERE id = ?", (vid,))


def get_settings(conn) -> dict:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


# các loại thông báo Telegram, bật riêng cho từng camera
NOTIFY_MODES = ("zone", "camera", "unknown", "watch")


def notify_matrix(settings: dict, cameras) -> dict:
    """camera -> tập loại thông báo được gửi.

    Trước khi có bảng theo camera, các chế độ bật chung cho mọi camera: dùng chúng làm mặc định.
    """
    try:
        saved = json.loads(settings["notify_matrix"])
    except (KeyError, ValueError):
        saved = None
    if isinstance(saved, dict):
        return {cam: set(saved.get(cam, [])) & set(NOTIFY_MODES) for cam in cameras}
    legacy_cams = set(filter(None, settings.get("camera_alert_cams", "").split(",")))
    matrix = {}
    for cam in cameras:
        modes = set()
        if settings.get("notify_zone", "1") == "1":
            modes.add("zone")
        if settings.get("notify_camera") == "1" and cam in legacy_cams:
            modes.add("camera")
        if settings.get("notify_unknown_face", "1") == "1":
            modes.add("unknown")
        if settings.get("notify_watch", "1") == "1":
            modes.add("watch")
        matrix[cam] = modes
    return matrix
