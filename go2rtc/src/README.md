# go2rtc bản sửa cho server

Server chạy go2rtc 1.9.14 (commit b5948cf) với thư mục `pkg/xiaomi/miss` thay bằng `xiaomi-miss-patched.tgz`:

- lấy từ github.com/genosajd/xiaomi-pc-view (MIT): phát lại thẻ nhớ (`xiaomi://...&playback=<unix>&speed=1`),
  danh sách file thẻ nhớ qua kênh RDT, vòng đọc lệnh (sửa go2rtc#2489);
- sửa lỗi loa CS2: `cs2/conn.go` WritePacket chép `payload` thay vì `hdr` (go2rtc PR #2406);
- phát lại: hỏi danh sách file trước để có giờ đầu file chính xác, báo giờ đó qua user_agent `playback=<unix>`,
  nối giờ khung hình qua các file liền nhau (camera đếm lại từ 0 mỗi file).
- đồng hồ tiếng: phiên trực tiếp bắt đầu `audioTS` từ giờ thật thay vì 0, để khi go2rtc nối lại camera (ffmpeg vẫn nối vào RTSP)
  giờ tiếng đi tiếp đúng bằng khoảng đứt; bản cũ về 0 làm các file sau mất hẳn tiếng.

Biên dịch (Go ≥ 1.24):

    tar xzf xiaomi-miss-patched.tgz -C <go2rtc-b5948cf>
    GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o go2rtc .
