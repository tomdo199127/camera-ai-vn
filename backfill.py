"""
Bù video từ thẻ nhớ camera vào các khoảng server không ghi được (camera rớt mạng, go2rtc / recorder khởi động lại).

recorder.py ghi lại mỗi lần mất luồng (add_outage) vào REC_DIR/<camera>/backfill.json. Chỉ những khoảng đó mới được
bù: đoạn người dùng tự xoá trên web không bị lấy lại. Khi camera ghi bình thường trở lại, luồng nền ở đây:
  1. tìm chỗ hở thật trong khoảng mất luồng (theo độ dài thật của các file MP4),
  2. tạo luồng tạm trong go2rtc = nguồn xiaomi:// của camera + &playback=<giờ>: camera phát lại thẻ nhớ từ đầu file
     (~1 phút) chứa giờ đó rồi phát tiếp các file sau, đúng tốc độ thật; go2rtc báo giờ đầu file qua user_agent
     "playback=<unix>" và nối giờ các file liền nhau (camera đếm lại từ 0 mỗi file),
  3. ffmpeg chép luồng đó (tiếng xử lý như lúc ghi trực tiếp), rồi cắt đúng đoạn hở thành các file 1 phút
     <HH-MM-SS>.mp4 đặt vào thư mục ngày như file ghi thường.
Mỗi lần chỉ bù một camera, mỗi đoạn tối đa BF_CHUNK giây. Camera không phát lại được (đời cũ không hỗ trợ, thẻ trống)
thì sau vài lần hỏng nghỉ BF_PAUSE rồi mới thử lại.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from common import GO2RTC_API, REC_DIR, RTSP_HOST, go2rtc_sources

# Camera Xiaomi được tự bù từ thẻ nhớ (cần go2rtc bản sửa, xem go2rtc/src/README.md): "cam1,cam2". Để trống = tắt.
# Chỉ ghi camera có thẻ nhớ và phát lại được.
BF_CAMERAS = set(filter(None, os.environ.get("LPR_BACKFILL_CAMS", "").split(",")))
BF_PREFIX = "_probe_bf_"  # luồng tạm trong go2rtc (web bỏ qua mọi luồng _probe_*)
BF_STATE = "backfill.json"
BF_TMP = REC_DIR.parent / "backfill_tmp"  # cùng ổ với REC_DIR (chuyển file không phải chép), ngoài REC_DIR
BF_EVERY = 60              # giây giữa 2 lần tìm việc
BF_SETTLE = 90             # camera phải ghi lại ổn định chừng này giây mới bù (đỡ tranh kết nối lúc vừa có hình)
BF_LOOKBACK = 24 * 3600    # chỉ bù các lần mất luồng trong chừng này giây gần đây
BF_MIN_GAP = 10            # chỗ hở ngắn hơn thì bỏ qua
BF_CHUNK = 600             # mỗi lần phát lại tối đa chừng này giây video (dài hơn thì chia nhiều lần)
BF_IDLE = 8                # file phát lại không lớn thêm chừng này giây: camera ngừng gửi (hết thẻ, rớt mạng)
BF_START_WAIT = 45         # chừng này giây chưa bắt đầu phát lại: camera không phát được (đời cũ, thẻ trống)
BF_TRIES = 2               # số lần thử một khoảng trước khi bỏ
BF_PAUSE = 12 * 3600       # camera hỏng BF_TRIES khoảng liền nhau: nghỉ chừng này giây
# Quét định kỳ tìm chỗ thiếu mà recorder không báo (máy chủ tắt lâu, file hỏng...). Phát lại thẻ nhớ đôi khi làm đứt
# luồng đang ghi 15-20 s nên chỉ bù chỗ thiếu đủ dài, mỗi chỗ thử một lần và có giới hạn mỗi ngày.
BF_SKIP = "backfill-skip.txt"  # các khoảng không được bù: người dùng xoá, camera tắt ghi hình
BF_SCAN_EVERY = 300        # giây giữa 2 lần quét (chỉ đọc tên và phần đầu file)
BF_SCAN_MIN_GAP = 45       # chỗ thiếu ngắn hơn thì không phát lại
BF_SCAN_SETTLE = 300       # chỗ thiếu mới kết thúc chưa lâu: để recorder báo trước, tránh bù trùng
BF_SCAN_MAX = 6 * 3600     # chỗ thiếu dài hơn thì chỉ bù phần cuối chừng này giây (phát đúng tốc độ thật)
BF_SCAN_DAILY = 6          # số chỗ thiếu mỗi camera được đưa vào hàng bù trong 24 giờ
BF_SCAN_MIN_FREE = 20 * 2**30  # ổ còn ít hơn chừng này byte thì không quét (đang phải xoá bớt, đừng thêm video)
OSD_SHIFT = 1              # khung đầu phát lại sớm hơn giờ đầu file camera báo ~1 s (đo theo giờ in trên hình)
SEGMENT = 60

_lock = threading.Lock()  # backfill.json được recorder (luồng chính) và luồng bù cùng ghi


def log(cam: str, msg: str):
    print(f"{datetime.now():%H:%M:%S}  {cam:<8}  {msg}", flush=True)


def load_state(cam: str) -> dict:
    try:
        st = json.loads((REC_DIR / cam / BF_STATE).read_text())
    except (OSError, ValueError):
        st = {}
    st.setdefault("outages", [])
    return st


def save_state(cam: str, st: dict):
    path = REC_DIR / cam / BF_STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 3 * BF_LOOKBACK
    st["outages"] = [o for o in st["outages"] if o["b"] > cutoff][-300:]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, separators=(",", ":")))
    tmp.replace(path)


def add_outage(cam: str, a: float, b: float):
    """Recorder gọi khi camera ghi lại được sau một lần mất luồng: [a, b] là khoảng có thể thiếu video."""
    if cam not in BF_CAMERAS or b - a < BF_MIN_GAP:
        return
    with _lock:
        st = load_state(cam)
        st["outages"].append({"a": round(a), "b": round(b), "state": "pending", "tries": 0})
        save_state(cam, st)


def note_skip(cam: str, a: float, b: float):
    """Ghi nhớ khoảng không được bù từ thẻ nhớ (web gọi khi người dùng xoá video)."""
    try:
        path = REC_DIR / cam / BF_SKIP
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(f"{round(a)} {round(b)}\n")
    except OSError as e:
        log(cam, f"không ghi được {BF_SKIP}: {e}")


def load_skips(cam: str) -> list[tuple[float, float]]:
    try:
        lines = (REC_DIR / cam / BF_SKIP).read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            a, b = line.split()
            out.append((float(a), float(b)))
        except ValueError:
            pass
    return out


def subtract(a: float, b: float, ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Phần của [a, b] nằm ngoài các khoảng ranges."""
    out, t = [], a
    for s, e in sorted(ranges):
        if e <= t or s >= b:
            continue
        if s > t:
            out.append((t, s))
        t = max(t, e)
    if t < b:
        out.append((t, b))
    return out


def oldest_file(cam: str) -> float | None:
    """Giờ của file cũ nhất còn lưu: trước đó là phần recorder đã dọn, không phải chỗ thiếu."""
    try:
        days = sorted(d.name for d in os.scandir(REC_DIR / cam) if re.match(r"^\d{4}-\d{2}-\d{2}$", d.name))
    except OSError:
        return None
    for day in days:
        names = sorted(f.name for f in os.scandir(REC_DIR / cam / day) if re.match(r"^\d{2}-\d{2}-\d{2}\.mp4$", f.name))
        if names:
            return file_start(day, names[0])
    return None


def mp4_duration(path) -> float | None:
    """Độ dài file MP4 (giây) đọc từ hộp mvhd ở đầu file (file ghi xong có moov ngay sau ftyp); None nếu chưa xong."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return None
    i = head.find(b"mvhd")
    if head[4:8] != b"ftyp" or i < 0 or i + 32 > len(head):
        return None
    if head[i + 4] == 1:
        scale, dur = int.from_bytes(head[i + 24:i + 28], "big"), int.from_bytes(head[i + 28:i + 36], "big")
    else:
        scale, dur = int.from_bytes(head[i + 16:i + 20], "big"), int.from_bytes(head[i + 20:i + 24], "big")
    return dur / scale if scale else None


def file_start(day: str, name: str) -> float:
    return datetime.strptime(f"{day} {name[:8]}", "%Y-%m-%d %H-%M-%S").timestamp()


def covered(cam: str, a: float, b: float) -> list[tuple[float, float]]:
    """Các khoảng [đầu, cuối] đã có video của camera, chạm vào [a, b]."""
    out = []
    day = datetime.fromtimestamp(a - SEGMENT).date()
    while day <= datetime.fromtimestamp(b).date():
        d = REC_DIR / cam / f"{day:%Y-%m-%d}"
        if d.is_dir():
            for f in os.scandir(d):
                if not re.match(r"^\d{2}-\d{2}-\d{2}\.mp4$", f.name):
                    continue
                s = file_start(f"{day:%Y-%m-%d}", f.name)
                dur = mp4_duration(f.path)
                if dur is None:
                    # đang ghi: tính tới lúc sửa file lần cuối; ghi dở (ffmpeg bị ngắt, không phát được): chưa có gì
                    mtime = f.stat().st_mtime
                    dur = max(0.0, mtime - s) if time.time() - mtime < 30 else 0.0
                if s < b and s + dur > a:
                    out.append((s, s + dur))
        day += timedelta(days=1)
    return sorted(out)


def find_gaps(cam: str, a: float, b: float) -> list[tuple[float, float]]:
    gaps, t = [], a
    for s, e in covered(cam, a, b):
        if s - t >= BF_MIN_GAP:
            gaps.append((t, s))
        t = max(t, e)
    if b - t >= BF_MIN_GAP:
        gaps.append((t, b))
    return gaps


def go2rtc(method: str, **params) -> bytes:
    req = urllib.request.Request(f"{GO2RTC_API}/api/streams?{urllib.parse.urlencode(params)}", method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read()


def playback_start(name: str) -> int | None:
    """Giờ đầu file thẻ nhớ go2rtc đang phát cho luồng tạm: user_agent của nguồn có "playback=<unix>"
    (link nguồn cũng có playback= nhưng đó là giờ mình hỏi, không phải giờ đầu file)."""
    try:
        info = json.loads(go2rtc("GET", src=name))
    except (OSError, ValueError):
        return None
    for p in info.get("producers") or []:
        m = re.search(r"playback=(\d+)", p.get("user_agent") or "")
        if m:
            return int(m.group(1))
    return None


class Backfill:
    def __init__(self, recorders: dict, thumbs, audio_args):
        self.recorders = recorders   # {camera: Recorder} của recorder.py (chỉ đọc)
        self.thumbs = thumbs         # Thumbs của recorder.py: thư mục ngày có file mới thì làm ảnh lại
        self.audio_args = audio_args
        self.stopping = False
        self.busy = ""               # camera đang được bù (để recorder ghi vào status.json)
        self.scan_at = time.time() + BF_SCAN_EVERY  # lúc khởi động recorder chưa kịp nạp danh sách camera
        self.scan_last = time.time()
        self.seen: dict = {}         # {camera: Recorder} lần quét trước: khác đi là vừa tắt rồi bật ghi hình

    def start(self):
        for name in (go2rtc_sources() or {}):  # luồng tạm còn sót (recorder bị tắt giữa chừng)
            if name.startswith(BF_PREFIX):
                try:
                    go2rtc("DELETE", src=name)
                except OSError:
                    pass
        shutil.rmtree(BF_TMP, ignore_errors=True)
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while not self.stopping:
            try:
                self.run_once()
            except Exception as e:  # luồng nền không được chết vì một lỗi lạ
                log("bù", f"lỗi: {e}")
            for _ in range(BF_EVERY):
                if self.stopping:
                    return
                time.sleep(1)

    def scan(self, sources: dict, now: float):
        """Thêm vào hàng bù các chỗ thiếu video mà chưa ai báo. Không đụng tới luồng đang ghi."""
        if now < self.scan_at:
            return
        self.scan_at = now + BF_SCAN_EVERY
        last, self.scan_last = self.scan_last, now
        if shutil.disk_usage(REC_DIR).free < BF_SCAN_MIN_FREE:
            return
        for cam in sorted(BF_CAMERAS):
            r = self.recorders.get(cam)
            with _lock:
                st = load_state(cam)
                if r is None:  # tắt ghi hình: đoạn không ghi trong lúc tắt là cố ý
                    self.seen.pop(cam, None)
                    if "off_since" not in st:
                        st["off_since"] = round(last - SEGMENT)
                        save_state(cam, st)
                    continue
                off = st.pop("off_since", None)
                if self.seen.get(cam) not in (None, r):  # tắt rồi bật lại giữa 2 lần quét
                    off = round(last - SEGMENT)
                self.seen[cam] = r
                if off is not None:
                    note_skip(cam, off, r.started + 5)
                new = "scan_from" not in st  # chỉ tìm từ lúc có quét: đoạn cũ thiếu có thể do người dùng xoá trước đó
                st.setdefault("scan_from", round(now))
                if off is not None or new:
                    save_state(cam, st)
            if not r.running() or now - r.started < BF_SETTLE or \
                    not any(s.startswith("xiaomi:") for s in sources.get(cam, [])):
                continue
            a = max(st["scan_from"], now - BF_LOOKBACK, oldest_file(cam) or now)
            b = now - BF_SCAN_SETTLE
            if b - a < BF_SCAN_MIN_GAP:
                continue
            known = [(o["a"], o["b"]) for o in st["outages"]] + load_skips(cam)
            found = [p for ga, gb in find_gaps(cam, a, b) for p in subtract(ga, gb, known)
                     if p[1] - p[0] >= BF_SCAN_MIN_GAP]
            if not found:
                continue
            with _lock:
                st = load_state(cam)
                count = sum(1 for o in st["outages"] if o.get("scan") and o["b"] > now - BF_LOOKBACK)
                for pa, pb in found:
                    if count >= BF_SCAN_DAILY:
                        log(cam, f"quét thấy thiếu video nhưng đã bù đủ {BF_SCAN_DAILY} chỗ trong 24 giờ, để lần sau")
                        break
                    if pb - pa > BF_SCAN_MAX:
                        note_skip(cam, pa, pb - BF_SCAN_MAX)
                        pa = pb - BF_SCAN_MAX
                    st["outages"].append({"a": round(pa), "b": round(pb), "state": "pending", "tries": 0, "scan": 1})
                    count += 1
                    log(cam, f"quét thấy thiếu video {datetime.fromtimestamp(pa):%d/%m %H:%M:%S}–"
                             f"{datetime.fromtimestamp(pb):%H:%M:%S}, đưa vào hàng bù")
                save_state(cam, st)

    def run_once(self):
        sources = go2rtc_sources() or {}
        now = time.time()
        self.scan(sources, now)
        for cam, r in list(self.recorders.items()):
            src = next((s for s in sources.get(cam, []) if s.startswith("xiaomi:")), None)
            if cam not in BF_CAMERAS or not src or not r.running() or now - r.started < BF_SETTLE:
                continue
            with _lock:
                st = load_state(cam)
            if st.get("pause_until", 0) > now:
                continue
            todo, expired = None, False
            for o in st["outages"]:
                if o["state"] != "pending":
                    continue
                if o["b"] < now - BF_LOOKBACK:
                    o["state"], expired = "expired", True
                elif o["b"] < now - 30 and todo is None:
                    todo = o
            if expired:
                with _lock:
                    self.save_outages(cam, st)
            if todo is None:
                continue
            self.busy = cam
            try:
                result = self.fill(cam, src, r, todo["a"], todo["b"])
            finally:
                self.busy = ""
            # lấy được một phần rồi hỏng giữa chừng (camera thỉnh thoảng từ chối một lần phát lại): thử lại phần còn
            left = result is not None and bool(find_gaps(cam, todo["a"], todo["b"]))
            # lần này có lấy thêm được video thì thẻ nhớ có đoạn đó, hỏng giữa chừng chỉ là camera từ chối một lần
            # phát lại: không tính là một lần thử, nếu không chỗ thiếu dài (máy chủ tắt vài giờ) bị bỏ dở sau lần hỏng
            # đầu tiên. Mỗi lần như vậy chỗ thiếu ngắn lại nên không lặp mãi.
            progress = bool(result)
            with _lock:
                st2 = load_state(cam)
                for o in st2["outages"]:
                    if o["a"] == todo["a"] and o["b"] == todo["b"]:
                        o["tries"] = o.get("tries", 0) + (result is None or (left and not progress))
                        limit = 1 if o.get("scan") else BF_TRIES  # chỗ quét thấy có thể không có trên thẻ: không thử lại
                        if result is not None:
                            o["got"] = o.get("got", 0) + result
                            st2["fails"] = 0
                            if not left or o["tries"] >= limit:
                                o["state"] = "done"
                        elif o["tries"] >= limit:
                            o["state"] = "failed"
                            st2["fails"] = st2.get("fails", 0) + (not o.get("scan"))
                            if st2["fails"] >= BF_TRIES:
                                st2["pause_until"] = time.time() + BF_PAUSE
                                st2["fails"] = 0
                                log(cam, f"không lấy được video từ thẻ nhớ, nghỉ {BF_PAUSE // 3600} giờ rồi thử lại")
                save_state(cam, st2)
            return  # mỗi lần một khoảng: camera khác không phải chờ lâu

    @staticmethod
    def save_outages(cam: str, st: dict):
        cur = load_state(cam)  # recorder có thể vừa thêm khoảng mới
        states = {(o["a"], o["b"]): o["state"] for o in st["outages"]}
        for o in cur["outages"]:
            o["state"] = states.get((o["a"], o["b"]), o["state"])
        save_state(cam, cur)

    def fill(self, cam: str, src: str, r, a: float, b: float) -> int | None:
        """Bù các chỗ hở trong [a, b]: số giây video đã thêm; None nếu camera không phát lại được."""
        gaps = find_gaps(cam, a, b)
        if not gaps:
            return 0
        got, ok = 0, False
        for ga, gb in gaps:
            t = ga
            while t < gb - BF_MIN_GAP / 2 and not self.stopping:
                end = min(gb, t + BF_CHUNK)
                res = self.fetch(cam, src, r, t, end)
                if res is None:
                    return got if ok else None
                ok = True
                n, reached = res
                got += n
                # lần sau phát tiếp từ chỗ vừa lấy tới (file kế tiếp trên thẻ); không tiến được thì bỏ qua đoạn này
                t = reached + 1 if reached > t + 2 else end  # +1 s: chắc chắn rơi vào file sau, không lặp file vừa phát
        if got:
            log(cam, f"đã bù {got // 60} phút {got % 60} giây từ thẻ nhớ "
                     f"({datetime.fromtimestamp(gaps[0][0]):%H:%M:%S}–{datetime.fromtimestamp(gaps[-1][1]):%H:%M:%S})")
        return got

    def fetch(self, cam: str, src: str, r, a: float, b: float) -> tuple[int, float] | None:
        """Phát lại thẻ nhớ từ giờ a, cắt phần trong [a, b] thành file 1 phút trong thư mục ngày.
        (số giây thêm được, giờ cuối của phần đã lấy); None nếu camera không phát lại được."""
        name = BF_PREFIX + cam
        work = BF_TMP / cam
        shutil.rmtree(work, ignore_errors=True)
        (work / "out").mkdir(parents=True)
        raw = work / "raw.mkv"
        try:
            go2rtc("PUT", name=name, src=f"{src}{'&' if '?' in src else '?'}playback={int(a)}&speed=1")
            # bước 1: chép luồng phát lại (từ đầu file thẻ nhớ chứa a, tức sớm hơn a tối đa ~1 phút);
            # tiếng xử lý như lúc ghi trực tiếp vì camera phát lại đúng tốc độ thật
            audio = self.audio_args(r.info) if r.audio else []
            cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
                   "-stimeout", "15000000", "-i", f"rtsp://{RTSP_HOST}/{name}", "-t", str(int(b - a) + 70),
                   "-map", "0:v:0", "-c:v", "copy", *audio, str(raw)]
            errf = open(work / "err.txt", "w+")
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=errf, preexec_fn=lambda: os.nice(5))
            start = None
            began = time.time()
            deadline = began + b - a + 200
            size, grown = -1, began
            while proc.poll() is None:
                if start is None:
                    start = playback_start(name)
                    if start is not None:  # phát đúng tốc độ thật: đủ tới b sau chừng này giây thì dừng
                        deadline = min(deadline, time.time() + b - (start - OSD_SHIFT) + 6)
                cur = raw.stat().st_size if raw.exists() else 0
                if cur != size:
                    size, grown = cur, time.time()
                if self.stopping or time.time() > deadline or (cur and time.time() - grown > BF_IDLE) or \
                        (start is None and time.time() - began > BF_START_WAIT):
                    proc.terminate()  # SIGTERM: ffmpeg ghi nốt cuối file
                    try:
                        proc.wait(15)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    break
                time.sleep(1)
            errf.seek(0)
            err = errf.read().strip()
            errf.close()
        finally:
            try:
                go2rtc("DELETE", src=name)
            except OSError:
                pass
        small = not raw.exists() or raw.stat().st_size < 10000
        if start is not None and (start - OSD_SHIFT >= b - BF_MIN_GAP or (small and start - OSD_SHIFT > a)):
            # camera trả về file sau khoảng cần bù: thẻ nhớ không có đoạn này (camera cũng mất điện / khởi động lại),
            # không phải lỗi phát lại nên không tính vào số lần hỏng, nếu không cả camera bị nghỉ bù BF_PAUSE
            log(cam, f"thẻ nhớ không có đoạn {datetime.fromtimestamp(a):%H:%M:%S}–{datetime.fromtimestamp(b):%H:%M:%S}"
                     f" (file kế tiếp bắt đầu {datetime.fromtimestamp(start):%H:%M:%S})")
            shutil.rmtree(work, ignore_errors=True)
            return 0, b
        if start is None or small:
            log(cam, f"thẻ nhớ không phát lại được lúc {datetime.fromtimestamp(a):%H:%M:%S}"
                     f"{': ' + err.splitlines()[-1][:120] if err else ''}")
            shutil.rmtree(work, ignore_errors=True)
            return None
        # bước 2: bỏ phần trước a (đã có video), cắt [a, b] thành file 1 phút
        t0 = start - OSD_SHIFT  # giờ của khung đầu tiên trong raw.mkv
        off = max(0.0, a - t0)
        subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{off:.2f}",
                        "-i", str(raw), "-t", f"{b - a + 2:.2f}", "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
                        *(["-tag:v", "hvc1"] if r.codec == "hevc" else []),
                        "-f", "segment", "-segment_time", str(SEGMENT), "-reset_timestamps", "1",
                        "-segment_list", str(work / "list.csv"), "-segment_list_type", "csv",
                        "-segment_format", "mp4", "-segment_format_options", "movflags=+faststart",
                        str(work / "out" / "%03d.mp4")], capture_output=True, timeout=300)
        got, reached = 0, a
        try:
            rows = (work / "list.csv").read_text().splitlines()
        except OSError:
            rows = []
        for row in rows:
            parts = row.split(",")
            if len(parts) < 3:
                continue
            piece, s, e = work / "out" / Path(parts[0]).name, float(parts[1]), float(parts[2])
            ws = t0 + off + s
            if ws < b:
                reached = max(reached, ws + e - s)
            if e - s < 2 or ws >= b or not piece.exists():
                continue
            day = REC_DIR / cam / f"{datetime.fromtimestamp(ws):%Y-%m-%d}"
            day.mkdir(parents=True, exist_ok=True)
            for shift in range(3):  # trùng tên file ghi thật thì lùi 1 giây, không ghi đè file xem được
                dest = day / f"{datetime.fromtimestamp(ws + shift):%H-%M-%S}.mp4"
                if not dest.exists() or (mp4_duration(dest) is None and time.time() - dest.stat().st_mtime > 60):
                    break  # file ghi dở (không có moov, không phát được) thì thay bằng đoạn từ thẻ nhớ
            else:
                continue
            os.replace(piece, dest)
            # giờ sửa file = cuối đoạn: recorder dùng giờ sửa file mới nhất để biết camera còn ghi không
            os.utime(dest, (ws + e - s, ws + e - s))
            self.thumbs.redo.add(str(dest))
            self.thumbs.done_days.discard(str(day))
            got += round(e - s)
        shutil.rmtree(work, ignore_errors=True)
        return got, reached
