"use strict";

const $view = document.getElementById("view");
const $modal = document.getElementById("modal");
const $modalBody = document.getElementById("modal-body");
const PAGE_SIZE = 60;
const LANE_COLORS = ["--lane-1", "--lane-2", "--lane-3", "--lane-4", "--lane-5"];

let cameras = [];
let refreshTimer = null;

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

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
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
const MORE_PAGES = ["people", "cameras", "settings"];

async function route() {
  clearInterval(refreshTimer);
  if ($sheet.open) $sheet.close();
  const path = location.hash.replace(/^#/, "").split("?")[0] || "/";
  const nav = path.startsWith("/plate") ? "plates" : path === "/" ? "detections" : path.slice(1).split("/")[0];
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === nav));
  document.querySelector("[data-nav-more]").classList.toggle("active", MORE_PAGES.includes(nav));
  document.getElementById("page-title").textContent = document.querySelector(`.mainnav [data-nav="${nav}"]`)?.textContent || "";
  window.scrollTo(0, 0);
  if (!cameras.length) cameras = await api("/api/cameras").catch(() => []);
  try {
    if (path === "/") await viewDetections();
    else if (path === "/plates") await viewPlates();
    else if (path.startsWith("/plate/")) await viewTimeline(decodeURIComponent(path.slice(7)));
    else if (path === "/cameras") await viewCameras();
    else if (path === "/events") await viewEvents();
    else if (path === "/people") await viewPeople();
    else if (path === "/visitors") await viewVisitors();
    else if (path === "/settings") await viewSettings();
    else $view.innerHTML = `<div class="empty">Không tìm thấy trang</div>`;
  } catch (e) {
    $view.innerHTML = `<div class="empty">Lỗi tải dữ liệu: ${esc(e.message)}</div>`;
  }
}
window.addEventListener("hashchange", route);

// ---------- Nhận diện ----------
async function viewDetections() {
  const p = readParams();
  const camOpts = cameras.map((c) => `<option value="${esc(c.id)}" ${p.camera === c.id ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  $view.innerHTML = `
    <h1>Nhận diện biển số</h1>
    <p class="sub">Mỗi thẻ là một lần xe xuất hiện trước camera, kèm ảnh chụp và biển số OCR đọc được.</p>
    <div class="stats" id="stats"></div>
    <form class="toolbar" id="filters">
      <input type="search" name="q" placeholder="Tìm biển số, vd 30A123" value="${esc(p.q || "")}">
      <select name="camera"><option value="">Tất cả camera</option>${camOpts}</select>
      <input type="date" name="date_from" value="${esc(p.date_from || "")}" title="Từ ngày">
      <input type="date" name="date_to" value="${esc(p.date_to || "")}" title="Đến ngày">
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
    setParams("#/", { ...f, offset: offset || "" });
    const [stats, data] = await Promise.all([
      api("/api/stats"),
      api(`/api/sightings?${new URLSearchParams({ ...f, limit: PAGE_SIZE, offset })}`),
    ]);
    renderStats(stats);
    renderCards(data, offset, (o) => { offset = o; load(); });
  };
  form.addEventListener("submit", (e) => { e.preventDefault(); offset = 0; load(); });
  document.getElementById("clear").onclick = () => { form.reset(); form.q.value = ""; form.camera.value = ""; form.date_from.value = ""; form.date_to.value = ""; offset = 0; load(); };
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
      <span>${offset + 1}–${offset + data.items.length} / ${data.total}</span>
      <button ${offset + PAGE_SIZE >= data.total ? "disabled" : ""} data-go="${offset + PAGE_SIZE}">Cũ hơn ›</button>
    </div>`;
  list.querySelectorAll(".card").forEach((el, i) => (el.onclick = () => openSighting(data.items[i], () => goto(offset))));
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
      <div class="meta"><b>${esc(s.camera_name || s.camera)}</b> · ${fmtTime(s.first_seen)}${dur >= 60 ? `–${fmtTime(s.last_seen)} (${fmtDuration(dur)})` : ""} · ${fmtDate(s.first_seen)}</div>
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
          <button class="danger" id="m-del" type="button">Xoá lần này</button>
        </div>
      </div>
    </div>`;
  $modal.showModal();
  const close = () => $modal.close();
  document.getElementById("m-close").onclick = close;
  $modalBody.querySelector("a").onclick = close;
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

// ---------- Biển số ----------
async function viewPlates() {
  const p = readParams();
  $view.innerHTML = `
    <h1>Biển số</h1>
    <p class="sub">Danh sách các xe đã từng được nhận diện. Bấm vào một biển để xem lịch trình di chuyển.</p>
    <form class="toolbar" id="f">
      <input type="search" name="q" placeholder="Tìm biển số" value="${esc(p.q || "")}">
      <button class="primary">Tìm</button>
    </form>
    <div id="list"></div>`;
  const form = document.getElementById("f");
  const load = async () => {
    setParams("#/plates", { q: form.q.value });
    const items = await api(`/api/plates?q=${encodeURIComponent(form.q.value)}`);
    const list = document.getElementById("list");
    if (!items.length) { list.innerHTML = `<div class="empty">Chưa có biển số nào.</div>`; return; }
    list.innerHTML = `<div class="table-wrap"><table>
      <thead><tr><th>Ảnh</th><th>Biển số</th><th>Số lần</th><th>Camera</th><th>Lần đầu</th><th>Lần cuối</th></tr></thead>
      <tbody>${items.map((r) => `<tr data-plate="${esc(r.plate)}">
        <td>${r.plate_img ? `<img class="crop" loading="lazy" src="${img(r.plate_img)}" alt="">` : ""}</td>
        <td><span class="plate">${esc(r.plate)}</span></td>
        <td class="num">${r.sightings}</td>
        <td>${(r.cameras || "").split(",").map((c) => `<span class="badge">${esc(camName(c))}</span>`).join(" ")}</td>
        <td class="num">${fmtDateTime(r.first_seen)}</td>
        <td class="num">${ago(r.last_seen)}</td>
      </tr>`).join("")}</tbody></table></div>`;
    list.querySelectorAll("tr[data-plate]").forEach((tr) => (tr.onclick = () => (location.hash = plateLink(tr.dataset.plate))));
  };
  form.onsubmit = (e) => { e.preventDefault(); load(); };
  await load();
}

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
    <p class="sub" style="margin-bottom:10px"><a href="#/plates">‹ Biển số</a></p>
    <div class="plate-head">
      <span class="plate lg">${esc(plate)}</span>
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

  document.getElementById("f").onsubmit = (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    location.hash = `${plateLink(plate)}?${new URLSearchParams(f)}`;
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
const ZONE_CLASSES = [["person", "Người"], ["car", "Ô tô"], ["bike", "Xe máy / xe đạp"]];
const ZONE_COLORS = ["#ff3b30", "#ff9500", "#ffcc00", "#34c759", "#5ac8fa", "#af52de"];
const zoneColor = (i) => ZONE_COLORS[i % ZONE_COLORS.length];

function zonesSvg(zones, w = 1000, h = 562) {
  return `<svg class="zone-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${zones.map((z, i) =>
    `<polygon points="${z.points.map(([x, y]) => `${x * w},${y * h}`).join(" ")}" style="--zc:${zoneColor(i)}" class="${z.enabled ? "" : "off"}"></polygon>`).join("")}</svg>`;
}

async function viewCameras() {
  const [cams, zones] = await Promise.all([api("/api/cameras"), api("/api/zones")]);
  cameras = cams;
  $view.innerHTML = `
    <div class="head-row"><h1>Camera</h1><button class="primary" id="cam-add">+ Thêm camera</button></div>
    <p class="sub">Bật tính năng cho từng camera. AI chỉ chạy khi khung hình có chuyển động nên camera đứng yên gần như không tốn CPU.</p>
    ${cameras.length ? "" : `<div class="empty">Chưa có camera nào. Bấm “Thêm camera” để thêm camera có RTSP (Dahua, Imou, Hikvision, EZVIZ, Tapo…).</div>`}
    <div class="cam-grid">${cameras.map((c) => camHtml(c, zones.filter((z) => z.camera === c.id))).join("")}</div>`;
  document.getElementById("cam-add").onclick = () => openCameraForm(null);
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
    el.querySelector("form").onsubmit = async (e) => {
      e.preventDefault();
      const f = e.target;
      await api(`/api/cameras/${encodeURIComponent(id)}`, {
        method: "PATCH",
        body: JSON.stringify({
          name: f.elements.name.value, location: f.elements.location.value,
          lpr_enabled: f.elements.lpr.checked, face_enabled: f.elements.face.checked, track_enabled: f.elements.track.checked,
        }),
      });
      toast("Đã lưu. Worker sẽ áp dụng trong khoảng 15 giây.");
      cameras = await api("/api/cameras");
    };
    loadSnap();
  });
}

function camHtml(c, zones) {
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
      <div class="status">${status}<span class="grow"></span><span>${c.sightings} lượt · stream <code>${esc(c.id)}</code></span></div>
      <div class="row"><input name="name" value="${esc(c.name)}" placeholder="Tên camera" aria-label="Tên camera"></div>
      <div class="row"><input name="location" value="${esc(c.location)}" placeholder="Vị trí (vd: cổng trước)" aria-label="Vị trí"></div>
      <div class="row wrap">
        <label class="check"><input type="checkbox" name="lpr" ${c.lpr_enabled ? "checked" : ""}> Biển số</label>
        <label class="check"><input type="checkbox" name="face" ${c.face_enabled ? "checked" : ""}> Khuôn mặt</label>
        <label class="check"><input type="checkbox" name="track" ${c.track_enabled ? "checked" : ""}> Đếm người qua lại</label>
        <button type="button" data-zones>Vùng cảnh báo${zones.length ? ` (${zones.length})` : ""}</button>
      </div>
      <div class="row src-row"><span class="src" title="${esc(c.source || "")}">${c.source ? `Nguồn: <code>${esc(c.source)}</code>` : "Nguồn: không thấy trong go2rtc"}</span></div>
      <div class="row wrap cam-actions">
        <button type="button" data-source>Đổi link</button>
        <button type="button" class="danger-text" data-delete>Xoá</button>
        <span style="flex:1"></span>
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
        <label class="check"><input type="checkbox" name="track"> Đếm người qua lại</label>
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
          lpr_enabled: f.elements.lpr.checked, face_enabled: f.elements.face.checked, track_enabled: f.elements.track.checked,
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
    <p class="sub">Người / xe đi vào vùng cảnh báo và các khuôn mặt camera bắt được.</p>
    <div class="stats" id="stats"></div>
    <form class="toolbar" id="filters">
      <select name="kind">${kinds.map(([k, l]) => `<option value="${k}" ${p.kind === k ? "selected" : ""}>${l}</option>`).join("")}</select>
      <select name="camera"><option value="">Tất cả camera</option>${camOpts}</select>
      <input type="date" name="date_from" value="${esc(p.date_from || "")}" title="Từ ngày">
      <input type="date" name="date_to" value="${esc(p.date_to || "")}" title="Đến ngày">
      <button class="primary" type="submit">Lọc</button>
      <span class="grow"></span>
      <label class="check"><input type="checkbox" id="auto" checked> Tự làm mới</label>
    </form>
    <div id="list"></div>`;
  const form = document.getElementById("filters");
  let offset = Number(p.offset || 0);
  const load = async () => {
    const f = Object.fromEntries(new FormData(form));
    setParams("#/events", { ...f, offset: offset || "" });
    const [stats, data] = await Promise.all([api("/api/stats"), api(`/api/events?${new URLSearchParams({ ...f, limit: PAGE_SIZE, offset })}`)]);
    document.getElementById("stats").innerHTML = [
      ["Vào vùng hôm nay", stats.today_zone_events],
      ["Khuôn mặt hôm nay", stats.today_face_events],
      ["Kết quả lọc", data.total],
    ].map(([l, v]) => `<div class="stat"><div class="label">${l}</div><div class="value">${v}</div></div>`).join("");
    const list = document.getElementById("list");
    if (!data.items.length) { list.innerHTML = `<div class="empty">Chưa có cảnh báo nào. Vẽ vùng cảnh báo hoặc bật “Khuôn mặt” ở trang Camera.</div>`; return; }
    list.innerHTML = `<div class="grid">${data.items.map(eventCardHtml).join("")}</div>
      <div class="pager">
        <button ${offset === 0 ? "disabled" : ""} data-go="${offset - PAGE_SIZE}">‹ Mới hơn</button>
        <span>${offset + 1}–${offset + data.items.length} / ${data.total}</span>
        <button ${offset + PAGE_SIZE >= data.total ? "disabled" : ""} data-go="${offset + PAGE_SIZE}">Cũ hơn ›</button>
      </div>`;
    list.querySelectorAll(".card").forEach((el, i) => (el.onclick = () => openEvent(data.items[i], load)));
    list.querySelectorAll("[data-go]").forEach((b) => (b.onclick = () => { offset = Math.max(0, Number(b.dataset.go)); load(); window.scrollTo(0, 0); }));
  };
  form.onsubmit = (e) => { e.preventDefault(); offset = 0; load(); };
  const auto = document.getElementById("auto");
  refreshTimer = setInterval(() => { if (auto.checked && !$modal.open && offset === 0) load(); }, 10000);
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

// ---------- Người qua lại ----------
const visitorName = (v) => v.person_name || v.label || `Người #${v.id}`;

async function viewVisitors() {
  const p = readParams();
  const today = isoDate(new Date());
  const from = p.date_from || today, to = p.date_to || today;
  const camOpts = cameras.map((c) => `<option value="${esc(c.id)}" ${p.camera === c.id ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  $view.innerHTML = `
    <h1>Người qua lại</h1>
    <p class="sub">Mỗi thẻ là một người, nhận ra lại theo màu quần áo và dáng người. Bấm vào để xem người đó đi qua lúc nào. Bật “Đếm người qua lại” cho camera ở trang Camera.</p>
    <form class="toolbar" id="f">
      <span>Từ</span><input type="date" name="date_from" value="${from}">
      <span>đến</span><input type="date" name="date_to" value="${to}">
      <select name="camera"><option value="">Tất cả camera</option>${camOpts}</select>
      <input type="search" name="q" class="search-q" value="${esc(p.q || "")}" placeholder="Tìm: áo trắng, xe máy vàng, áo GSM…" enterkeyhint="search">
      <button class="primary">Xem</button>
      <span class="grow"></span>
      <label class="check"><input type="checkbox" id="auto" checked> Tự làm mới</label>
    </form>
    <p class="hint" id="search-note" hidden></p>
    <div id="list"></div>`;
  const form = document.getElementById("f");
  const list = document.getElementById("list");
  const PAGE = 60;
  let items = [], done = false, busy = false;
  const card = (v, i) => `
      <div class="visitor" data-i="${i}">
        <img loading="lazy" src="${img(v.img)}" alt="">
        <div class="body">
          <div class="row"><b>${esc(visitorName(v))}</b><span class="badge ${v.visits >= 3 ? "warn" : ""}">${v.visits} lượt</span></div>
          ${v.suggestions ? `<div class="meta"><span class="badge accent">Có thể trùng người khác</span></div>` : ""}
          <div class="meta">${fmtTime(v.first_seen)}${v.last_seen - v.first_seen >= 60 ? ` – ${fmtTime(v.last_seen)}` : ""} · ${(v.cameras || "").split(",").map((c) => esc(camName(c))).join(", ")}</div>
        </div>
      </div>`;
  const fetchPage = (offset, limit) => {
    const f = Object.fromEntries(new FormData(form));
    setParams("#/visitors", f);
    return api(`/api/visitors?${new URLSearchParams({ ...f, offset, limit })}`);
  };
  const note = document.getElementById("search-note");
  const render = () => {
    if (!items.length) {
      list.innerHTML = `<div class="empty">${form.q.value.trim() ? "Không tìm thấy ai khớp mô tả." : "Chưa có ai trong khoảng thời gian này."}</div>`;
      return;
    }
    list.innerHTML = `<div class="visitor-grid">${items.map(card).join("")}</div>
      <div id="more" class="empty more">${done ? `${items.length} người` : `<button type="button">Tải thêm</button>`}</div>`;
    list.querySelectorAll(".visitor").forEach((el) => (el.onclick = () => openVisitor(items[el.dataset.i].id, form, items, load)));
    const more = document.getElementById("more");
    if (!done) {
      more.querySelector("button").onclick = loadMore;
      observer.observe(more);
    }
  };
  // tải từng trang PAGE người khi cuộn gần tới cuối danh sách (hoặc bấm “Tải thêm”)
  const observer = new IntersectionObserver((entries) => { if (entries.some((e) => e.isIntersecting)) loadMore(); },
    { rootMargin: "600px" });
  async function loadMore() {
    if (busy || done) return;
    busy = true;
    try {
      const more = await fetchPage(items.length, PAGE);
      done = more.length < PAGE;
      items = items.concat(more);
      render();
    } finally { busy = false; }
  }
  // tải lại từ đầu (đổi bộ lọc) hoặc làm mới những người đang hiển thị (tự làm mới, sau khi gộp / sửa)
  const load = async (reset = false) => {
    observer.disconnect();
    if (reset) showSearchNote();
    const limit = reset ? PAGE : Math.min(500, Math.max(PAGE, items.length));
    busy = true;
    try {
      items = await fetchPage(0, limit);
      done = items.length < limit;
      render();
    } finally { busy = false; }
  };
  // tìm theo mô tả: báo cách xếp và số ảnh còn đang được chuẩn bị (lượt mới / dữ liệu cũ đang tính bù)
  async function showSearchNote() {
    const q = form.q.value.trim();
    note.hidden = !q;
    if (!q) return;
    const st = await api("/api/visitor-search/status").catch(() => ({ pending: 0 }));
    note.textContent = `Xếp theo mức giống “${q}”, giống nhất trước. Tìm tốt nhất với màu áo / mũ / xe; không đọc được chữ trên áo.`
      + (st.pending > 50 ? ` Còn ${st.pending.toLocaleString("vi-VN")} ảnh đang được chuẩn bị để tìm.` : "");
  }
  form.onsubmit = (e) => { e.preventDefault(); load(true); };
  const auto = document.getElementById("auto");
  refreshTimer = setInterval(() => { if (auto.checked && !$modal.open && !busy) load(); }, 15000);
  await load(true);
}

function aiVerdictText(a) {
  const label = { same: "cùng người", different: "khác người", unsure: "chưa chắc" }[a.verdict] || a.verdict;
  return `<b>AI: ${label}${a.conf != null && a.verdict !== "unsure" ? ` ${Math.round(a.conf * 100)}%` : ""}</b>${a.reason ? ` · ${esc(a.reason)}` : ""}`;
}

async function openVisitor(vid, form, others, onChange) {
  const f = Object.fromEntries(new FormData(form));
  const v = await api(`/api/visitors/${vid}?${new URLSearchParams({ date_from: f.date_from, date_to: f.date_to })}`);
  const visits = v.visits;
  // biểu đồ 24 giờ: mỗi lượt là một vạch trên thanh của ngày đó
  const days = {};
  for (const x of visits) (days[dayKey(x.ts)] ??= []).push(x);
  const chart = Object.keys(days).sort().reverse().map((k) => {
    const start = new Date(k + "T00:00:00").getTime() / 1000;
    return `<div class="day">${fmtDayLabel(start)}</div><div class="track">${days[k].map((x) =>
      `<div class="bar" style="left:${(x.ts - start) / 864}%;width:${Math.max(0.4, (x.last_seen - x.ts) / 864)}%;background:${camColor(x.camera)}" title="${esc(x.camera_name || x.camera)} ${fmtTime(x.ts)}"></div>`).join("")}</div>`;
  }).join("");
  const axis = [0, 3, 6, 9, 12, 15, 18, 21, 24].map((h) => `<span style="left:${(h / 24) * 100}%">${h}h</span>`).join("");
  $modalBody.innerHTML = `
    <div class="modal-head">
      <b>${esc(visitorName(v))} · ${visits.length} lượt</b>
      <button class="icon-btn" id="m-close" aria-label="Đóng">×</button>
    </div>
    <div class="person-modal">
      ${visits.length ? `<div class="daychart">${chart}<div></div><div class="axis">${axis}</div></div>` : ""}
      ${v.suggestions.length ? `<section>
        <h2>Có thể là cùng một người?</h2>
        <p class="hint" style="margin:0 0 8px">Hệ thống thấy khá giống nhưng chưa đủ chắc để tự gộp. Kiểm tra ảnh rồi chọn.</p>
        <div class="visit-list">${v.suggestions.map((g, i) => `
          <div class="visit sugg">
            <img loading="lazy" src="${img(g.img)}" alt="Ảnh ${esc(visitorName(g))}" data-sugg="${i}">
            <div>
              <b>${esc(visitorName(g))}</b>
              <div class="meta">${g.visits} lượt · giống ${Math.round(g.sim * 100)}%${g.ts ? ` · ${fmtTime(g.ts)} ${esc(g.camera_name || "")}` : ""}</div>
              ${g.ai ? `<div class="ai-note ${g.ai.verdict}">${aiVerdictText(g.ai)}</div>` : ""}
              <div class="sugg-actions">
                <button type="button" class="primary" data-merge="${g.id}">Đúng, gộp</button>
                <button type="button" data-dismiss="${g.id}">Không phải</button>
              </div>
            </div>
          </div>`).join("")}</div>
      </section>` : ""}
      <section>
        <h2>Các lần đi qua</h2>
        <div class="visit-list">${visits.map((x, i) => `
          <div class="visit">
            <img loading="lazy" src="${img(x.img)}" alt="" data-frame="${i}">
            <div>
              <b>${fmtTime(x.ts)}${x.last_seen - x.ts >= 60 ? ` – ${fmtTime(x.last_seen)}` : ""}</b> <span class="meta">${fmtDate(x.ts)}</span>
              <div class="meta">${esc(x.camera_name || x.camera)} · ${x.last_seen - x.ts >= 5 ? `ở lại ${fmtDuration(x.last_seen - x.ts)}` : "đi qua"}</div>
              <div class="meta">${x.similarity ? `giống ${Math.round(x.similarity * 100)}% với lần trước` : "lần đầu thấy"}</div>
              ${x.ai_verdict === "same" ? `<div class="ai-note same">${aiVerdictText({ verdict: "same", conf: x.ai_conf, reason: x.ai_reason })}</div>` : ""}
            </div>
            <div class="visit-btns">
              ${visits.length > 1 ? `<button type="button" class="ghost" data-split="${x.id}" title="Không phải người này — tách thành người riêng">Tách</button>` : ""}
              <button type="button" class="icon-btn" data-del="${x.id}" title="Xoá lượt này">×</button>
            </div>
          </div>`).join("")}</div>
      </section>
      <form id="m-v" class="zone-form">
        ${v.person_name ? `<p class="hint" style="margin:0">Đã nhận ra khuôn mặt: <b>${esc(v.person_name)}</b></p>` : ""}
        <label>Đặt tên<input name="label" value="${esc(v.label)}" placeholder="vd: Shipper áo xanh"></label>
        <label>Hệ thống tách nhầm thành 2 người? Gộp vào:
          <select name="merge"><option value="">— Không gộp —</option>${others.filter((o) => o.id !== vid).map((o) => `<option value="${o.id}">${esc(visitorName(o))} (${o.visits} lượt)</option>`).join("")}</select>
        </label>
        <div class="actions"><button class="primary">Lưu</button></div>
      </form>
    </div>`;
  if (!$modal.open) $modal.showModal();
  document.getElementById("m-close").onclick = () => $modal.close();
  // xem ảnh lớn: mỗi lượt có ảnh người và ảnh toàn cảnh, vuốt / mũi tên để chuyển
  const visitCap = (x) => `${fmtTime(x.ts)} ${fmtDate(x.ts)} · ${x.camera_name || x.camera}`;
  const gallery = visits.flatMap((x) => [
    { src: img(x.img), caption: visitCap(x) },
    ...(x.frame_img ? [{ src: img(x.frame_img), caption: `${visitCap(x)} · toàn cảnh` }] : []),
  ]);
  const galleryStart = visits.map((x, i) => visits.slice(0, i).reduce((n, y) => n + (y.frame_img ? 2 : 1), 0));
  $modalBody.querySelectorAll("[data-frame]").forEach((im) => (im.onclick = () => openViewer(gallery, galleryStart[im.dataset.frame])));
  // gợi ý: đặt ảnh người này cạnh ảnh người kia để so, rồi tới ảnh toàn cảnh của người kia
  $modalBody.querySelectorAll("[data-sugg]").forEach((im) => (im.onclick = () => {
    const g = v.suggestions[im.dataset.sugg];
    const caption = `${visitorName(g)} · giống ${Math.round(g.sim * 100)}%`;
    const items = [{ caption, pair: [
      ...(visits[0] ? [{ src: img(visits[0].img), label: visitorName(v) }] : []),
      { src: img(g.img), label: visitorName(g) },
    ] }];
    if (g.frame_img) items.push({ src: img(g.frame_img), caption: `${visitorName(g)} · ${fmtTime(g.ts)} ${g.camera_name || ""} · toàn cảnh` });
    openViewer(items, 0);
  }));
  $modalBody.querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => {
    if (!confirm("Xoá lượt này khỏi người này?")) return;
    await api(`/api/visits/${b.dataset.del}`, { method: "DELETE" });
    onChange?.();
    if (visits.length > 1) openVisitor(vid, form, others, onChange); else $modal.close();
  }));
  $modalBody.querySelectorAll("[data-split]").forEach((b) => (b.onclick = async () => {
    await api(`/api/visits/${b.dataset.split}/split`, { method: "POST" });
    toast("Đã tách thành người riêng");
    onChange?.();
    openVisitor(vid, form, others, onChange);
  }));
  $modalBody.querySelectorAll("[data-merge]").forEach((b) => (b.onclick = async () => {
    await api(`/api/visitors/${b.dataset.merge}`, { method: "PATCH", body: JSON.stringify({ merge_into: vid }) });
    toast("Đã gộp");
    onChange?.();
    openVisitor(vid, form, others, onChange);
  }));
  $modalBody.querySelectorAll("[data-dismiss]").forEach((b) => (b.onclick = async () => {
    await api(`/api/visitors/${vid}`, { method: "PATCH", body: JSON.stringify({ dismiss: Number(b.dataset.dismiss) }) });
    onChange?.();
    openVisitor(vid, form, others, onChange);
  }));
  document.getElementById("m-v").onsubmit = async (e) => {
    e.preventDefault();
    const el = e.target.elements;
    const body = el.merge.value ? { merge_into: Number(el.merge.value) } : { label: el.label.value };
    await api(`/api/visitors/${vid}`, { method: "PATCH", body: JSON.stringify(body) });
    toast(el.merge.value ? "Đã gộp" : "Đã lưu");
    $modal.close();
    onChange?.();
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
  box.innerHTML = list.map((x) => `<div class="session">
      <div><b>${esc(deviceName(x.agent))}</b>${x.current ? ` <span class="badge ok">Thiết bị này</span>` : ""}
        <div class="hint" style="margin:0">${esc(x.ip)} · mở lần cuối ${esc(ago(x.last_seen))} · hết hạn ${esc(fmtDate(x.expires_at))}</div></div>
      <button type="button" class="${x.current ? "" : "danger"}" data-sid="${esc(x.id)}" data-current="${x.current ? 1 : ""}">Đăng xuất</button>
    </div>`).join("") || `<span class="hint">Không có phiên nào.</span>`;
  box.querySelectorAll("[data-sid]").forEach((b) => (b.onclick = async () => {
    if (!confirm("Đăng xuất thiết bị này?")) return;
    await api(`/api/sessions/${b.dataset.sid}`, { method: "DELETE" });
    if (b.dataset.current) location.replace("/login"); else loadSessions();
  }));
}

async function viewSettings() {
  const [s, people] = await Promise.all([api("/api/settings"), api("/api/people")]);
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
        <p class="hint" style="margin:0 0 10px">Mỗi thiết bị giữ đăng nhập 30 ngày kể từ lần mở gần nhất. Mất điện thoại thì đăng xuất thiết bị đó ở đây.</p>
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

// ---------- khung ứng dụng (PWA, menu Thêm, đăng xuất) ----------
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
