"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const view = $("#view");
const modal = $("#modal");
const modalBody = $("#modalBody");
const ICON_PLAY = '<svg viewBox="0 0 24 24"><path d="M7 4v16l13-8z"/></svg>';
const ICON_PLUS = '<svg viewBox="0 0 24 24"><path d="M11 5h2v6h6v2h-6v6h-2v-6H5v-2h6z"/></svg>';
const ICON_CHECK = '<svg viewBox="0 0 24 24"><path d="M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4z"/></svg>';

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const api = async (path) => {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return r.json();
};
const fmtTime = (s) => { s = Math.round(s || 0); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60); return h ? `${h}h ${m}m` : `${m}m`; };
const cardImg = (t) => t.backdrop || t.thumbnail || t.poster;

/* ---------- per-viewer storage (continue watching, my list) ---------- */
const store = {
  get(key) { try { return JSON.parse(localStorage.getItem(key)) || {}; } catch { return {}; } },
  set(key, val) { try { localStorage.setItem(key, JSON.stringify(val)); } catch { /* private mode etc. */ } },
};
const cardInfo = (t) => ({ id: t.id, name: t.name, year: t.year, thumbnail: t.thumbnail, backdrop: t.backdrop, poster: t.poster });
const progress = {
  all: () => store.get("or.progress"),
  get: (id) => progress.all()[id],
  save(t, v, time, dur) {
    const all = progress.all();
    if (dur && time > dur - 90 && v === (t.videos.length - 1)) delete all[t.id];
    else all[t.id] = { ...cardInfo(t), v, t: time, d: dur, at: Date.now() };
    store.set("or.progress", all);
  },
  list: () => Object.values(progress.all()).sort((a, b) => b.at - a.at).slice(0, 30),
};
const myList = {
  all: () => store.get("or.list"),
  has: (id) => id in myList.all(),
  toggle(t) { const all = myList.all(); if (all[t.id]) delete all[t.id]; else all[t.id] = { ...cardInfo(t), at: Date.now() }; store.set("or.list", all); },
  list: () => Object.values(myList.all()).sort((a, b) => b.at - a.at),
};

/* ---------- analytics (anonymous, first-party; see /admin) ---------- */
const track = (() => {
  let vid;
  try { vid = localStorage.getItem("mt.vid"); } catch { /* ignore */ }
  if (!vid) {
    vid = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`).toLowerCase();
    try { localStorage.setItem("mt.vid", vid); } catch { /* ignore */ }
  }
  let queue = [];
  let timer;
  const flush = () => {
    clearTimeout(timer);
    if (!queue.length) return;
    const body = JSON.stringify({ v: vid, events: queue });
    queue = [];
    if (!(navigator.sendBeacon && navigator.sendBeacon("/api/events", body))) {
      fetch("/api/events", { method: "POST", body, keepalive: true }).catch(() => {});
    }
  };
  addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") flush(); });
  addEventListener("pagehide", flush);
  return (type, data = {}) => {
    queue.push({ t: type, ...data });
    clearTimeout(timer);
    if (queue.length >= 20) flush(); else timer = setTimeout(flush, 3000);
  };
})();
let landed = false;
function trackPageview(section) {
  const data = { l: `/${section || ""}` };
  if (!landed) {
    landed = true;
    data.n = 1; // landing page of this visit
    try {
      const ref = document.referrer && new URL(document.referrer).hostname;
      if (ref && ref !== location.hostname) data.r = ref.replace(/^www\./, "");
    } catch { /* ignore */ }
  }
  track("pageview", data);
}

/* ---------- components ---------- */
function card(t) {
  const img = cardImg(t);
  const p = t.href ? null : progress.get(t.id);
  const pct = p && p.d ? Math.min(100, (p.t / p.d) * 100) : 0;
  return `<a class="card" href="${t.href || `#/title/${t.id}`}" title="${esc(t.name)}">
    ${img ? `<img src="${esc(img)}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'fallback',textContent:this.closest('.card').title}))">` : `<div class="fallback">${esc(t.name)}</div>`}
    <div class="shade"></div>
    <div class="label">${esc(t.name)}${t.year ? `<small>${t.year}</small>` : ""}</div>
    ${pct ? `<div class="bar" style="width:${pct}%"></div>` : ""}
  </a>`;
}

function row(title, items, link) {
  return `<section class="row">
    <h2>${esc(title)}${link ? ` <a href="${link}">Explore all &rsaquo;</a>` : ""}</h2>
    <div class="slider">
      <button class="arrow left" aria-label="Scroll left">&lsaquo;</button>
      <div class="track">${items.map(card).join("")}</div>
      <button class="arrow right" aria-label="Scroll right">&rsaquo;</button>
    </div>
  </section>`;
}

function metaLine(t) {
  const bits = [];
  if (t.rating) bits.push(`<span class="score">${Math.round(t.rating * 10)}% rating</span>`);
  if (t.year) bits.push(`<span>${t.year}</span>`);
  if (t.runtime_min) bits.push(`<span>${fmtTime(t.runtime_min * 60)}</span>`);
  return `<div class="meta">${bits.join("")}</div>`;
}

const skeleton = () => `<div class="page skeleton"><div class="grid">${'<div class="card"></div>'.repeat(18)}</div></div>`;

/* ---------- views ---------- */
async function renderHome() {
  view.innerHTML = skeleton();
  const data = await api("/api/home");
  if (!data.hero) {
    const s = await api("/api/status");
    view.innerHTML = `<div class="building"><h1>Building the library…</h1>
      <p>The harvester is collecting films from the Internet Archive${s.harvest.running ? " right now" : ""}.
      Titles appear as they're added; refresh in a minute.</p></div>`;
    return;
  }
  const h = data.hero;
  const cont = progress.list();
  view.innerHTML = `
    <section class="hero">
      <div class="hero-bg ${h.backdrop ? "" : "blur"}" style="background-image:url('${esc(h.backdrop || h.thumbnail)}')"></div>
      <div class="hero-content">
        <h1>${esc(h.name)}</h1>
        ${metaLine(h)}
        <p>${esc(h.description || "")}</p>
        <div class="btns">
          <a class="btn btn-play" href="#/watch/${h.id}">${ICON_PLAY} Play</a>
          <a class="btn btn-ghost" href="#/title/${h.id}">More info</a>
        </div>
      </div>
    </section>
    <div class="rows">
      ${cont.length ? row("Continue Watching", cont) : ""}
      ${data.rows.map((r) => row(r.title, r.items, r.category_id ? `#/genre/${r.category_id}` : null)).join("")}
    </div>`;
}

async function renderGenres() {
  view.innerHTML = skeleton();
  const cats = await api("/api/categories");
  view.innerHTML = `<div class="page"><h1>Genres</h1><div class="chips">
    ${cats.map((c) => `<a class="chip" href="#/genre/${c.id}">${esc(c.name)}<small>${c.n}</small></a>`).join("") || '<p class="empty">No genres yet.</p>'}
  </div></div>`;
}

async function renderGenre(id) {
  view.innerHTML = skeleton();
  let page = 1;
  let sort = "popular";
  const load = async (append) => {
    const data = await api(`/api/category/${id}?page=${page}&sort=${sort}`);
    if (!append) {
      view.innerHTML = `<div class="page"><h1>${esc(data.name)}</h1>
        <div class="toolbar"><label>Sort <select id="sort">
          <option value="popular">Most popular</option><option value="rating">Top rated</option>
          <option value="year">Newest</option><option value="az">A–Z</option></select></label></div>
        <div class="grid" id="grid"></div>
        <button class="btn btn-ghost more" id="more">Load more</button></div>`;
      $("#sort").value = sort;
      $("#sort").onchange = (e) => { sort = e.target.value; page = 1; load(false); };
      $("#more").onclick = () => { page++; load(true); };
    }
    $("#grid").insertAdjacentHTML("beforeend", data.items.map(card).join(""));
    $("#more").hidden = data.items.length < 60;
  };
  await load(false);
}

async function renderSearch(term) {
  $("#searchInput").value = term;
  view.innerHTML = skeleton();
  const data = await api(`/api/search?q=${encodeURIComponent(term)}`);
  clearTimeout(renderSearch.timer);
  renderSearch.timer = setTimeout(() => track("search", { l: term, n: data.items.length }), 1500);
  view.innerHTML = `<div class="page"><h1>Results for “${esc(term)}”</h1>
    ${data.items.length ? `<div class="grid">${data.items.map(card).join("")}</div>` : '<p class="empty">Nothing in the free library. Try another title or a genre like “noir” or “western”.</p>'}
    <div id="newResults"></div></div>`;
  if (!discoverOn) return;
  const more = await api(`/api/discover/search?q=${encodeURIComponent(term)}`).catch(() => ({ items: [] }));
  if (more.items.length && $("#newResults")) {
    $("#newResults").innerHTML = `<h2 style="margin:36px 0 4px">Newer movies</h2>
      <p class="sub">Not hosted here. Open one to see where it's streaming.</p>
      <div class="grid">${more.items.map(card).join("")}</div>`;
  }
}

function renderList() {
  const items = myList.list();
  view.innerHTML = `<div class="page"><h1>My List</h1>
    ${items.length ? `<div class="grid">${items.map(card).join("")}</div>` : '<p class="empty">Titles you add with “My List” show up here.</p>'}</div>`;
}

/* ---------- title modal ---------- */
async function openTitle(id) {
  modal.hidden = false;
  document.body.style.overflow = "hidden";
  modalBody.innerHTML = '<div class="m-hero"></div><div class="m-body"><p>Loading…</p></div>';
  const t = await api(`/api/title/${id}`);
  track("title_open", { e: t.id });
  const p = progress.get(t.id);
  const inList = myList.has(t.id);
  const multi = t.videos.length > 1;
  modalBody.innerHTML = `
    <div class="m-hero" style="background-image:url('${esc(t.backdrop || t.thumbnail)}')">
      <div class="hero-content">
        <h2 id="modalTitle">${esc(t.name)}</h2>
        <div class="btns">
          <a class="btn btn-play" href="#/watch/${t.id}">${ICON_PLAY} ${p ? "Resume" : "Play"}</a>
          <button class="btn btn-ghost" id="listBtn">${inList ? ICON_CHECK : ICON_PLUS} My List</button>
        </div>
      </div>
    </div>
    <div class="m-body">
      <div>${metaLine(t)}<p>${esc(t.description || "No description available.")}</p></div>
      <div class="m-side">
        ${t.genres.length ? `<div>Genres: ${t.genres.map((g) => `<a href="#/genre/${g.id}">${esc(g.name)}</a>`).join(", ")}</div>` : ""}
        ${t.source_url ? `<div>Source: <a href="${esc(t.source_url)}" target="_blank" rel="noopener">Internet Archive</a></div>` : ""}
        ${t.license ? `<div>License: <a href="${esc(t.license)}" target="_blank" rel="noopener">view</a></div>` : ""}
        ${t.imdb_id ? `<div><a href="https://www.imdb.com/title/${esc(t.imdb_id)}/" target="_blank" rel="noopener">IMDb page</a></div>` : ""}
      </div>
    </div>
    ${multi ? `<div class="m-section"><h3>Parts</h3><div class="parts">
      ${t.videos.map((v, i) => `<a class="part" href="#/watch/${t.id}/${i}"><b>${i + 1}</b><span>${esc(v.name || `Part ${i + 1}`)}</span>${v.duration_sec ? `<small>${fmtTime(v.duration_sec)}</small>` : ""}</a>`).join("")}
    </div></div>` : ""}
    ${t.similar.length ? `<div class="m-section"><h3>More like this</h3><div class="similar">${t.similar.map(card).join("")}</div></div>` : ""}`;
  $("#listBtn").onclick = (e) => {
    myList.toggle(t);
    e.currentTarget.innerHTML = `${myList.has(t.id) ? ICON_CHECK : ICON_PLUS} My List`;
  };
  $(".modal-card").scrollIntoView({ block: "start" });
}

function closeModal() {
  modal.hidden = true;
  document.body.style.overflow = "";
}

/* ---------- player ---------- */
let playerCleanup = null;
async function openPlayer(id, part) {
  const t = await api(`/api/title/${id}`);
  const saved = progress.get(t.id);
  let v = part != null ? part : saved ? saved.v : 0;
  if (!t.videos[v]) v = 0;

  const el = document.createElement("div");
  el.className = "player";
  el.innerHTML = `<div class="player-top">
      <button class="icon-btn" id="pBack" aria-label="Back">&larr;</button>
      <h1>${esc(t.name)}</h1>
      ${t.videos.length > 1 ? `<select id="pPart" aria-label="Part">${t.videos.map((x, i) => `<option value="${i}">${esc(x.name || `Part ${i + 1}`)}</option>`).join("")}</select>` : ""}
    </div>
    <video id="pVideo" controls autoplay playsinline preload="metadata"></video>
    <div class="spinner" id="pSpin" role="status"><div></div><span>Loading from the Internet Archive…</span></div>
    <div class="skips">
      <button class="skip" data-skip="-10" aria-label="Rewind 10 seconds" title="Rewind 10s (←)">
        <svg viewBox="0 0 48 48"><path d="M24 8a16 16 0 1 1-15.2 11" /><path d="M24 2v12l-8-6z" class="fill"/><text x="24" y="30">10</text></svg>
      </button>
      <button class="skip" data-skip="10" aria-label="Forward 10 seconds" title="Forward 10s (→)">
        <svg viewBox="0 0 48 48"><path d="M24 8a16 16 0 1 0 15.2 11" /><path d="M24 2v12l8-6z" class="fill"/><text x="24" y="30">10</text></svg>
      </button>
    </div>
    <div class="skip-toast" id="pToast" hidden></div>`;
  document.body.appendChild(el);
  document.body.style.overflow = "hidden";
  const video = $("#pVideo", el);

  const load = (i, resumeAt) => {
    v = i;
    if ($("#pPart", el)) $("#pPart", el).value = String(i);
    video.src = `/api/stream/${t.videos[i].id}`;
    track("play", { e: t.id, n: i });
    if (resumeAt) video.addEventListener("loadedmetadata", () => { video.currentTime = resumeAt; }, { once: true });
    video.play().catch(() => {});
  };
  const spin = $("#pSpin", el);
  const setLoading = (on) => { spin.hidden = !on; };
  video.addEventListener("waiting", () => setLoading(true));
  video.addEventListener("loadstart", () => setLoading(true));
  video.addEventListener("playing", () => setLoading(false));
  video.addEventListener("pause", () => setLoading(false));
  const toast = $("#pToast", el);
  let toastTimer;
  const skip = (secs) => {
    if (!Number.isFinite(video.duration)) return;
    video.currentTime = Math.min(Math.max(video.currentTime + secs, 0), video.duration - 0.5);
    toast.textContent = secs < 0 ? `« ${-secs}s` : `${secs}s »`;
    toast.className = `skip-toast ${secs < 0 ? "left" : "right"}`;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 600);
  };
  el.querySelectorAll("[data-skip]").forEach((b) => b.addEventListener("click", () => { skip(Number(b.dataset.skip)); wake(); }));
  const onKey = (e) => {
    if (e.target instanceof Element && e.target.closest("select, input")) return;
    if (e.key === "ArrowLeft" || e.key === "j") { e.preventDefault(); skip(-10); wake(); }
    else if (e.key === "ArrowRight" || e.key === "l") { e.preventDefault(); skip(10); wake(); }
    else if (e.key === " " || e.key === "k") { e.preventDefault(); video.paused ? video.play() : video.pause(); wake(); }
  };
  document.addEventListener("keydown", onKey, true);
  // Watch time: count only real playback (small forward steps), report every 30s.
  let watched = 0;
  let lastPos = null;
  const beat = () => {
    if (watched < 1) return;
    const total = t.videos.reduce((a, x) => a + (x.duration_sec || 0), 0) || video.duration || 0;
    const before = t.videos.slice(0, v).reduce((a, x) => a + (x.duration_sec || 0), 0);
    track("watch", { e: t.id, n: Math.round(watched), n2: total ? Math.min(100, Math.round(((before + video.currentTime) / total) * 1000) / 10) : null });
    watched = 0;
  };
  video.addEventListener("timeupdate", () => {
    const pos = video.currentTime;
    if (lastPos !== null && !video.paused && pos > lastPos && pos - lastPos < 2) watched += pos - lastPos;
    lastPos = pos;
    if (watched >= 30) beat();
  });
  video.addEventListener("seeking", () => { lastPos = null; });
  let lastSave = 0;
  video.addEventListener("timeupdate", () => {
    if (Date.now() - lastSave < 5000) return;
    lastSave = Date.now();
    progress.save(t, v, video.currentTime, video.duration);
  });
  video.addEventListener("ended", () => {
    beat();
    progress.save(t, v, video.duration, video.duration);
    if (t.videos[v + 1]) load(v + 1, 0);
  });
  video.addEventListener("error", () => {
    setLoading(false);
    if (!$(".notice", el)) el.insertAdjacentHTML("beforeend", '<p class="notice">This video could not be loaded. The Internet Archive may be busy; try again shortly.</p>');
  });
  $("#pPart", el)?.addEventListener("change", (e) => load(Number(e.target.value), 0));
  $("#pBack", el).onclick = () => history.length > 1 ? history.back() : (location.hash = `#/title/${t.id}`);

  let idleTimer;
  const wake = () => { el.classList.remove("idle"); clearTimeout(idleTimer); idleTimer = setTimeout(() => el.classList.add("idle"), 2500); };
  el.addEventListener("mousemove", wake);
  el.addEventListener("touchstart", wake);
  wake();

  const resume = saved && saved.v === v && saved.t > 10 && (!saved.d || saved.t < saved.d - 60) ? saved.t : 0;
  load(v, resume);

  playerCleanup = () => {
    beat();
    if (video.currentTime > 0) progress.save(t, v, video.currentTime, video.duration);
    video.pause();
    video.removeAttribute("src");
    video.load();
    document.removeEventListener("keydown", onKey, true);
    el.remove();
    document.body.style.overflow = "";
    clearTimeout(idleTimer);
  };
}

/* ---------- new & popular (TMDB + where to watch) ---------- */
let discoverOn = false;
const ICON_OUT = '<svg viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9M18 14v6H4V6h6"/></svg>';
const region = {
  get() { try { return localStorage.getItem("or.region") || ""; } catch { return ""; } },
  set(v) { try { localStorage.setItem("or.region", v); } catch { /* ignore */ } },
  qs() { const r = region.get(); return r ? `region=${encodeURIComponent(r)}` : ""; },
};

async function regionSelect(current) {
  let regions = [];
  try { regions = await api("/api/discover/regions"); } catch { /* keep default */ }
  return `<label class="region toolbar">Country <select id="regionSel">
    ${regions.map((r) => `<option value="${esc(r.code)}"${r.code === current ? " selected" : ""}>${esc(r.name)}</option>`).join("")}
  </select></label>`;
}

async function renderNew() {
  view.innerHTML = skeleton();
  const data = await api(`/api/discover?${region.qs()}`);
  const h = data.hero;
  view.innerHTML = `
    ${h ? `<section class="hero">
      <div class="hero-bg" style="background-image:url('${esc(h.backdrop)}')"></div>
      <div class="hero-content">
        <h1>${esc(h.name)}</h1>
        ${metaLine(h)}
        <p>${esc(h.description || "")}</p>
        <div class="btns"><a class="btn btn-play" href="${h.href}">Where to watch</a></div>
      </div>
    </section>` : ""}
    <div class="${h ? "rows" : "rows row-page"}">
      <div class="page-head" style="padding:0 var(--gutter)"><p class="sub" style="margin:0;padding:0">Newer movies aren't hosted here. Pick one to see where it's streaming.</p>${await regionSelect(data.region)}</div>
      <div style="height:18px"></div>
      ${data.rows.map((r) => row(r.title, r.items)).join("")}
    </div>`;
  $("#regionSel")?.addEventListener("change", (e) => { region.set(e.target.value); renderNew(); });
}

async function openNew(id) {
  modal.hidden = false;
  document.body.style.overflow = "hidden";
  modalBody.innerHTML = '<div class="m-hero"></div><div class="m-body"><p>Loading…</p></div>';
  const m = await api(`/api/discover/movie/${id}?${region.qs()}`);
  track("new_open", { m: m.id, l: m.name });
  const whereHtml = m.where.length
    ? `<div class="where">${m.where.map((g) => `<div><h4>${esc(g.label)}</h4><div class="providers">
        ${g.providers.map((p) => `<a class="provider" href="${esc(p.url)}" target="_blank" rel="noopener sponsored">
          ${p.logo ? `<img src="${esc(p.logo)}" alt="" loading="lazy">` : ""}${esc(p.name)} ${ICON_OUT}</a>`).join("")}
      </div></div>`).join("")}</div>`
    : `<p class="empty" style="padding:0">Not available to stream in ${esc(m.region)} yet${m.year >= new Date().getFullYear() ? " — it may still be in theaters" : ""}.</p>`;
  modalBody.innerHTML = `
    <div class="m-hero" id="mHero" style="background-image:url('${esc(m.backdrop || m.poster)}')">
      <div class="hero-content">
        <h2 id="modalTitle">${esc(m.name)}</h2>
        <div class="btns">
          ${m.local_id ? `<a class="btn btn-free" href="#/watch/${m.local_id}">${ICON_PLAY} Watch free here</a>` : ""}
          ${m.trailer ? `<button class="btn ${m.local_id ? "btn-ghost" : "btn-play"}" id="trailerBtn">${ICON_PLAY} Trailer</button>` : ""}
        </div>
      </div>
    </div>
    <div class="m-body">
      <div>${metaLine(m)}${m.tagline ? `<p><em>${esc(m.tagline)}</em></p><br>` : ""}<p>${esc(m.overview || "No description available.")}</p></div>
      <div class="m-side">
        ${m.genres.length ? `<div>Genres: <span style="color:var(--text)">${m.genres.map(esc).join(", ")}</span></div>` : ""}
        <div><a href="https://www.themoviedb.org/movie/${m.id}" target="_blank" rel="noopener">View on TMDB</a></div>
      </div>
    </div>
    <div class="m-section"><h3>Where to watch <small style="color:var(--muted);font-weight:400">· ${esc(m.region)}</small></h3>
      ${whereHtml}
      <p class="note">Availability from JustWatch via TMDB. <a href="${esc(m.watch_page)}" target="_blank" rel="noopener">See all options</a></p>
    </div>
    ${m.similar.length ? `<div class="m-section"><h3>More like this</h3><div class="similar">${m.similar.map(card).join("")}</div></div>` : ""}`;
  modalBody.querySelectorAll(".provider").forEach((a) => a.addEventListener("click", () => {
    track("outbound", { m: m.id, l: a.textContent.trim(), l2: m.name });
  }));
  $("#trailerBtn")?.addEventListener("click", () => {
    track("trailer", { m: m.id, l: m.name });
    $("#mHero").outerHTML = `<div class="trailer"><iframe src="https://www.youtube-nocookie.com/embed/${encodeURIComponent(m.trailer)}?autoplay=1&rel=0"
      title="Trailer" allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowfullscreen></iframe></div>`;
  });
  $(".modal-card").scrollIntoView({ block: "start" });
}

/* ---------- router ---------- */
let baseRoute = null; // the page underneath the modal/player
async function route() {
  const wasOverlay = !modal.hidden || playerCleanup;
  if (playerCleanup) { playerCleanup(); playerCleanup = null; }
  const hash = location.hash.slice(1) || "/";
  const [, section, arg, arg2] = hash.split("/");
  trackPageview(section);
  document.querySelectorAll(".links a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === `#${hash}`));

  try {
    const isNewTitle = section === "new" && arg;
    if (section === "title" || section === "watch" || isNewTitle) {
      if (baseRoute === null) { baseRoute = isNewTitle ? "/new" : "/"; await (isNewTitle ? renderNew() : renderHome()); }
      if (section === "title") await openTitle(arg);
      else if (isNewTitle) await openNew(arg);
      else { closeModal(); await openPlayer(arg, arg2 != null ? Number(arg2) : null); }
      return;
    }
    closeModal();
    if (wasOverlay && hash === baseRoute) return; // back to the page underneath; keep its scroll
    baseRoute = hash;
    window.scrollTo(0, 0);
    if (section !== "search" && document.activeElement !== $("#searchInput")) $("#searchInput").value = "";
    if (section === "new") await renderNew();
    else if (section === "genres") await renderGenres();
    else if (section === "genre") await renderGenre(arg);
    else if (section === "search") await renderSearch(decodeURIComponent(arg || ""));
    else if (section === "list") renderList();
    else await renderHome();
  } catch (err) {
    console.error(err);
    view.innerHTML = `<div class="building"><h1>Something went wrong</h1><p>${esc(err.message)}</p></div>`;
  }
}

modal.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) location.hash = `#${baseRoute || "/"}`; });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !modal.hidden) location.hash = `#${baseRoute || "/"}`; });

// Slider arrows (event delegation, rows are re-rendered often).
document.addEventListener("click", (e) => {
  const arrow = e.target.closest(".arrow");
  if (!arrow) return;
  const track = arrow.parentElement.querySelector(".track");
  track.scrollBy({ left: (arrow.classList.contains("left") ? -1 : 1) * track.clientWidth * 0.9, behavior: "smooth" });
});

// Search as you type (debounced).
let searchTimer;
$("#searchInput").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const term = e.target.value.trim();
  searchTimer = setTimeout(() => { location.hash = term ? `#/search/${encodeURIComponent(term)}` : "#/"; }, 350);
});
$("#searchForm").addEventListener("submit", (e) => e.preventDefault());

window.addEventListener("scroll", () => $("#nav").classList.toggle("solid", window.scrollY > 40), { passive: true });
window.addEventListener("hashchange", route);
api("/api/status").then((s) => {
  $("#stats").textContent = `${s.titles.toLocaleString()} free films in the library.`;
  discoverOn = s.discover;
  $("#newLink").hidden = !discoverOn;
}).catch(() => {}).finally(route);
