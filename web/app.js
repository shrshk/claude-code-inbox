/* Claude Code Inbox PWA. No build step. Gesture logic lives in attachDrag() and intent(). */
(() => {
  const $ = (s) => document.querySelector(s);
  const SWIPE_FRACTION = 0.35; // of card width, for done / dismiss
  const SNOOZE_PX = 90; // upward drag distance for snooze
  const state = { items: [], snoozed: [], resolved: [], history: [], token: null, busy: false, loaded: false, view: "cards" };
  try { state.view = localStorage.getItem("ccinboxView") || "cards"; } catch (e) { /* ignore */ }

  try { state.token = localStorage.getItem("ccinboxToken"); } catch (e) { /* private mode */ }
  const q = new URLSearchParams(location.search);
  if (q.get("token")) {
    state.token = q.get("token");
    try { localStorage.setItem("ccinboxToken", state.token); } catch (e) { /* ignore */ }
    history.replaceState(null, "", location.pathname);
  }

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const icon = (id) => `<svg><use href="#${id}"/></svg>`;
  const label = (a) => ({ done: "Done", dismiss: "Dismissed", snooze: "Snoozed", reopen: "Back in the inbox" }[a] || a);
  const utc = (iso) => new Date(iso.endsWith("Z") ? iso : iso + "Z");
  const fmtDate = (iso) => iso ? utc(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "";
  function fmtUntil(iso) {
    if (!iso) return "";
    const d = utc(iso), now = new Date();
    const time = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    const dayDiff = Math.round((new Date(d.toDateString()) - new Date(now.toDateString())) / 86400000);
    return (dayDiff === 0 ? "Today" : dayDiff === 1 ? "Tomorrow" : d.toLocaleDateString(undefined, { weekday: "short" })) + " " + time;
  }

  // ---- api ---------------------------------------------------------------------
  async function api(path, opts = {}) {
    const headers = { "Content-Type": "application/json" };
    if (state.token) headers["X-Token"] = state.token;
    const res = await fetch(path, { ...opts, headers });
    if (res.status === 401) { showTokenModal(); throw new Error("unauthorized"); }
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    return res.json();
  }

  let elapsedTimer = null;
  function setLoading(on) {
    const el = $("#loading");
    el.classList.toggle("hidden", !on);
    $("#deck").classList.toggle("hidden", on);
    $("#hints").classList.toggle("hidden", on);
    $("#actions").classList.toggle("hidden", on);
    $("#empty").classList.add("hidden");
    clearInterval(elapsedTimer);
    if (on) {
      const t0 = performance.now();
      $("#elapsed").textContent = "0.0s";
      elapsedTimer = setInterval(() => { $("#elapsed").textContent = ((performance.now() - t0) / 1000).toFixed(1) + "s"; }, 100);
    }
  }

  async function load({ quiet = false } = {}) {
    if (!quiet && !state.loaded) setLoading(true);
    $("#refresh").classList.add("spinning");
    try {
      const data = await api("/api/items");
      if (!state.loaded) await sleep(350); // let the loader read as a state, not a flicker
      state.items = data.items; state.snoozed = data.snoozed; state.resolved = data.resolved; state.loaded = true;
      setLoading(false);
      render();
    } catch (err) {
      setLoading(false);
      if (err.message !== "unauthorized") toast("Could not load: " + err.message, false);
    } finally { $("#refresh").classList.remove("spinning"); }
  }

  // ---- render ------------------------------------------------------------------
  function render() {
    $("#count-open").textContent = state.items.length;
    $("#count-snoozed").textContent = state.snoozed.length;
    const deck = $("#deck"); deck.innerHTML = "";
    const top = state.items.slice(0, 3);
    top.slice().reverse().forEach((item, i) => {
      const depth = top.length - 1 - i;
      const card = buildCard(item, depth);
      deck.appendChild(card);
      if (depth === 0) attachDrag(card, item);
    });
    const empty = state.items.length === 0;
    const listMode = state.view === "list";
    $("#empty").classList.toggle("hidden", !empty);
    $("#deck").classList.toggle("hidden", empty || listMode);
    $("#actions").classList.toggle("hidden", empty || listMode);
    $("#hints").classList.toggle("hidden", empty || listMode);
    $("#list").classList.toggle("hidden", empty || !listMode);
    document.querySelectorAll("[data-view]").forEach((b) => b.classList.toggle("on", b.dataset.view === state.view));
    if (listMode) renderList();
    renderRows($("#snoozed-list"), state.snoozed, "snoozed");
    renderRows($("#resolved-list"), state.resolved, "resolved");
  }

  function buildCard(item, depth) {
    const el = document.createElement("article");
    el.className = "card"; el.style.setProperty("--depth", depth); el.dataset.id = item.id;
    el.style.animationDelay = `${depth * 60}ms`;
    el.innerHTML = `
      <div class="meta"><span class="chip">${esc(item.project)}</span>${item.topic ? `<span class="chip topic-chip">${esc(item.topic)}</span>` : ""}<span class="when tab">${fmtDate(item.created_at)}</span></div>
      <h2>${esc(item.title)}</h2>
      <p class="details clamp">${esc(item.details || "")}</p>
      ${item.session_title ? `<div class="session">from <b>“${esc(item.session_title)}”</b></div>` : `<div class="session">session <b class="mono">${esc((item.session_id || "").slice(0, 8))}</b></div>`}
      <span class="intent-pill"></span>
      <div class="wash"></div>`;
    const d = el.querySelector(".details");
    d.addEventListener("click", (e) => { e.stopPropagation(); d.classList.toggle("clamp"); });
    return el;
  }

  // Every open item, grouped by topic (the workstream the extractor labelled), as expandable task rows.
  const UNSORTED = "Unsorted";
  let collapsed = new Set();
  try { collapsed = new Set(JSON.parse(localStorage.getItem("ccinboxCollapsed") || "[]")); } catch (e) { /* ignore */ }
  const saveCollapsed = () => { try { localStorage.setItem("ccinboxCollapsed", JSON.stringify([...collapsed])); } catch (e) { /* ignore */ } };
  function renderList() {
    const list = $("#list"); list.innerHTML = "";
    const groups = new Map();
    for (const it of state.items) { const k = it.topic || UNSORTED; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(it); }
    const keys = [...groups.keys()].sort((a, b) => (a === UNSORTED) - (b === UNSORTED) || groups.get(b).length - groups.get(a).length || a.localeCompare(b));
    let n = 0;
    for (const topic of keys) {
      const items = groups.get(topic);
      const g = document.createElement("div"); g.className = "group" + (collapsed.has(topic) ? " collapsed" : "");
      g.innerHTML = `<button type="button" class="group-head" aria-expanded="${!collapsed.has(topic)}"><span class="topic">${esc(topic)}</span><span class="count">${items.length} item${items.length === 1 ? "" : "s"}</span><span class="chev">${icon("i-chev")}</span></button><div class="group-body"><div class="group-rows"></div></div>`;
      g.querySelector(".group-head").addEventListener("click", () => {
        const now = g.classList.toggle("collapsed");
        g.querySelector(".group-head").setAttribute("aria-expanded", String(!now));
        if (now) collapsed.add(topic); else collapsed.delete(topic);
        saveCollapsed();
      });
      const rowsEl = g.querySelector(".group-rows");
      items.forEach((it, i) => {
        const row = document.createElement("div");
        row.className = "row"; row.style.animationDelay = `${Math.min(n++, 12) * 40}ms`;
        row.innerHTML = `
          <button type="button" class="row-main" aria-expanded="false">
            <span class="row-icon hollow">${i + 1}</span>
            <span class="row-title">${esc(it.title)}</span>
            <span class="row-when">${esc(fmtDate(it.created_at))}</span>
            <span class="chev">${icon("i-chev")}</span>
          </button>
          <div class="row-body"><div><div class="row-inner"><span class="vline"></span><div class="row-details">
            ${it.details ? `<div>${esc(it.details)}</div>` : ""}
            <div class="k"><span>Project</span><span>${esc(it.project)}</span></div>
            ${it.session_title ? `<div class="k"><span>From session</span><span>“${esc(it.session_title)}”</span></div>` : ""}
            <div class="row-actions">
              <button type="button" class="ghost done" data-act="done">${icon("i-check")}Done</button>
              <button type="button" class="ghost snooze" data-act="snooze">${icon("i-clock")}Snooze</button>
              <button type="button" class="ghost dismiss" data-act="dismiss">${icon("i-x")}Dismiss</button>
            </div>
          </div></div></div></div>`;
        row.querySelector(".row-main").addEventListener("click", () => {
          const open = row.classList.toggle("open");
          row.querySelector(".row-main").setAttribute("aria-expanded", String(open));
        });
        row.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); act(it, b.dataset.act); }));
        rowsEl.appendChild(row);
      });
      list.appendChild(g);
    }
  }

  function renderRows(container, rows, kind) {
    container.innerHTML = "";
    rows.forEach((r, i) => {
      const row = document.createElement("div");
      row.className = "row"; row.style.animationDelay = `${i * 60}ms`;
      const iconCls = kind === "snoozed" ? "orange" : "green";
      const iconId = kind === "snoozed" ? "i-clock" : "i-check";
      const pill = kind === "snoozed" ? `<span class="pill orange">${esc(fmtUntil(r.snoozed_until))}</span>` : `<span class="pill green">Closed</span>`;
      const facts = kind === "snoozed"
        ? `<div class="k"><span>Snoozed until</span><span>${esc(fmtUntil(r.snoozed_until))}</span></div>`
        : `<div class="k"><span>Closed by session</span><span>${esc((r.resolved_by || "").slice(0, 8))}</span></div>`;
      row.innerHTML = `
        <button type="button" class="row-main" aria-expanded="false">
          <span class="row-icon ${iconCls}">${icon(iconId)}</span>
          <span class="row-title">${esc(r.title)}</span>
          <span class="row-meta">${esc(r.project)}</span>
          ${pill}
          <span class="chev">${icon("i-chev")}</span>
        </button>
        <div class="row-body"><div><div class="row-inner"><span class="vline"></span><div class="row-details">
          ${r.details ? `<div>${esc(r.details)}</div>` : ""}
          ${facts}
          <div class="k"><span>Created</span><span>${esc(fmtDate(r.created_at))}</span></div>
          <button type="button" class="ghost" data-reopen="${r.id}">${icon("i-undo")}Back to inbox</button>
        </div></div></div></div>`;
      row.querySelector(".row-main").addEventListener("click", () => {
        const open = row.classList.toggle("open");
        row.querySelector(".row-main").setAttribute("aria-expanded", String(open));
      });
      row.querySelector("[data-reopen]").addEventListener("click", async (e) => {
        e.stopPropagation();
        try { await api(`/api/items/${r.id}`, { method: "POST", body: JSON.stringify({ action: "reopen" }) }); toast(`${label("reopen")}: ${r.title}`, false); await load({ quiet: true }); }
        catch (err) { toast("Failed: " + err.message, false); }
      });
      container.appendChild(row);
    });
  }

  // ---- gestures ----------------------------------------------------------------
  // Decide what a drag means. dx/dy in px, width = card width.
  function intent(dx, dy, width) {
    const ax = Math.abs(dx), ay = Math.abs(dy);
    if (dy < -SNOOZE_PX && ay > ax) return "snooze";
    if (dx > width * SWIPE_FRACTION) return "done";
    if (dx < -width * SWIPE_FRACTION) return "dismiss";
    return null;
  }

  const pillText = { done: `${icon("i-check")}Done`, dismiss: `${icon("i-x")}Dismiss`, snooze: `${icon("i-clock")}Snooze` };

  function attachDrag(card, item) {
    let startX = 0, startY = 0, dx = 0, dy = 0, dragging = false;
    const width = () => card.offsetWidth || 320;
    const pill = card.querySelector(".intent-pill");
    const reset = () => { card.style.transition = ""; card.style.transform = ""; card.dataset.intent = ""; card.style.setProperty("--wash", 0); };
    card.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || state.busy) return;
      dragging = true; startX = e.clientX; startY = e.clientY; dx = dy = 0;
      card.style.animation = "none";
      if (card.setPointerCapture) card.setPointerCapture(e.pointerId);
    });
    card.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      dx = e.clientX - startX; dy = e.clientY - startY;
      card.style.transition = "none";
      card.style.transform = `translate(${dx}px, ${dy}px) rotate(${dx / 22}deg)`;
      const i = intent(dx, dy, width());
      if (i && card.dataset.intent !== i) pill.innerHTML = pillText[i];
      card.dataset.intent = i || "";
      const strength = Math.min(1, Math.max(Math.abs(dx) / (width() * SWIPE_FRACTION), -dy / SNOOZE_PX, 0));
      card.style.setProperty("--wash", i ? 1 : strength * 0.6);
    });
    const finish = (e) => {
      if (!dragging) return;
      dragging = false;
      if (card.releasePointerCapture) { try { card.releasePointerCapture(e.pointerId); } catch (err) { /* ignore */ } }
      const i = intent(dx, dy, width());
      if (i) { flyOut(card, i); act(item, i); } else { reset(); }
    };
    card.addEventListener("pointerup", finish);
    card.addEventListener("pointercancel", finish);
  }

  function flyOut(card, i) {
    const w = window.innerWidth, h = window.innerHeight;
    card.style.transition = "transform .28s var(--ease), opacity .28s var(--ease)";
    card.style.transform = i === "done" ? `translate(${w}px, -40px) rotate(18deg)`
      : i === "dismiss" ? `translate(${-w}px, -40px) rotate(-18deg)`
      : `translate(0, ${-h}px) scale(.9)`;
    card.style.opacity = "0";
  }

  async function act(item, action) {
    if (state.busy) return;
    state.busy = true;
    state.items = state.items.filter((x) => x.id !== item.id);
    if (state.view === "cards") await sleep(220);
    render();
    try {
      const updated = await api(`/api/items/${item.id}`, { method: "POST", body: JSON.stringify({ action }) });
      state.history.push({ item, action });
      if (action === "snooze") { state.snoozed = [updated, ...state.snoozed]; render(); }
      toast(`${label(action)} · ${item.title}`);
    } catch (err) {
      state.items.unshift(item); render();
      toast("Failed: " + err.message, false);
    } finally { state.busy = false; }
  }

  async function undo() {
    const last = state.history.pop();
    hideToast();
    if (!last) return;
    try {
      await api(`/api/items/${last.item.id}`, { method: "POST", body: JSON.stringify({ action: "undo" }) });
      await load({ quiet: true });
    } catch (err) { toast("Undo failed: " + err.message, false); }
  }

  // ---- chrome ------------------------------------------------------------------
  let toastTimer = null;
  function toast(text, undoable = true) {
    $("#toast-text").textContent = text;
    $("#undo").classList.toggle("hidden", !undoable);
    $("#toast .sep").classList.toggle("hidden", !undoable);
    const el = $("#toast"); el.classList.add("hidden"); void el.offsetWidth; el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(hideToast, 6000);
  }
  function hideToast() { $("#toast").classList.add("hidden"); }
  function showTokenModal() { $("#token-modal").classList.remove("hidden"); $("#token-input").focus(); }

  function applyTheme(theme) {
    if (theme) document.documentElement.dataset.theme = theme; else delete document.documentElement.dataset.theme;
    try { theme ? localStorage.setItem("ccinboxTheme", theme) : localStorage.removeItem("ccinboxTheme"); } catch (e) { /* ignore */ }
    const effective = theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    document.querySelectorAll("[data-set-theme]").forEach((b) => b.classList.toggle("on", b.dataset.setTheme === effective));
  }
  document.querySelectorAll("[data-set-theme]").forEach((b) => b.addEventListener("click", () => applyTheme(b.dataset.setTheme)));
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(document.documentElement.dataset.theme || null));
  applyTheme(document.documentElement.dataset.theme || null);

  document.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => {
    state.view = b.dataset.view;
    try { localStorage.setItem("ccinboxView", state.view); } catch (e) { /* ignore */ }
    render();
  }));
  // Refresh = run a sweep (extract new turns from every changed session), then reload the inbox.
  let sweeping = false, sweepTimer = null;
  function setSweepStatus(on, elapsed) {
    $("#sweep-status").classList.toggle("hidden", !on);
    $("#refresh").classList.toggle("spinning", on);
    if (on) $("#sweep-elapsed").textContent = (elapsed ?? 0).toFixed(1) + "s";
  }
  async function runSweep() {
    if (sweeping) return;
    sweeping = true;
    const t0 = performance.now();
    try {
      let st = await api("/api/sweep", { method: "POST", body: "{}" });
      setSweepStatus(true, 0);
      sweepTimer = setInterval(() => setSweepStatus(true, (performance.now() - t0) / 1000), 100);
      while (st.running) { await sleep(1500); st = await api("/api/sweep"); }
      clearInterval(sweepTimer); setSweepStatus(false);
      await load({ quiet: true });
      if (st.error) toast("Sweep failed: " + st.error.slice(0, 120), false);
      else if (st.result) {
        const r = st.result;
        toast(r.processed ? `${r.added} new, ${r.resolved} closed · ${r.processed} session${r.processed === 1 ? "" : "s"}, $${r.cost_usd.toFixed(2)}` : "Nothing new since last check", false);
      }
    } catch (err) {
      clearInterval(sweepTimer); setSweepStatus(false);
      if (err.message !== "unauthorized") toast("Refresh failed: " + err.message, false);
      await load({ quiet: true });
    } finally { sweeping = false; }
  }
  $("#undo").addEventListener("click", undo);
  $("#refresh").addEventListener("click", runSweep);
  document.querySelectorAll(".actions button").forEach((b) => b.addEventListener("click", () => {
    const item = state.items[0]; if (!item) return;
    const card = $("#deck .card:last-child"); if (card) flyOut(card, b.dataset.action);
    act(item, b.dataset.action);
  }));
  $("#token-form").addEventListener("submit", (e) => {
    e.preventDefault();
    state.token = $("#token-input").value.trim();
    try { localStorage.setItem("ccinboxToken", state.token); } catch (err) { /* ignore */ }
    $("#token-modal").classList.add("hidden");
    load();
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.loaded) load({ quiet: true }); });
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("sw.js").catch(() => {});
    // When an updated worker takes control, reload once so the page runs the new shell files.
    let swReloaded = false;
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (swReloaded || !navigator.serviceWorker.controller) return;
      swReloaded = true; location.reload();
    });
  }
  load();
})();
