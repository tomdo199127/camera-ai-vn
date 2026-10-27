// Service worker: chỉ giữ "vỏ" ứng dụng để mở nhanh và mở được khi mạng chập chờn.
// Dữ liệu (/api) và ảnh (/images) luôn lấy mới từ server, không cache.
const CACHE = "camera-ai-v11";
const SHELL = ["/", "/static/app.js", "/static/style.css", "/static/icons/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  // chỉ giữ trang chính và tệp giao diện; /api, /images, /login và mọi đường dẫn khác luôn đi thẳng tới server
  if (url.pathname !== "/" && !url.pathname.startsWith("/static/")) return;
  // mạng trước, lỗi mạng mới dùng bản đã lưu; không lưu trang đăng nhập (bị chuyển hướng khi hết phiên)
  e.respondWith(
    fetch(e.request)
      .then((r) => {
        if (r.ok && !r.redirected) {
          const copy = r.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return r;
      })
      .catch(() => caches.match(e.request, { ignoreSearch: true }).then((r) => r || caches.match("/")))
  );
});
