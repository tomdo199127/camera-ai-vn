"use strict";

const $view = document.getElementById("view");
const $modal = document.getElementById("modal");
const $modalBody = document.getElementById("modal-body");
const PAGE_SIZE = 60;
const LANE_COLORS = ["--lane-1", "--lane-2", "--lane-3", "--lane-4", "--lane-5"];

let cameras = [];
let refreshTimer = null;
let me = null;  // tài khoản đang đăng nhập: { username, is_admin, perms, pages, cameras (null = tất cả) }
let leavePage = null;  // dọn dẹp của trang đang mở (vd ngắt các luồng trực tiếp) khi chuyển trang
// trang Xem (trực tiếp) dùng chung quyền với Xem lại
const can = (page) => { page = page === "live" ? "playback" : page; return !me || !(page in me.pages) || me.perms.includes(page); };

// ---------- TV (Android TV, điều khiển bằng remote) ----------
// Nhận theo trình duyệt (tên TV trong user agent, hoặc Android không cảm ứng); ép bật / tắt bằng ?tv=1 / ?tv=0 (nhớ lại).
const TV = (() => {
  const m = (location.search + location.hash).match(/[?&]tv=([01])/);
  try {
    if (m) localStorage.setItem("tv", m[1]);
    const v = localStorage.getItem("tv");
    if (v) return v === "1";
  } catch {}
  const ua = navigator.userAgent;
  return /Android ?TV|Google ?TV|\bAFT[A-Z0-9]|BRAVIA|SMART-?TV|SmartTV|MiTV|MIBOX|Tizen|Web0S|webOS|HbbTV|CrKey|TV Bro/i.test(ua) ||
    (/Android/i.test(ua) && !/Mobile/i.test(ua) && navigator.maxTouchPoints === 0);  // TV box: Android, không "Mobile", không cảm ứng
})();
document.documentElement.classList.toggle("tv", TV);

// Phím mũi tên chuyển ô được chọn theo vị trí trên màn hình (ô gần nhất theo hướng bấm); OK / Enter = bấm.
const FOCUSABLE = "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea, [tabindex]:not([tabindex='-1'])";
let spatialEdge = null;  // trang đang mở xử lý khi bấm mũi tên mà không còn ô nào theo hướng đó (vd sang trang camera kế)
function spatialMove(dir) {
  const scope = [...document.querySelectorAll("dialog[open]")].pop() || document.fullscreenElement || document;
  const els = [...scope.querySelectorAll(FOCUSABLE)].filter((el) => {
    if (el.closest("[hidden], .tabbar") || el.tabIndex < 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== "hidden";
  });
  const cur = document.activeElement;
  if (!cur || cur === document.body || !els.includes(cur)) {
    const first = els.find((el) => el.closest(".live-tile, main")) || els[0];
    first?.focus();
    return;
  }
  const a = cur.getBoundingClientRect();
  const ax = a.left + a.width / 2, ay = a.top + a.height / 2;
  let best = null, bestScore = Infinity;
  for (const el of els) {
    if (el === cur || cur.contains(el) || el.contains(cur)) continue;
    const b = el.getBoundingClientRect();
    const bx = b.left + b.width / 2, by = b.top + b.height / 2;
    // khoảng cách theo hướng bấm (tính từ mép) và độ lệch sang ngang
    let main, side;
    // chỉ xét ô nằm hẳn về phía bấm (lệch mép tối đa 20 px), để từ camera bấm sang phải không nhảy lên nút ở góc trên
    if (dir === "ArrowRight") { main = b.left - a.right; side = Math.abs(by - ay); }
    else if (dir === "ArrowLeft") { main = a.left - b.right; side = Math.abs(by - ay); }
    else if (dir === "ArrowDown") { main = b.top - a.bottom; side = Math.abs(bx - ax); }
    else { main = a.top - b.bottom; side = Math.abs(bx - ax); }
    if (main < -20) continue;
    // sang trái / phải: chỉ ô cùng hàng (chồng nhau theo chiều dọc)
    if ((dir === "ArrowRight" || dir === "ArrowLeft") && (b.bottom <= a.top || b.top >= a.bottom)) continue;
    const score = Math.max(main, -10) + side * 2;
    if (score < bestScore) { bestScore = score; best = el; }
  }
  if (best) {
    best.focus({ preventScroll: true });
    best.scrollIntoView({ block: "nearest", inline: "nearest" });
  } else spatialEdge?.(dir);
}
if (TV) {
  window.addEventListener("keydown", (e) => {
    if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
    if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(e.key)) return;
    const t = e.target;
    // ô nhập chữ: trái / phải để di con trỏ; danh sách chọn: lên / xuống để chọn
    if (t.matches?.("input:not([type=checkbox]):not([type=radio]):not([type=button]), textarea") && (e.key === "ArrowLeft" || e.key === "ArrowRight")) return;
    if (t.matches?.("select, textarea") && (e.key === "ArrowUp" || e.key === "ArrowDown")) return;
    e.preventDefault();
    spatialMove(e.key);
  });
}

// ---------- helpers ----------
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const img = (rel) => "/images/" + rel.split("/").map(encodeURIComponent).join("/");
const pad = (n) => String(n).padStart(2, "0");
const fmtTime = (t) => { const d = new Date(t * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const fmtTimeSec = (t) => { const d = new Date(t * 1000); return `${fmtTime(t)}:${pad(d.getSeconds())}`; };
const fmtDate = (t) => { const d = new Date(t * 1000); return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()}`; };
const fmtDateTime = (t) => `${fmtTime(t)} ${fmtDate(t)}`;
const isoDate = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const dayKey = (t) => isoDate(new Date(t * 1000));
const WEEKDAYS = ["Chủ nhật", "Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7"];
const fmtDayLabel = (t) => {
  const d = new Date(t * 1000);
  const today = isoDate(new Date());
  const yest = isoDate(new Date(Date.now() - 86400000));
  const k = isoDate(d);
  const name = k === today ? "Hôm nay" : k === yest ? "Hôm qua" : WEEKDAYS[d.getDay()];
  return `${name}, ${pad(d.getDate())}/${pad(d.getMonth() + 1)}`;
};
function fmtDuration(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec} giây`;
  const m = Math.round(sec / 60);
  if (m < 60) return `${m} phút`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h} giờ ${m % 60} phút` : `${h} giờ`;
}
function ago(t) {
  const s = Date.now() / 1000 - t;
  if (s < 60) return "vừa xong";
  if (s < 3600) return `${Math.floor(s / 60)} phút trước`;
  if (s < 86400) return `${Math.floor(s / 3600)} giờ trước`;
  return fmtDate(t);
}
const camName = (id) => cameras.find((c) => c.id === id)?.name || id;
const camColor = (id) => `var(${LANE_COLORS[Math.max(0, cameras.findIndex((c) => c.id === id)) % LANE_COLORS.length]})`;
const plateLink = (p) => `#/plate/${encodeURIComponent(p)}`;
// nút mở video ghi hình lúc sự việc xảy ra (lùi vài giây để thấy cả lúc đi tới)
const videoLink = (cam, t) => cameras.find((c) => c.id === cam)?.record_enabled
  ? `<a href="#/playback?camera=${encodeURIComponent(cam)}&t=${Math.floor(t) - 5}"><button type="button">Xem video</button></a>` : "";

// mỗi lần chuyển trang thì huỷ các lượt đọc dữ liệu (GET) của trang cũ: trang cũ tải xong muộn (mạng chậm) không được
// ghi vào trang mới. Lượt bị huỷ không bao giờ trả về, nên đoạn code đang chờ nó của trang cũ dừng luôn.
let pageAbort = new AbortController();
async function api(path, opts = {}) {
  const signal = !opts.method || opts.method === "GET" ? pageAbort.signal : undefined;
  try {
    return await request(path, { headers: { "Content-Type": "application/json" }, signal, ...opts });
  } catch (e) {
    if (signal?.aborted) return new Promise(() => {});
    throw e;
  }
}
async function request(path, opts) {
  const r = await fetch(path, opts);
  if (r.status === 401) {  // hết phiên đăng nhập: về trang đăng nhập, xong quay lại đúng trang đang xem
    try { sessionStorage.setItem("afterLogin", location.hash); } catch {}
    location.replace("/login");
    throw new Error("Chưa đăng nhập");
  }
  if (!r.ok) {
    const text = await r.text();
    let msg = text || r.statusText;
    try { msg = JSON.parse(text).detail || msg; } catch {}
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return r.json();
}
function toast(msg) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2200);
}
function readParams() {
  const q = location.hash.split("?")[1] || "";
  return Object.fromEntries(new URLSearchParams(q));
}
function setParams(base, params) {
  const clean = Object.fromEntries(Object.entries(params).filter(([, v]) => v !== "" && v != null));
  const qs = new URLSearchParams(clean).toString();
  history.replaceState(null, "", base + (qs ? "?" + qs : ""));
}

// ---------- router ----------
const $sheet = document.getElementById("more-sheet");
const MORE_PAGES = ["cameras", "settings", "users"];

// ẩn các trang tài khoản này không được mở
function applyAccount() {
  document.querySelectorAll("[data-nav]").forEach((a) => (a.hidden = !can(a.dataset.nav)));
  document.querySelectorAll("[data-account]").forEach((b) => (b.hidden = me.open));
  document.querySelectorAll("[data-logout]").forEach((b) => (b.hidden = me.open));
  document.querySelector(".topbar [data-account]").textContent = me.username;
}

let routeGen = 0;
// trang vừa rời (để có nút quay lại; app trên iPhone mở toàn màn hình không có nút back của trình duyệt).
// Chỉ tính khi đổi trang: đổi tham số trên cùng trang (replaceState) hay tải lại dữ liệu không tính.
let curPage = "", prevPage = "";
async function route() {
  const gen = ++routeGen;
  const page = location.hash.split("?")[0];
  const changed = page !== curPage;
  if (changed) { prevPage = curPage; curPage = page; }
  pageAbort.abort();
  pageAbort = new AbortController();
  clearInterval(refreshTimer);
  leavePage?.();
  leavePage = null;
  document.documentElement.classList.remove("pb-lock");  // rời Xem lại khi đang xem toàn màn hình
  if ($sheet.open) $sheet.close();
  const path = location.hash.replace(/^#/, "").split("?")[0] || "/";
  const nav = path.startsWith("/plate/") ? "detections" : path.slice(1).split("/")[0];
  const tab = nav === "playback" ? "live" : nav;  // Xem lại một camera mở từ trang Xem
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === tab));
  document.querySelector("[data-nav-more]").classList.toggle("active", MORE_PAGES.includes(nav));
  document.getElementById("page-title").textContent = nav === "playback" ? "Xem lại"
    : document.querySelector(`.mainnav [data-nav="${nav}"]`)?.textContent || "";
  window.scrollTo(0, 0);
  if (!me) {
    me = await api("/api/me");
    applyAccount();
  }
  const first = Object.keys(me.pages).find(can);  // trang mặc định: Xem (đầu menu), không có quyền thì trang kế tiếp
  if (path === "/" && first) { location.replace(`#/${first === "playback" ? "live" : first}`); return; }
  if (path === "/plates") { location.replace("#/detections"); return; }  // trang Biển số cũ đã gộp vào Nhận diện
  if (path === "/visitors") { location.replace("#/events"); return; }   // trang Qua lại đã bỏ: tìm theo mô tả ở Cảnh báo
  if (!can(nav)) {
    $view.innerHTML = `<div class="empty">${first ? "Tài khoản của bạn không được mở trang này." : "Tài khoản của bạn chưa được cấp quyền mở trang nào. Nhờ admin cấp quyền."}</div>`;
    return;
  }
  // đổi trang mà trang mới tải lâu (mạng chậm, máy chủ bận): đừng để nội dung trang cũ dưới tiêu đề mới
  const oldContent = changed ? $view.firstChild : null;
  const loading = oldContent && setTimeout(() => {
    if (gen === routeGen && $view.firstChild === oldContent) $view.innerHTML = `<div class="empty">Đang tải…</div>`;
  }, 300);
  if (!cameras.length) cameras = await api("/api/cameras").catch(() => []);
  if (gen !== routeGen) return;
  try {
    if (path === "/detections") await viewDetections();
    else if (path.startsWith("/plate/")) await viewTimeline(decodeURIComponent(path.slice(7)));
    else if (path === "/cameras") await viewCameras();
    else if (path === "/events") await viewEvents();
    else if (path === "/people") await viewPeople();
    else if (path === "/playback") await viewPlayback();
    else if (path === "/live") await viewLive();
    else if (path === "/settings") await viewSettings();
    else if (path === "/users") await viewUsers();
    else $view.innerHTML = `<div class="empty">Không tìm thấy trang</div>`;
  } catch (e) {
    if (gen === routeGen) $view.innerHTML = `<div class="empty">Lỗi tải dữ liệu: ${esc(e.message)}</div>`;
  } finally {
    clearTimeout(loading);
  }
}
window.addEventListener("hashchange", route);

// ---------- Nhận diện ----------
async function viewDetections() {
  const p = readParams();
  const camOpts = cameras.map((c) => `<option value="${esc(c.id)}" ${p.camera === c.id ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  $view.innerHTML = `
    <h1>Nhận diện biển số</h1>
    <p class="sub">Mỗi thẻ là một lần xe xuất hiện trước camera, kèm ảnh chụp và biển số OCR đọc được. Bấm “… lần” để xem lịch trình của biển đó.</p>
    <div class="stats" id="stats"></div>
    <form class="toolbar" id="filters">
      <input type="search" name="q" placeholder="Tìm biển số, vd 30A123" value="${esc(p.q || "")}">
      <select name="camera"><option value="">Tất cả camera</option>${camOpts}</select>
      <input type="date" name="date_from" value="${esc(p.date_from || "")}" title="Từ ngày">
      <input type="date" name="date_to" value="${esc(p.date_to || "")}" title="Đến ngày">
      <select name="sort" aria-label="Sắp xếp">
        <option value="">Mới nhất trước</option>
        <option value="oldest" ${p.sort === "oldest" ? "selected" : ""}>Cũ nhất trước</option>
        <option value="count" ${p.sort === "count" ? "selected" : ""}>Nhiều lần nhất (mỗi biển 1 thẻ)</option>
      </select>
      <button class="primary" type="submit">Lọc</button>
      <button type="button" id="clear">Xoá lọc</button>
      <span class="grow"></span>
      <label class="check"><input type="checkbox" id="auto" checked> Tự làm mới</label>
    </form>
    <div id="list"></div>`;

  const form = document.getElementById("filters");
  let offset = Number(p.offset || 0);
  const load = async () => {
    const f = Object.fromEntries(new FormData(form));
    setParams("#/detections", { ...f, offset: offset || "" });
    const [stats, data] = await Promise.all([
      api("/api/stats"),
      api(`/api/sightings?${new URLSearchParams({ ...f, limit: PAGE_SIZE, offset })}`),
    ]);
    renderStats(stats);
    renderCards(data, offset, (o) => { offset = o; load(); });
  };
  form.addEventListener("submit", (e) => { e.preventDefault(); offset = 0; load(); });
  form.sort.onchange = () => { offset = 0; load(); };
  document.getElementById("clear").onclick = () => { form.reset(); form.q.value = ""; form.camera.value = ""; form.date_from.value = ""; form.date_to.value = ""; form.sort.value = ""; offset = 0; load(); };
  const auto = document.getElementById("auto");
  refreshTimer = setInterval(() => { if (auto.checked && !$modal.open && offset === 0) load(); }, 10000);
  await load();
}

function renderStats(s) {
  document.getElementById("stats").innerHTML = [
    ["Lượt xuất hiện hôm nay", s.today_sightings],
    ["Biển số khác nhau hôm nay", s.today_plates],
    ["Tổng lượt đã lưu", s.total_sightings],
    ["Tổng biển số", s.total_plates],
  ].map(([l, v]) => `<div class="stat"><div class="label">${l}</div><div class="value">${v}</div></div>`).join("");
}

function renderCards(data, offset, goto) {
  const list = document.getElementById("list");
  if (!data.items.length) {
    list.innerHTML = `<div class="empty">Chưa có lần nhận diện nào khớp bộ lọc.</div>`;
    return;
  }
  list.innerHTML = `<div class="grid">${data.items.map(cardHtml).join("")}</div>
    <div class="pager">
      <button ${offset === 0 ? "disabled" : ""} data-go="${offset - PAGE_SIZE}">‹ Mới hơn</button>
      <span>${offset + 1}–${offset + data.items.length} / ${data.total}${readParams().sort === "count" ? " biển" : ""}</span>
      <button ${offset + PAGE_SIZE >= data.total ? "disabled" : ""} data-go="${offset + PAGE_SIZE}">Cũ hơn ›</button>
    </div>`;
  list.querySelectorAll(".card").forEach((el, i) => (el.onclick = (e) => { if (!e.target.closest("a")) openSighting(data.items[i], () => goto(offset)); }));
  list.querySelectorAll("[data-go]").forEach((b) => (b.onclick = () => { goto(Math.max(0, Number(b.dataset.go))); window.scrollTo(0, 0); }));
}

function cardHtml(s) {
  const dur = s.last_seen - s.first_seen;
  const score = s.ocr_score >= 0.9 ? "ok" : s.ocr_score >= 0.8 ? "accent" : "warn";
  return `<div class="card">
    <div class="thumb">
      <img class="frame" loading="lazy" src="${img(s.frame_img)}" alt="">
      <img class="crop" loading="lazy" src="${img(s.plate_img)}" alt="">
    </div>
    <div class="body">
      <div class="row">
        <span class="plate">${esc(s.plate)}</span>
        ${s.ai_status === "read" && !s.corrected_plate ? `<span class="badge accent" title="OCR không đọc được, AI đọc">AI đọc</span>`
          : s.ai_status === "fixed" && s.corrected_plate === s.ai_plate ? `<span class="badge accent">AI đã sửa</span>`
          : s.corrected_plate ? `<span class="badge accent">Đã sửa</span>` : `<span class="badge ${score}">OCR ${Math.round(s.ocr_score * 100)}%</span>`}
        ${s.ai_status === "differs" && !s.corrected_plate ? `<span class="badge warn" title="AI đọc là ${esc(s.ai_plate)}">AI: ${esc(s.ai_plate)}?</span>` : ""}
      </div>
      <div class="meta"><b>${esc(s.camera_name || s.camera)}</b> · ${fmtTime(s.first_seen)}${dur >= 60 ? `–${fmtTime(s.last_seen)} (${fmtDuration(dur)})` : ""} · ${fmtDate(s.first_seen)}${s.plate_count ? `<a class="seen-count" href="${plateLink(s.plate)}" title="Xem lịch trình biển này">${s.plate_count} lần ›</a>` : ""}</div>
    </div>
  </div>`;
}

function aiPlateText(s) {
  const pct = s.ai_conf != null ? ` <span class="badge">${Math.round(s.ai_conf * 100)}%</span>` : "";
  return {
    ok: `<b>${esc(s.ai_plate)}</b>${pct} <span class="badge ok">giống OCR</span>`,
    fixed: `<b>${esc(s.ai_plate)}</b>${pct} <span class="badge accent">đã tự sửa</span>`,
    read: `<b>${esc(s.ai_plate)}</b>${pct} <span class="badge accent">OCR không đọc được, AI đọc</span>`,
    differs: `<b>${esc(s.ai_plate)}</b>${pct} <span class="badge warn">khác OCR</span>`,
    unreadable: `<span class="hint" style="margin:0">không đọc rõ được${s.ai_plate ? ` (${esc(s.ai_plate)})` : ""}</span>`,
    error: `<span class="hint" style="margin:0">lỗi khi gọi AI</span>`,
  }[s.ai_status] || "";
}

function openSighting(s, onChange) {
  const dur = s.last_seen - s.first_seen;
  $modalBody.innerHTML = `
    <div class="modal-head">
      <span class="plate lg">${esc(s.plate)}</span>
      <button class="icon-btn" id="m-close" aria-label="Đóng">×</button>
    </div>
    <div class="modal-content">
      <div><img class="frame" src="${img(s.frame_img)}" alt="Ảnh toàn cảnh"></div>
      <div class="modal-side">
        <img class="crop" src="${img(s.plate_img)}" alt="Ảnh biển số">
        <dl class="kv">
          <dt>OCR đọc thô</dt><dd><code>${esc(s.raw_text)}</code></dd>
          <dt>OCR luận ra</dt><dd><b>${esc(s.ocr_plate)}</b> <span class="badge">${Math.round(s.ocr_score * 100)}%</span></dd>
          ${s.ai_status ? `<dt>AI đọc lại</dt><dd>${aiPlateText(s)}</dd>` : ""}
          <dt>Camera</dt><dd>${esc(s.camera_name || s.camera)}${s.camera_location ? ` · ${esc(s.camera_location)}` : ""}</dd>
          <dt>Xuất hiện</dt><dd>${fmtTimeSec(s.first_seen)} ${fmtDate(s.first_seen)}</dd>
          <dt>Thấy lần cuối</dt><dd>${fmtTimeSec(s.last_seen)}${dur >= 1 ? ` (${fmtDuration(dur)})` : ""}</dd>
          <dt>Số lần đọc</dt><dd>${s.reads}</dd>
        </dl>
        <div>
          <div class="sub" style="margin:0 0 6px">Sửa biển số nếu OCR đọc sai</div>
          <form class="edit-row" id="m-edit">
            <input name="plate" value="${esc(s.corrected_plate || s.ocr_plate)}" aria-label="Biển số">
            <button class="primary">Lưu</button>
          </form>
          ${s.corrected_plate ? `<button id="m-reset" style="margin-top:6px">Dùng lại kết quả OCR</button>` : ""}
          ${s.ai_status === "differs" && s.ai_plate && s.ai_plate !== s.plate ? `<button id="m-ai" style="margin-top:6px">Dùng kết quả AI: ${esc(s.ai_plate)}</button>` : ""}
        </div>
        <div class="actions">
          <a href="${plateLink(s.plate)}"><button type="button">Xem lịch trình</button></a>
          ${videoLink(s.camera, s.first_seen)}
          <button class="danger" id="m-del" type="button">Xoá lần này</button>
        </div>
      </div>
    </div>`;
  $modal.showModal();
  const close = () => $modal.close();
  document.getElementById("m-close").onclick = close;
  $modalBody.querySelectorAll("a").forEach((a) => (a.onclick = close));
  const save = async (plate) => {
    await api(`/api/sightings/${s.id}`, { method: "PATCH", body: JSON.stringify({ plate }) });
    toast("Đã lưu");
    close();
    onChange?.();
  };
  document.getElementById("m-edit").onsubmit = (e) => { e.preventDefault(); save(e.target.plate.value); };
  const reset = document.getElementById("m-reset");
  if (reset) reset.onclick = () => save("");
  const useAi = document.getElementById("m-ai");
  if (useAi) useAi.onclick = () => save(s.ai_plate);
  document.getElementById("m-del").onclick = async () => {
    if (!confirm(`Xoá lần nhận diện ${s.plate} lúc ${fmtDateTime(s.first_seen)}? Ảnh cũng sẽ bị xoá.`)) return;
    await api(`/api/sightings/${s.id}`, { method: "DELETE" });
    toast("Đã xoá");
    close();
    onChange?.();
  };
}
$modal.addEventListener("click", (e) => { if (e.target === $modal) $modal.close(); });

// ---------- Lịch trình ----------
async function viewTimeline(plate) {
  const p = readParams();
  const today = new Date();
  const from = p.date_from || isoDate(new Date(today.getTime() - 6 * 86400000));
  const to = p.date_to || isoDate(today);
  const data = await api(`/api/plates/${encodeURIComponent(plate)}/timeline?date_from=${from}&date_to=${to}`);
  const sm = data.summary;
  const camsSeen = [...new Set(data.items.map((s) => s.camera))];

  $view.innerHTML = `
    <div class="plate-head">
      <span><button type="button" class="back-btn" id="tl-back" aria-label="Quay lại trang trước">‹</button><span class="plate lg">${esc(plate)}</span></span>
      <button type="button" id="watch" class="${data.watched ? "primary" : ""}" title="Gửi Telegram mỗi khi xe này xuất hiện">${data.watched ? "★ Đang theo dõi" : "☆ Theo dõi biển này"}</button>
      <div class="facts">
        <div class="fact"><div class="label">Tổng số lần xuất hiện</div><div class="value">${sm.n || 0}</div></div>
        <div class="fact"><div class="label">Lần đầu thấy</div><div class="value">${sm.first_seen ? fmtDateTime(sm.first_seen) : "—"}</div></div>
        <div class="fact"><div class="label">Lần cuối thấy</div><div class="value">${sm.last_seen ? fmtDateTime(sm.last_seen) : "—"}</div></div>
        <div class="fact"><div class="label">Tổng thời gian có mặt</div><div class="value">${fmtDuration(sm.dwell || 0)}</div></div>
      </div>
    </div>
    <form class="toolbar" id="f">
      <span>Từ</span><input type="date" name="date_from" value="${from}">
      <span>đến</span><input type="date" name="date_to" value="${to}">
      <button class="primary">Xem</button>
      <span class="grow"></span>
      <span class="sub" style="margin:0">${data.items.length} lần trong khoảng đã chọn</span>
    </form>
    <div id="chart"></div>
    <div id="timeline"></div>`;

  // vào thẳng từ link (không có trang trước) thì quay về Nhận diện
  document.getElementById("tl-back").onclick = () => (prevPage ? history.back() : (location.hash = "#/detections"));
  document.getElementById("f").onsubmit = (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    location.replace(`${plateLink(plate)}?${new URLSearchParams(f)}`);
  };
  document.getElementById("watch").onclick = async () => {
    const r = await api(`/api/plates/${encodeURIComponent(plate)}/watch`, { method: "POST", body: JSON.stringify({ watch: !data.watched }) });
    toast(r.watched ? "Đã theo dõi. Bật cột “Đối tượng cụ thể” cho camera trong Cài đặt để nhận Telegram." : "Đã bỏ theo dõi");
    route();
  };

  if (!data.items.length) {
    document.getElementById("timeline").innerHTML = `<div class="empty">Không có lần xuất hiện nào trong khoảng thời gian này.</div>`;
    return;
  }
  renderDayChart(data.items, camsSeen);
  renderTimeline(data.items);
}

function renderDayChart(items, camsSeen) {
  // gom theo ngày, mỗi ngày là một thanh 24 giờ; mỗi lần xuất hiện là một đoạn màu theo camera
  const days = {};
  for (const s of items) (days[dayKey(s.first_seen)] ??= []).push(s);
  const keys = Object.keys(days).sort().reverse();
  const rowsHtml = keys.map((k) => {
    const start = new Date(k + "T00:00:00").getTime() / 1000;
    const bars = days[k].map((s, i) => {
      const left = Math.max(0, (s.first_seen - start) / 864);
      const width = Math.min(100 - left, Math.max(0.3, (s.last_seen - s.first_seen) / 864));
      return `<div class="bar" data-k="${k}" data-i="${i}" style="left:${left}%;width:${width}%;background:${camColor(s.camera)}"
        title="${esc(s.camera_name || s.camera)}: ${fmtTime(s.first_seen)}–${fmtTime(s.last_seen)}"></div>`;
    }).join("");
    return `<div class="day">${fmtDayLabel(start)}</div><div class="track">${bars}</div>`;
  }).join("");
  const axis = [0, 3, 6, 9, 12, 15, 18, 21, 24].map((h) => `<span style="left:${(h / 24) * 100}%">${h}h</span>`).join("");
  const el = document.getElementById("chart");
  el.innerHTML = `<div class="panel">
    <h2>Biểu đồ có mặt theo ngày</h2>
    <div class="daychart">${rowsHtml}<div></div><div class="axis">${axis}</div></div>
    <div class="legend">${camsSeen.map((c) => `<span><i style="background:${camColor(c)}"></i>${esc(camName(c))}</span>`).join("")}</div>
  </div>`;
  el.querySelectorAll(".bar").forEach((b) => (b.onclick = () => openSighting(days[b.dataset.k][b.dataset.i], route)));
}

function renderTimeline(items) {
  // mới nhất lên trên; giữa 2 lần ở 2 camera khác nhau hiển thị bước di chuyển
  const sorted = [...items].sort((a, b) => b.first_seen - a.first_seen);
  let html = "", currentDay = "";
  sorted.forEach((s, i) => {
    const k = dayKey(s.first_seen);
    if (k !== currentDay) {
      if (currentDay) html += `</div>`;
      html += `<div class="tl-day">${fmtDayLabel(s.first_seen)}</div><div class="tl">`;
      currentDay = k;
    }
    const dur = s.last_seen - s.first_seen;
    html += `<div class="tl-item" style="--dot:${camColor(s.camera)}">
      <div class="when"><b>${fmtTime(s.first_seen)}${dur >= 60 ? ` – ${fmtTime(s.last_seen)}` : ""}</b><span>${dur >= 60 ? `có mặt ${fmtDuration(dur)}` : "đi qua"}</span></div>
      <div class="what">
        <div class="cam">${esc(s.camera_name || s.camera)}</div>
        ${s.camera_location ? `<div class="loc">${esc(s.camera_location)}</div>` : ""}
        <div class="loc">OCR ${Math.round(s.ocr_score * 100)}% · ${s.reads} lần đọc${s.corrected_plate ? " · đã sửa tay" : ""}</div>
      </div>
      <img class="frame" loading="lazy" src="${img(s.frame_img)}" data-i="${i}" alt="">
    </div>`;
    const prev = sorted[i + 1]; // lần xuất hiện trước đó (theo thời gian)
    if (prev && dayKey(prev.first_seen) === k) {
      const gap = s.first_seen - prev.last_seen;
      html += prev.camera !== s.camera
        ? `<div class="tl-move">↑ di chuyển từ <b>${esc(prev.camera_name || prev.camera)}</b> sang <b>${esc(s.camera_name || s.camera)}</b> sau ${fmtDuration(gap)}</div>`
        : `<div class="tl-move">↑ quay lại sau ${fmtDuration(gap)}</div>`;
    }
  });
  html += `</div>`;
  const el = document.getElementById("timeline");
  el.innerHTML = `<div class="panel"><h2>Lịch trình di chuyển</h2>${html}</div>`;
  el.querySelectorAll("img.frame").forEach((im) => (im.onclick = () => openSighting(sorted[im.dataset.i], route)));
}

// ---------- Camera ----------
const ZONE_CLASSES = [["person", "Người"], ["car", "Ô tô"], ["bike", "Xe máy / xe đạp"], ["animal", "Chó / mèo"]];
const ZONE_COLORS = ["#ff3b30", "#ff9500", "#ffcc00", "#34c759", "#5ac8fa", "#af52de"];
const zoneColor = (i) => ZONE_COLORS[i % ZONE_COLORS.length];

function zonesSvg(zones, w = 1000, h = 562) {
  return `<svg class="zone-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${zones.map((z, i) =>
    `<polygon points="${z.points.map(([x, y]) => `${x * w},${y * h}`).join(" ")}" style="--zc:${zoneColor(i)}" class="${z.enabled ? "" : "off"}"></polygon>`).join("")}</svg>`;
}

async function viewCameras() {
  const [cams, zones, rec] = await Promise.all([api("/api/cameras"), api("/api/zones"), api("/api/recordings").catch(() => ({}))]);
  cameras = cams;
  $view.innerHTML = `
    <div class="head-row"><h1>Camera</h1>${me?.cameras ? "" : `<button class="primary" id="cam-add">+ Thêm camera</button>`}</div>
    <p class="sub">Bật tính năng cho từng camera. AI chỉ chạy khi khung hình có chuyển động nên camera đứng yên gần như không tốn CPU.</p>
    ${cameras.length ? "" : `<div class="empty">Chưa có camera nào. Bấm “Thêm camera” để thêm camera có RTSP (Dahua, Imou, Hikvision, EZVIZ, Tapo…).</div>`}
    <div class="cam-grid">${cameras.map((c) => camHtml(c, zones.filter((z) => z.camera === c.id), rec.cameras?.[c.id])).join("")}</div>`;
  const addBtn = document.getElementById("cam-add");
  if (addBtn) addBtn.onclick = () => openCameraForm(null);
  $view.querySelectorAll(".cam").forEach((el) => {
    const id = el.dataset.id;
    el.querySelector("[data-source]").onclick = () => openCameraForm(cameras.find((c) => c.id === id));
    el.querySelector("[data-delete]").onclick = async () => {
      const cam = cameras.find((c) => c.id === id);
      if (!confirm(`Xoá camera “${cam.name}”?\nLịch sử biển số, người qua lại vẫn giữ; vùng cảnh báo của camera này sẽ bị xoá.`)) return;
      try {
        await api(`/api/cameras/${encodeURIComponent(id)}`, { method: "DELETE" });
        toast("Đã xoá camera");
        cameras = await api("/api/cameras");
        viewCameras();
      } catch (e) { toast(e.message); }
    };
    const camZones = zones.filter((z) => z.camera === id);
    const snap = el.querySelector(".snap");
    const loadSnap = () => {
      snap.innerHTML = "Đang lấy ảnh…";
      const im = new Image();
      im.onload = () => { snap.innerHTML = ""; snap.appendChild(im); snap.insertAdjacentHTML("beforeend", zonesSvg(camZones)); };
      im.onerror = () => { snap.textContent = "Không lấy được ảnh (camera offline?)"; };
      im.src = `/api/cameras/${encodeURIComponent(id)}/snapshot?t=${Date.now()}`;
    };
    el.querySelector("[data-snap]").onclick = loadSnap;
    el.querySelector("[data-zones]").onclick = () => openZoneEditor(cameras.find((c) => c.id === id), route);
    const recBox = el.querySelector("[name=record]");
    recBox.onchange = () => (el.querySelector("[data-rec-audio]").hidden = !recBox.checked);
    el.querySelector("form").onsubmit = async (e) => {
      e.preventDefault();
      const f = e.target;
      await api(`/api/cameras/${encodeURIComponent(id)}`, {
        method: "PATCH",
        body: JSON.stringify({
          name: f.elements.name.value, location: f.elements.location.value,
          lpr_enabled: f.elements.lpr.checked, face_enabled: f.elements.face.checked,
          record_enabled: f.elements.record.checked, record_audio: f.elements.record_audio.checked,
          talk_enabled: f.elements.talk.checked,
        }),
      });
      toast("Đã lưu. Worker sẽ áp dụng trong khoảng 15 giây.");
      cameras = await api("/api/cameras");
      el.querySelector("[data-playback]").hidden = !f.elements.record.checked;
    };
    loadSnap();
  });
}

function recBadge(c, r) {
  if (!c.record_enabled) return "";
  if (r?.recording) return `<span class="badge danger rec-dot">Đang ghi</span>`;
  if (!r?.error) return `<span class="badge warn">Chờ ghi</span>`;
  // camera Xiaomi hay rớt vài chục giây rồi tự có hình lại: chỉ báo lỗi khi đã mất quá 2 phút
  const down = r.down_since ? Date.now() / 1000 - r.down_since : Infinity;
  const label = down < 120 ? "Đang nối lại" : `Ghi hình lỗi${r.down_since ? ` từ ${ago(r.down_since)}` : ""}`;
  return `<span class="badge warn" title="${esc(r.error)}">${label}</span>`;
}

function camHtml(c, zones, rec) {
  const now = Date.now() / 1000;
  const active = c.lpr_enabled || c.face_enabled || c.track_enabled || c.zones;
  let status;
  if (!active) status = `<span class="badge">Chưa bật tính năng</span>`;
  else if (c.connected && c.last_frame_at && now - c.last_frame_at < 30) {
    const motion = c.motion != null ? ` · AI chạy ${Math.round(c.motion * 100)}% số khung` : "";
    status = `<span class="badge ok">Đang hoạt động</span><span>${(c.fps || 0).toFixed(1)} khung/giây${motion}</span>`;
  } else status = `<span class="badge danger">Mất kết nối</span>${c.last_frame_at ? `<span>khung cuối ${ago(c.last_frame_at)}</span>` : ""}`;
  return `<div class="cam" data-id="${esc(c.id)}">
    <div class="snap"></div>
    <form class="body">
      <div class="status">${status}${recBadge(c, rec)}<span class="grow"></span><span>${c.sightings} lượt · stream <code>${esc(c.id)}</code></span></div>
      <div class="row"><input name="name" value="${esc(c.name)}" placeholder="Tên camera" aria-label="Tên camera"></div>
      <div class="row"><input name="location" value="${esc(c.location)}" placeholder="Vị trí (vd: cổng trước)" aria-label="Vị trí"></div>
      <div class="row wrap">
        <label class="check"><input type="checkbox" name="lpr" ${c.lpr_enabled ? "checked" : ""}> Biển số</label>
        <label class="check"><input type="checkbox" name="face" ${c.face_enabled ? "checked" : ""}> Khuôn mặt</label>
        <label class="check"><input type="checkbox" name="record" ${c.record_enabled ? "checked" : ""}> Ghi hình</label>
        <label class="check" data-rec-audio ${c.record_enabled ? "" : "hidden"}><input type="checkbox" name="record_audio" ${c.record_audio ? "checked" : ""}> Kèm tiếng</label>
        <label class="check" title="Hiện nút 🎙 khi xem trực tiếp (camera phải có loa). Camera ngoài đường thì để tắt."><input type="checkbox" name="talk" ${c.talk_enabled ? "checked" : ""}> Nói qua loa</label>
        <button type="button" data-zones>Vùng cảnh báo${zones.length ? ` (${zones.length})` : ""}</button>
      </div>
      <div class="row src-row"><span class="src" title="${esc(c.source || "")}">${c.source ? `Nguồn: <code>${esc(c.source)}</code>` : "Nguồn: không thấy trong go2rtc"}</span></div>
      <div class="row wrap cam-actions">
        <button type="button" data-source>Đổi link</button>
        <button type="button" class="danger-text" data-delete>Xoá</button>
        <span style="flex:1"></span>
        <a href="#/playback?camera=${encodeURIComponent(c.id)}" data-playback ${c.record_enabled ? "" : "hidden"}><button type="button">Xem lại</button></a>
        <button type="button" data-snap>Làm mới ảnh</button>
        <button class="primary">Lưu</button>
      </div>
    </form>
  </div>`;
}

// ---------- Thêm camera / đổi link ----------
const LAN_TIP = "Máy chủ phải cùng mạng với camera (hoặc nối VPN tới mạng đó).";
const CAM_BRANDS = {
  dahua: {
    label: "Dahua / KBVision / đầu ghi Dahua", port: 554, user: "admin", channel: true,
    path: (ch, sub) => `/cam/realmonitor?channel=${ch}&subtype=${sub}`,
    help: `Tài khoản là tài khoản tạo lúc <b>khởi tạo camera</b> (app DMSS, ConfigTool hoặc vào IP camera bằng trình duyệt). RTSP bật sẵn.
      Camera nối qua <b>đầu ghi</b>: nhập IP và tài khoản của đầu ghi, <b>Kênh</b> = số kênh của camera trên đầu ghi.`,
  },
  imou: {
    label: "Imou", port: 554, user: "admin", channel: false,
    path: (ch, sub) => `/cam/realmonitor?channel=1&subtype=${sub}`,
    help: `Tài khoản <code>admin</code>, mật khẩu là <b>Safety code</b> in trên tem dưới đế camera (nếu đã đổi mật khẩu thiết bị trong app Imou Life thì dùng mật khẩu mới).
      Dòng chạy pin (Cell, Cell Go…) không có RTSP. Một số firmware mới phải bật RTSP / ONVIF trong app Imou Life → cài đặt thiết bị.`,
  },
  hik: {
    label: "Hikvision / HiLook / đầu ghi Hikvision", port: 554, user: "admin", channel: true,
    path: (ch, sub) => `/Streaming/Channels/${ch}0${sub + 1}`,
    help: `Tài khoản <code>admin</code> và mật khẩu đặt lúc <b>kích hoạt</b> (app Hik-Connect hoặc phần mềm SADP).
      Qua <b>đầu ghi</b>: nhập IP đầu ghi, <b>Kênh</b> = số kênh camera (kênh 3 luồng chính → <code>/Streaming/Channels/301</code>).`,
  },
  ezviz: {
    label: "EZVIZ", port: 554, user: "admin", channel: false,
    path: (ch, sub) => `/Streaming/Channels/10${sub + 1}`,
    help: `Tài khoản <code>admin</code>, mật khẩu là <b>mã xác minh</b> 6 chữ in hoa trên tem camera.
      Một số mẫu phải bật RTSP trong app EZVIZ (Cài đặt thiết bị → mục mạng / RTSP), một số mẫu đời mới không có RTSP.`,
  },
  tapo: {
    label: "TP-Link Tapo / VIGI", port: 554, user: "", channel: false,
    path: (ch, sub) => `/stream${sub + 1}`,
    help: `Tapo: tạo <b>Tài khoản camera</b> trong app Tapo → chọn camera → Cài đặt → Cài đặt nâng cao → Tài khoản camera, rồi dùng tài khoản đó
      (không phải tài khoản TP-Link của bạn). VIGI: dùng tài khoản đăng nhập camera.`,
  },
  reolink: {
    label: "Reolink", port: 554, user: "admin", channel: true,
    path: (ch, sub) => `/h264Preview_${String(ch).padStart(2, "0")}_${sub ? "sub" : "main"}`,
    help: `Bật RTSP trong app / Reolink Client: Cài đặt → Mạng → Nâng cao → Cài đặt cổng → RTSP.
      Camera 4K / H.265 không ra hình thì chọn “Nhập link đầy đủ” và đổi <code>h264Preview</code> thành <code>h265Preview</code>.`,
  },
  uniview: {
    label: "Uniview (UNV)", port: 554, user: "admin", channel: true,
    path: (ch, sub) => `/unicast/c${ch}/s${sub}/live`,
    help: `Tài khoản đăng nhập camera / đầu ghi Uniview. Qua đầu ghi: <b>Kênh</b> = số kênh camera.`,
  },
  onvif: {
    label: "Hãng khác có ONVIF (tự tìm luồng)", port: 80, user: "admin", channel: false, onvif: true,
    help: `Đa số camera IP có <b>ONVIF</b>: hệ thống tự hỏi camera link RTSP. Cổng ONVIF thường là 80 (một số hãng 8000, 8080 hoặc 2020).
      Có hãng phải bật ONVIF và tạo tài khoản ONVIF riêng trong app camera.`,
  },
  custom: {
    label: "Nhập link RTSP đầy đủ", custom: true,
    help: `Dán link RTSP của camera, ví dụ <code>rtsp://user:matkhau@192.168.1.50:554/...</code>. Link thường có trong hướng dẫn của hãng,
      hoặc tìm bằng phần mềm <b>ONVIF Device Manager</b> (Windows). Nên thử trước bằng VLC (Media → Open Network Stream).
      Mật khẩu có ký tự đặc biệt (@ # : /) thì phải viết dạng mã hoá, vd <code>@</code> → <code>%40</code>.`,
  },
};

function cameraSource(f, masked = false) {
  const b = CAM_BRANDS[f.brand.value];
  if (b.custom) {
    const url = f.url.value.trim();
    return masked ? url.replace(/(:\/\/[^:/@?#]*:)[^@/]*@/, "$1•••@") : url;
  }
  const ip = f.ip.value.trim();
  if (!ip) return "";
  const user = f.user.value.trim(), pass = f.pass.value;
  const auth = user ? `${encodeURIComponent(user)}${pass ? ":" + (masked ? "•••" : encodeURIComponent(pass)) : ""}@` : "";
  const host = ip + (f.port.value.trim() ? `:${f.port.value.trim()}` : "");
  if (b.onvif) return `onvif://${auth}${host}`;
  const ch = Math.max(1, parseInt(f.channel.value, 10) || 1);
  return `rtsp://${auth}${host}${b.path(ch, parseInt(f.sub.value, 10) || 0)}`;
}

async function fetchImage(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return URL.createObjectURL(await r.blob());
}

function openCameraForm(cam) {
  const edit = !!cam;
  $modalBody.innerHTML = `
    <div class="modal-head">
      <b>${edit ? `Đổi link · ${esc(cam.name)}` : "Thêm camera"}</b>
      <button class="icon-btn" id="cf-close" aria-label="Đóng">×</button>
    </div>
    <form id="cf" class="zone-form cam-form" autocomplete="off">
      ${edit ? `<p class="hint" style="margin:0">Link hiện tại: <code>${esc(cam.source || "—")}</code></p>` : `
      <label>Tên camera<input name="name" required placeholder="vd: Cổng trước"></label>
      <label>Vị trí<input name="location" placeholder="vd: cổng trước, nhìn ra đường (không bắt buộc)"></label>`}
      <label>Hãng camera<select name="brand">${Object.entries(CAM_BRANDS).map(([k, b]) => `<option value="${k}">${esc(b.label)}</option>`).join("")}</select></label>
      <div class="cf-guide" id="cf-guide"></div>
      <div class="cf-fields" id="cf-fields">
        <label>Địa chỉ IP<input name="ip" inputmode="decimal" placeholder="192.168.1.50"></label>
        <label>Cổng<input name="port" inputmode="numeric"></label>
        <label>Tài khoản<input name="user" autocomplete="off"></label>
        <label>Mật khẩu<input name="pass" type="password" autocomplete="new-password"></label>
        <label id="cf-ch">Kênh<input name="channel" inputmode="numeric" value="1"></label>
        <label id="cf-sub">Luồng<select name="sub">
          <option value="0">Chính (nét nhất, dùng để đọc biển số)</option>
          <option value="1">Phụ (nhẹ, đủ cho vùng / người qua lại)</option>
        </select></label>
      </div>
      <label id="cf-url" hidden>Link camera<input name="url" placeholder="rtsp://user:matkhau@192.168.1.50:554/..."></label>
      <p class="hint" style="margin:0">Link sẽ dùng: <code id="cf-preview">—</code></p>
      ${edit ? "" : `
      <div class="row wrap">
        <label class="check"><input type="checkbox" name="lpr"> Biển số</label>
        <label class="check"><input type="checkbox" name="face"> Khuôn mặt</label>
      </div>`}
      <div class="cf-snap" id="cf-snap" hidden></div>
      <p class="cf-msg" id="cf-msg" role="status"></p>
      <div class="row">
        <span style="flex:1"></span>
        <button type="button" id="cf-test">Thử kết nối</button>
        <button class="primary" id="cf-save">${edit ? "Lưu link mới" : "Thêm camera"}</button>
      </div>
      <details class="hint">
        <summary>Tìm IP camera và các lỗi hay gặp</summary>
        <ul>
          <li><b>IP camera</b>: xem trong app của hãng (thông tin thiết bị) hoặc danh sách thiết bị trong trang quản trị router. Nên đặt <b>IP tĩnh</b> (giữ IP) trên router để link không đổi.</li>
          <li>${LAN_TIP}</li>
          <li><b>Sai mật khẩu</b>: camera Dahua / Hikvision khoá đăng nhập vài phút sau nhiều lần sai, chờ rồi thử lại.</li>
          <li><b>Không kết nối được</b>: kiểm tra IP, cổng 554, camera đã bật RTSP chưa. Thử link bằng VLC trên máy tính cùng mạng.</li>
          <li>Đọc biển số nên dùng <b>luồng chính</b>. Camera chỉ để cảnh báo vùng / người qua lại có thể dùng luồng phụ cho nhẹ máy chủ.</li>
          <li>Camera Xiaomi không có RTSP nên không thêm ở đây được, phải cấu hình go2rtc kiểu <code>xiaomi://</code>.</li>
        </ul>
      </details>
    </form>`;
  $modal.classList.add("narrow");
  $modal.addEventListener("close", () => $modal.classList.remove("narrow"), { once: true });
  $modal.showModal();
  const f = document.getElementById("cf");
  const msg = document.getElementById("cf-msg"), snap = document.getElementById("cf-snap");
  const close = () => $modal.close();
  document.getElementById("cf-close").onclick = close;
  let touched = {};  // ô người dùng đã tự sửa thì đổi hãng không ghi đè
  ["port", "user"].forEach((k) => f[k].addEventListener("input", () => { touched[k] = true; }));
  const update = () => {
    const b = CAM_BRANDS[f.brand.value];
    document.getElementById("cf-guide").innerHTML = b.help;
    document.getElementById("cf-fields").hidden = !!b.custom;
    document.getElementById("cf-url").hidden = !b.custom;
    document.getElementById("cf-ch").hidden = !b.channel;
    document.getElementById("cf-sub").hidden = !!b.onvif;
    if (!b.custom) {
      if (!touched.port) f.port.value = b.port;
      if (!touched.user) f.user.value = b.user;
    }
    document.getElementById("cf-preview").textContent = cameraSource(f, true) || "—";
  };
  f.brand.onchange = update;
  f.addEventListener("input", () => { document.getElementById("cf-preview").textContent = cameraSource(f, true) || "—"; });
  update();

  const busy = (on, text = "") => {
    f.querySelectorAll("button").forEach((b) => { if (b.id !== "cf-close") b.disabled = on; });
    msg.className = "cf-msg"; msg.textContent = text;
  };
  const fail = (e) => { busy(false); msg.className = "cf-msg err"; msg.textContent = e.message; };
  const source = () => {
    const s = cameraSource(f);
    if (!s) throw new Error(CAM_BRANDS[f.brand.value].custom ? "Chưa nhập link camera" : "Chưa nhập địa chỉ IP");
    return s;
  };

  document.getElementById("cf-test").onclick = async () => {
    try {
      busy(true, "Đang kết nối camera… (có thể mất tới 20 giây)");
      const url = await fetchImage("/api/cameras/test", { source: source() });
      snap.hidden = false;
      snap.innerHTML = `<img src="${url}" alt="Ảnh thử từ camera">`;
      busy(false, "Kết nối được, hình ở trên. Bấm “" + (edit ? "Lưu link mới" : "Thêm camera") + "” để lưu.");
      msg.className = "cf-msg ok";
    } catch (e) { fail(e); }
  };
  f.onsubmit = async (e) => {
    e.preventDefault();
    try {
      busy(true, "Đang kết nối camera và lưu…");
      if (edit) {
        await api(`/api/cameras/${encodeURIComponent(cam.id)}/source`, { method: "PUT", body: JSON.stringify({ source: source() }) });
        toast("Đã đổi link camera");
      } else {
        await api("/api/cameras", { method: "POST", body: JSON.stringify({
          name: f.elements.name.value.trim(), location: f.elements.location.value.trim(), source: source(),
          lpr_enabled: f.elements.lpr.checked, face_enabled: f.elements.face.checked,
        }) });
        toast("Đã thêm camera. Worker bắt đầu xử lý trong khoảng 15 giây.");
      }
      close();
      cameras = await api("/api/cameras");
      viewCameras();
    } catch (e) { fail(e); }
  };
}

// ---------- Trình vẽ vùng cảnh báo ----------
async function openZoneEditor(cam, onClose) {
  let zones = await api(`/api/zones?camera=${encodeURIComponent(cam.id)}`);
  let sel = zones.length ? 0 : -1;      // vị trí vùng đang sửa; -1 = vùng mới
  let points = sel >= 0 ? zones[0].points.map((p) => [...p]) : [];
  let drawing = sel < 0;                // đang bấm để thêm điểm
  let size = { w: 1000, h: 562 };

  $modalBody.innerHTML = `
    <div class="modal-head">
      <b>Vùng cảnh báo · ${esc(cam.name)}</b>
      <button class="icon-btn" id="z-close" aria-label="Đóng">×</button>
    </div>
    <div class="zone-editor">
      <div>
        <div class="stage" id="z-stage"><div class="snap-msg">Đang lấy ảnh camera…</div></div>
        <p class="hint" id="z-hint"></p>
      </div>
      <div class="zone-side">
        <div class="zone-list" id="z-list"></div>
        <form id="z-form" class="zone-form"></form>
      </div>
    </div>`;
  $modal.classList.add("wide");
  $modal.showModal();
  const close = () => { $modal.close(); };
  $modal.addEventListener("close", () => { $modal.classList.remove("wide"); onClose?.(); }, { once: true });
  document.getElementById("z-close").onclick = close;

  const stage = document.getElementById("z-stage");
  const im = new Image();
  im.onload = () => { size = { w: im.naturalWidth, h: im.naturalHeight }; stage.innerHTML = ""; stage.appendChild(im); stage.insertAdjacentHTML("beforeend", `<svg id="z-svg"></svg>`); bindSvg(); render(); };
  im.onerror = () => { stage.innerHTML = `<div class="snap-msg">Không lấy được ảnh camera. Kiểm tra camera có đang online không.</div>`; };
  im.src = `/api/cameras/${encodeURIComponent(cam.id)}/snapshot?t=${Date.now()}`;

  const color = () => zoneColor(sel >= 0 ? sel : zones.length);

  function renderSvg() {
    const svg = document.getElementById("z-svg");
    if (!svg) return;
    const { w, h } = size;
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    const r = Math.max(w, h) / 120;
    const others = zones.map((z, i) => i === sel ? "" :
      `<polygon class="other" style="--zc:${zoneColor(i)}" points="${z.points.map(([x, y]) => `${x * w},${y * h}`).join(" ")}"></polygon>`).join("");
    const pts = points.map(([x, y]) => `${x * w},${y * h}`).join(" ");
    const shape = points.length >= 3 && !drawing ? `<polygon class="current" style="--zc:${color()}" points="${pts}"></polygon>`
      : `<polyline class="current" style="--zc:${color()}" points="${pts}"></polyline>`;
    const handles = points.map(([x, y], i) => `<circle data-i="${i}" cx="${x * w}" cy="${y * h}" r="${i === 0 && drawing && points.length >= 3 ? r * 1.6 : r}" style="--zc:${color()}"></circle>`).join("");
    svg.innerHTML = others + shape + handles;
  }

  function render() {
    renderSvg();
    document.getElementById("z-hint").textContent = drawing
      ? (points.length < 3 ? `Bấm lên ảnh để đặt các góc của vùng (đã có ${points.length}/3 điểm tối thiểu).`
        : "Bấm vào điểm đầu tiên hoặc nút “Xong” để khép vùng. Có thể tiếp tục thêm điểm.")
      : "Kéo các chấm tròn để chỉnh vùng. Cảnh báo khi chân người / bánh xe nằm trong vùng.";
    document.getElementById("z-list").innerHTML = zones.map((z, i) => `
      <button type="button" class="zone-item ${i === sel ? "active" : ""}" data-i="${i}">
        <i style="background:${zoneColor(i)}"></i><span>${esc(z.name)}</span>
        ${z.enabled ? "" : `<span class="badge">Tắt</span>`}
      </button>`).join("") + `<button type="button" class="zone-item ${sel < 0 ? "active" : ""}" data-i="-1">＋ Vùng mới</button>`;
    document.querySelectorAll(".zone-item").forEach((b) => (b.onclick = () => select(Number(b.dataset.i))));
    const z = sel >= 0 ? zones[sel] : { name: `Vùng ${zones.length + 1}`, classes: ["person"], start_time: "", end_time: "", cooldown: 60, notify: 1, enabled: 1 };
    const form = document.getElementById("z-form");
    form.innerHTML = `
      <label>Tên vùng<input name="zname" value="${esc(z.name)}" required></label>
      <fieldset><legend>Báo khi có</legend>${ZONE_CLASSES.map(([k, l]) =>
        `<label class="check"><input type="checkbox" name="cls" value="${k}" ${z.classes.includes(k) ? "checked" : ""}> ${l}</label>`).join("")}</fieldset>
      <fieldset><legend>Khung giờ gửi Telegram (trống = cả ngày; ngoài giờ vẫn ghi lịch sử)</legend>
        <div class="row"><input type="time" name="start" value="${esc(z.start_time)}" aria-label="Từ giờ"><span>→</span><input type="time" name="end" value="${esc(z.end_time)}" aria-label="Đến giờ"></div>
      </fieldset>
      <label>Không gửi lặp lại trong (giây)<input type="number" name="cooldown" min="0" max="86400" value="${z.cooldown}"></label>
      <label class="check"><input type="checkbox" name="notify" ${z.notify ? "checked" : ""}> Gửi Telegram</label>
      <label class="check"><input type="checkbox" name="enabled" ${z.enabled ? "checked" : ""}> Đang bật</label>
      <div class="actions">
        ${drawing ? `<button type="button" id="z-done" ${points.length < 3 ? "disabled" : ""}>Xong</button>` : `<button type="button" id="z-redraw">Vẽ lại</button>`}
        <button class="primary" ${points.length < 3 ? "disabled" : ""}>${sel >= 0 ? "Lưu vùng" : "Tạo vùng"}</button>
        ${sel >= 0 ? `<button type="button" class="danger" id="z-del">Xoá</button>` : ""}
      </div>`;
    const done = document.getElementById("z-done");
    if (done) done.onclick = () => { drawing = false; render(); };
    const redraw = document.getElementById("z-redraw");
    if (redraw) redraw.onclick = () => { points = []; drawing = true; render(); };
    const del = document.getElementById("z-del");
    if (del) del.onclick = async () => {
      if (!confirm(`Xoá vùng “${zones[sel].name}”?`)) return;
      await api(`/api/zones/${zones[sel].id}`, { method: "DELETE" });
      zones.splice(sel, 1);
      select(zones.length ? 0 : -1);
      toast("Đã xoá vùng");
    };
    form.onsubmit = async (e) => {
      e.preventDefault();
      const f = form.elements;
      const body = {
        name: f.zname.value, points,
        classes: [...form.querySelectorAll("[name=cls]:checked")].map((c) => c.value),
        start_time: f.start.value, end_time: f.end.value,
        cooldown: Number(f.cooldown.value || 0), notify: f.notify.checked, enabled: f.enabled.checked,
      };
      if ((body.start_time && !body.end_time) || (!body.start_time && body.end_time)) { toast("Nhập đủ cả giờ bắt đầu và kết thúc"); return; }
      try {
        if (sel >= 0) zones[sel] = await api(`/api/zones/${zones[sel].id}`, { method: "PATCH", body: JSON.stringify(body) });
        else { zones.push(await api("/api/zones", { method: "POST", body: JSON.stringify({ ...body, camera: cam.id }) })); sel = zones.length - 1; }
        drawing = false;
        toast("Đã lưu vùng. Worker áp dụng trong khoảng 15 giây.");
        render();
      } catch (err) { toast(err.message); }
    };
  }

  function select(i) {
    sel = i;
    points = i >= 0 ? zones[i].points.map((p) => [...p]) : [];
    drawing = i < 0;
    render();
  }

  function bindSvg() {
    const svg = document.getElementById("z-svg");
    const toNorm = (e) => {
      const r = svg.getBoundingClientRect();
      return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))];
    };
    let drag = -1;
    svg.addEventListener("pointerdown", (e) => {
      const c = e.target.closest("circle");
      if (c) {
        const i = Number(c.dataset.i);
        if (drawing && i === 0 && points.length >= 3) { drawing = false; render(); return; }
        drag = i;
        svg.setPointerCapture(e.pointerId);
        return;
      }
      if (drawing) { points.push(toNorm(e).map((v) => Math.round(v * 10000) / 10000)); render(); }
    });
    svg.addEventListener("pointermove", (e) => { if (drag >= 0) { points[drag] = toNorm(e); renderSvg(); } });
    svg.addEventListener("pointerup", () => { drag = -1; });
  }
}

// ---------- Cảnh báo ----------
async function viewEvents() {
  const p = readParams();
  const camOpts = cameras.map((c) => `<option value="${esc(c.id)}" ${p.camera === c.id ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  const kinds = [["", "Tất cả"], ["zone", "Vào vùng"], ["face", "Khuôn mặt"]];
  $view.innerHTML = `
    <h1>Cảnh báo</h1>
    <p class="sub">Người / xe / chó mèo đi vào vùng cảnh báo và các khuôn mặt camera bắt được.</p>
    <div class="stats compact" id="stats"></div>
    <form class="toolbar" id="filters">
      <input type="search" name="q" class="search-q" value="${esc(p.q || "")}" placeholder="Tìm: áo đỏ, nữ, ô tô trắng, chó…" enterkeyhint="search">
      <select name="kind">${kinds.map(([k, l]) => `<option value="${k}" ${p.kind === k ? "selected" : ""}>${l}</option>`).join("")}</select>
      <select name="camera"><option value="">Tất cả camera</option>${camOpts}</select>
      <input type="date" name="date_from" value="${esc(p.date_from || "")}" title="Từ ngày">
      <input type="date" name="date_to" value="${esc(p.date_to || "")}" title="Đến ngày">
      <button class="primary" type="submit">Lọc</button>
      <span class="grow"></span>
      <label class="check"><input type="checkbox" id="auto" checked> Tự làm mới</label>
    </form>
    <p class="hint" id="search-note" hidden></p>
    <div id="list"></div>`;
  const form = document.getElementById("filters");
  const note = document.getElementById("search-note");
  let offset = Number(p.offset || 0);
  // tìm theo mô tả: báo cách xếp và số ảnh còn đang được chuẩn bị (cảnh báo mới / dữ liệu cũ đang tính bù)
  const showSearchNote = async () => {
    const q = form.q.value.trim();
    note.hidden = !q;
    if (!q) return;
    const st = await api("/api/events/search-status").catch(() => ({ pending: 0 }));
    note.textContent = `Xếp theo mức giống “${q}”, giống nhất trước. Tìm tốt nhất với màu áo / mũ / xe, nam / nữ, ô tô, chó / mèo; không đọc được chữ.`
      + (st.pending > 50 ? ` Còn ${st.pending.toLocaleString("vi-VN")} ảnh đang được chuẩn bị để tìm.` : "");
  };
  const load = async () => {
    const f = Object.fromEntries(new FormData(form));
    setParams("#/events", { ...f, offset: offset || "" });
    const [stats, data] = await Promise.all([api("/api/stats"), api(`/api/events?${new URLSearchParams({ ...f, limit: PAGE_SIZE, offset })}`)]);
    document.getElementById("stats").innerHTML = [
      ["Vùng hôm nay", stats.today_zone_events],
      ["Mặt hôm nay", stats.today_face_events],
      ["Kết quả lọc", data.total],
    ].map(([l, v]) => `<div class="stat"><div class="label">${l}</div><div class="value">${v}</div></div>`).join("");
    const list = document.getElementById("list");
    if (!data.items.length) {
      list.innerHTML = `<div class="empty">${f.q.trim() ? "Không tìm thấy cảnh báo nào khớp mô tả." : "Chưa có cảnh báo nào. Vẽ vùng cảnh báo hoặc bật “Khuôn mặt” ở trang Camera."}</div>`;
      return;
    }
    list.innerHTML = `<div class="grid">${data.items.map(eventCardHtml).join("")}</div>
      <div class="pager">
        <button ${offset === 0 ? "disabled" : ""} data-go="${offset - PAGE_SIZE}">${f.q.trim() ? "‹ Giống hơn" : "‹ Mới hơn"}</button>
        <span>${offset + 1}–${offset + data.items.length} / ${data.total}</span>
        <button ${offset + PAGE_SIZE >= data.total ? "disabled" : ""} data-go="${offset + PAGE_SIZE}">${f.q.trim() ? "Kém giống hơn ›" : "Cũ hơn ›"}</button>
      </div>`;
    list.querySelectorAll(".card").forEach((el, i) => (el.onclick = () => openEvent(data.items[i], load)));
    list.querySelectorAll("[data-go]").forEach((b) => (b.onclick = () => { offset = Math.max(0, Number(b.dataset.go)); load(); window.scrollTo(0, 0); }));
  };
  form.onsubmit = (e) => { e.preventDefault(); offset = 0; showSearchNote(); load(); };
  const auto = document.getElementById("auto");
  // đang tìm theo mô tả thì không tự làm mới, để danh sách không nhảy chỗ
  refreshTimer = setInterval(() => { if (auto.checked && !$modal.open && offset === 0 && !form.q.value.trim()) load(); }, 10000);
  showSearchNote();
  await load();
}

function eventBadge(e) {
  if (e.kind === "zone") return `<span class="badge danger">⚠ ${esc(e.label)}</span>`;
  return e.person_id ? `<span class="badge ok">${esc(e.label)}</span>` : `<span class="badge warn">Người lạ</span>`;
}

function eventCardHtml(e) {
  const dur = e.last_seen - e.ts;
  return `<div class="card">
    <div class="thumb">
      <img class="frame" loading="lazy" src="${img(e.frame_img)}" alt="">
      <img class="crop ${e.kind === "face" ? "face" : ""}" loading="lazy" src="${img(e.img)}" alt="">
    </div>
    <div class="body">
      <div class="row">${eventBadge(e)}${e.notified ? `<span class="badge accent" title="Đã gửi Telegram">Telegram</span>` : e.note ? `<span class="badge" title="${esc(e.note)}">Không gửi</span>` : ""}</div>
      <div class="meta"><b>${esc(e.camera_name || e.camera)}</b>${e.zone_name ? ` · vùng ${esc(e.zone_name)}` : e.kind === "zone" ? " · toàn khung hình" : ""} · ${fmtTime(e.ts)}${dur >= 60 ? `–${fmtTime(e.last_seen)}` : ""} · ${fmtDate(e.ts)}</div>
    </div>
  </div>`;
}

async function openEvent(e, onChange) {
  const people = e.kind === "face" ? await api("/api/people") : [];
  const dur = e.last_seen - e.ts;
  $modalBody.innerHTML = `
    <div class="modal-head">
      <span>${eventBadge(e)}</span>
      <button class="icon-btn" id="m-close" aria-label="Đóng">×</button>
    </div>
    <div class="modal-content">
      <div><img class="frame" src="${img(e.frame_img)}" alt="Ảnh toàn cảnh"></div>
      <div class="modal-side">
        <img class="crop ${e.kind === "face" ? "face" : ""}" src="${img(e.img)}" alt="">
        <dl class="kv">
          <dt>Loại</dt><dd>${e.kind === "zone" ? "Đi vào vùng cảnh báo" : "Khuôn mặt"}</dd>
          ${e.kind === "zone" ? `<dt>Vùng</dt><dd>${e.zone_name ? esc(e.zone_name) : "Toàn khung hình"}</dd>` : ""}
          <dt>Camera</dt><dd>${esc(e.camera_name || e.camera)}</dd>
          <dt>Thời gian</dt><dd>${fmtTimeSec(e.ts)} ${fmtDate(e.ts)}</dd>
          ${dur >= 1 ? `<dt>Thấy đến</dt><dd>${fmtTimeSec(e.last_seen)} (${fmtDuration(dur)})</dd>` : ""}
          <dt>${e.kind === "face" ? "Độ giống" : "Độ tin cậy"}</dt><dd>${Math.round(e.score * 100)}%</dd>
          <dt>Telegram</dt><dd>${e.notified ? "Đã gửi" : `Không gửi${e.note ? ` — ${esc(e.note)}` : ""}`}</dd>
        </dl>
        ${e.kind === "face" && e.has_face ? `
        <form id="m-assign" class="assign">
          <div class="sub" style="margin:0">${e.person_id ? "Nhận sai? Gán cho người khác:" : "Biết người này? Gán tên để lần sau tự nhận ra:"}</div>
          <select name="person"><option value="">— Chọn người quen —</option>${people.map((pp) => `<option value="${pp.id}" ${pp.id === e.person_id ? "selected" : ""}>${esc(pp.name)}</option>`).join("")}</select>
          <input name="newname" placeholder="hoặc nhập tên người mới">
          <button class="primary">Gán</button>
        </form>` : ""}
        <div class="actions">
          ${e.person_id ? `<a href="#/people?person=${e.person_id}"><button type="button">Xem người này</button></a>` : ""}
          ${videoLink(e.camera, e.ts)}
          <button class="danger" id="m-del" type="button">Xoá</button>
        </div>
      </div>
    </div>`;
  if (!$modal.open) $modal.showModal();
  const close = () => $modal.close();
  document.getElementById("m-close").onclick = close;
  $modalBody.querySelectorAll("a").forEach((a) => (a.onclick = close));
  const assign = document.getElementById("m-assign");
  if (assign) assign.onsubmit = async (ev) => {
    ev.preventDefault();
    const f = assign.elements;
    if (!f.person.value && !f.newname.value.trim()) { toast("Chọn người hoặc nhập tên"); return; }
    try {
      const r = await api(`/api/events/${e.id}/assign`, { method: "POST", body: JSON.stringify({ person_id: f.person.value ? Number(f.person.value) : null, name: f.newname.value }) });
      toast(`Đã gán cho ${r.name}${r.relabeled > 1 ? ` (và ${r.relabeled - 1} lần thấy giống khác)` : ""}`);
      close();
      onChange?.();
    } catch (err) { toast(err.message); }
  };
  document.getElementById("m-del").onclick = async () => {
    if (!confirm("Xoá cảnh báo này? Ảnh cũng sẽ bị xoá.")) return;
    await api(`/api/events/${e.id}`, { method: "DELETE" });
    toast("Đã xoá");
    close();
    onChange?.();
  };
}

// ---------- Khuôn mặt ----------
async function viewPeople() {
  const p = readParams();
  const [people, unknown] = await Promise.all([api("/api/people"), api("/api/events?unknown=true&limit=36")]);
  $view.innerHTML = `
    <h1>Khuôn mặt</h1>
    <p class="sub">Thêm người quen kèm 3–5 ảnh rõ mặt (chính diện, đủ sáng) để hệ thống gọi đúng tên. Có thể gán trực tiếp từ “Người lạ gần đây”.</p>
    <form class="toolbar" id="add">
      <input name="pname" placeholder="Tên người quen, vd: Bố" required>
      <button class="primary">Thêm người</button>
      <span class="grow"></span>
      <span class="sub" style="margin:0">Bật “Khuôn mặt” cho camera ở trang Camera</span>
    </form>
    <div id="people">${people.length ? `<div class="people-grid">${people.map((pp) => `
      <div class="person" data-id="${pp.id}">
        ${pp.face_img ? `<img src="${img(pp.face_img)}" alt="">` : `<div class="avatar">${esc(pp.name.slice(0, 1).toUpperCase())}</div>`}
        <div>
          <div class="name">${esc(pp.name)} ${pp.notify ? `<span class="badge accent">Theo dõi</span>` : ""}</div>
          <div class="meta">${pp.faces} ảnh mẫu${pp.faces < 3 ? ` <span class="badge warn">nên thêm ảnh</span>` : ""}</div>
          <div class="meta">${pp.last_seen ? `thấy ${ago(pp.last_seen)} · ${pp.events} lần` : "chưa thấy lần nào"}</div>
        </div>
      </div>`).join("")}</div>` : `<div class="empty">Chưa có người quen nào.</div>`}</div>
    <div class="panel" style="margin-top:16px">
      <h2>Người lạ gần đây</h2>
      ${unknown.items.length ? `<div class="face-strip">${unknown.items.map((e, i) => `
        <button type="button" class="face-tile" data-i="${i}" title="${esc(camName(e.camera))} · ${fmtDateTime(e.ts)}">
          <img loading="lazy" src="${img(e.img)}" alt=""><span>${fmtTime(e.ts)} ${fmtDate(e.ts).slice(0, 5)}</span>
        </button>`).join("")}</div>` : `<div class="sub" style="margin:0">Chưa có khuôn mặt lạ nào.</div>`}
    </div>`;
  document.getElementById("add").onsubmit = async (e) => {
    e.preventDefault();
    const r = await api("/api/people", { method: "POST", body: JSON.stringify({ name: e.target.elements.pname.value }) });
    await viewPeople();
    openPerson(r.id);
  };
  $view.querySelectorAll(".person").forEach((el) => (el.onclick = () => openPerson(Number(el.dataset.id))));
  $view.querySelectorAll(".face-tile").forEach((b) => (b.onclick = () => openEvent(unknown.items[b.dataset.i], viewPeople)));
  if (p.person) { history.replaceState(null, "", "#/people"); openPerson(Number(p.person)); }
}

async function openPerson(pid) {
  const [people, faces, events] = await Promise.all([
    api("/api/people"), api(`/api/people/${pid}/faces`), api(`/api/events?person_id=${pid}&limit=12`),
  ]);
  const pp = people.find((x) => x.id === pid);
  if (!pp) return;
  $modalBody.innerHTML = `
    <div class="modal-head">
      <b>${esc(pp.name)}</b>
      <button class="icon-btn" id="m-close" aria-label="Đóng">×</button>
    </div>
    <div class="person-modal">
      <section>
        <h2>Ảnh mẫu (${faces.length})</h2>
        <div class="face-strip">${faces.map((f) => `
          <div class="face-tile"><img src="${img(f.img)}" alt=""><button type="button" class="x" data-del="${f.id}" aria-label="Xoá ảnh">×</button></div>`).join("")}
          <label class="face-tile add"><input type="file" accept="image/*" multiple hidden id="m-upload"><span>＋<br>Thêm ảnh</span></label>
        </div>
        <p class="hint">Mỗi ảnh nên có một khuôn mặt rõ. Nếu ảnh có nhiều người, hệ thống lấy mặt lớn nhất.</p>
      </section>
      <form id="m-person" class="zone-form">
        <label>Tên<input name="pname" value="${esc(pp.name)}" required></label>
        <label>Ghi chú<input name="note" value="${esc(pp.note)}" placeholder="vd: người nhà, giao hàng…"></label>
        <label class="check"><input type="checkbox" name="notify" ${pp.notify ? "checked" : ""}> Theo dõi: gửi Telegram mỗi khi thấy (cột “Đối tượng cụ thể” trong Cài đặt)</label>
        <div class="actions"><button class="primary">Lưu</button><button type="button" class="danger" id="m-del">Xoá người này</button></div>
      </form>
      <section>
        <h2>Lần thấy gần đây</h2>
        ${events.items.length ? `<div class="face-strip">${events.items.map((e, i) => `
          <button type="button" class="face-tile" data-ev="${i}"><img loading="lazy" src="${img(e.img)}" alt=""><span>${esc(camName(e.camera))}<br>${fmtTime(e.ts)} ${fmtDate(e.ts).slice(0, 5)}</span></button>`).join("")}</div>`
          : `<div class="sub" style="margin:0">Chưa thấy lần nào.</div>`}
      </section>
    </div>`;
  if (!$modal.open) $modal.showModal();
  const refresh = () => openPerson(pid);
  const closeAndReload = () => { $modal.close(); if (location.hash.startsWith("#/people")) viewPeople(); };
  document.getElementById("m-close").onclick = closeAndReload;
  document.getElementById("m-upload").onchange = async (e) => {
    let ok = 0;
    for (const file of e.target.files) {
      try {
        await api(`/api/people/${pid}/faces`, { method: "POST", body: file, headers: { "Content-Type": file.type || "application/octet-stream" } });
        ok++;
      } catch (err) { toast(`${file.name}: ${err.message}`); await new Promise((r) => setTimeout(r, 1500)); }
    }
    if (ok) toast(`Đã thêm ${ok} ảnh mẫu`);
    refresh();
  };
  $modalBody.querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => {
    if (!confirm("Xoá ảnh mẫu này?")) return;
    await api(`/api/faces/${b.dataset.del}`, { method: "DELETE" });
    refresh();
  }));
  $modalBody.querySelectorAll("[data-ev]").forEach((b) => (b.onclick = () => openEvent(events.items[b.dataset.ev], refresh)));
  document.getElementById("m-person").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target.elements;
    await api(`/api/people/${pid}`, { method: "PATCH", body: JSON.stringify({ name: f.pname.value, note: f.note.value, notify: f.notify.checked }) });
    toast("Đã lưu");
    closeAndReload();
  };
  document.getElementById("m-del").onclick = async () => {
    if (!confirm(`Xoá ${pp.name} và toàn bộ ảnh mẫu? Các lần thấy trước đây sẽ chuyển thành “Người lạ”.`)) return;
    await api(`/api/people/${pid}`, { method: "DELETE" });
    toast("Đã xoá");
    closeAndReload();
  };
}

// ---------- Cài đặt ----------
const NOTIFY_MODES = [
  ["zone", "Vùng cảnh báo", "có người / xe vào vùng đã vẽ (tắt riêng từng vùng trong trình vẽ vùng)"],
  ["camera", "Toàn khung hình", "có đối tượng xuất hiện ở bất kỳ đâu trên camera, không cần vẽ vùng"],
  ["unknown", "Người lạ", "thấy khuôn mặt không có trong danh sách người quen"],
  ["watch", "Đối tượng cụ thể", "thấy người quen / biển số trong danh sách theo dõi bên dưới"],
];
function deviceName(ua) {
  const os = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android"
    : /Mac OS X/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows" : /Linux/.test(ua) ? "Linux" : "Thiết bị khác";
  const app = /EdgA?\//.test(ua) ? "Edge" : /CriOS|Chrome\//.test(ua) ? "Chrome" : /FxiOS|Firefox\//.test(ua) ? "Firefox"
    : /Safari\//.test(ua) ? "Safari" : "";
  return app ? `${os} · ${app}` : os;
}

function aiModeOpts(cur, auto, suggest) {
  return [["off", "Tắt"], ["auto", auto], ["suggest", suggest]]
    .map(([v, l]) => `<option value="${v}" ${v === cur ? "selected" : ""}>${l}</option>`).join("");
}

function aiStatusText(s) {
  const st = s.ai_state || {}, t = s.ai_today;
  if (!s.ai_key_set) return "Chưa nhập API key.";
  if (!s.ai_model) return "Chưa chọn model.";
  const parts = [];
  if (st.at) parts.push(st.ok ? `Hoạt động bình thường (${ago(st.at)})` : `<span class="warn">Lỗi ${ago(st.at)}: ${esc(st.message)}</span>`);
  parts.push(`Hôm nay: ${st.calls || 0} lần gọi, ${(st.tokens || 0).toLocaleString("vi-VN")} token${st.cost ? `, $${st.cost.toFixed(4)}` : ""}`);
  const v = t.visits, pl = t.plates;
  const nv = Object.values(v).reduce((a, b) => a + b, 0), np = Object.values(pl).reduce((a, b) => a + b, 0);
  if (nv) parts.push(`đã xem ${nv} cặp người (tự gộp ${t.merged}, khác người ${v.different || 0}, chưa chắc ${v.unsure || 0})`);
  const np2 = np - (pl.read || 0);
  if (np2) parts.push(`đã đọc lại ${np2} biển (giống OCR ${pl.ok || 0}, tự sửa ${pl.fixed || 0}, khác ${pl.differs || 0})`);
  const cd = t.candidates || {};
  if (pl.read || cd.unreadable) parts.push(`đọc được ${pl.read || 0} biển OCR bỏ sót${cd.unreadable ? `, ${cd.unreadable} biển AI không đọc được` : ""}`);
  if (cd.blurry) parts.push(`bỏ qua ${cd.blurry} biển quá mờ (không gọi AI)`);
  if (cd.parked) parts.push(`bỏ qua ${cd.parked} lần xe đỗ (không gọi AI)`);
  if (s.ai_pending && s.ai_visits !== "off") parts.push(`còn ${s.ai_pending} cặp chờ AI xem`);
  return parts.join(" · ");
}

async function loadSessions() {
  const box = document.getElementById("sessions");
  const list = await api("/api/sessions").catch(() => null);
  if (!box) return;
  if (!list) { box.innerHTML = `<span class="hint">Đăng nhập bằng mật khẩu trực tiếp (không có phiên).</span>`; return; }
  const canLock = (x) => x.user_id && x.username !== me?.username && (me?.is_admin || (me?.perms.includes("users") && !x.is_admin));
  box.innerHTML = list.map((x) => `<div class="session">
      <div><b>${esc(deviceName(x.agent))}</b>${x.current ? ` <span class="badge ok">Thiết bị này</span>` : ""}
        <div class="hint" style="margin:0">${x.username !== me?.username ? `<b>${esc(x.username)}</b> · ` : ""}${x.location ? `${esc(x.location)} · ` : ""}${esc(x.ip)} · mở lần cuối ${esc(ago(x.last_seen))} · đăng nhập ${esc(fmtDate(x.created_at))}</div></div>
      <div class="session-actions">
        <button type="button" class="${x.current ? "" : "danger"}" data-sid="${esc(x.id)}" data-current="${x.current ? 1 : ""}">Đăng xuất</button>
        ${canLock(x) ? `<button type="button" class="danger" data-lock="${x.user_id}" data-name="${esc(x.username)}">Khoá tài khoản</button>` : ""}
      </div>
    </div>`).join("") || `<span class="hint">Không có phiên nào.</span>`;
  box.querySelectorAll("[data-lock]").forEach((b) => (b.onclick = async () => {
    if (!confirm(`Khoá tài khoản ${b.dataset.name}? Người này bị đăng xuất ngay trên mọi thiết bị và không đăng nhập lại được cho đến khi được mở khoá.`)) return;
    try {
      await api(`/api/users/${b.dataset.lock}`, { method: "PATCH", body: JSON.stringify({ disabled: true }) });
    } catch (e) { toast(e.message); return; }
    if (document.getElementById("u-add")) viewUsers(); else loadSessions();
  }));
  box.querySelectorAll("[data-sid]").forEach((b) => (b.onclick = async () => {
    if (!confirm("Đăng xuất thiết bị này?")) return;
    await api(`/api/sessions/${b.dataset.sid}`, { method: "DELETE" });
    if (b.dataset.current) location.replace("/login"); else loadSessions();
  }));
}

const fmtGB = (bytes) => (bytes < 1e9 ? `${Math.round(bytes / 1e6)} MB` : `${(bytes / 1e9).toFixed(bytes >= 1e11 ? 0 : 1)} GB`);
const fmtRecKey = (k) => (k ? `${k.slice(11, 13)}:${k.slice(14, 16)} ${k.slice(8, 10)}/${k.slice(5, 7)}/${k.slice(0, 4)}` : "—");
const recKeyTs = (k) => new Date(`${k.slice(0, 10)}T${k.slice(11, 19).replace(/-/g, ":")}`).getTime() / 1000;

function recSettingsHtml(r, limitGb) {
  const cams = cameras.filter((c) => c.record_enabled || r.cameras?.[c.id]?.files);
  let perDay = 0;
  const rowsHtml = cams.map((c) => {
    const x = r.cameras?.[c.id] || {};
    const span = x.oldest && x.newest ? recKeyTs(x.newest) - recKeyTs(x.oldest) : 0;
    const rate = span > 3600 ? (x.bytes / span) * 86400 : 0;  // byte / ngày, cần ghi đủ 1 giờ mới ước tính
    if (c.record_enabled) perDay += rate;
    return `<tr>
      <td><b>${esc(c.name)}</b><div>${recBadge(c, x) || `<span class="badge">Đã tắt ghi</span>`}</div></td>
      <td>${fmtGB(x.bytes || 0)}${rate ? `<div class="hint" style="margin:0">~${fmtGB(rate)}/ngày</div>` : ""}</td>
      <td>${fmtRecKey(x.oldest)}</td>
      <td>${x.files ? `<a href="#/playback?camera=${encodeURIComponent(c.id)}">Xem lại</a>` : ""}</td>
    </tr>`;
  }).join("");
  const days = perDay ? Math.floor((limitGb * 1e9) / perDay) : null;
  return `
    <div class="zone-form">
      <label>Dung lượng tối đa cho video (GB)<input type="number" name="rec_max_gb" min="1" step="1" value="${Math.round(limitGb)}"></label>
      <p class="hint" style="margin:0">Đầy thì tự xoá video cũ nhất. Đang dùng <b>${fmtGB(r.bytes || 0)}</b>${r.disk_free != null ? `, ổ còn trống ${fmtGB(r.disk_free)}` : ""}.${days != null ? ` Với các camera đang ghi, lưu được khoảng <b>${days} ngày</b>.` : ""}${r.recorder_alive === false && cams.some((c) => c.record_enabled) ? ` <span class="warn">Dịch vụ ghi hình chưa chạy.</span>` : ""}</p>
    </div>
    ${cams.length ? `<div class="table-wrap matrix-wrap" style="margin-top:12px"><table>
      <thead><tr><th>Camera</th><th>Dung lượng</th><th>Lưu từ</th><th></th></tr></thead>
      <tbody>${rowsHtml}</tbody></table></div>`
    : `<p class="hint" style="margin:12px 0 0">Chưa camera nào bật ghi hình. Tick “Ghi hình” ở trang <a href="#/cameras">Camera</a>.</p>`}`;
}

async function viewSettings() {
  const [s, people, rec] = await Promise.all([api("/api/settings"), api("/api/people"), api("/api/recordings").catch(() => ({}))]);
  cameras = await api("/api/cameras");
  const checks = (name, items, selected) => items.map(([v, l]) =>
    `<label class="check"><input type="checkbox" name="${name}" value="${esc(v)}" ${selected.includes(v) ? "checked" : ""}> ${esc(l)}</label>`).join("");
  // điều kiện để từng loại thông báo có dữ liệu trên một camera
  const needs = {
    zone: (c) => (c.zones ? "" : "chưa vẽ vùng"),
    camera: () => "",
    unknown: (c) => (c.face_enabled ? "" : "chưa bật Khuôn mặt"),
    watch: (c) => (c.face_enabled || c.lpr_enabled ? "" : "chưa bật Biển số / Khuôn mặt"),
  };
  const features = (c) => [c.lpr_enabled && "Biển số", c.face_enabled && "Khuôn mặt", c.zones && `${c.zones} vùng`].filter(Boolean);

  $view.innerHTML = `
    <h1>Cài đặt</h1>
    <p class="sub">Cảnh báo được gửi qua Telegram kèm ảnh chụp. Mọi sự kiện vẫn được lưu ở trang Cảnh báo, kể cả khi không gửi.</p>
    <form id="settings">
      <div class="panel settings">
        <h2>Gửi Telegram theo từng camera</h2>
        <dl class="mode-legend">${NOTIFY_MODES.map(([k, l, d]) => `<dt>${l}</dt><dd>${d}</dd>`).join("")}</dl>
        <div class="table-wrap matrix-wrap"><table class="matrix">
          <thead><tr><th>Camera</th>${NOTIFY_MODES.map(([, l]) => `<th>${l}</th>`).join("")}</tr></thead>
          <tbody>${cameras.map((c) => `<tr>
            <td><b>${esc(c.name)}</b><div class="feat">${features(c).map((f) => `<span class="badge">${f}</span>`).join(" ") || `<span class="hint" style="margin:0">chưa bật tính năng</span>`}</div></td>
            ${NOTIFY_MODES.map(([k]) => {
              const on = (s.notify_matrix[c.id] || []).includes(k);
              const warn = needs[k](c);
              return `<td><label class="cell"><input type="checkbox" data-cam="${esc(c.id)}" data-mode="${k}" ${on ? "checked" : ""}>${warn ? `<small class="${on ? "warn" : ""}">${warn}</small>` : ""}</label></td>`;
            }).join("")}
          </tr>`).join("")}</tbody>
        </table></div>
        <p class="hint">Ô có ghi chú vàng: đã bật gửi nhưng camera chưa bật tính năng tương ứng ở trang <a href="#/cameras">Camera</a> nên sẽ chưa có gì để gửi.</p>
      </div>
      <div class="panel settings">
        <h2>Thời gian gửi</h2>
        <p class="hint" style="margin:0 0 10px">Ngoài khung giờ hoặc đang trong thời gian chờ thì không gửi Telegram, nhưng sự kiện vẫn được ghi vào trang Cảnh báo. Vùng cảnh báo có khung giờ / thời gian chờ riêng trong trình vẽ vùng.</p>
        <div class="table-wrap matrix-wrap"><table class="times">
          <thead><tr><th>Loại</th><th>Gửi từ</th><th>đến</th><th>Không gửi lặp lại trong (giây)</th></tr></thead>
          <tbody>${[["camera", "Toàn khung hình", "mỗi camera"], ["unknown", "Người lạ", "mỗi camera"], ["watch", "Đối tượng cụ thể", "mỗi người / biển trên mỗi camera"]].map(([k, l, per]) => `<tr>
            <td><b>${l}</b><div class="hint" style="margin:0">tính riêng ${per}</div></td>
            <td><input type="time" name="${k}_start" value="${esc(s.times[k].start)}" aria-label="${l}: gửi từ"></td>
            <td><input type="time" name="${k}_end" value="${esc(s.times[k].end)}" aria-label="${l}: đến"></td>
            <td><input type="number" name="${k}_cooldown" min="0" max="86400" value="${s.times[k].cooldown}" aria-label="${l}: thời gian chờ"></td>
          </tr>`).join("")}</tbody>
        </table></div>
        <div class="zone-form" style="margin-top:12px">
          <div class="field"><span class="hint" style="margin:0">“Toàn khung hình” báo khi xuất hiện</span><div class="wrap">${checks("cls", ZONE_CLASSES, s.camera_alert_classes)}</div></div>
        </div>
      </div>
      <div class="panel settings">
        <h2>Tần suất quét</h2>
        <div class="zone-form">
          <label>Khi không có chuyển động, vẫn quét mỗi (giây)<input type="number" name="scan_interval" min="2" max="60" value="${s.scan_interval}"></label>
          <p class="hint" style="margin:0">Khi có chuyển động hệ thống luôn quét 2 lần/giây. Quét định kỳ thưa hơn thì đỡ tốn CPU nhưng người đứng yên lâu có thể bị tính thành nhiều lượt.</p>
        </div>
      </div>
      <div class="panel settings">
        <h2>Người qua lại</h2>
        <div class="zone-form">
          <label>Nhận ra lại người đã gặp trong vòng (giờ)
            <span class="edit-row">
              <input type="number" name="reid_window_hours" min="1" max="168" value="${s.reid_window_hours}" list="reid-presets">
              <datalist id="reid-presets"><option value="12"><option value="24"><option value="48"><option value="72"><option value="168"></datalist>
            </span>
          </label>
          <p class="hint" style="margin:0">Người mới xuất hiện chỉ được so với những người đã thấy trong khoảng này, tối đa 168 giờ (7 ngày). Hệ thống nhận ra người chủ yếu qua quần áo, mũ, xe: để dài thì nhận ra được người quay lại hôm sau nếu mặc giống, nhưng dễ gộp nhầm những người lạ mặc giống nhau hơn. Mặc định 12 giờ. Người quen có khuôn mặt trong trang <a href="#/people">Khuôn mặt</a> thì luôn được nhận ra, không phụ thuộc khoảng này.</p>
        </div>
      </div>
      <div class="panel settings">
        <h2>Ghi hình</h2>
        ${recSettingsHtml(rec, s.rec_max_gb)}
      </div>
      <div class="panel settings">
        <h2>Danh sách “Đối tượng cụ thể”</h2>
        <div class="zone-form">
          <div class="field"><span class="hint" style="margin:0">Người quen</span><div class="wrap">${people.length
            ? checks("person", people.map((p) => [String(p.id), p.name]), s.watch_people.map(String))
            : `<span class="hint" style="margin:0">Chưa có người quen nào. Thêm ở trang <a href="#/people">Khuôn mặt</a>.</span>`}</div></div>
          <label>Biển số (mỗi dòng một biển)<textarea name="plates" rows="3" placeholder="30A-123.45&#10;29X1-678.90">${esc(s.watch_plates.join("\n"))}</textarea></label>
        </div>
      </div>
      <div class="panel settings">
        <h2>Kết nối Telegram</h2>
        <ol class="steps">
          <li>Trong Telegram, mở <b>@BotFather</b>, gửi <code>/newbot</code>, đặt tên cho bot. BotFather sẽ gửi lại một <b>token</b>.</li>
          <li>Dán token vào ô dưới rồi bấm <b>Lưu</b>.</li>
          <li>Mở bot vừa tạo, bấm <b>Start</b> (hoặc thêm bot vào nhóm gia đình rồi gửi một tin bất kỳ trong nhóm).</li>
          <li>Bấm <b>Tự lấy chat ID</b>, sau đó bấm <b>Gửi thử</b>.</li>
        </ol>
        <div class="zone-form">
          <label>Token bot<input name="token" type="password" autocomplete="off" placeholder="${s.telegram_token_set ? `Đã lưu (${esc(s.telegram_token_hint)}) — nhập để thay` : "123456789:AA…"}"></label>
          <label>Chat ID<input name="chat" value="${esc(s.telegram_chat_id)}" placeholder="Bấm “Tự lấy chat ID”"></label>
          <div class="actions">
            <button type="button" id="tg-detect">Tự lấy chat ID</button>
            <button type="button" id="tg-test">Gửi thử</button>
            ${s.telegram_token_set ? `<button type="button" class="danger" id="tg-clear">Xoá token</button>` : ""}
          </div>
        </div>
      </div>
      <div class="panel settings">
        <h2>Trợ lý AI</h2>
        <p class="hint" style="margin:0 0 10px">Nhờ AI xem lại những chỗ hệ thống trên máy chưa chắc. Chỉ gửi ảnh biển số / ảnh cắt người lên nhà cung cấp AI, không gửi video.</p>
        <div class="zone-form">
          <label>API key<input name="ai_key" type="password" autocomplete="off" placeholder="${s.ai_key_set ? `Đã lưu (${esc(s.ai_key_hint)}) — nhập để thay` : "sk-or-…"}"></label>
          <p class="hint" style="margin:0">Tạo key OpenRouter ở <a href="https://openrouter.ai/settings/keys" target="_blank" rel="noopener">openrouter.ai/settings/keys</a>, nên đặt giới hạn chi tiêu cho key đó.</p>
          <label>Địa chỉ API<input name="ai_base_url" value="${esc(s.ai_base_url)}" inputmode="url"></label>
          <label>Model so người (phải đọc được ảnh)
            <span class="edit-row">
              <select name="ai_model" id="ai-model">${s.ai_model ? `<option value="${esc(s.ai_model)}">${esc(s.ai_model)}</option>` : `<option value="">— Bấm “Tải danh sách” —</option>`}</select>
              <button type="button" id="ai-load">Tải danh sách</button>
            </span>
          </label>
          <label>Model đọc biển số
            <select name="ai_plate_model" id="ai-plate-model"><option value="">— Dùng chung model so người —</option>${s.ai_plate_model ? `<option value="${esc(s.ai_plate_model)}" selected>${esc(s.ai_plate_model)}</option>` : ""}</select>
          </label>
          <p class="hint" style="margin:0">Biển xe máy nhỏ, chụp chéo cần model đọc chữ tốt hơn: thử thực tế Gemini 3.1 Flash Lite đọc đúng biển mà GLM Flash không đọc chắc được.</p>
          <h3 class="sub-h">AI dùng cho việc gì</h3>
          <label class="check"><input type="checkbox" name="ai_read_missed" ${s.ai_read_missed ? "checked" : ""}> Đọc biển số máy thấy nhưng không đọc ra (xe chạy nhanh, biển nhỏ / nghiêng)</label>
          <label>Chỉ gửi AI khi máy đọc được biển từ
            <select name="ai_min_ocr">${[[0, "0% (gửi tất cả)"], [0.2, "20%"], [0.3, "30%"], [0.4, "40%"], [0.5, "50%"]].map(([x, t]) => `<option value="${x}" ${Math.abs(s.ai_min_ocr - x) < 0.01 ? "selected" : ""}>${t}</option>`).join("")}</select>
          </label>
          <p class="hint" style="margin:0">Ảnh quá mờ thì AI đọc cũng hay sai mà vẫn tốn tiền. Mức càng cao càng rẻ nhưng bỏ sót nhiều xe chạy nhanh hơn.</p>
          <label>Kiểm tra lại biển số máy đã đọc
            <select name="ai_plates">${aiModeOpts(s.ai_plates, "Tự sửa khi OCR không chắc mà AI chắc chắn", "Chỉ ghi ý kiến AI, tôi tự chọn")}</select>
          </label>
          <label>So người qua lại “có thể trùng”
            <select name="ai_visits">${aiModeOpts(s.ai_visits, "Tự gộp / bỏ gợi ý khi AI chắc chắn", "Chỉ ghi ý kiến AI, tôi tự chọn")}</select>
          </label>
          <p class="hint" style="margin:0">Khuôn mặt và cảnh báo vùng luôn chạy trên máy chủ, không gửi lên AI. Muốn tiết kiệm thì chỉ bật mục đọc biển bỏ sót.</p>
          <div class="actions">
            <button type="button" id="ai-test">Thử AI</button>
            ${s.ai_key_set ? `<button type="button" class="danger" id="ai-clear">Xoá key</button>` : ""}
          </div>
          <div class="hint ai-status" style="margin:0">${aiStatusText(s)}</div>
        </div>
      </div>
      <div class="panel settings">
        <h2>Thiết bị đang đăng nhập</h2>
        <p class="hint" style="margin:0 0 10px">Thiết bị giữ đăng nhập cho đến khi bấm đăng xuất. Mất điện thoại hoặc thấy thiết bị lạ thì đăng xuất thiết bị đó ở đây.</p>
        <div id="sessions" class="sessions"><span class="hint">Đang tải…</span></div>
      </div>
      <div class="save-bar"><button class="primary">Lưu cài đặt</button></div>
    </form>`;
  loadSessions();

  const form = document.getElementById("settings");
  form.querySelectorAll("[data-mode]").forEach((cb) => (cb.onchange = () => cb.parentElement.querySelector("small")?.classList.toggle("warn", cb.checked)));
  const values = (name) => [...form.querySelectorAll(`[name=${name}]:checked`)].map((c) => c.value);
  const save = async () => {
    const f = form.elements;
    const matrix = Object.fromEntries(cameras.map((c) => [c.id, []]));
    form.querySelectorAll("[data-mode]:checked").forEach((cb) => matrix[cb.dataset.cam].push(cb.dataset.mode));
    const body = {
      telegram_chat_id: f.chat.value,
      notify_matrix: matrix, camera_alert_classes: values("cls"),
      scan_interval: Number(f.scan_interval.value || 10),
      reid_window_hours: Number(f.reid_window_hours.value || 12),
      rec_max_gb: Number(f.rec_max_gb.value) || s.rec_max_gb,
      ...Object.fromEntries(["camera", "unknown", "watch"].flatMap((k) => {
        const prefix = k === "camera" ? "camera_alert" : k;
        return [[`${prefix}_start`, f[`${k}_start`].value], [`${prefix}_end`, f[`${k}_end`].value], [`${prefix}_cooldown`, Number(f[`${k}_cooldown`].value || 0)]];
      })),
      watch_people: values("person").map(Number),
      watch_plates: f.plates.value.split(/[\n,;]+/).map((x) => x.trim()).filter(Boolean),
    };
    if (f.token.value.trim()) body.telegram_token = f.token.value.trim();
    Object.assign(body, { ai_base_url: f.ai_base_url.value.trim(), ai_model: f.ai_model.value, ai_plate_model: f.ai_plate_model.value, ai_visits: f.ai_visits.value, ai_plates: f.ai_plates.value, ai_read_missed: f.ai_read_missed.checked, ai_min_ocr: parseFloat(f.ai_min_ocr.value) });
    if (f.ai_key.value.trim()) body.ai_api_key = f.ai_key.value.trim();
    await api("/api/settings", { method: "PUT", body: JSON.stringify(body) });
  };
  const run = async (fn) => { try { await fn(); } catch (err) { toast(err.message); } };
  form.onsubmit = (e) => { e.preventDefault(); run(async () => { await save(); toast("Đã lưu. Worker áp dụng trong khoảng 15 giây."); viewSettings(); }); };
  document.getElementById("tg-detect").onclick = () => run(async () => {
    await save();
    const r = await api("/api/settings/telegram/detect-chat", { method: "POST" });
    form.elements.chat.value = r.chat_id;
    toast(`Đã lấy chat ID của ${r.title || r.chat_id}`);
  });
  document.getElementById("tg-test").onclick = () => run(async () => {
    await save();
    await api("/api/settings/telegram/test", { method: "POST" });
    toast("Đã gửi tin thử, kiểm tra Telegram");
  });
  document.getElementById("ai-load").onclick = () => run(async () => {
    await save();
    const models = await api("/api/settings/ai/models");
    const price = (m) => (m.price_in != null ? ` · vào $${m.price_in.toFixed(2)} / ra $${m.price_out.toFixed(2)} mỗi 1M token` : "");
    const opts = (cur) => models.map((m) => `<option value="${esc(m.id)}" ${m.id === cur ? "selected" : ""}>${esc(m.id)}${price(m)}</option>`).join("");
    const sel = document.getElementById("ai-model"), psel = document.getElementById("ai-plate-model");
    sel.innerHTML = `<option value="">— Chọn model —</option>` + opts(sel.value);
    psel.innerHTML = `<option value="">— Dùng chung model so người —</option>` + opts(psel.value);
    toast(models.some((m) => m.vision === null) ? `${models.length} model — chọn một model rồi bấm “Thử AI” để chắc nó đọc được ảnh` : `${models.length} model đọc được ảnh`);
  });
  document.getElementById("ai-test").onclick = () => run(async () => {
    await save();
    const r = await api("/api/settings/ai/test", { method: "POST" });
    alert(`AI trả lời sau ${r.seconds} giây (${r.tokens} token${r.cost ? `, $${r.cost.toFixed(5)}` : ""})${r.with_image ? ", đã gửi kèm ảnh biển số gần nhất" : ""}:\n\n${r.reply}`);
  });
  const aiClear = document.getElementById("ai-clear");
  if (aiClear) aiClear.onclick = () => run(async () => {
    if (!confirm("Xoá API key? AI sẽ ngừng xem lại.")) return;
    await api("/api/settings", { method: "PUT", body: JSON.stringify({ ai_api_key: "" }) });
    viewSettings();
  });
  const clear = document.getElementById("tg-clear");
  if (clear) clear.onclick = () => run(async () => {
    if (!confirm("Xoá token Telegram? Sẽ không gửi cảnh báo nữa.")) return;
    await api("/api/settings", { method: "PUT", body: JSON.stringify({ telegram_token: "" }) });
    viewSettings();
  });
}

// ---------- xem lại video ghi hình ----------
// Video là các file 1 phút; thanh thời gian bên dưới vẽ cả ngày, kéo sang trái / phải để tua như app camera,
// hết một file thì tự phát file kế tiếp.
const PB_SPANS = [[86400, "24 giờ"], [7200, "2 giờ"], [1200, "20 phút"]];
const PB_SPEEDS = [1, 2, 4, 8, 16];
const dayStartTs = (date) => new Date(`${date}T00:00:00`).getTime() / 1000;
const shiftDate = (date, days) => { const d = new Date(`${date}T12:00:00`); d.setDate(d.getDate() + days); return isoDate(d); };

// mức thay đổi trong phút (recorder.py đo % khung hình đổi rõ): 0 gần như đứng yên … 3 thay đổi nhiều
const motionLevel = (m) => (m == null ? 0 : m >= 10 ? 3 : m >= 4 ? 2 : m >= 1.5 ? 1 : 0);

async function viewPlayback() {
  const p = readParams();
  if (!cameras.length) {
    $view.innerHTML = `<h1>Xem lại</h1><div class="empty">Chưa có camera nào. Thêm camera ở trang <a href="#/cameras">Camera</a>.</div>`;
    return;
  }
  // camera không bật ghi hình vẫn mở được ở đây để xem trực tiếp màn hình to (không có thanh thời gian)
  const recording = (id) => !!cameras.find((c) => c.id === id)?.record_enabled;
  let cam = cameras.some((c) => c.id === p.camera) ? p.camera : (cameras.find((c) => c.record_enabled) || cameras[0]).id;
  let pos = p.t ? Number(p.t) : null;                 // thời điểm đang xem (unix giây)
  let date = pos ? dayKey(pos) : p.date || isoDate(new Date());
  let span = 7200, speed = 1, muted = true;
  let segs = [], cur = -1, pendingOffset = null, waitingLive = false, drag = null;
  let swap = null, swapTimer = null;  // đang chuẩn bị chuyển sang thẻ video kia
  let listHour = null;                 // null = lưới các giờ, số = lưới các phút của giờ đó
  let selecting = false;               // admin đang chọn video để xoá
  const picked = new Set();            // tên file đã chọn
  let live = null, liveTimer = null;   // đang xem trực tiếp: { player }

  $view.innerHTML = `
    <h1>${prevPage ? `<button type="button" class="back-btn" id="pb-return" aria-label="Quay lại trang trước">‹</button>` : ""}Xem lại</h1>
    <div class="toolbar pb-bar">
      <select id="pb-cam" aria-label="Camera">${cameras.map((c) => `<option value="${esc(c.id)}" ${c.id === cam ? "selected" : ""}>${esc(c.name)}${c.record_enabled ? "" : " (chỉ trực tiếp)"}</option>`).join("")}</select>
      <button type="button" id="pb-live" class="pb-live-btn" title="Xem trực tiếp luồng camera">Trực tiếp</button>
      <span class="pb-day">
        <button type="button" id="pb-prev" aria-label="Ngày trước">‹</button>
        <input type="date" id="pb-date" value="${date}" aria-label="Ngày">
        <button type="button" id="pb-next" aria-label="Ngày sau">›</button>
      </span>
    </div>
    <div class="pb" id="pb">
      <div class="pb-screen" id="pb-screen">
        <video class="on" playsinline muted preload="auto"></video>
        <video playsinline muted preload="auto"></video>
        <video class="pb-live-v" playsinline muted></video>
        <div class="pb-clock" id="pb-clock"></div>
        <div class="pb-msg" id="pb-msg"></div>
      </div>
      <div class="pb-controls">
        <button type="button" id="pb-back" title="Lùi 10 giây">−10s</button>
        <button type="button" id="pb-play" class="primary" aria-label="Phát / tạm dừng">▶</button>
        <button type="button" id="pb-fwd" title="Tới 10 giây">+10s</button>
        <button type="button" id="pb-speed" title="Tốc độ phát">1×</button>
        <span class="grow"></span>
        <button type="button" id="pb-talk" class="pb-talk" title="Nói qua loa camera" hidden>🎙</button>
        <button type="button" id="pb-mute" title="Bật / tắt tiếng">🔇</button>
        <a id="pb-dl" download title="Tải đoạn 1 phút đang xem"><button type="button">⤓</button></a>
        <button type="button" id="pb-full" title="Toàn màn hình">⛶</button>
      </div>
      <div class="pb-timeline" id="pb-tl"><canvas></canvas><div class="pb-needle"></div>
        <button type="button" class="pb-zoom" id="pb-zoom" title="Độ dài thanh thời gian">2 giờ</button></div>
      <p class="hint pb-norec">Camera này không bật ghi hình nên chỉ xem trực tiếp. Muốn xem lại thì vào trang <a href="#/cameras">Camera</a>, tick “Ghi hình” rồi bấm Lưu.</p>
      <p class="hint pb-hint">Kéo thanh thời gian sang trái / phải để tua, chạm vào một chỗ để nhảy tới đó. Vạch xanh là khoảng có video, vạch cam / đỏ là phút có nhiều thay đổi.</p>
      <section class="pb-list">
        <div class="pb-list-head">
          <button type="button" id="pb-list-back" class="ghost" hidden>‹ Các giờ</button>
          <h2 id="pb-list-title"></h2>
          <span class="grow"></span>
          ${me?.is_admin ? `<button type="button" id="pb-sel" class="ghost">Chọn để xoá</button>` : ""}
        </div>
        <div class="pb-selbar" id="pb-selbar" hidden>
          <span id="pb-sel-n"></span>
          <span class="grow"></span>
          <button type="button" id="pb-sel-all"></button>
          <button type="button" id="pb-sel-del" class="danger" disabled>Xoá</button>
          <button type="button" id="pb-sel-cancel">Huỷ</button>
        </div>
        <div class="pb-grid" id="pb-grid"></div>
      </section>
    </div>`;

  const $ = (id) => document.getElementById(id);
  const tl = $("pb-tl"), canvas = tl.querySelector("canvas"), msg = $("pb-msg");
  // 2 thẻ video luân phiên: thẻ ẩn tải sẵn phút kế tiếp, tới lúc chuyển thì phát thẻ ẩn rồi mới hiện nó lên,
  // nên giữa 2 phút không bị màn hình đen như khi đổi file trên cùng một thẻ
  const vids = [...$("pb-screen").querySelectorAll("video")];
  let video = vids[0];
  const spare = () => (vids[0] === video ? vids[1] : vids[0]);
  const liveV = $("pb-screen").querySelector(".pb-live-v");
  const shown = () => (live ? liveV : video);
  const isToday = () => date === isoDate(new Date());
  const day0 = () => dayStartTs(date);
  const clampDay = (t) => Math.min(Math.max(t, day0()), day0() + 86399, Date.now() / 1000);
  const showMsg = (text) => { msg.textContent = text; msg.classList.toggle("show", !!text); };
  const fileUrl = (s) => `/rec/${encodeURIComponent(cam)}/${date}/${s.f}`;
  const thumbUrl = (s) => `/rec/${encodeURIComponent(cam)}/${date}/${s.f.slice(0, -4)}.jpg`;
  const hourOf = (s) => new Date(s.t * 1000).getHours();

  // ranh giới các khoảng có video liên tục, để vẽ thanh thời gian
  const ranges = () => {
    const out = [];
    for (const s of segs) {
      const last = out[out.length - 1];
      if (last && s.t - last[1] < 5) last[1] = s.t + s.d;
      else out.push([s.t, s.t + s.d]);
    }
    return out;
  };

  function draw() {
    const w = tl.clientWidth, h = tl.clientHeight, dpr = window.devicePixelRatio || 1;
    if (!w) return;
    if (canvas.width !== Math.round(w * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const css = getComputedStyle(document.documentElement);
    const color = (v) => css.getPropertyValue(v).trim();
    const center = pos ?? day0() + 43200;
    const t0 = center - span / 2, k = w / span;
    const X = (t) => (t - t0) * k;
    // ngoài ngày đang xem: tô xám
    ctx.fillStyle = color("--surface-2");
    if (X(day0()) > 0) ctx.fillRect(0, 0, X(day0()), h);
    if (X(day0() + 86400) < w) ctx.fillRect(X(day0() + 86400), 0, w - X(day0() + 86400), h);
    // khoảng có video
    ctx.fillStyle = color("--accent");
    for (const [a, b] of ranges()) {
      if (b < t0 || a > t0 + span) continue;
      ctx.fillRect(X(a), 8, Math.max(2, X(b) - X(a)), h - 34);
    }
    // phút có nhiều thay đổi: vạch cam / đỏ ở đáy dải video
    for (const s of segs) {
      const lv = motionLevel(s.m);
      if (lv < 2 || s.t + s.d < t0 || s.t > t0 + span) continue;
      ctx.fillStyle = color(lv === 3 ? "--danger" : "--warn");
      ctx.fillRect(X(s.t), h - 33, Math.max(2, X(s.t + s.d) - X(s.t)), 7);
    }
    // vạch chia và giờ
    const [minor, major] = span >= 86400 ? [3600, 3 * 3600] : span >= 7200 ? [600, 1800] : [60, 300];
    ctx.fillStyle = color("--muted");
    ctx.strokeStyle = color("--muted");
    ctx.font = "11px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
    ctx.textAlign = "center";
    const first = day0() + Math.ceil((t0 - day0()) / minor) * minor;
    for (let t = first; t <= t0 + span; t += minor) {
      const x = Math.round(X(t)) + 0.5, isMajor = Math.round(t - day0()) % major === 0;
      ctx.globalAlpha = isMajor ? 0.9 : 0.4;
      ctx.beginPath(); ctx.moveTo(x, h - 24); ctx.lineTo(x, h - (isMajor ? 15 : 19)); ctx.stroke();
      if (isMajor) ctx.fillText(fmtTime(t), x, h - 3);
    }
    ctx.globalAlpha = 1;
    $("pb-clock").textContent = live ? `Trực tiếp · ${fmtTimeSec(Date.now() / 1000)}`
      : pos ? `${fmtTimeSec(pos)} · ${fmtDate(pos)}` : fmtDate(day0());
    $("pb-clock").classList.toggle("live", !!live);
  }

  // ---- lưới ảnh thu nhỏ: các giờ trong ngày -> các phút trong giờ ----
  const thumbHtml = (s, lv) => `<span class="pb-thumb">${s.th
    ? `<img src="${thumbUrl(s)}" alt="" loading="lazy" decoding="async">`
    : `<span class="pb-noimg">Đang tạo ảnh…</span>`}${lv ? `<span class="pb-lv lv${lv}"></span>` : ""}</span>`;
  let listSig = "";
  // các file trong giờ đang mở (hoặc cả ngày ở lưới các giờ): dùng cho nút chọn / bỏ chọn tất cả
  const visibleFiles = () => segs.filter((s) => listHour == null || hourOf(s) === listHour).map((s) => s.f);
  function renderSelbar() {
    $("pb-selbar").hidden = !selecting;
    $("pb-grid").classList.toggle("selecting", selecting);
    if ($("pb-sel")) $("pb-sel").hidden = selecting || !segs.length;
    if (!selecting) return;
    const all = visibleFiles(), allOn = all.length > 0 && all.every((f) => picked.has(f));
    $("pb-sel-n").textContent = picked.size ? `Đã chọn ${picked.size} video` : "Chạm vào video để chọn";
    $("pb-sel-all").textContent = allOn ? "Bỏ chọn hết" : listHour == null ? "Chọn cả ngày" : "Chọn cả giờ";
    $("pb-sel-all").disabled = !all.length;
    $("pb-sel-del").disabled = !picked.size;
  }
  function renderList() {
    const grid = $("pb-grid"), title = $("pb-list-title");
    listSig = `${date}|${segs.length}|${segs.filter((s) => s.th).length}`;
    $("pb-list-back").hidden = listHour == null;
    renderSelbar();
    if (!segs.length) { title.textContent = ""; grid.innerHTML = ""; return; }
    if (listHour == null) {
      const hours = new Map();
      segs.forEach((s, i) => { const h = hourOf(s); if (!hours.has(h)) hours.set(h, []); hours.get(h).push(i); });
      title.textContent = "Theo giờ";
      patchGrid(grid, [...hours].map(([h, idx]) => {
        // ảnh đại diện của giờ: phút thay đổi nhiều nhất (đã có ảnh)
        const withImg = idx.filter((i) => segs[i].th);
        const rep = withImg.reduce((a, b) => ((segs[b].m ?? -1) > (segs[a].m ?? -1) ? b : a), withImg[0] ?? idx[0]);
        const busy = idx.filter((i) => motionLevel(segs[i].m) >= 2).length;
        // đang chọn: dấu tick góc trái chọn / bỏ cả giờ, chạm chỗ khác thì mở giờ để chọn từng phút
        const n = selecting ? idx.filter((i) => picked.has(segs[i].f)).length : 0;
        const check = selecting ? `<span class="pb-check ${n === idx.length ? "on" : n ? "some" : ""}" data-check
          title="Chọn / bỏ chọn cả giờ">${n && n < idx.length ? n : "✓"}</span>` : "";
        return `<button type="button" class="pb-card${n === idx.length && selecting ? " picked" : ""}" data-hour="${h}">${thumbHtml(segs[rep], 0)}${check}
          ${busy ? `<span class="pb-busy" title="Số phút có nhiều thay đổi">${busy} phút</span>` : ""}
          <span class="pb-card-label">${pad(h)}:00</span></button>`;
      }));
    } else {
      const idx = segs.map((_, i) => i).filter((i) => hourOf(segs[i]) === listHour);
      title.textContent = `${pad(listHour)}:00 – ${pad(listHour)}:59`;
      patchGrid(grid, idx.map((i) => {
        const s = segs[i];
        const on = selecting && picked.has(s.f);
        return `<button type="button" class="pb-card${on ? " picked" : ""}" data-i="${i}"
          title="${s.m == null ? "" : `Thay đổi ${s.m}% khung hình`}">${thumbHtml(s, motionLevel(s.m))}${selecting ? `<span class="pb-check${on ? " on" : ""}">✓</span>` : ""}
          <span class="pb-card-label">${fmtTime(s.t)}</span></button>`;
      }));
    }
    markCurrent();
  }
  // chỉ thay các ô đổi nội dung (ảnh mới làm xong, phút mới): lưới không nháy, không mất chỗ đang chạm
  function patchGrid(grid, cards) {
    const tpl = document.createElement("template");
    cards.forEach((html, i) => {
      const old = grid.children[i];
      if (old && old.dataset.html === html) return;
      tpl.innerHTML = html.trim();
      const el = tpl.content.firstElementChild;
      el.dataset.html = html;
      if (old) old.replaceWith(el);
      else grid.append(el);
    });
    while (grid.children.length > cards.length) grid.lastElementChild.remove();
  }
  const markCurrent = () => {
    for (const b of $("pb-grid").querySelectorAll(".pb-card[data-i]")) b.classList.toggle("cur", Number(b.dataset.i) === cur);
  };
  $("pb-grid").onclick = (e) => {
    const b = e.target.closest(".pb-card");
    if (!b) return;
    if (selecting && (b.dataset.i != null || e.target.closest("[data-check]"))) {
      const files = b.dataset.i != null ? [segs[Number(b.dataset.i)].f]
        : segs.filter((s) => hourOf(s) === Number(b.dataset.hour)).map((s) => s.f);
      const on = !files.every((f) => picked.has(f));
      for (const f of files) on ? picked.add(f) : picked.delete(f);
      return renderList();
    }
    if (b.dataset.hour != null) {
      listHour = Number(b.dataset.hour);
      renderList();
      $("pb-list-title").scrollIntoView({ block: "nearest" });
      return;
    }
    seek(segs[Number(b.dataset.i)].t, true);
    const top = $("pb-screen").getBoundingClientRect().top;
    if (top < 0 || top > innerHeight / 2) $("pb-screen").scrollIntoView({ behavior: "smooth", block: "start" });
  };
  $("pb-list-back").onclick = () => { listHour = null; renderList(); };

  // ---- admin xoá video: chọn từng phút (hoặc cả giờ / cả ngày) rồi xoá hẳn khỏi ổ ----
  const endSelect = () => { selecting = false; picked.clear(); renderList(); };
  if ($("pb-sel")) $("pb-sel").onclick = () => { selecting = true; renderList(); };
  $("pb-sel-cancel").onclick = endSelect;
  $("pb-sel-all").onclick = () => {
    const all = visibleFiles(), on = !all.every((f) => picked.has(f));
    for (const f of all) on ? picked.add(f) : picked.delete(f);
    renderList();
  };
  $("pb-sel-del").onclick = async () => {
    const files = [...picked];
    const camLabel = cameras.find((c) => c.id === cam)?.name || cam;
    if (!files.length || !confirm(`Xoá hẳn ${files.length} video (${files.length} phút) của ${camLabel} ngày ${fmtDate(day0())}? Không khôi phục được.`)) return;
    if (cur >= 0 && picked.has(segs[cur].f)) {  // đang phát đúng file sắp xoá: dừng lại trước
      clearTimeout(swapTimer);
      swap = null;
      for (const v of vids) { v.pause(); v.removeAttribute("src"); v.load(); }
      cur = -1;
    }
    const r = await api(`/api/recordings/${encodeURIComponent(cam)}/${date}`, { method: "DELETE", body: JSON.stringify({ files }) });
    toast(`Đã xoá ${r.deleted} video${r.skipped ? `, bỏ qua ${r.skipped} video đang ghi` : ""}.`);
    const hour = listHour;
    selecting = false;
    picked.clear();
    await loadDay();
    if (hour != null && segs.some((s) => hourOf(s) === hour)) { listHour = hour; renderList(); }
    draw();
  };

  async function loadDay() {
    $("pb").classList.toggle("live-only", !recording(cam));
    segs = recording(cam) ? await api(`/api/recordings/${encodeURIComponent(cam)}/${date}`) : [];
    cur = -1;
    listHour = null;
    selecting = false;
    picked.clear();
    setParams("#/playback", { camera: cam, date });
    $("pb-date").value = date;
    $("pb-next").disabled = isToday();
    renderList();
  }

  function seek(t, play = !video.paused || video.ended || cur < 0 || !!live) {
    if (!recording(cam)) return;  // camera không ghi hình: chỉ có trực tiếp
    stopLive();
    t = clampDay(t);
    let i = segs.findIndex((s) => t >= s.t && t < s.t + s.d);
    if (i < 0) {  // rơi vào khoảng trống: nhảy tới đoạn có video kế tiếp
      i = segs.findIndex((s) => s.t > t);
      if (i < 0) {
        pos = t;
        video.pause();
        waitingLive = isToday();
        showMsg(!segs.length ? "Ngày này không có video" : isToday() ? "Đang chờ đoạn video mới…" : "Không có video sau thời điểm này");
        draw();
        return;
      }
      t = segs[i].t;
    }
    waitingLive = false;
    showMsg("");
    pos = t;
    const offset = t - segs[i].t;
    if (i !== cur || swap) open(i, offset, play);
    else if (pendingOffset != null) pendingOffset = offset;  // file chưa tải xong phần đầu
    else {
      video.currentTime = offset;
      if (play) playVideo();
    }
    draw();
  }

  const load = (v, url) => {
    if (v.dataset.url === url && !v.error) return;
    v.dataset.url = url;
    v.src = url;
  };
  // mở phút i. Phát tiếp sang phút kế (từ giây 0) mà thẻ ẩn đã tải sẵn: phát thẻ ẩn rồi mới hiện nó lên,
  // thẻ cũ giữ khung hình cuối nên không đen màn hình. Tua / chọn phút khác: đổi file ngay trên thẻ đang hiện
  // (iPhone không tải dữ liệu cho thẻ chưa phát nên tua trên thẻ ẩn bị treo).
  function open(i, offset, play) {
    cur = i;
    markCurrent();
    const url = fileUrl(segs[i]);
    $("pb-dl").href = url;
    $("pb-dl").download = `${cam}_${date}_${segs[i].f}`;
    clearTimeout(swapTimer);
    swap = null;
    const v = spare();
    if (offset > 0.5 || !play || v.dataset.url !== url || v.error || video.webkitDisplayingFullscreen) {
      load(video, url);
      pendingOffset = offset;
      if (video.readyState >= 1) applyPending();
      if (play) playVideo();
      return;
    }
    v.muted = muted;
    v.playbackRate = v.defaultPlaybackRate = speed;
    swap = { v, offset: 0, play, stage: "meta" };
    if (v.readyState >= 1) swapSeek();
    // thẻ ẩn không chịu tải / phát: hiện nó lên luôn
    swapTimer = setTimeout(() => {
      if (!swap || swap.v !== v) return;
      playVideo(v);
      finishSwap();
    }, 4000);
  }
  function swapSeek() {
    swap.stage = "seek";
    const v = swap.v;
    v.currentTime = Math.min(swap.offset, Math.max(0, (v.duration || 60) - 0.2));
  }
  function finishSwap() {
    const v = swap.v;
    swap = null;
    clearTimeout(swapTimer);
    pendingOffset = null;
    if (v !== video) {
      const old = video;
      video = v;
      v.classList.add("on");
      old.classList.remove("on");
      old.pause();
    }
    syncPlayBtn();
  }
  function applyPending() {
    video.currentTime = Math.min(pendingOffset, Math.max(0, video.duration - 0.2));
    pendingOffset = null;
  }
  // tải sẵn phút kế tiếp vào thẻ ẩn khi phút đang xem còn dưới 20 giây
  function preloadNext() {
    const n = segs[cur + 1];
    if (!n || swap || video.webkitDisplayingFullscreen || video.duration - video.currentTime > 20) return;
    const v = spare();
    if (v.dataset.url !== fileUrl(n)) {
      v.muted = muted;
      load(v, fileUrl(n));
    }
  }
  // trình duyệt chặn tự phát có tiếng khi chưa chạm vào trang: khi đó phát không tiếng
  function playVideo(v = video) {
    v.play().catch((e) => {
      if (e.name !== "NotAllowedError" || v.muted) return;
      setMuted(true);
      v.play().catch(() => {});
    });
  }
  function setMuted(m) {
    muted = m;
    for (const v of [...vids, liveV]) v.muted = m;
    $("pb-mute").textContent = m ? "🔇" : "🔊";
  }
  const syncPlayBtn = () => { $("pb-play").textContent = shown().paused ? "▶" : "❚❚"; };

  for (const v of vids) {
    v.onloadedmetadata = () => {
      v.playbackRate = speed;
      if (swap && swap.v === v && swap.stage === "meta") swapSeek();
      else if (v === video && !swap && pendingOffset != null) applyPending();
    };
    v.onseeked = () => {
      if (!swap || swap.v !== v || swap.stage !== "seek") return;
      if (!swap.play) return finishSwap();
      swap.stage = "play";
      video.pause();
      playVideo(v);
    };
    v.onplaying = () => { if (swap && swap.v === v && swap.stage === "play") finishSwap(); };
    v.ontimeupdate = () => {
      // đang chờ / đang kéo / đang chuyển thẻ thì kim không theo video
      if (v !== video || swap || cur < 0 || drag || waitingLive || pendingOffset != null || msg.classList.contains("show")) return;
      pos = segs[cur].t + v.currentTime;
      draw();
      preloadNext();
    };
    v.onended = () => {
      if (v !== video || swap) return;
      if (cur + 1 < segs.length) return seek(segs[cur + 1].t, true);
      // hết video của ngày; hôm nay thì chờ đoạn mới ghi xong rồi phát tiếp
      pos = segs[cur].t + v.duration;
      waitingLive = isToday();
      showMsg(waitingLive ? "Đang chờ đoạn video mới…" : "Hết video của ngày này");
      draw();
    };
    v.onplay = v.onpause = () => { if (v === video) syncPlayBtn(); };
    v.onerror = () => {
      if (!v.dataset.url) return;
      if (swap && swap.v === v) finishSwap();
      if (v !== video) return;  // lỗi ở thẻ đang tải sẵn: lúc cần sẽ tải lại
      showMsg(v.error?.code === 4
        ? "Trình duyệt này không phát được video H.265 của camera. Dùng Safari (iPhone, Mac) hoặc Chrome / Edge (Android, Windows, Mac), hoặc bấm “Tải đoạn” để xem bằng ứng dụng khác."
        : "Không tải được đoạn video này.");
    };
  }
  // iPhone chỉ cho thẻ video phát có tiếng sau khi người dùng chạm vào chính thẻ đó (gọi play() trong lúc chạm):
  // lần chạm đầu tiên vào trang xem lại thì "mở khoá" cả 2 thẻ để lúc tự chuyển phút vẫn có tiếng
  $view.querySelector(".pb").addEventListener("click", () => {
    for (const v of [...vids, liveV]) {
      if (v.dataset.unlocked || !v.paused || (swap && swap.v === v)) continue;
      v.dataset.unlocked = "1";
      v.play().catch(() => {});
      v.pause();
    }
  }, true);

  $("pb-play").onclick = () => {
    if (live) liveV.paused ? playVideo(liveV) : liveV.pause();
    else if (waitingLive || cur < 0) seek(pos ?? day0(), true);
    else if (swap) swap.play = !swap.play;
    else if (video.paused) playVideo();
    else video.pause();
  };
  // iPhone (nhất là app thêm vào màn hình chính): link tải mở trang xem trước file ngay trong app, không có nút quay
  // lại. Máy cảm ứng chia sẻ được file thì tải về rồi mở bảng chia sẻ (Lưu video vào Ảnh / Lưu vào Tệp).
  // Bảng chia sẻ chỉ mở được ngay lúc bấm: tải lâu quá thì báo bấm lần nữa (file đã tải sẵn).
  let dlFile = null;  // { url, file }
  $("pb-dl").onclick = async (e) => {
    const a = e.currentTarget, url = a.href;
    if (!url || !navigator.canShare || !matchMedia("(pointer: coarse)").matches) return;
    e.preventDefault();
    try {
      if (dlFile?.url !== url) {
        toast("Đang tải video…");
        const r = await fetch(url);
        if (!r.ok) throw new Error(r.statusText);
        dlFile = { url, file: new File([await r.blob()], a.download, { type: "video/mp4" }) };
      }
      if (!navigator.canShare({ files: [dlFile.file] })) return void window.open(url, "_blank");
      await navigator.share({ files: [dlFile.file] });
    } catch (err) {
      if (err.name === "NotAllowedError") toast("Đã tải xong, bấm ⤓ lần nữa để lưu.");
      else if (err.name !== "AbortError") toast(`Không tải được video: ${err.message}`);
    }
  };
  if (prevPage) $("pb-return").onclick = () => history.back();
  $("pb-back").onclick = () => seek((pos ?? day0()) - 10);
  $("pb-fwd").onclick = () => seek((pos ?? day0()) + 10);
  $("pb-speed").onclick = (e) => {
    speed = PB_SPEEDS[(PB_SPEEDS.indexOf(speed) + 1) % PB_SPEEDS.length];
    for (const v of vids) v.playbackRate = v.defaultPlaybackRate = speed;
    e.currentTarget.textContent = `${speed}×`;
  };
  $("pb-zoom").onclick = (e) => {
    const i = (PB_SPANS.findIndex(([s]) => s === span) + 1) % PB_SPANS.length;
    span = PB_SPANS[i][0];
    e.currentTarget.textContent = PB_SPANS[i][1];
    draw();
  };
  $("pb-mute").onclick = () => {
    setMuted(!muted);
    try { localStorage.setItem("pbSound", muted ? "0" : "1"); } catch {}
  };
  try { if (localStorage.getItem("pbSound") === "1") setMuted(false); } catch {}
  // Toàn màn hình kiểu app camera: video phủ màn hình, nút và thanh thời gian nổi bên trên nên vẫn tua được
  // (trình phát toàn màn hình của iOS thì mất thanh thời gian). Chạm vào video để ẩn / hiện nút.
  // Điện thoại xoay ngang thì tự vào chế độ này, xoay dọc lại thì thoát (nếu vào do xoay).
  // Máy tính / Android còn xin toàn màn hình thật (Fullscreen API) và khoá xoay ngang nếu được.
  const pb = $("pb");
  let autoFull = false;
  const isFull = () => pb.classList.contains("immersive");
  const setFull = (on, auto = false) => {
    if (on === isFull()) return;
    autoFull = on && auto;
    pb.classList.toggle("immersive", on);
    pb.classList.remove("bare");
    document.documentElement.classList.toggle("pb-lock", on);
    $("pb-full").textContent = on ? "✕" : "⛶";
    $("pb-full").title = on ? "Thoát toàn màn hình" : "Toàn màn hình";
    if (on && !auto && pb.requestFullscreen && !document.fullscreenElement) {
      pb.requestFullscreen().then(() => screen.orientation?.lock?.("landscape")).catch(() => {});
    }
    if (!on && document.fullscreenElement) document.exitFullscreen().catch(() => {});
    canvas.width = 0;
    requestAnimationFrame(draw);
  };
  $("pb-full").onclick = () => setFull(!isFull());
  $("pb-screen").onclick = () => { if (isFull()) pb.classList.toggle("bare"); };
  const onFsChange = () => {
    if (!document.body.contains(video)) return document.removeEventListener("fullscreenchange", onFsChange);
    if (!document.fullscreenElement && isFull() && !autoFull) setFull(false);  // thoát bằng Esc / nút của máy
  };
  document.addEventListener("fullscreenchange", onFsChange);
  const landscape = matchMedia("(orientation: landscape) and (pointer: coarse) and (max-height: 600px)");
  const onTurn = () => {
    if (!document.body.contains(video)) return landscape.removeEventListener("change", onTurn);
    if (landscape.matches) setFull(true, true);
    else if (autoFull) setFull(false);
  };
  landscape.addEventListener("change", onTurn);
  if (landscape.matches) setFull(true, true);

  // kéo thanh thời gian: kim đứng giữa, kéo dải thời gian qua lại bên dưới kim
  tl.onpointerdown = (e) => {
    if (e.target.closest("button")) return;  // nút đổi độ dài nằm trên thanh
    tl.setPointerCapture(e.pointerId);
    drag = { x: e.clientX, start: pos ?? day0() + 43200, moved: false };
  };
  tl.onpointermove = (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x;
    if (Math.abs(dx) > 4) drag.moved = true;
    if (drag.moved) { pos = clampDay(drag.start - (dx / tl.clientWidth) * span); draw(); }
  };
  tl.onpointerup = (e) => {
    if (!drag) return;
    const d = drag;
    drag = null;
    if (!d.moved) {  // chạm: nhảy tới thời điểm dưới ngón tay
      const r = tl.getBoundingClientRect();
      pos = clampDay(d.start + ((e.clientX - r.left - r.width / 2) / r.width) * span);
    }
    seek(pos, true);
  };
  tl.onpointercancel = () => { drag = null; };
  tl.addEventListener("wheel", (e) => {
    e.preventDefault();
    const i = PB_SPANS.findIndex(([s]) => s === span) + (e.deltaY > 0 ? -1 : 1);
    if (i < 0 || i >= PB_SPANS.length) return;
    span = PB_SPANS[i][0];
    $("pb-zoom").textContent = PB_SPANS[i][1];
    draw();
  }, { passive: false });

  const changeDay = async (d) => {
    const tod = pos != null ? pos - day0() : null;  // giữ giờ trong ngày khi đổi ngày / camera
    date = d;
    await loadDay();
    if (tod != null) seek(day0() + tod, true);
    else startPoint();
  };
  const startPoint = () => {
    if (!segs.length) { pos = null; seek(day0()); return; }
    // hôm nay: mở phút mới nhất; ngày cũ: mở từ đầu ngày
    seek(isToday() ? segs[segs.length - 1].t : segs[0].t, true);
  };
  $("pb-cam").onchange = async (e) => {
    cam = e.target.value;
    if (!live && recording(cam)) return changeDay(date);
    await loadDay();
    startLive();
  };
  $("pb-date").onchange = (e) => { if (e.target.value) { pos = null; changeDay(e.target.value); } };
  $("pb-prev").onclick = () => { pos = null; changeDay(shiftDate(date, -1)); };
  $("pb-next").onclick = () => { pos = null; changeDay(shiftDate(date, 1)); };

  const onKey = (e) => {
    if (!document.body.contains(video)) return document.removeEventListener("keydown", onKey);
    if (e.target.closest("input, select, textarea") || $modal.open) return;
    if (e.key === "Escape" && isFull()) setFull(false);
    else if (e.key === " ") { e.preventDefault(); $("pb-play").click(); }
    else if (TV) return;  // TV: mũi tên để chọn nút (tua bằng nút ⟲ / ⟳)
    else if (e.key === "ArrowLeft") seek((pos ?? day0()) - 10);
    else if (e.key === "ArrowRight") seek((pos ?? day0()) + 10);
  };
  document.addEventListener("keydown", onKey);
  const onResize = () => {
    if (!document.body.contains(video)) return window.removeEventListener("resize", onResize);
    canvas.width = 0;
    draw();
  };
  window.addEventListener("resize", onResize);

  // hôm nay: lấy thêm các đoạn mới ghi (và ảnh mới làm xong); đang chờ ở cuối thì phát tiếp
  refreshTimer = setInterval(async () => {
    if (!isToday() || drag) return;
    const fresh = await api(`/api/recordings/${encodeURIComponent(cam)}/${date}`).catch(() => null);
    if (!fresh || !isToday()) return;
    const playingFile = cur >= 0 ? segs[cur].f : null;
    segs = fresh;
    if (playingFile) cur = segs.findIndex((s) => s.f === playingFile);
    if (`${date}|${segs.length}|${segs.filter((s) => s.th).length}` !== listSig) renderList();
    if (waitingLive) seek(pos, true);
    else draw();
  }, 20000);

  // ---- xem trực tiếp (playLive) ----
  async function startLive() {
    stopLive();
    clearTimeout(swapTimer);
    swap = null;
    video.pause();
    waitingLive = false;
    if (!isToday()) { date = isoDate(new Date()); await loadDay(); }
    const L = live = {};
    $("pb-screen").classList.add("live");
    $("pb-live").classList.add("on");
    for (const id of ["pb-back", "pb-fwd", "pb-speed"]) $(id).disabled = true;
    $("pb-dl").removeAttribute("href");
    liveV.muted = muted;
    L.player = playLive(liveV, cam, { onMsg: showMsg, play: playVideo });
    pos = Date.now() / 1000;
    draw();
    markCurrent();
    liveTimer = setInterval(() => {
      if (!document.body.contains(liveV)) return stopLive();
      if (document.hidden) stopTalk();  // rời ứng dụng / khoá máy: tắt micro
      if (!drag) { pos = Date.now() / 1000; draw(); }
    }, 1000);
    checkTalk(L);
  }
  function stopLive() {
    if (!live) return;
    const L = live;
    live = null;
    clearInterval(liveTimer);
    stopTalk();
    $("pb-talk").hidden = true;
    L.player.stop();
    $("pb-screen").classList.remove("live");
    $("pb-live").classList.remove("on");
    for (const id of ["pb-back", "pb-fwd", "pb-speed"]) $(id).disabled = false;
    if (cur >= 0) $("pb-dl").href = fileUrl(segs[cur]);
    showMsg("");
    syncPlayBtn();
  }
  liveV.onplay = liveV.onpause = () => { if (live) syncPlayBtn(); };

  // ---- nói qua loa camera (khi xem trực tiếp camera có loa) ----
  // Bấm 🎙 để bật micro, bấm lần nữa để tắt. Trong lúc nói thì tắt tiếng camera trên web, không thì tiếng từ loa
  // điện thoại lọt lại vào micro, phát ra loa camera rồi vọng về (hú).
  let talk = null;  // { ws, ctx, stream, unmute }
  async function checkTalk(L) {
    // go2rtc chỉ biết camera có loa khi đã nối camera: hỏi lại vài lần trong lúc luồng trực tiếp đang mở
    if (!navigator.mediaDevices?.getUserMedia || !window.WebSocket) return;
    if (!cameras.find((c) => c.id === cam)?.talk_enabled) return;  // chưa bật ở trang Camera
    for (let i = 0; i < 6 && live === L; i++) {
      const r = await api(`/api/live/${encodeURIComponent(cam)}/talk`).catch(() => null);
      if (live !== L) return;
      if (r?.ok) { $("pb-talk").hidden = false; return; }
      await new Promise((res) => setTimeout(res, 2500));
    }
  }
  const setTalkBtn = (state) => {
    const b = $("pb-talk");
    b.classList.toggle("wait", state === "wait");
    b.classList.toggle("on", state === "on");
    b.title = state === "off" ? "Nói qua loa camera" : "Tắt micro";
  };
  async function startTalk() {
    const T = talk = { unmute: !muted };
    setTalkBtn("wait");
    setMuted(true);
    try {
      T.stream = await navigator.mediaDevices.getUserMedia(
        { audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    } catch {
      if (talk === T) { stopTalk(); toast("Không mở được micro: cho phép trang này dùng micro rồi thử lại."); }
      return;
    }
    if (talk !== T) return T.stream.getTracks().forEach((t) => t.stop());
    const ctx = T.ctx = new (window.AudioContext || window.webkitAudioContext)();
    ctx.resume?.();
    // đổi tần số máy (44,1 / 48 kHz) về 16 kHz, mỗi mẫu mới là trung bình các mẫu gốc nó thay (lọc thô chống rè)
    const proc = ctx.createScriptProcessor(2048, 1, 1), sink = ctx.createGain();
    sink.gain.value = 0;
    ctx.createMediaStreamSource(T.stream).connect(proc);
    proc.connect(sink);
    sink.connect(ctx.destination);
    const step = ctx.sampleRate / 16000;
    let at = 0;  // vị trí (mẫu gốc, tính từ đầu khối) của mẫu 16 kHz kế tiếp
    proc.onaudioprocess = (e) => {
      const x = e.inputBuffer.getChannelData(0);
      const out = new Int16Array(Math.ceil(x.length / step) + 1);
      let n = 0;
      for (; at < x.length; at += step) {
        const a = Math.floor(at), b = Math.max(a + 1, Math.min(x.length, Math.floor(at + step)));
        let s = 0;
        for (let i = a; i < b; i++) s += x[i];
        out[n++] = Math.max(-1, Math.min(1, s / (b - a))) * 0x7fff;
      }
      at -= x.length;
      if (T.ws?.readyState === WebSocket.OPEN) T.ws.send(out.slice(0, n));
    };
    const ws = T.ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/talk/${encodeURIComponent(cam)}`);
    ws.onmessage = (e) => {
      let m = {};
      try { m = JSON.parse(e.data); } catch {}
      if (talk !== T) return;
      if (m.type === "ready") { setTalkBtn("on"); toast("Đang nói qua loa camera. Bấm 🎙 lần nữa để tắt micro."); }
      else if (m.type === "error") { stopTalk(); toast(m.msg); }
    };
    ws.onclose = () => { if (talk === T) { stopTalk(); toast("Đã ngắt nói qua loa camera."); } };
  }
  function stopTalk() {
    const T = talk;
    if (!T) return;
    talk = null;
    T.ws?.close();
    T.stream?.getTracks().forEach((t) => t.stop());
    T.ctx?.close();
    if (T.unmute) setMuted(false);
    setTalkBtn("off");
  }
  $("pb-talk").onclick = () => (talk ? stopTalk() : startTalk());
  $("pb-live").onclick = () => {
    if (!live) return startLive();
    if (!recording(cam)) return;  // không có video ghi để quay về
    stopLive();
    startPoint();
  };

  await loadDay();
  if (p.live || !recording(cam)) startLive();
  else if (pos != null) seek(pos, true);
  else startPoint();
}

// iPhone cũ không có MediaSource: dùng trình phát HLS có sẵn, trễ hơn vài giây
function playHls(v, cam, say, getMsg, onLost, onPlaying, play) {
  let stopped = false, closed = false;
  const ac = new AbortController();
  const lost = () => { if (!stopped && !closed) { closed = true; onLost(false); } };
  v.addEventListener("playing", () => { if (!stopped) { say(""); onPlaying(); } }, { signal: ac.signal });
  v.addEventListener("error", lost, { signal: ac.signal });
  v.addEventListener("ended", lost, { signal: ac.signal });
  say("Đang kết nối camera…");
  api(`/api/live/${encodeURIComponent(cam)}/hls`).then(({ url }) => {
    if (stopped) return;
    v.src = url;
    play(v);
  }).catch((e) => { if (!stopped) { say(e.message); lost(); } });
  return {
    ok: () => !closed && !getMsg(),
    stop() {
      stopped = true;
      ac.abort();
      v.pause();
      v.removeAttribute("src");
      v.load();
    },
  };
}

// ---------- xem trực tiếp ----------
// Phát luồng camera vào thẻ video: fMP4 từ /api/live/<camera> qua WebSocket, phát bằng Media Source Extensions
// (iPhone: ManagedMediaSource). Giữ hình sát thời gian thực. Trả về { stop() }.
// quality "sd": luồng phụ nhẹ (camera Xiaomi). Trang Xem không dùng: phải mở kết nối riêng tới camera nên lâu có hình
// hơn luồng chính (3,5–5 s so với 1–2 s, luồng chính đã mở sẵn cho ghi hình / nhận diện) mà không đỡ trễ.
// Mất kết nối (camera Wi-Fi yếu cắt luồng vài phút một lần) thì tự kết nối lại, chờ lâu dần tới 30 s.
function playLive(v, cam, opts = {}) {
  const onMsg = opts.onMsg || (() => {});
  let cur = null, timer = null, stopped = false, tries = 0;
  const connect = () => {
    timer = null;
    cur = playLiveOnce(v, cam, { ...opts, onMsg }, (fatal) => {
      if (stopped || fatal || timer) return;
      cur.stop();
      onMsg("Mất kết nối với camera, đang kết nối lại…");
      timer = setTimeout(connect, Math.min(30, 2 ** tries++) * 1000);
    }, () => { tries = 0; });
  };
  connect();
  return {
    ok: () => !timer && cur.ok(),
    stop() {
      stopped = true;
      clearTimeout(timer);
      cur.stop();
    },
  };
}

// một lần kết nối; onLost(fatal) khi luồng đứt (fatal: thử lại cũng không xem được), onPlaying khi bắt đầu có hình
function playLiveOnce(v, cam, { onMsg = () => {}, play, quality = "" }, onLost, onPlaying) {
  const MS = window.ManagedMediaSource || window.MediaSource;
  let msg = "";
  const say = (t) => { msg = t; onMsg(t); };
  play = play || ((x) => x.play().catch(() => { if (!x.muted) { x.muted = true; x.play().catch(() => {}); } }));
  if (!MS) {
    if (v.canPlayType("application/vnd.apple.mpegurl")) return playHls(v, cam, say, () => msg, onLost, onPlaying, play);  // iPhone dưới iOS 17.1
    say("Trình duyệt này không xem trực tiếp được.");
    return { stop() {}, ok: () => false };
  }
  // target: độ đệm giữ lại (giây). Bắt đầu 1 s; mỗi lần hết đệm phải chờ (mạng camera về từng cụm) thì nới thêm,
  // tối đa 3 s: trễ hơn chút nhưng không giật hình / tiếng. Chạy êm 30 s liền thì thu lại 0,5 s (tối thiểu 1 s), để
  // camera Wi-Fi yếu không kẹt mãi ở 3 s sau vài lần khựng; đổi lại thỉnh thoảng khựng nhẹ rồi nới lại.
  const ms = new MS(), L = { sb: null, ws: null, queue: [], stopped: false, playing: false, target: 1, fast: false, fatal: false, calm: 0 };
  const ac = new AbortController();
  v.addEventListener("waiting", () => { if (L.playing) { L.target = Math.min(3, L.target + 0.5); L.calm = performance.now(); } }, { signal: ac.signal });
  say("Đang kết nối camera…");
  ms.addEventListener("sourceopen", () => {
    if (L.stopped) return;
    const ws = L.ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/live/${encodeURIComponent(cam)}${quality ? `?q=${quality}` : ""}`);
    ws.binaryType = "arraybuffer";
    ws.onmessage = (e) => {
      if (L.stopped) return;
      if (typeof e.data !== "string") { L.queue.push(e.data); return feed(); }
      const m = JSON.parse(e.data);
      if (m.type === "error") return say(m.msg);
      if (!MS.isTypeSupported(m.mime)) {
        L.fatal = true;
        ws.close();
        return say("Trình duyệt này không phát được luồng camera (H.265). Dùng Safari hoặc Chrome / Edge mới.");
      }
      L.sb = ms.addSourceBuffer(m.mime);
      L.sb.addEventListener("updateend", feed);
    };
    ws.onclose = (e) => {
      L.closed = true;
      if (!L.stopped) onLost(L.fatal || e.code === 4401);  // 4401: hết phiên đăng nhập
    };
  }, { once: true });
  if (window.ManagedMediaSource) {  // iPhone / iPad
    v.disableRemotePlayback = true;
    v.srcObject = ms;
  } else v.src = URL.createObjectURL(ms);
  v.playbackRate = 1;
  // nối dữ liệu mới vào bộ đệm; bỏ phần đã xem, tụt lại quá xa thì nhảy tới
  function feed() {
    const sb = L.sb;
    if (L.stopped || !sb || sb.updating) return;
    if (L.queue.length) {
      const data = new Uint8Array(L.queue.reduce((n, b) => n + b.byteLength, 0));
      let o = 0;
      for (const b of L.queue) { data.set(new Uint8Array(b), o); o += b.byteLength; }
      L.queue = [];
      try { sb.appendBuffer(data); } catch { say("Lỗi phát luồng trực tiếp."); L.ws?.close(); }
      return;
    }
    if (!sb.buffered.length) return;
    const start = sb.buffered.start(0), end = sb.buffered.end(sb.buffered.length - 1);
    if (end - start > 30) return sb.remove(start, end - 10);
    // lúc mới vào bỏ qua phần dồn sẵn; tụt quá xa thì nhảy tới; tụt hơn 1 s thì phát nhanh hơn rất ít (tiếng không méo)
    const lag = end - v.currentTime;
    if (L.playing && L.target > 1 && performance.now() - L.calm > 30000) {
      L.target = Math.max(1, L.target - 0.5);
      L.calm = performance.now();
    }
    if (!L.playing || v.currentTime < start || lag > L.target + 3) v.currentTime = Math.max(start, end - L.target);
    if (L.fast ? lag < L.target + 0.3 : lag > L.target + 1) {
      L.fast = !L.fast;
      v.playbackRate = L.fast ? 1.05 : 1;
    }
    if (!L.playing) {
      L.playing = true;
      L.calm = performance.now();
      say("");
      onPlaying();
      play(v);
    }
  }
  return {
    ok: () => !L.closed && !msg,  // đang phát / đang kết nối bình thường
    stop() {
      if (L.stopped) return;
      L.stopped = true;
      ac.abort();
      try { L.ws?.close(); } catch {}
      v.pause();
      v.removeAttribute("src");
      v.srcObject = null;
      v.load();
    },
  };
}

// ---------- Xem: trực tiếp nhiều camera cùng lúc ----------
async function viewLive() {
  if (!cameras.length) {
    $view.innerHTML = `<h1>Xem</h1><div class="empty">Chưa có camera nào. Thêm camera ở trang <a href="#/cameras">Camera</a>.</div>`;
    return;
  }
  const canFull = !!document.documentElement.requestFullscreen;
  // bố cục: 0 = tự động (mọi camera vừa một màn hình), 2 / 4 / 6 = số camera mỗi trang (nhiều hơn thì chuyển trang)
  const LAYOUTS = { 2: [2, 1], 4: [2, 2], 6: [3, 2] };
  let layout = 0;
  try { layout = Number(localStorage.getItem("liveLayout")) || 0; } catch {}
  if (!LAYOUTS[layout]) layout = 0;
  let page = 0;
  $view.innerHTML = `
    <div class="head-row"><h1>Xem trực tiếp</h1><div class="head-actions">
      ${cameras.length > 2 ? `<div class="live-layout" role="group" aria-label="Bố cục">${[[0, "Tự động"], [2, "2"], [4, "4"], [6, "6"]].map(([n, t]) =>
        `<button type="button" data-layout="${n}"${n ? ` aria-label="${n} camera"` : ""}>${t}</button>`).join("")}</div>` : ""}
      <span class="live-pages" hidden><button type="button" data-page="-1" aria-label="Trang trước">‹</button><span></span><button type="button" data-page="1" aria-label="Trang sau">›</button></span>
      ${cameras.length > 1 && !me?.open && !TV ? `<button type="button" id="live-sort">↕ Sắp xếp</button>` : ""}${canFull ? `<button type="button" id="live-full">⛶ Toàn màn hình</button>` : ""}<button type="button" id="live-all">▶ Phát tất cả</button></div></div>
    <p class="sub">${TV ? "Mũi tên để chọn camera (đi quá mép là sang trang camera kế), OK để phóng to và nghe tiếng, Quay lại để về lưới."
      : "Bấm ▶ trên camera muốn xem. Chạm vào hình đang phát để bật / tắt tiếng, chạm hai lần để phóng to; “Mở” để xem riêng và tua lại video đã ghi."}</p>
    <div class="live-grid">${cameras.map((c) => `
      <article class="live-tile" data-cam="${esc(c.id)}"${TV ? ` tabindex="0" aria-label="${esc(c.name)}"` : ""}>
        <video playsinline muted></video>
        <div class="live-msg"></div>
        <button type="button" class="live-play" aria-label="Xem trực tiếp ${esc(c.name)}"${TV ? ` tabindex="-1"` : ""}>▶</button>
        <header>
          <div class="live-name"><b>${esc(c.name)}</b>${c.location ? `<small>${esc(c.location)}</small>` : ""}</div>
          <a class="live-open" href="#/playback?camera=${encodeURIComponent(c.id)}&live=1"${TV ? ` tabindex="-1"` : ""}>Mở ›</a>
        </header>
        <button type="button" class="live-stop" aria-label="Dừng"${TV ? ` tabindex="-1"` : ""}>■</button>
        <span class="live-sound" hidden>🔊</span>
      </article>`).join("")}</div>`;

  const grid = $view.querySelector(".live-grid");
  const tiles = [...grid.querySelectorAll(".live-tile")];
  const players = new Map();  // ô đang phát -> playLive()
  const allBtn = document.getElementById("live-all");
  const fullBtn = document.getElementById("live-full");
  const pagesBox = $view.querySelector(".live-pages");
  let zoomed = null;          // ô đang phóng to (chiếm cả khung)
  let wake = null;            // giữ màn hình TV không tắt khi đang xem
  const keepAwake = async () => {
    if (!TV || wake || !players.size || document.hidden || !navigator.wakeLock) return;
    try { wake = await navigator.wakeLock.request("screen"); wake.onrelease = () => (wake = null); } catch {}
  };
  const syncAll = () => {
    allBtn.textContent = players.size ? "■ Dừng tất cả" : "▶ Phát tất cả";
    if (players.size) keepAwake(); else wake?.release().catch(() => {});
  };
  // chỉ một camera có tiếng một lúc; null = tắt hết
  const setSound = (tile) => tiles.forEach((t) => {
    const has = t === tile;
    t.classList.toggle("sound", has);
    t.querySelector(".live-sound").hidden = !has;
    t.querySelector("video").muted = !has;
  });
  const start = (tile) => {
    players.get(tile)?.stop();
    const v = tile.querySelector("video"), box = tile.querySelector(".live-msg");
    v.muted = !tile.classList.contains("sound");
    tile.classList.add("playing");
    players.set(tile, playLive(v, tile.dataset.cam, {
      onMsg: (t) => (box.textContent = t),
    }));
    syncAll();
  };
  const stop = (tile) => {
    players.get(tile)?.stop();
    players.delete(tile);
    tile.classList.remove("playing", "sound");
    tile.querySelector(".live-msg").textContent = "";
    tile.querySelector(".live-sound").hidden = true;
    if (zoomed === tile) zoom(null);
    syncAll();
  };

  const isFull = () => document.fullscreenElement === grid;
  // điện thoại dọc: luôn cuộn hết các camera (bố cục 2 / 4 / 6 chỉ cho màn hình ngang)
  const narrow = () => !isFull() && innerWidth < 760 && innerHeight > innerWidth;
  const lay = () => (narrow() ? 0 : layout);
  const pages = () => (lay() ? Math.ceil(tiles.length / lay()) : 1);
  const onPage = () => (lay() ? tiles.slice(page * lay(), page * lay() + lay()) : tiles);
  // Xếp lưới vừa khung: bố cục tự động thử từng số cột, chọn cách cho ô 16:9 to nhất; bố cục 2 / 4 / 6 dùng số cột cố định.
  // Đặt cả chiều cao hàng để không phụ thuộc trình duyệt (WebView TV) và đo lại mỗi khi khung đổi cỡ.
  const fit = () => {
    if (!document.body.contains(grid)) return;
    const full = isFull();
    grid.classList.toggle("compact", full || TV);
    const shown = zoomed ? [zoomed] : onPage();
    tiles.forEach((t) => t.classList.toggle("off", !shown.includes(t)));
    if (narrow() && !zoomed) { grid.style.gridTemplateColumns = grid.style.gridAutoRows = ""; return; }  // điện thoại dọc: cuộn như cũ
    const gap = parseFloat(getComputedStyle(grid).columnGap) || 0;
    const W = grid.clientWidth;
    const H = full ? grid.clientHeight
      : innerHeight - (grid.getBoundingClientRect().top + window.scrollY) - (parseFloat(getComputedStyle($view).paddingBottom) || 0);
    const size = (cols, rows) => Math.min((W - gap * (cols - 1)) / cols, ((H - gap * (rows - 1)) / rows) * 16 / 9);
    let best = { w: 0, cols: 1 };
    if (zoomed) best = { w: size(1, 1), cols: 1 };
    else if (lay()) best = { w: size(...LAYOUTS[lay()]), cols: LAYOUTS[lay()][0] };
    else for (let cols = 1; cols <= shown.length; cols++) {
      const w = size(cols, Math.ceil(shown.length / cols));
      if (w > best.w + 0.5) best = { w, cols };
    }
    if (best.w < 150) { grid.style.gridTemplateColumns = grid.style.gridAutoRows = ""; return; }  // quá bé: cuộn như cũ
    const w = Math.floor(best.w);
    grid.style.gridTemplateColumns = `repeat(${best.cols}, ${w}px)`;
    grid.style.gridAutoRows = `${Math.floor(w * 9 / 16)}px`;
  };
  const anyPlaying = () => TV || players.size > 0;
  // chuyển bố cục / trang: camera ra khỏi màn hình thì ngắt luồng, camera mới hiện thì phát (nếu đang xem)
  const show = () => {
    if (page >= pages()) page = 0;
    const was = anyPlaying();
    const shown = onPage();
    tiles.forEach((t) => { if (!shown.includes(t) && players.has(t)) stop(t); });
    if (was) shown.forEach((t) => { if (!players.has(t)) start(t); });
    $view.querySelectorAll("[data-layout]").forEach((b) => b.classList.toggle("on", Number(b.dataset.layout) === layout));
    pagesBox.hidden = pages() < 2;
    pagesBox.querySelector("span").textContent = `${page + 1}/${pages()}`;
    fit();
  };
  const goPage = (d) => {
    if (pages() < 2 || zoomed) return false;
    page = (page + d + pages()) % pages();
    show();
    onPage()[d > 0 ? 0 : onPage().length - 1]?.focus({ preventScroll: true });
    toast(`Trang ${page + 1}/${pages()}`);
    return true;
  };
  $view.querySelectorAll("[data-layout]").forEach((b) => (b.onclick = () => {
    layout = Number(b.dataset.layout);
    page = 0;
    try { localStorage.setItem("liveLayout", String(layout)); } catch {}
    show();
  }));
  pagesBox.querySelectorAll("[data-page]").forEach((b) => (b.onclick = () => goPage(Number(b.dataset.page))));
  // TV: mũi tên đi quá mép lưới thì sang trang camera kế
  spatialEdge = (dir) => {
    if (!tiles.includes(document.activeElement)) return;
    goPage(dir === "ArrowRight" || dir === "ArrowDown" ? 1 : -1);
  };

  const zoom = (tile) => {
    if (zoomed === tile) return;
    const was = zoomed;
    zoomed = tile;
    grid.classList.toggle("zoomed", !!tile);
    tiles.forEach((t) => t.classList.toggle("zoom", t === tile));
    if (tile) {
      if (!players.has(tile)) start(tile);
      setSound(tile);
      if (!was) history.pushState({ liveZoom: true }, "");  // nút Quay lại của remote / trình duyệt: về lưới
      tile.focus({ preventScroll: true });
    } else {
      if (TV) setSound(null);
      if (history.state?.liveZoom) history.back();
      was?.focus({ preventScroll: true });
    }
    fit();
  };
  const onPop = () => { if (zoomed && !history.state?.liveZoom) zoom(null); };
  window.addEventListener("popstate", onPop);

  allBtn.onclick = () => (players.size ? tiles.forEach(stop) : onPage().forEach(start));
  if (fullBtn) fullBtn.onclick = () => (document.fullscreenElement ? document.exitFullscreen() : grid.requestFullscreen()).catch(() => {});
  const onFull = () => {
    if (fullBtn) fullBtn.textContent = document.fullscreenElement ? "✕ Thoát toàn màn hình" : "⛶ Toàn màn hình";
    fit();
    if (TV && isFull() && !grid.contains(document.activeElement)) (zoomed || onPage()[0])?.focus({ preventScroll: true });
  };
  document.addEventListener("fullscreenchange", onFull);
  const sortBtn = document.getElementById("live-sort");
  if (sortBtn) sortBtn.onclick = openCameraOrder;
  tiles.forEach((tile) => {
    tile.querySelector(".live-play").onclick = () => start(tile);
    tile.querySelector(".live-stop").onclick = (e) => { e.stopPropagation(); stop(tile); };
    tile.onclick = (e) => {
      if (e.target.closest("a, button")) return;
      if (TV) return zoom(zoomed === tile ? null : tile);
      if (!players.has(tile)) return;
      if (!players.get(tile).ok()) return start(tile);  // lỗi / mất kết nối: chạm để thử lại
      setSound(tile.classList.contains("sound") ? null : tile);
    };
    tile.ondblclick = (e) => {
      if (TV || e.target.closest("a, button")) return;
      zoom(zoomed === tile ? null : tile);
    };
    tile.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        zoom(zoomed === tile ? null : tile);  // luồng đang lỗi thì tự nối lại, không cần bấm
      }
    };
  });
  // TV: viền cam của camera đang chọn chỉ hiện khi vừa bấm mũi tên, 4 s không bấm thì ẩn (xem toàn màn hình không bị vướng)
  let idleTimer = 0;
  if (TV) grid.classList.add("idle");
  const navActive = () => {
    grid.classList.remove("idle");
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => grid.classList.add("idle"), 4000);
  };
  // đang phóng to: Esc / Quay lại về lưới; nút chuyển kênh / trang của remote: sang trang camera kế
  const onKey = (e) => {
    if (e.key.startsWith("Arrow")) navActive();
    if ($modal.open) return;
    if (zoomed && ["Escape", "Backspace", "GoBack", "BrowserBack"].includes(e.key)) {
      e.preventDefault();
      zoom(null);
    } else if (["PageDown", "ChannelDown", "MediaTrackNext"].includes(e.key)) {
      if (goPage(1)) e.preventDefault();
    } else if (["PageUp", "ChannelUp", "MediaTrackPrevious"].includes(e.key)) {
      if (goPage(-1)) e.preventDefault();
    }
  };
  document.addEventListener("keydown", onKey);
  // ảnh xem trước: khung cuối đã ghi của hôm nay (camera có ghi hình)
  const today = isoDate(new Date());
  tiles.forEach(async (tile) => {
    if (!cameras.find((c) => c.id === tile.dataset.cam)?.record_enabled) return;
    const segs = await api(`/api/recordings/${encodeURIComponent(tile.dataset.cam)}/${today}`).catch(() => []);
    const last = [...segs].reverse().find((x) => x.th);
    if (last) tile.querySelector("video").poster = `/rec/${encodeURIComponent(tile.dataset.cam)}/${today}/${last.f.slice(0, -4)}.jpg`;
  });
  // ẩn app / chuyển tab: ngắt luồng cho đỡ tốn mạng, mở lại thì phát tiếp các ô đang xem
  let resume = [];
  const onVis = () => {
    if (document.hidden) {
      resume = [...players.keys()];
      players.forEach((p) => p.stop());
    } else {
      resume.forEach(start);
      resume = [];
      keepAwake();
    }
  };
  document.addEventListener("visibilitychange", onVis);
  document.documentElement.classList.add("live-fit");
  let wasNarrow = narrow();
  const onResize = () => { if (narrow() !== wasNarrow) { wasNarrow = narrow(); show(); } else fit(); };
  window.addEventListener("resize", onResize);
  const ro = window.ResizeObserver ? new ResizeObserver(() => fit()) : null;
  ro?.observe(grid);
  show();
  requestAnimationFrame(fit);
  if (TV) onPage()[0].focus({ preventScroll: true });  // TV: show() đã phát luôn các camera đang hiện
  leavePage = () => {
    spatialEdge = null;
    clearTimeout(idleTimer);
    ro?.disconnect();
    document.removeEventListener("visibilitychange", onVis);
    document.removeEventListener("fullscreenchange", onFull);
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("popstate", onPop);
    window.removeEventListener("resize", onResize);
    document.documentElement.classList.remove("live-fit");
    if (document.fullscreenElement === grid) document.exitFullscreen().catch(() => {});
    wake?.release().catch(() => {});
    players.forEach((p) => p.stop());
  };
}

// thứ tự camera riêng của tài khoản: áp dụng cho mọi trang có danh sách camera, trên mọi thiết bị
function openCameraOrder() {
  const order = cameras.map((c) => c.id);
  $modalBody.innerHTML = `
    <div class="modal-head">
      <b>Sắp xếp camera</b>
      <button class="icon-btn" id="co-close" aria-label="Đóng">×</button>
    </div>
    <form id="co-form" class="zone-form cam-form">
      <p class="hint" style="margin:0">Thứ tự này chỉ áp dụng cho tài khoản của bạn, trên mọi thiết bị.</p>
      <div class="cam-order" id="co-list"></div>
      <div class="actions">
        <button class="primary">Lưu</button>
        <button type="button" id="co-reset">Về thứ tự chung</button>
      </div>
    </form>`;
  const list = document.getElementById("co-list");
  const render = () => {
    list.innerHTML = order.map((id, i) => `<div class="cam-order-row">
        <b>${i + 1}. ${esc(camName(id))}</b>
        <button type="button" data-move="top" data-i="${i}" ${i ? "" : "disabled"} aria-label="Đưa ${esc(camName(id))} lên đầu">⤒</button>
        <button type="button" data-move="up" data-i="${i}" ${i ? "" : "disabled"} aria-label="Đưa ${esc(camName(id))} lên">▲</button>
        <button type="button" data-move="down" data-i="${i}" ${i < order.length - 1 ? "" : "disabled"} aria-label="Đưa ${esc(camName(id))} xuống">▼</button>
      </div>`).join("");
  };
  list.onclick = (e) => {
    const b = e.target.closest("[data-move]");
    if (!b) return;
    const i = Number(b.dataset.i);
    const [id] = order.splice(i, 1);
    order.splice(b.dataset.move === "top" ? 0 : b.dataset.move === "up" ? i - 1 : i + 1, 0, id);
    render();
  };
  const save = async (ids) => {
    try {
      await api("/api/me/camera-order", { method: "PUT", body: JSON.stringify({ cameras: ids }) });
      cameras = await api("/api/cameras");
    } catch (err) { toast(err.message); return; }
    toast("Đã lưu thứ tự camera");
    $modal.close();
    route();
  };
  render();
  narrowModal();
  document.getElementById("co-close").onclick = () => $modal.close();
  document.getElementById("co-reset").onclick = () => save([]);
  document.getElementById("co-form").onsubmit = (e) => { e.preventDefault(); save(order); };
}

// ---------- xem ảnh lớn ----------
// dialog riêng nằm trên cùng, mở chồng lên hộp chi tiết; Esc / × / chạm nền để đóng, không đóng hộp bên dưới
const $viewer = document.getElementById("viewer");
function openViewer(items, start = 0) {
  if (!items.length) return;
  let i = Math.min(Math.max(0, start), items.length - 1);
  const go = (d) => { i = (i + d + items.length) % items.length; render(); };
  function render() {
    const it = items[i];
    $viewer.innerHTML = `
      <div class="viewer-bar">
        <span class="viewer-count">${items.length > 1 ? `${i + 1}/${items.length}` : ""}</span>
        <span class="viewer-cap">${esc(it.caption || "")}</span>
        <button type="button" class="icon-btn" data-vclose aria-label="Đóng">×</button>
      </div>
      <div class="viewer-stage ${it.pair ? "pair" : ""}">${(it.pair || [it]).map((p) => `
        <figure><img src="${p.src}" alt="${esc(p.label || it.caption || "")}">${p.label ? `<figcaption>${esc(p.label)}</figcaption>` : ""}</figure>`).join("")}
      </div>
      ${items.length > 1 ? `<button type="button" class="viewer-nav prev" data-vgo="-1" aria-label="Ảnh trước">‹</button>
        <button type="button" class="viewer-nav next" data-vgo="1" aria-label="Ảnh sau">›</button>` : ""}`;
    $viewer.querySelector("[data-vclose]").onclick = () => $viewer.close();
    $viewer.querySelectorAll("[data-vgo]").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); go(Number(b.dataset.vgo)); }));
  }
  render();
  if (!$viewer.open) $viewer.showModal();
  $viewer.onkeydown = (e) => {
    if (e.key === "ArrowLeft") go(-1);
    else if (e.key === "ArrowRight") go(1);
  };
  let sx = null, sy = 0;
  $viewer.onpointerdown = (e) => { sx = e.clientX; sy = e.clientY; };
  $viewer.onpointerup = (e) => {
    if (sx === null) return;
    const dx = e.clientX - sx, dy = e.clientY - sy;
    sx = null;
    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy)) go(dx < 0 ? 1 : -1);        // vuốt ngang: chuyển ảnh
    else if (dy > 90 && Math.abs(dy) > Math.abs(dx)) $viewer.close();                  // vuốt xuống: đóng
    else if (Math.abs(dx) < 8 && Math.abs(dy) < 8 && !e.target.closest("img, button")) $viewer.close();  // chạm nền
  };
}

// ảnh trong hộp chi tiết biển số / cảnh báo: chạm để xem lớn
$modalBody.addEventListener("click", (e) => {
  const im = e.target.closest(".modal-content img.frame, .modal-content img.crop");
  if (!im) return;
  const all = [...$modalBody.querySelectorAll(".modal-content img.frame, .modal-content img.crop")];
  openViewer(all.map((x) => ({ src: x.src, caption: x.alt })), all.indexOf(im));
});

// ---------- Người dùng ----------
async function viewUsers() {
  const users = await api("/api/users");
  $view.innerHTML = `
    <h1>Người dùng</h1>
    <p class="sub">Mỗi người một tài khoản riêng. Admin mở được mọi trang; thành viên chỉ thấy các trang được cấp.</p>
    <div class="toolbar"><button class="primary" type="button" id="u-add">Thêm người dùng</button></div>
    <div class="table-wrap"><table>
      <thead><tr><th>Tài khoản</th><th>Được mở</th><th>Camera</th><th>Dùng gần nhất</th></tr></thead>
      <tbody>${users.map((u) => `<tr data-uid="${u.id}">
        <td><b>${esc(u.username)}</b>${u.username === me.username ? ` <span class="badge accent">Bạn</span>` : ""}${u.is_admin ? ` <span class="badge ok">Admin</span>` : ""}${u.disabled ? ` <span class="badge danger">Đã khoá</span>` : ""}</td>
        <td>${u.is_admin ? "Tất cả" : u.perms.map((p) => esc(me.pages[p])).join(", ") || `<span class="hint">Chưa có quyền</span>`}</td>
        <td>${u.cameras.length ? u.cameras.map((c) => esc(camName(c))).join(", ") : "Tất cả"}</td>
        <td>${u.last_seen ? esc(ago(u.last_seen)) : "—"}</td></tr>`).join("")}</tbody>
    </table></div>
    <div class="panel settings" style="margin-top:16px">
      <h2>Đang đăng nhập</h2>
      <p class="hint" style="margin:0 0 10px">Vị trí tính theo IP nên chỉ gần đúng, cỡ tỉnh/thành. Thấy thiết bị lạ thì đăng xuất thiết bị đó hoặc khoá tài khoản, rồi đổi mật khẩu trước khi mở khoá.</p>
      <div id="sessions" class="sessions"><span class="hint">Đang tải…</span></div>
    </div>`;
  document.getElementById("u-add").onclick = () => openUser(null);
  loadSessions();
  $view.querySelectorAll("tr[data-uid]").forEach((tr) => (tr.onclick = () => openUser(users.find((u) => u.id === Number(tr.dataset.uid)))));
}

function narrowModal() {
  $modal.classList.add("narrow");
  $modal.addEventListener("close", () => $modal.classList.remove("narrow"), { once: true });
  $modal.showModal();
}

function openUser(u) {
  const edit = !!u;
  const self = edit && u.username === me.username;
  const locked = self || (edit && u.is_admin && !me.is_admin);
  // thành viên quản lý người dùng chỉ cấp thêm được quyền chính họ có
  const grantable = (p) => me.is_admin || me.perms.includes(p);
  $modalBody.innerHTML = `
    <div class="modal-head">
      <b>${edit ? esc(u.username) : "Thêm người dùng"}</b>
      <button class="icon-btn" id="u-close" aria-label="Đóng">×</button>
    </div>
    <form id="u-form" class="zone-form cam-form" autocomplete="off">
      ${edit ? "" : `<label>Tên đăng nhập<input name="username" required pattern="[A-Za-z0-9_.\\-]{2,32}" title="2–32 ký tự: chữ không dấu, số, . _ -" autocapitalize="none" autocorrect="off" spellcheck="false" placeholder="vd: bao-ve"></label>`}
      ${locked ? `<p class="hint" style="margin:0">${self ? "Đây là tài khoản của bạn. Đổi mật khẩu bằng nút Đổi mật khẩu; quyền của chính mình thì nhờ admin khác sửa." : "Chỉ admin sửa được tài khoản admin."}</p>` : `
      <label>${edit ? "Mật khẩu mới (để trống nếu không đổi)" : "Mật khẩu"}<input name="password" type="password" minlength="8" autocomplete="new-password" ${edit ? "" : "required"} placeholder="ít nhất 8 ký tự"></label>
      ${me.is_admin ? `<label class="check"><input type="checkbox" name="is_admin" ${u?.is_admin ? "checked" : ""}> Admin: mở được mọi trang, sửa được cả tài khoản admin khác</label>` : ""}
      <fieldset id="u-perms" ${u?.is_admin ? "hidden" : ""}><legend>Được mở các trang</legend>
        <div class="wrap">${Object.entries(me.pages).map(([k, label]) => `
          <label class="check"><input type="checkbox" name="perm" value="${k}" ${u?.perms.includes(k) ? "checked" : ""} ${grantable(k) ? "" : "disabled"}> ${esc(label)}</label>`).join("")}
        </div>
        <p class="hint" style="margin:4px 0 0">“Người dùng”: được thêm, sửa, khoá tài khoản thành viên khác.</p>
      </fieldset>
      <fieldset id="u-cams" ${u?.is_admin ? "hidden" : ""}><legend>Được xem camera</legend>
        <label class="check"><input type="checkbox" name="allcams" ${(u ? !u.cameras.length : !me.cameras) ? "checked" : ""} ${me.cameras ? "disabled" : ""}> Tất cả camera (kể cả camera thêm sau này)</label>
        <div class="wrap" id="u-camlist">${cameras.map((c) => `
          <label class="check"><input type="checkbox" name="cam" value="${esc(c.id)}" ${u?.cameras.includes(c.id) ? "checked" : ""}> ${esc(c.name)}</label>`).join("")}
        </div>
        <p class="hint" style="margin:4px 0 0">Chỉ thấy biển số, cảnh báo, người qua lại, video và xem trực tiếp của các camera này.</p>
      </fieldset>
      ${edit ? `<label class="check"><input type="checkbox" name="disabled" ${u.disabled ? "checked" : ""}> Khoá tài khoản (đăng xuất ngay trên mọi thiết bị)</label>` : ""}
      <div class="actions">
        <button class="primary">${edit ? "Lưu" : "Tạo tài khoản"}</button>
        ${edit ? `<button type="button" class="danger" id="u-del">Xoá tài khoản</button>` : ""}
      </div>`}
    </form>`;
  narrowModal();
  document.getElementById("u-close").onclick = () => $modal.close();
  if (locked) return;
  const form = document.getElementById("u-form");
  if (form.elements.is_admin) form.elements.is_admin.onchange = (e) => {
    document.getElementById("u-perms").hidden = document.getElementById("u-cams").hidden = e.target.checked;
  };
  const allCams = form.elements.allcams;
  const syncCams = () => (document.getElementById("u-camlist").hidden = allCams.checked);
  allCams.onchange = syncCams;
  syncCams();
  form.onsubmit = async (e) => {
    e.preventDefault();
    const f = form.elements;
    const body = { perms: [...form.querySelectorAll("[name=perm]:checked")].map((c) => c.value) };
    if (f.is_admin) body.is_admin = f.is_admin.checked;
    // camera người kia đang xem mà mình không thấy (không có trong danh sách) thì giữ nguyên
    const hiddenCams = (u?.cameras || []).filter((c) => !cameras.some((x) => x.id === c));
    body.cameras = allCams.checked ? [] : [...form.querySelectorAll("[name=cam]:checked")].map((c) => c.value).concat(hiddenCams);
    if (!allCams.checked && !body.cameras.length) { toast("Chọn ít nhất một camera"); return; }
    if (f.password.value) body.password = f.password.value;
    if (edit) body.disabled = f.disabled.checked;
    else body.username = f.username.value.trim();
    try {
      await api(edit ? `/api/users/${u.id}` : "/api/users", { method: edit ? "PATCH" : "POST", body: JSON.stringify(body) });
    } catch (err) { toast(err.message); return; }
    toast(edit ? "Đã lưu" : "Đã tạo tài khoản");
    $modal.close();
    viewUsers();
  };
  if (edit) document.getElementById("u-del").onclick = async () => {
    if (!confirm(`Xoá tài khoản ${u.username}? Người này bị đăng xuất ngay.`)) return;
    try { await api(`/api/users/${u.id}`, { method: "DELETE" }); } catch (err) { toast(err.message); return; }
    toast("Đã xoá");
    $modal.close();
    viewUsers();
  };
}

function openPassword() {
  if ($sheet.open) $sheet.close();
  $modalBody.innerHTML = `
    <div class="modal-head">
      <b>Đổi mật khẩu · ${esc(me.username)}</b>
      <button class="icon-btn" id="pw-close" aria-label="Đóng">×</button>
    </div>
    <form id="pw-form" class="zone-form cam-form">
      <input name="username" autocomplete="username" value="${esc(me.username)}" hidden>
      <label>Mật khẩu hiện tại<input name="old" type="password" autocomplete="current-password" required></label>
      <label>Mật khẩu mới<input name="new" type="password" autocomplete="new-password" minlength="8" required placeholder="ít nhất 8 ký tự"></label>
      <label>Nhập lại mật khẩu mới<input name="new2" type="password" autocomplete="new-password" minlength="8" required></label>
      <p class="hint" style="margin:0">Các thiết bị khác đang đăng nhập tài khoản này sẽ phải đăng nhập lại.</p>
      <div class="actions"><button class="primary">Đổi mật khẩu</button></div>
    </form>`;
  narrowModal();
  document.getElementById("pw-close").onclick = () => $modal.close();
  document.getElementById("pw-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target.elements;
    if (f.new.value !== f.new2.value) { toast("Hai lần nhập mật khẩu mới không khớp"); return; }
    try {
      await api("/api/me/password", { method: "POST", body: JSON.stringify({ old: f.old.value, new: f.new.value }) });
    } catch (err) { toast(err.message); return; }
    toast("Đã đổi mật khẩu");
    $modal.close();
  };
}

// ---------- khung ứng dụng (PWA, menu Thêm, đăng xuất) ----------
document.querySelectorAll("[data-account]").forEach((b) => (b.onclick = openPassword));
document.querySelector("[data-more]").onclick = () => $sheet.showModal();
$sheet.addEventListener("click", (e) => { if (e.target === $sheet) $sheet.close(); });  // chạm ra ngoài để đóng
document.querySelectorAll("[data-logout]").forEach((b) => (b.onclick = async () => {
  if (!confirm("Đăng xuất khỏi thiết bị này?")) return;
  await fetch("/api/logout", { method: "POST" }).catch(() => {});
  location.replace("/login");
}));
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").then((r) => r.update()).catch(() => {});
}
// có bản giao diện mới trên server thì tự tải lại (app trên điện thoại hay giữ bản cũ rất lâu)
const BOOT_VERSION = new URL(document.querySelector('script[src*="app.js"]').src).searchParams.get("v");
async function checkVersion() {
  if (!BOOT_VERSION || $modal.open) return;
  const r = await fetch("/api/version").then((x) => x.json()).catch(() => null);
  if (r && r.version !== BOOT_VERSION) location.reload();
}
// mở lại app sau một lúc ở nền thì tải lại dữ liệu trang đang xem
let hiddenAt = 0;
document.addEventListener("visibilitychange", () => {
  if (document.hidden) hiddenAt = Date.now();
  else {
    checkVersion();
    if (hiddenAt && Date.now() - hiddenAt > 60000 && !$modal.open) route();
  }
});

route();
checkVersion();
