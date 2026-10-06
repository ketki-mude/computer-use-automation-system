// Shared by the requests page and the control room: fetch helper, escaping, toasts, the live screen.

const POLL_INTERVAL_MS = 1000;
const TOAST_MS = 4500;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, body) {
  const init = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const response = await fetch(path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("; ") : data.detail;
    throw new Error(data.error || detail || response.statusText);
  }
  return data;
}

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => (el.hidden = true), TOAST_MS);
}

// Re-render a region only when its HTML changed, so focus, scroll and open sections survive polling.
const painted = {};
function paint(id, html) {
  if (painted[id] !== html) { $(id).innerHTML = html; painted[id] = html; }
}

function kv(obj, labels = {}) {
  const rows = Object.entries(obj || {}).map(([k, v]) =>
    `<dt>${esc(labels[k] || humanize(k))}</dt><dd>${esc(typeof v === "object" ? JSON.stringify(v) : v)}</dd>`);
  return rows.length ? `<dl class="kv">${rows.join("")}</dl>` : "";
}

function humanize(name) {
  const words = String(name).replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function ago(iso) {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!isFinite(seconds) || seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

// A request's status as a coloured pill with a plain word.
const STATUS = {
  routing: ["busy", "Starting"], running: ["busy", "Working on it"], discovering: ["busy", "Learning it"],
  needs_input: ["warn", "Needs a detail"], success: ["ok", "Answered"], discovered: ["ok", "Answered"],
  business_outcome: ["info", "Answered"], failed: ["bad", "Couldn't finish"], escalated: ["bad", "Couldn't finish"],
  done_by_staff: ["ok", "Done by staff"], cancelled: ["info", "Cancelled"],
};
function statusPill(job, overrides = {}) {
  if (job.ticket_id) return `<span class="pill human">Waiting for staff</span>`;
  const [tone, word] = overrides[job.status] || STATUS[job.status] || ["", job.status];
  return `<span class="pill ${tone}">${esc(word)}</span>`;
}

// The bank screen as a JPEG that refreshes while the request runs. Clicks are passed on only
// when `onClick` is given and the person viewing holds the request's ticket.
class LiveView {
  constructor(root, onClick) {
    root.innerHTML = `<div class="live"><img alt="the bank system's screen for this request" hidden>
      <div class="empty-screen">The bank screen appears here once the request starts.</div><div class="over"></div></div>`;
    this.box = root.querySelector(".live");
    this.img = root.querySelector("img");
    this.empty = root.querySelector(".empty-screen");
    this.over = root.querySelector(".over");
    this.job = null;
    this.held = false;
    this.img.addEventListener("click", (e) => {
      if (!onClick || !this.job) return;
      if (!this.held) return toast("Take the ticket first. Only the person holding it can use the screen.");
      const scaleX = this.img.naturalWidth / this.img.clientWidth, scaleY = this.img.naturalHeight / this.img.clientHeight;
      onClick(this.job, e.offsetX * scaleX, e.offsetY * scaleY);
    });
  }

  update(job, overlay, held = false) {
    const changedJob = !this.job || this.job.id !== job.id;
    this.job = job;
    this.held = held;
    this.box.classList.toggle("held", held);
    this.over.textContent = overlay;
    if (!job.has_frame) { this.img.hidden = true; this.empty.hidden = false; return; }
    if (job.live || changedJob || !this.img.dataset.final) {  // after the run, fetch the last screen once
      const next = new Image();
      next.onload = () => { this.img.src = next.src; };
      next.src = `/api/jobs/${job.id}/screen?t=${Date.now()}`;
      this.img.dataset.final = job.live ? "" : "1";
    }
    this.img.hidden = false;
    this.empty.hidden = true;
  }
}

function every(fn) { fn(); return setInterval(fn, POLL_INTERVAL_MS); }
