"""
Ghi hình liên tục cho các camera bật "Ghi hình".

Mỗi camera có một tiến trình ffmpeg đọc luồng RTSP từ go2rtc và chép nguyên luồng (không nén lại,
gần như không tốn CPU) thành các file MP4 dài 1 phút: REC_DIR/<camera>/<YYYY-MM-DD>/<HH-MM-SS>.mp4.
Tên file là giờ bắt đầu đoạn. Khi tổng dung lượng vượt giới hạn (cài đặt rec_max_gb) thì xoá file cũ nhất.
Trạng thái (dung lượng, đang ghi hay lỗi) được ghi ra REC_STATUS để trang web đọc.
"""

import json
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

from backfill import Backfill, add_outage
from common import GO2RTC_API, REC_DIR, REC_STATUS, RTSP_HOST, connect, get_settings, init_db, rec_limit_gb

CHECK_EVERY = 10          # giây giữa 2 lần kiểm tra camera
CLEANUP_EVERY = 60        # giây giữa 2 lần tính dung lượng / xoá file cũ
SEGMENT = 60              # độ dài mỗi file (giây)
STALL = 45                # file đang ghi không lớn thêm trong chừng này giây -> khởi động lại ffmpeg
AUDIO_BAD = 2             # chừng này file xong liên tiếp hỏng tiếng -> khởi động lại ffmpeg
AUDIO_DRIFT = 5           # tiếng kết thúc lệch hình quá chừng này giây là hỏng (lệch tiếng, thiếu tiếng)
AUDIO_RETRY = 600         # giữa 2 lần ghi lại vì hỏng tiếng; ghi lại mà vẫn hỏng thì chờ gấp đôi...
AUDIO_RETRY_MAX = 3600    # ... tới mức này, để camera lỗi tiếng lâu dài không bị mất hình liên tục
RECONNECT_POLL = 3        # giây giữa hai lần hỏi go2rtc xem kết nối tới camera có đổi không
MIN_FREE_GB = 5           # luôn chừa ít nhất chừng này GB trống trên ổ, kể cả khi chưa tới giới hạn
GB = 1e9
THUMB_W, THUMB_H = 320, 180   # ảnh thu nhỏ mỗi phút cho lưới xem lại
THUMB_BUDGET = 4          # giây tối đa làm ảnh thu nhỏ mỗi vòng (làm dần các file cũ chưa có ảnh)
MOTION_FILE = "motion.json"  # trong mỗi thư mục ngày: {"HH-MM-SS": % khung hình thay đổi, null = file hỏng}
OUTAGE_MAX = 6 * 3600     # khoảng mất luồng dài hơn thì chỉ bù từ thẻ nhớ phần cuối chừng này giây
RESTART_MAX = 3600        # recorder vừa chạy lại: chỗ trống từ file cuối tới giờ (ngắn hơn chừng này) cũng là mất luồng
# Camera hay rớt sóng: theo dõi mỗi 0,5 s, thử lại sau 3 s và bỏ bước đọc thử 8 s (dùng lại kết quả lần trước).
# Hết FAST_TRIES lần hỏng liên tiếp (camera tắt hẳn) thì quay về cách chờ thường.
FAST_RETRY_CAMS = set(filter(None, os.environ.get("LPR_FAST_RETRY_CAMS", "").split(",")))
FAST_RETRY = 3
FAST_TRIES = 10

# Biểu thức asetpts sửa giờ tiếng đếm theo số mẫu (Xiaomi qua go2rtc). Biến ffmpeg:
#   2 = độ chênh giờ nhận gói - giờ theo số mẫu; mất tiếng bao nhiêu thì độ chênh tăng bấy nhiêu
#   0 = mức đáy của độ chênh: theo xuống ngay (gói đến trễ chỉ làm độ chênh tăng tạm), theo lên chậm 20 ms/s
#       (camera gửi thiếu mẫu + mất gói lẻ tẻ có lúc tới ~8 ms/s; theo lên chậm hơn mức đó thì tiếng đi trước hình
#       ngày càng xa, ~5 s sau nửa giờ), riêng khi gói đến sau khoảng ngắt > 0.5 s (rớt mạng) thì nhảy lên ngay
#   1 = mức đáy lúc 15 s đầu (sau đợt gói dồn lúc ffmpeg mới mở luồng); 3, 4 = giờ nhận gói trước / khoảng ngắt
# Giờ tiếng = giờ theo số mẫu + (mức đáy hiện tại - mức đáy ban đầu).
# {follow} = tốc độ theo lên (giây / giây). Ghi hình: 0.02. Xem trực tiếp: LIVE_AUDIO_FOLLOW, vì camera Wi-Fi yếu
# hụt tới ~22 ms/s: theo lên chậm hơn thì giờ tiếng tụt sau giờ thật ~1,3 s mỗi phút, ffmpeg giữ hình lại
# chờ tiếng cho khớp nên hình trực tiếp trễ dần (tới chục giây) đến khi có lần rớt mạng > 0.5 s mới đặt lại.
AUDIO_RETIME = (
    "st(2,(RTCTIME-RTCSTART)/1000000-(PTS-STARTPTS)*TB);"
    "st(4,(RTCTIME-RTCSTART)/1000000-ld(3));st(3,(RTCTIME-RTCSTART)/1000000);"
    "st(0,if(eq(N,0),ld(2),if(gt(ld(4),0.5),max(ld(2),ld(0)),min(ld(2),ld(0)+{follow}*ld(4)))));"
    "if(lt((PTS-STARTPTS)*TB,15),st(1,ld(0)));"
    "PTS+if(gte((PTS-STARTPTS)*TB,15),ld(0)-ld(1),0)/TB"
)


def producer_id(cam: str):
    """Số hiệu kết nối go2rtc -> camera của luồng; đổi tức là go2rtc vừa nối lại. None nếu chưa có / không đọc được."""
    try:
        with urllib.request.urlopen(f"{GO2RTC_API}/api/streams?src={urllib.parse.quote(cam)}", timeout=2) as r:
            return next((p["id"] for p in json.load(r).get("producers", []) if p.get("id")), None)
    except (OSError, ValueError):
        return None


def probe(url: str) -> dict:
    """Đọc thử vài giây luồng: mã hoá hình / tiếng, tần số tiếng, và tỉ lệ thời gian tiếng / hình.
    Camera Xiaomi (qua go2rtc) gửi tiếng 16 kHz nhưng ghi là 8 kHz, nên mỗi giây tiếng bị tính thành 2 giây;
    so độ dài tiếng với hình đọc cùng lúc để biết tần số thật. {} nếu không đọc được."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-stimeout", "10000000", "-read_intervals", "%+8",
             "-show_entries", "stream=index,codec_type,codec_name,sample_rate:packet=stream_index,pts_time",
             "-of", "json", url],
            capture_output=True, text=True, timeout=40).stdout
        data = json.loads(out or "{}")
    except (subprocess.TimeoutExpired, ValueError):
        return {}
    found, spans = {}, {}
    for s in data.get("streams", []):
        kind = s.get("codec_type")
        if kind in ("video", "audio") and kind not in found:
            found[kind] = s.get("codec_name")
            if kind == "audio":
                found["audio_rate"] = int(s.get("sample_rate") or 0)
            ts = [float(p["pts_time"]) for p in data.get("packets", [])
                  if p.get("stream_index") == s.get("index") and p.get("pts_time") not in (None, "N/A")]
            spans[kind] = ts[-1] - ts[0] if len(ts) > 1 else 0
    if spans.get("video", 0) >= 2 and spans.get("audio"):
        found["audio_ratio"] = spans["audio"] / spans["video"]
    return found


LIVE_AUDIO_FOLLOW = 0.06


def audio_args(info: dict, follow: float = 0.02) -> list[str]:
    """Tham số ffmpeg cho tiếng (dùng cả cho ghi hình lẫn xem trực tiếp trên web);
    [] = bỏ tiếng (không có tiếng, hoặc tiếng lệch thời gian không sửa được). follow: xem AUDIO_RETIME."""
    audio, ratio, rate = info.get("audio"), info.get("audio_ratio"), info.get("audio_rate") or 8000
    if not audio:
        return []
    if ratio is None or 0.85 <= ratio <= 1.15:
        return ["-map", "0:a:0"] + (["-c:a", "copy"] if audio == "aac" else
                                   ["-c:a", "aac", "-ar", "16000", "-ac", "1", "-b:a", "32k"])
    if 1.7 <= ratio <= 2.3 and audio != "aac":
        # tần số thật gấp đôi tần số khai báo: đặt lại tần số (không đổi dữ liệu) để tiếng đúng tốc độ và khớp hình.
        # Giờ của tiếng ở luồng này là đếm số mẫu nên camera rớt mạng thì tiếng bị nối liền, đi sớm hơn hình
        # (hình theo giờ camera nên có khoảng hở) => asetpts bù theo giờ nhận gói, aresample chèn khoảng lặng.
        # flags=res bật sẵn bộ lấy mẫu: không có thì lần đầu tiếng lệch 0.03-0.3 s (min_comp..min_hard_comp) aresample
        # tự khởi tạo lại và first_pts=0 đưa giờ tiếng về 0, tiếng dồn cục rồi trễ hẳn so với hình đến khi ffmpeg dừng.
        real = rate * 2
        return ["-map", "0:a:0", "-af", f"asetrate={real},asetpts='{AUDIO_RETIME.format(follow=follow)}',"
                f"aresample={real}:async=400:min_comp=0.03:min_hard_comp=0.3:first_pts=0:flags=res",
                "-c:a", "aac", "-ac", "1", "-b:a", "32k"]
    return []


def audio_healthy(path) -> bool:
    """Tiếng của file đã ghi xong có phát được không. Rớt mạng mà ffmpeg không dừng thì giờ của tiếng có thể hỏng:
    mất hẳn track, cả phút tiếng dồn vào vài phần giây, hoặc tiếng lệch hẳn khỏi hình."""
    def packets(stream: str) -> list[float]:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries", "packet=pts_time",
                              "-of", "csv=p=0", str(path)], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=30).stdout.split()
        return [float(t.strip(",")) for t in out if t.strip(",") not in ("", "N/A")]
    try:
        video, pts = packets("v"), packets("a")
    except (subprocess.TimeoutExpired, ValueError):
        return True  # không đọc được file thì không kết luận gì về tiếng
    if not video or video[-1] - video[0] < 20:
        return True  # file quá ngắn (vừa ghi lại) chưa đủ để kết luận
    return len(pts) > 1 and pts[0] > -5 and abs(pts[-1] - video[-1]) <= AUDIO_DRIFT


class Recorder:
    def __init__(self, cam: str):
        self.cam = cam
        self.dir = REC_DIR / cam
        self.proc: subprocess.Popen | None = None
        self.started = 0.0
        self.retry_at = 0.0
        self.fails = 0
        self.error = ""
        self.codec = ""
        self.info: dict = {}  # kết quả probe() lần ghi gần nhất (web dùng lại khi xem trực tiếp)
        self.audio = True     # ghi kèm tiếng (tuỳ chọn của camera)
        self.starting = False  # đang đọc thử luồng để bắt đầu ghi (chạy riêng, có khi mất ~40 s)
        self.removed = False   # đã tắt ghi hình camera này: lần đọc thử đang chạy dở thì không ghi nữa
        self.down_since: float | None = None  # mất luồng từ lúc này (giờ file cuối được ghi), để bù từ thẻ nhớ
        self.first_start = True
        self.has_audio = False     # lần ghi này có ghi tiếng
        self.audio_seen = ""       # file đã xét tiếng gần nhất
        self.audio_bad = 0         # số file hỏng tiếng liên tiếp
        self.audio_retry_at = 0.0  # chưa được ghi lại vì hỏng tiếng trước lúc này
        self.audio_wait = AUDIO_RETRY
        self.producer = None       # kết nối go2rtc -> camera lúc đang ghi (chỉ theo dõi khi tiếng không qua asetpts)
        self.producer_at = 0.0
        self.good_ratio = None     # tỉ lệ tiếng / hình đọc được gần nhất mà dùng được (nhớ qua lần chạy lại recorder)
        try:
            self.good_ratio = json.loads(REC_STATUS.read_text())["cameras"][cam].get("good_ratio")
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ensure_day_dirs(self):
        # ffmpeg không tự tạo thư mục ngày nên tạo sẵn cả hôm nay và ngày mai (qua nửa đêm vẫn ghi tiếp)
        today = datetime.now()
        for d in (today, today + timedelta(days=1)):
            (self.dir / f"{d:%Y-%m-%d}").mkdir(parents=True, exist_ok=True)

    def start(self):
        url = f"rtsp://{RTSP_HOST}/{self.cam}"
        if self.fast() and self.fails and self.info.get("video"):
            info = self.info  # vừa rớt sóng: mã hoá / tiếng không đổi, khỏi mất 8 s đọc thử
        else:
            info = self.probe_audio(url)
        if self.removed:
            return
        if not info.get("video"):
            self.fail("Không đọc được luồng từ camera (camera offline?)")
            return
        self.codec = info["video"]
        self.info = info
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-rtsp_transport", "tcp", "-stimeout", "10000000", "-i", url, "-map", "0:v:0", "-c:v", "copy"]
        if self.codec == "hevc":
            cmd += ["-tag:v", "hvc1"]  # Safari / iPhone chỉ phát H.265 trong MP4 khi có tag hvc1
        # camera Wi-Fi yếu hụt nhiều mẫu tiếng: theo giờ tiếng nhanh như xem trực tiếp, không thì tiếng tụt dần sau hình
        audio = audio_args(info, LIVE_AUDIO_FOLLOW if self.cam in FAST_RETRY_CAMS else 0.02) if self.audio else []
        if self.audio and info.get("audio") and not audio:
            print(f"{datetime.now():%H:%M:%S}  {self.cam:<8}  tiếng lệch thời gian "
                  f"(x{info.get('audio_ratio') or 0:.2f}), chỉ ghi hình", flush=True)
        cmd += audio
        self.has_audio = bool(audio)
        cmd += ["-f", "segment", "-segment_time", str(SEGMENT), "-segment_atclocktime", "1",
                "-reset_timestamps", "1", "-strftime", "1", "-segment_format", "mp4",
                # moov đặt đầu file: trình duyệt phát / tua được ngay khi mới tải một phần
                "-segment_format_options", "movflags=+faststart", "%Y-%m-%d/%H-%M-%S.mp4"]
        self.ensure_day_dirs()
        self.proc = subprocess.Popen(cmd, cwd=self.dir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        self.started = time.time()
        self.audio_bad = 0
        self.producer = None
        if self.first_start and self.down_since is None:
            last = self.newest_mtime()  # recorder / máy chủ vừa khởi động lại
            if 0 < self.started - last < RESTART_MAX:
                self.down_since = last
        self.first_start = False
        if self.down_since is not None:
            # từ đầu file cuối: file đang ghi lúc mất luồng có thể hỏng (ffmpeg bị ngắt, không có moov)
            add_outage(self.cam, max(self.down_since, self.started - OUTAGE_MAX) - SEGMENT - 5, self.started + 5)
            self.down_since = None
        self.error = ""
        print(f"{datetime.now():%H:%M:%S}  {self.cam:<8}  bắt đầu ghi ({self.codec}"
              f"{', có tiếng' if '-c:a' in cmd else ''})", flush=True)

    def start_bg(self):
        try:
            self.start()
        finally:
            self.starting = False

    def fail(self, msg: str):
        if self.down_since is None:
            self.down_since = self.newest_mtime() or time.time()
        self.fails += 1
        self.error = msg
        # chờ ngắn (tối đa 30 s): camera Xiaomi hay rớt rồi có hình lại sau vài chục giây, chờ lâu thì mất thêm
        # cả mấy phút video. Thử lại chỉ tốn một lần đọc thử luồng qua go2rtc.
        self.retry_at = time.time() + (FAST_RETRY if self.fast() else min(30, 10 * 2 ** min(self.fails, 2)))
        print(f"{datetime.now():%H:%M:%S}  {self.cam:<8}  {msg}, thử lại sau {self.retry_at - time.time():.0f}s",
              flush=True)

    def probe_audio(self, url: str) -> dict:
        """probe(); camera Wi-Fi yếu có lúc 8 s đọc thử tiếng về không đều nên tỉ lệ tiếng / hình sai (x1.3-1.5
        thay vì ~2) và bị bỏ tiếng: đọc lại tối đa 2 lần, vẫn sai thì dùng tỉ lệ tốt lần trước."""
        info = probe(url)
        bad = lambda i: self.audio and i.get("video") and i.get("audio") and not audio_args(i)
        for _ in range(2):
            if not bad(info):
                break
            print(f"{datetime.now():%H:%M:%S}  {self.cam:<8}  tiếng x{info.get('audio_ratio') or 0:.2f}, đọc thử lại",
                  flush=True)
            again = probe(url)
            if again.get("video"):
                info = again
        if bad(info) and self.good_ratio:
            print(f"{datetime.now():%H:%M:%S}  {self.cam:<8}  tiếng vẫn x{info.get('audio_ratio') or 0:.2f}, "
                  f"dùng tỉ lệ lần trước x{self.good_ratio:.2f}", flush=True)
            info = {**info, "audio_ratio": self.good_ratio}
        if info.get("audio") and audio_args(info):
            self.good_ratio = info.get("audio_ratio")
        return info

    def fast(self) -> bool:
        return self.cam in FAST_RETRY_CAMS and self.fails <= FAST_TRIES

    def quick_check(self):
        """Phần mất luồng / thử lại của check(), gọi mỗi 0,5 s cho camera hay rớt sóng:
        check() chỉ chạy mỗi ~10 s nên riêng việc đợi tới lượt đã mất thêm tới 10 s mỗi lần rớt."""
        if self.removed:
            return
        if self.proc is not None and not self.running():
            self.proc = None
            self.fail("ffmpeg dừng (mất luồng)")
        if self.proc is None and not self.starting and time.time() >= self.retry_at:
            self.starting = True
            threading.Thread(target=self.start_bg, daemon=True).start()

    def stop(self):
        if self.running():
            self.proc.terminate()  # SIGTERM: ffmpeg ghi nốt phần cuối của file đang ghi
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def newest_mtime(self) -> float:
        newest = 0.0
        if not self.dir.is_dir():
            return newest
        for day in sorted((d for d in self.dir.iterdir() if d.is_dir()), reverse=True)[:2]:
            for f in os.scandir(day):
                if f.name.endswith(".mp4"):
                    newest = max(newest, f.stat().st_mtime)
        return newest

    def check(self):
        now = time.time()
        if self.proc is not None and not self.running():
            self.proc = None
            self.fail("ffmpeg dừng (mất luồng)")
        if self.starting:
            return
        if self.proc is None:
            if now >= self.retry_at:
                # đọc thử luồng chạy riêng: camera rớt mạng (đọc thử lâu) không làm chậm các camera khác
                self.starting = True
                threading.Thread(target=self.start_bg, daemon=True).start()
            return
        self.ensure_day_dirs()
        if now - self.started > STALL + 15 and now - self.newest_mtime() > STALL:
            self.stop()
            self.fail(f"không có hình mới quá {STALL}s")
        elif now - self.started > 3 * SEGMENT:
            self.fails = 0
        if self.has_audio:
            self.check_audio(now)
        if self.proc is not None and self.has_audio and "-af" not in audio_args(self.info):
            self.check_reconnect(now)

    def check_reconnect(self, now: float):
        """Tiếng giữ nguyên giờ của camera (Dahua): go2rtc nối lại thì giờ tiếng nhảy lung tung mà ffmpeg không biết,
        tiếng dồn cục hoặc lệch => ghi lại ngay. Xiaomi không cần: tiếng đã qua asetpts theo giờ nhận gói."""
        if now < self.producer_at:
            return
        self.producer_at = now + RECONNECT_POLL
        pid = producer_id(self.cam)
        if pid is None:
            return
        if self.producer is None:
            self.producer = pid
        elif pid != self.producer:
            print(f"{datetime.now():%H:%M:%S}  {self.cam:<8}  go2rtc nối lại camera, ghi lại để giữ tiếng", flush=True)
            self.stop()
            self.retry_at = 0

    def check_audio(self, now: float):
        day = self.dir / f"{datetime.now():%Y-%m-%d}"
        try:
            names = sorted(f.name for f in os.scandir(day) if f.name.endswith(".mp4"))
        except OSError:
            return
        done = next((n for n in reversed(names[:-1]) if mp4_done(day / n)), None)  # file cuối còn đang ghi
        path = day / done if done else None
        if not path or done == self.audio_seen or path.stat().st_mtime < self.started:
            return
        self.audio_seen = done
        if audio_healthy(path):
            self.audio_bad = 0
            self.audio_wait = AUDIO_RETRY
            return
        self.audio_bad += 1
        if self.audio_bad >= AUDIO_BAD and now >= self.audio_retry_at:
            print(f"{datetime.now():%H:%M:%S}  {self.cam:<8}  {self.audio_bad} file liền hỏng tiếng, ghi lại"
                  f" (lần sau chờ tối thiểu {self.audio_wait}s)", flush=True)
            self.audio_retry_at = now + self.audio_wait
            self.audio_wait = min(AUDIO_RETRY_MAX, self.audio_wait * 2)
            self.stop()
            self.retry_at = 0


def mp4_done(path) -> bool:
    """File đã ghi xong: moov nằm ngay sau ftyp (faststart). File đang ghi hoặc bị ngắt giữa chừng chưa có moov."""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        return False
    n = int.from_bytes(head[:4], "big")
    return head[4:8] == b"ftyp" and head[n + 4:n + 8] == b"moov"


def make_thumb(path: Path) -> float | None:
    """Ảnh thu nhỏ HH-MM-SS.jpg cạnh file video, trả về mức thay đổi (% khung hình, 0–100); None nếu không đọc được.
    Chỉ giải mã khung khoá (~1 khung / 3 s) nên nhẹ. Ảnh là khung khoá thay đổi nhiều nhất so với khung trước,
    mức thay đổi là tỉ lệ điểm ảnh đổi rõ (> 25/255) lớn nhất giữa 2 khung khoá liền nhau trong phút."""
    size = THUMB_W * THUMB_H * 3
    try:
        raw = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-skip_frame", "nokey", "-i", str(path), "-map", "0:v:0",
             "-vsync", "0", "-vf", f"scale={THUMB_W}:{THUMB_H}", "-pix_fmt", "bgr24", "-f", "rawvideo", "-"],
            capture_output=True, timeout=60, preexec_fn=lambda: os.nice(10)).stdout
    except subprocess.TimeoutExpired:
        return None
    n = len(raw) // size
    if not n:
        return None
    frames = np.frombuffer(raw[:n * size], np.uint8).reshape(n, THUMB_H, THUMB_W, 3)
    gray = frames[:, ::4, ::4].mean(axis=3)
    change = (np.abs(np.diff(gray, axis=0)) > 25).mean(axis=(1, 2)) * 100 if n > 1 else np.zeros(1)
    best = int(change.argmax()) + 1 if n > 1 else 0
    ok, jpg = cv2.imencode(".jpg", frames[best], [cv2.IMWRITE_JPEG_QUALITY, 70])
    if not ok:
        return None
    tmp = path.with_suffix(".jpg.tmp")
    tmp.write_bytes(jpg.tobytes())
    tmp.replace(path.with_suffix(".jpg"))
    return round(float(change.max()), 2)


class Thumbs:
    """Làm ảnh thu nhỏ + mức thay đổi cho các file đã ghi xong: file mới trước, rồi dần dần tới file cũ."""

    def __init__(self):
        self.done_days: set[str] = set()  # thư mục ngày cũ đã làm hết
        self.redo: set[str] = set()       # file vừa được bù từ thẻ nhớ (có thể thay file hỏng đã ghi null): làm lại

    def run(self, budget: float):
        end = time.time() + budget
        today = f"{datetime.now():%Y-%m-%d}"
        for cam in sorted(os.scandir(REC_DIR), key=lambda e: e.name) if REC_DIR.exists() else []:
            if not cam.is_dir():
                continue
            days = sorted((d.path for d in os.scandir(cam.path) if d.is_dir()), reverse=True)
            newest_seen = False  # file mới nhất của camera có thể đang ghi: chưa xong thì để lần sau
            for day in days:
                if day in self.done_days:
                    continue
                names = sorted((f.name for f in os.scandir(day) if f.name.endswith(".mp4")), reverse=True)
                mfile = Path(day) / MOTION_FILE
                try:
                    motion = json.loads(mfile.read_text())
                except (OSError, ValueError):
                    motion = {}
                todo = False
                for i, name in enumerate(names):
                    stem = name[:-4]
                    newest = not newest_seen and i == 0
                    path = Path(day) / name
                    if stem in motion and str(path) not in self.redo:
                        continue
                    if time.time() >= end:
                        return
                    self.redo.discard(str(path))
                    if not mp4_done(path):
                        if newest:
                            todo = True
                            continue
                        motion[stem] = None  # file ghi dở (mất điện, ffmpeg bị kill): bỏ qua luôn
                    else:
                        motion[stem] = make_thumb(path)
                    tmp = mfile.with_suffix(".tmp")
                    tmp.write_text(json.dumps(motion, separators=(",", ":")))
                    tmp.replace(mfile)
                if names:
                    newest_seen = True
                if not todo and Path(day).name < today:
                    self.done_days.add(day)


def remove_day(path: str):
    """Xoá thư mục ngày không còn video (kèm ảnh thu nhỏ, motion.json)."""
    for f in os.scandir(path):
        try:
            os.remove(f.path)
        except OSError:
            pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def scan():
    """[(khoá sắp xếp "YYYY-MM-DD/HH-MM-SS", camera, đường dẫn, byte)] của mọi file video."""
    files = []
    if not REC_DIR.exists():
        return files
    for cam in os.scandir(REC_DIR):
        if not cam.is_dir():
            continue
        for day in os.scandir(cam.path):
            if not day.is_dir():
                continue
            for f in os.scandir(day.path):
                if f.name.endswith(".mp4"):
                    files.append((f"{day.name}/{f.name}", cam.name, f.path, f.stat().st_size))
    return files


def cleanup(limit_gb: float, recorders: dict):
    files = scan()
    total = sum(f[3] for f in files)
    free = shutil.disk_usage(REC_DIR).free
    # vượt giới hạn thì xoá xuống dưới 1 GB (để không phải xoá mỗi phút); ổ sắp đầy thì xoá thêm cho đủ chỗ trống
    excess = total - (limit_gb - 1) * GB if total > limit_gb * GB else 0
    excess = max(excess, MIN_FREE_GB * GB - free)
    removed = 0
    if excess > 0:
        newest = {}  # file mới nhất của mỗi camera đang được ghi, không xoá
        for key, cam, _, _ in files:
            newest[cam] = max(newest.get(cam, ""), key)
        for key, cam, path, size in sorted(files):
            if removed >= excess:
                break
            if newest[cam] == key:
                continue
            try:
                os.remove(path)
                removed += size
            except OSError:
                pass
            try:
                os.remove(path[:-4] + ".jpg")
            except OSError:
                pass
        print(f"{datetime.now():%H:%M:%S}  [dọn] đã xoá {removed / GB:.1f} GB video cũ "
              f"(giới hạn {limit_gb:.0f} GB)", flush=True)
        files = scan()
    today = datetime.now()
    keep = {f"{today:%Y-%m-%d}", f"{today + timedelta(days=1):%Y-%m-%d}"}
    for cam in os.scandir(REC_DIR):
        if cam.is_dir():
            for day in os.scandir(cam.path):
                if day.is_dir() and day.name not in keep and \
                        not any(f.name.endswith(".mp4") for f in os.scandir(day.path)):
                    remove_day(day.path)

    per_cam = {}
    for key, cam, _, size in files:
        c = per_cam.setdefault(cam, {"bytes": 0, "files": 0, "oldest": key, "newest": key})
        c["bytes"] += size
        c["files"] += 1
        c["oldest"] = min(c["oldest"], key)
        c["newest"] = max(c["newest"], key)
    for cam, r in recorders.items():
        per_cam.setdefault(cam, {"bytes": 0, "files": 0, "oldest": None, "newest": None})
        per_cam[cam].update(recording=r.running(), error=r.error, down_since=r.down_since, codec=r.codec,
                            probe=r.info, good_ratio=r.good_ratio)
    status = {"updated": time.time(), "limit_gb": limit_gb, "bytes": sum(f[3] for f in files),
              "disk_free": shutil.disk_usage(REC_DIR).free, "disk_total": shutil.disk_usage(REC_DIR).total,
              "cameras": per_cam}
    tmp = REC_STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(status))
    tmp.replace(REC_STATUS)


def main():
    init_db()
    REC_DIR.mkdir(parents=True, exist_ok=True)
    recorders: dict[str, Recorder] = {}
    stopping = False

    def on_term(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    print(f"Recorder đã chạy, lưu video vào {REC_DIR}", flush=True)
    last_cleanup = 0.0
    thumbs = Thumbs()
    backfill = Backfill(recorders, thumbs, audio_args)
    backfill.start()
    while not stopping:
        conn = connect()
        try:
            wanted = dict(conn.execute("SELECT id, record_audio FROM cameras WHERE record_enabled = 1"))
            limit_gb = rec_limit_gb(get_settings(conn))
        finally:
            conn.close()
        for cam in list(recorders):
            if cam not in wanted:
                r = recorders.pop(cam)
                r.removed = True
                r.stop()
                print(f"{datetime.now():%H:%M:%S}  {cam:<8}  ngừng ghi", flush=True)
        for cam, audio in wanted.items():
            r = recorders.setdefault(cam, Recorder(cam))
            if r.audio != bool(audio):  # đổi tuỳ chọn tiếng: ghi lại từ file mới
                r.audio = bool(audio)
                if r.running():
                    r.stop()
                    r.retry_at = 0
                    print(f"{datetime.now():%H:%M:%S}  {cam:<8}  {'bật' if audio else 'tắt'} ghi tiếng", flush=True)
            r.check()
        if time.time() - last_cleanup >= CLEANUP_EVERY:
            cleanup(limit_gb, recorders)
            last_cleanup = time.time()
        thumbs.run(THUMB_BUDGET)
        for _ in range(CHECK_EVERY * 2):
            if stopping:
                break
            for r in list(recorders.values()):
                if r.cam in FAST_RETRY_CAMS:
                    r.quick_check()
            time.sleep(0.5)
    backfill.stopping = True
    for r in recorders.values():
        r.removed = True
        r.stop()
    print("Recorder đã dừng", flush=True)


if __name__ == "__main__":
    main()
