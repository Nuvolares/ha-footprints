/* Footprints: every place a person has been, more solid the more often they go there. */
(() => {
  "use strict";

  const COLORS = ["#e8590c", "#1c7ed6", "#2f9e44", "#ae3ec9", "#e03131", "#0c8599", "#f08c00", "#5f3dc4"];
  const MIN_ALPHA = 0.07; // a place visited once: barely there
  const SCALE = {
    visits: { max: 200, unit: (n) => (n === 1 ? "1 visit" : `${n} visits`), short: (n) => `${n}+` },
    time: { max: 500, unit: (n) => (n === 1 ? "1 hour" : `${n} hours`), short: (n) => `${n} h+` },
  };

  const $ = (id) => document.getElementById(id);
  const store = {
    get(k, d) { try { const v = localStorage.getItem("fp." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("fp." + k, JSON.stringify(v)); } catch { /* private mode */ } },
  };

  const state = {
    people: [],
    hidden: new Set(store.get("hidden", [])),
    range: store.get("range", "all"),
    mode: store.get("mode", "visits"),
    sat: store.get("sat", { visits: 20, time: 24 }),
    stay: store.get("stay", 0),
    cell: store.get("cell", null),
    cells: {},        // entity_id -> cells from the server
    layers: {},       // entity_id -> L.LayerGroup of dots
    nowMarkers: {},   // entity_id -> L.Marker
    fitted: false,
  };

  // --- map ---------------------------------------------------------------------

  const dark = matchMedia("(prefers-color-scheme: dark)");
  const map = L.map("map", { zoomControl: false, preferCanvas: true, worldCopyJump: true }).setView([30, 0], 2);
  L.control.zoom({ position: "topright" }).addTo(map);
  const renderer = L.canvas({ padding: 0.5 });
  let tiles = null;

  function tileLayer(cfg) {
    const osm = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
    if (cfg.tile_url) return L.tileLayer(cfg.tile_url, { maxZoom: 19, attribution: osm });
    if (cfg.map_style === "osm") {
      return L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: osm });
    }
    const isDark = cfg.map_style === "dark" || (cfg.map_style !== "light" && dark.matches);
    return L.tileLayer(`https://{s}.basemaps.cartocdn.com/${isDark ? "dark_all" : "light_all"}/{z}/{x}/{y}{r}.png`, {
      maxZoom: 20, subdomains: "abcd", attribution: `${osm} &copy; <a href="https://carto.com/attributions">CARTO</a>`,
    });
  }

  function setTiles(cfg) {
    if (tiles) map.removeLayer(tiles);
    tiles = tileLayer(cfg).addTo(map);
  }

  function radiusPx(lat) {
    const metersPerPx = (156543.03392 * Math.cos((lat * Math.PI) / 180)) / 2 ** map.getZoom();
    return Math.min(250, Math.max(3.5, state.cell / 2 / metersPerPx));
  }

  // --- shading -----------------------------------------------------------------

  function alpha(value) {
    const n = state.sat[state.mode];
    if (value <= 0) return MIN_ALPHA; // visited, but no measurable time there
    const f = Math.min(1, Math.log1p(value) / Math.log1p(n));
    return MIN_ALPHA + (1 - MIN_ALPHA) * f;
  }

  const metric = (c) => (state.mode === "visits" ? c.visits : c.dwell / 3600);

  function satToSlider(n) { return Math.round((100 * Math.log(n)) / Math.log(SCALE[state.mode].max)); }
  function sliderToSat(v) { return Math.max(1, Math.round(Math.exp((v / 100) * Math.log(SCALE[state.mode].max)))); }

  function updateLegend() {
    const n = state.sat[state.mode];
    const s = SCALE[state.mode];
    $("sat-label").textContent = `Solid after ${s.unit(n)}`;
    $("ramp-lo").textContent = s.unit(1);
    $("ramp-hi").textContent = s.short(n);
    const stops = [0, 0.25, 0.5, 0.75, 1].map((f) => {
      const v = Math.expm1(f * Math.log1p(n));
      return `color-mix(in srgb, var(--accent) ${Math.round(alpha(Math.max(v, 1)) * 100)}%, transparent) ${f * 100}%`;
    });
    $("ramp").style.background = `linear-gradient(90deg, ${stops.join(", ")})`;
  }

  // --- formatting --------------------------------------------------------------

  function duration(s) {
    if (s < 60) return "under a minute";
    const m = Math.round(s / 60);
    if (m < 60) return `${m} min`;
    const h = Math.floor(m / 60);
    if (h < 48) return `${h} h ${m % 60} min`;
    return `${Math.round(h / 24)} days`;
  }

  function when(ts) {
    const d = new Date(ts * 1000);
    const days = Math.floor((Date.now() - d) / 86400000);
    if (days < 1) return `today, ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
    if (days < 2) return "yesterday";
    if (days < 14) return `${days} days ago`;
    return d.toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" });
  }

  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const initials = (name) => name.split(/\s+/).map((w) => w[0] || "").join("").slice(0, 2).toUpperCase();

  function avatarHtml(p) {
    const pic = p.picture ? ` style="background-image:url('${encodeURI(p.picture)}')"` : "";
    return `<span class="avatar" style="--pc:${p.color}"${pic}>${p.picture ? "" : esc(initials(p.name))}</span>`;
  }

  function popupHtml(p, c) {
    return `<b>${esc(p.name)}</b><br>` +
      `${c.visits === 1 ? "1 visit" : `${c.visits} visits`} · ${duration(c.dwell)} in total<br>` +
      `<span class="pop-meta">First ${when(c.first)} · last ${when(c.last)}</span>`;
  }

  // --- drawing -----------------------------------------------------------------

  function draw(entity) {
    const p = state.people.find((x) => x.entity_id === entity);
    if (state.layers[entity]) map.removeLayer(state.layers[entity]);
    delete state.layers[entity];
    if (!p || state.hidden.has(entity)) return;
    const group = L.layerGroup();
    for (const c of state.cells[entity] || []) {
      const a = alpha(metric(c));
      if (a <= 0) continue;
      const dot = L.circleMarker([c.lat, c.lon], {
        renderer, radius: radiusPx(c.lat), stroke: false, fillColor: p.color, fillOpacity: a,
      });
      dot.bindPopup(() => popupHtml(p, c));
      group.addLayer(dot);
    }
    group.addTo(map);
    state.layers[entity] = group;
  }

  function redrawAll() {
    for (const p of state.people) draw(p.entity_id);
    drawNow();
    renderTop();
  }

  function resize() {
    for (const group of Object.values(state.layers)) {
      group.eachLayer((dot) => dot.setRadius(radiusPx(dot.getLatLng().lat)));
    }
  }
  map.on("zoomend", resize);

  function drawNow() {
    for (const p of state.people) {
      const m = state.nowMarkers[p.entity_id];
      const show = !state.hidden.has(p.entity_id) && typeof p.lat === "number" && typeof p.lon === "number";
      if (!show) {
        if (m) { map.removeLayer(m); delete state.nowMarkers[p.entity_id]; }
        continue;
      }
      if (m) {
        m.setLatLng([p.lat, p.lon]);
        continue;
      }
      const icon = L.divIcon({ className: "now-marker", html: avatarHtml(p), iconSize: [34, 34], iconAnchor: [17, 17] });
      state.nowMarkers[p.entity_id] = L.marker([p.lat, p.lon], { icon, zIndexOffset: 1000, title: `${p.name} (now)` })
        .bindPopup(`<b>${esc(p.name)}</b><br><span class="pop-meta">Current location</span>`)
        .addTo(map);
    }
  }

  function renderTop() {
    const all = [];
    for (const p of state.people) {
      if (state.hidden.has(p.entity_id)) continue;
      for (const c of state.cells[p.entity_id] || []) all.push({ p, c });
    }
    all.sort((a, b) => metric(b.c) - metric(a.c));
    const list = $("top");
    list.innerHTML = "";
    if (!all.length) {
      list.innerHTML = '<li class="empty">No places recorded in this range yet.</li>';
      return;
    }
    for (const { p, c } of all.slice(0, 8)) {
      const li = document.createElement("li");
      const value = state.mode === "visits" ? `${c.visits}×` : duration(c.dwell);
      li.innerHTML = `<button type="button"><span class="dot" style="background:${p.color}"></span>` +
        `<span class="lbl">${esc(p.name)} · ${c.lat.toFixed(4)}, ${c.lon.toFixed(4)}</span>` +
        `<span class="num">${esc(value)}</span></button>`;
      li.firstChild.addEventListener("click", () => {
        map.flyTo([c.lat, c.lon], Math.max(map.getZoom(), 16), { duration: 0.6 });
        L.popup().setLatLng([c.lat, c.lon]).setContent(popupHtml(p, c)).openOn(map);
      });
      list.appendChild(li);
    }
  }

  function fitOnce() {
    if (state.fitted) return;
    const pts = [];
    for (const p of state.people) {
      if (state.hidden.has(p.entity_id)) continue;
      // Frame the places that matter, not the one-off holiday trip.
      const cells = (state.cells[p.entity_id] || []).filter((c) => c.visits > 1);
      for (const c of cells.length ? cells : state.cells[p.entity_id] || []) pts.push([c.lat, c.lon]);
      if (!pts.length && typeof p.lat === "number") pts.push([p.lat, p.lon]);
    }
    if (!pts.length) return;
    map.fitBounds(L.latLngBounds(pts), { padding: [40, 40], maxZoom: 15 });
    state.fitted = true;
  }

  // --- data --------------------------------------------------------------------

  async function getJson(url) {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    return res.json();
  }

  function setStatus(text, err = false) {
    $("status").textContent = text;
    $("status").classList.toggle("err", err);
  }

  async function loadPeople() {
    const data = await getJson("api/people");
    state.people = data.people.map((p, i) => ({ ...p, color: COLORS[i % COLORS.length] }));
    renderPeople();
    if (data.error && !data.connected) setStatus(`Not connected to Home Assistant: ${data.error}`, true);
    else if (data.importing) setStatus(`Importing history for ${data.importing}…`);
    else if (!state.people.length) setStatus("No people found. Add people in Settings → People, or list entities in the app options.");
    else {
      const pts = state.people.reduce((n, p) => n + (p.points || 0), 0);
      const first = Math.min(...state.people.map((p) => p.first || Infinity));
      setStatus(`${pts.toLocaleString()} location points${Number.isFinite(first) ? ` since ${new Date(first * 1000).toLocaleDateString()}` : ""}${data.connected ? " · live" : ""}`);
    }
  }

  async function loadCells(auto = false) {
    const q = `range=${state.range}&cell=${state.cell}&min_stay=${state.stay}`;
    const fresh = {};
    await Promise.all(state.people.map(async (p) => {
      const data = await getJson(`api/cells?entity=${encodeURIComponent(p.entity_id)}&${q}`);
      fresh[p.entity_id] = data.cells;
    }));
    // A background refresh must not close the popup someone is reading.
    const popupOpen = !!document.querySelector(".leaflet-popup");
    if (auto && popupOpen) {
      drawNow();
      return;
    }
    state.cells = fresh;
    redrawAll();
    fitOnce();
  }

  async function refresh(auto = false) {
    try {
      await loadPeople();
      await loadCells(auto);
    } catch (err) {
      setStatus(`Could not load data: ${err.message}`, true);
    }
  }

  // --- controls ----------------------------------------------------------------

  function renderPeople() {
    const ul = $("people");
    ul.innerHTML = "";
    for (const p of state.people) {
      const off = state.hidden.has(p.entity_id);
      const li = document.createElement("li");
      li.innerHTML = `<label class="person${off ? " off" : ""}" style="--pc:${p.color}">` +
        `<input type="checkbox"${off ? "" : " checked"}>${avatarHtml(p)}` +
        `<span class="who"><span class="name">${esc(p.name)}</span><br>` +
        `<span class="meta">${p.points ? `${p.points.toLocaleString()} points` : "nothing recorded yet"}</span></span>` +
        `<span class="check" aria-hidden="true"></span></label>` +
        (p.points ? `<button type="button" class="icon-btn del" title="Delete ${esc(p.name)}'s history" aria-label="Delete ${esc(p.name)}'s history">` +
          `<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M5 7h14M10 11v6M14 11v6M6 7l1 12h10l1-12M9 7V4h6v3" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></button>` : "");
      li.querySelector(".del")?.addEventListener("click", async () => {
        if (!confirm(`Delete all ${p.points.toLocaleString()} recorded locations of ${p.name}? This cannot be undone.`)) return;
        try {
          const res = await fetch(`api/history/${encodeURIComponent(p.entity_id)}`, { method: "DELETE" });
          if (!res.ok) throw new Error(`${res.status}`);
          await refresh();
        } catch (err) {
          setStatus(`Could not delete: ${err.message}`, true);
        }
      });
      li.querySelector("input").addEventListener("change", (e) => {
        if (e.target.checked) state.hidden.delete(p.entity_id); else state.hidden.add(p.entity_id);
        store.set("hidden", [...state.hidden]);
        li.firstChild.classList.toggle("off", !e.target.checked);
        draw(p.entity_id);
        drawNow();
        renderTop();
      });
      ul.appendChild(li);
    }
  }

  function segmented(id, get, set) {
    const el = $(id);
    const sync = () => el.querySelectorAll("button").forEach((b) => {
      b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", String(b.dataset.v === get()));
    });
    el.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b || b.dataset.v === get()) return;
      set(b.dataset.v);
      sync();
    });
    sync();
  }

  function initControls(cfg) {
    if (!state.cell) state.cell = cfg.cell_size_m;
    const cellSel = $("cell");
    if (![...cellSel.options].some((o) => +o.value === state.cell)) {
      cellSel.add(new Option(`${state.cell} m`, state.cell));
    }
    cellSel.value = state.cell;
    cellSel.addEventListener("change", () => { state.cell = +cellSel.value; store.set("cell", state.cell); loadCells(); });

    $("stay").value = state.stay;
    $("stay").addEventListener("change", () => { state.stay = +$("stay").value; store.set("stay", state.stay); loadCells(); });

    segmented("range", () => state.range, (v) => { state.range = v; store.set("range", v); loadCells(); });
    segmented("mode", () => state.mode, (v) => {
      state.mode = v; store.set("mode", v);
      $("sat").value = satToSlider(state.sat[v]);
      updateLegend();
      redrawAll();
    });

    const sat = $("sat");
    sat.value = satToSlider(state.sat[state.mode]);
    let pending = 0;
    sat.addEventListener("input", () => {
      state.sat[state.mode] = sliderToSat(+sat.value);
      store.set("sat", state.sat);
      updateLegend();
      cancelAnimationFrame(pending);
      pending = requestAnimationFrame(() => {
        for (const p of state.people) draw(p.entity_id);
      });
    });
    updateLegend();

    const panel = $("panel");
    const btn = $("collapse");
    const setCollapsed = (c) => {
      panel.classList.toggle("collapsed", c);
      btn.setAttribute("aria-expanded", String(!c));
      btn.title = c ? "Show controls" : "Hide controls";
      store.set("collapsed", c);
    };
    setCollapsed(store.get("collapsed", matchMedia("(max-width: 600px)").matches));
    btn.addEventListener("click", () => setCollapsed(!panel.classList.contains("collapsed")));
  }

  async function start() {
    let cfg = { cell_size_m: 50, map_style: "auto", tile_url: "" };
    try { cfg = await getJson("api/config"); } catch (err) { setStatus(`Could not load settings: ${err.message}`, true); }
    setTiles(cfg);
    dark.addEventListener("change", () => setTiles(cfg));
    initControls(cfg);
    await refresh();
    setInterval(() => { if (!document.hidden) refresh(true); }, 60000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(true); });
  }

  start();
})();
