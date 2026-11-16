/* Footprints: every place a person has been, more solid the more often they go there. */
(() => {
  "use strict";

  const COLORS = ["#e8590c", "#1c7ed6", "#2f9e44", "#ae3ec9", "#e03131", "#0c8599", "#f08c00", "#5f3dc4"];
  const MIN_ALPHA = 0.07; // a place visited once: barely there
  const ARROW_SPACING = 90; // px between direction arrows along a route
  const MAX_ARROWS = 2500;
  const SCALE = {
    visits: { max: 200, unit: (n) => (n === 1 ? "1 visit" : `${n} visits`), short: (n) => `${n}+` },
    time: { max: 500, unit: (n) => (n === 1 ? "1 hour" : `${n} hours`), short: (n) => `${n} h+` },
  };
  const RANGES = ["day", "7d", "30d", "1y", "all"];

  const $ = (id) => document.getElementById(id);
  const store = {
    get(k, d) { try { const v = localStorage.getItem("fp." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("fp." + k, JSON.stringify(v)); } catch { /* private mode */ } },
  };

  const todayIso = () => {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  };

  const state = {
    cfg: {},
    people: [],
    hidden: new Set(store.get("hidden", [])),
    range: RANGES.includes(store.get("range", "all")) ? store.get("range", "all") : "all",
    day: todayIso(),
    mode: store.get("mode", "visits"),
    sat: store.get("sat", { visits: 20, time: 24 }),
    stay: store.get("stay", 0),
    cell: store.get("cell", null),
    basemap: store.get("basemap", "ha"),
    showRoutes: store.get("routes2", true), // "routes2": 0.3.0 shows routes by default, also for earlier viewers
    cells: {},        // entity_id -> cells from the server
    trips: {},        // entity_id -> trips from the server
    layers: {},       // entity_id -> L.LayerGroup of dots
    routeLayers: {},  // entity_id -> L.LayerGroup of route lines
    nowMarkers: {},   // entity_id -> L.Marker
    fitted: false,
  };

  // --- map ---------------------------------------------------------------------

  const dark = matchMedia("(prefers-color-scheme: dark)");
  const map = L.map("map", { zoomControl: false, preferCanvas: true, worldCopyJump: true }).setView([30, 0], 2);
  L.control.zoom({ position: "topright" }).addTo(map);
  map.createPane("routes").style.zIndex = 390; // under the dots
  const renderer = L.canvas({ padding: 0.5 });
  const routeRenderer = L.canvas({ padding: 0.5, pane: "routes" });
  const arrowLayer = L.layerGroup().addTo(map);
  let tiles = null;

  const OSM_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

  // Home Assistant's own map: core serves the tiles at /api/map_tiles (same origin as this page,
  // which runs inside Home Assistant) with a short-lived token from "map_tiles/access_token".
  const BASEMAPS = {
    ha: { label: "Home Assistant map", darkable: true, make: () => haTileLayer() },
    osm: {
      label: "OpenStreetMap", darkable: true,
      make: () => L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19, attribution: OSM_ATTRIBUTION, referrerPolicy: "origin",
      }),
    },
    satellite: {
      label: "Satellite", darkable: false,
      make: () => L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
        maxZoom: 19, attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics",
      }),
    },
    topo: {
      label: "Topographic", darkable: false,
      make: () => L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", {
        maxZoom: 17, subdomains: "abc", referrerPolicy: "origin",
        attribution: `${OSM_ATTRIBUTION}, SRTM | &copy; <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)`,
      }),
    },
    custom: {
      label: "Custom (from options)", darkable: false,
      make: () => L.tileLayer(state.cfg.tile_url, { maxZoom: 19, attribution: OSM_ATTRIBUTION }),
    },
  };

  async function mapToken() {
    // Ask through the signed-in user's own Home Assistant connection (the parent page)...
    try {
      const hc = window.parent && window.parent !== window && window.parent.hassConnection;
      if (hc) {
        const { conn } = await hc;
        const res = await conn.sendMessagePromise({ type: "map_tiles/access_token" });
        if (res && res.token) return res.token;
      }
    } catch { /* not reachable from here: fall back to the app's own connection */ }
    // ...or through the app's connection to Home Assistant.
    const res = await getJson("api/map_token");
    return res.token;
  }

  async function haTileLayer() {
    const layer = L.tileLayer("/api/map_tiles/raster/{z}/{x}/{y}.png?token={token}", {
      token: await mapToken(), maxZoom: 20, maxNativeZoom: 19, attribution: OSM_ATTRIBUTION,
    });
    let lastRefresh = Date.now();
    layer.on("tileerror", async () => {
      if (Date.now() - lastRefresh < 30000) return; // token expired? refresh at most every 30 s
      lastRefresh = Date.now();
      try {
        layer.options.token = await mapToken();
        layer.redraw();
      } catch (err) {
        setStatus(`Could not load the map: ${err.message}`, true);
      }
    });
    return layer;
  }

  function applyDarkTiles() {
    const style = state.cfg.map_style;
    const isDark = BASEMAPS[state.basemap].darkable && (style === "dark" || (style === "auto" && dark.matches));
    map.getContainer().classList.toggle("dark-tiles", isDark);
  }

  async function setTiles() {
    if (state.cfg.demo && state.basemap === "ha") state.basemap = "osm"; // no Home Assistant to ask
    let layer;
    try {
      layer = await BASEMAPS[state.basemap].make();
    } catch (err) {
      setStatus(`Could not load the map: ${err.message}`, true);
      return;
    }
    if (tiles) map.removeLayer(tiles);
    tiles = layer.addTo(map);
    applyDarkTiles();
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

  const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const dayName = (ts) => new Date(ts * 1000).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  const km = (m) => (m < 1000 ? `${m} m` : `${(m / 1000).toFixed(m < 10000 ? 1 : 0)} km`);

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

  function tripKind(t) {
    if (t.mode === "air") return "Flight (too fast for the road)";
    if (t.kind === "jump") {
      return t.mode === "street"
        ? "No recorded points in between · likely route along the streets"
        : "No recorded points in between · straight line (no road found: a ferry, boat or off-road)";
    }
    return t.snapped ? "Following the streets" : "Straight lines between recorded points";
  }

  function tripHtml(p, t) {
    const span = t.end - t.start;
    return `<b>${esc(p.name)}</b> · ${esc(dayName(t.start))}${dayName(t.end) !== dayName(t.start) ? ` → ${esc(dayName(t.end))}` : ""}<br>` +
      `${clock(t.start)} → ${clock(t.end)} · ${km(t.dist)} · ${duration(span)}<br>` +
      `<span class="pop-meta">${tripKind(t)}</span>`;
  }

  // How each kind of line looks: solid along the streets, dotted arc for flights, dashed when straight.
  function lineStyle(t) {
    if (t.mode === "air") return { weight: 2.5, opacity: 0.9, dashArray: "1 7" };
    if (t.kind === "jump") return t.mode === "street"
      ? { weight: 3, opacity: 0.55, dashArray: null }
      : { weight: 3, opacity: 0.7, dashArray: "12 8" };
    return { weight: 3.5, opacity: 0.8, dashArray: t.snapped ? null : "6 6" };
  }

  // --- drawing: dots -----------------------------------------------------------

  function draw(entity) {
    const p = state.people.find((x) => x.entity_id === entity);
    if (state.layers[entity]) map.removeLayer(state.layers[entity]);
    delete state.layers[entity];
    if (!p || state.hidden.has(entity)) return;
    const group = L.layerGroup();
    for (const c of state.cells[entity] || []) {
      const dot = L.circleMarker([c.lat, c.lon], {
        renderer, radius: radiusPx(c.lat), stroke: false, fillColor: p.color, fillOpacity: alpha(metric(c)),
      });
      dot.bindPopup(() => popupHtml(p, c));
      group.addLayer(dot);
    }
    group.addTo(map);
    state.layers[entity] = group;
  }

  // --- drawing: routes ---------------------------------------------------------

  const routesOn = () => state.showRoutes;

  function drawRoutes(entity) {
    const p = state.people.find((x) => x.entity_id === entity);
    if (state.routeLayers[entity]) map.removeLayer(state.routeLayers[entity]);
    delete state.routeLayers[entity];
    if (!p || !routesOn() || state.hidden.has(entity)) return;
    const group = L.layerGroup();
    for (const t of state.trips[entity] || []) {
      const line = L.polyline(t.line, {
        renderer: routeRenderer, color: p.color, lineCap: "round", lineJoin: "round", ...lineStyle(t),
      });
      line.bindPopup(() => tripHtml(p, t));
      line.color = p.color;
      group.addLayer(line);
    }
    group.addTo(map);
    state.routeLayers[entity] = group;
  }

  // Small chevrons along each visible route, pointing the way the person went.
  function drawArrows() {
    arrowLayer.clearLayers();
    if (!routesOn()) return;
    const view = map.getBounds().pad(0.1);
    let count = 0;
    for (const group of Object.values(state.routeLayers)) {
      group.eachLayer((line) => {
        if (count >= MAX_ARROWS || !view.intersects(line.getBounds())) return;
        const pts = line.getLatLngs().map((ll) => map.latLngToLayerPoint(ll));
        let next = ARROW_SPACING / 2;
        for (let i = 1; i < pts.length && count < MAX_ARROWS; i++) {
          const a = pts[i - 1];
          const b = pts[i];
          const len = a.distanceTo(b);
          if (len === 0) continue;
          const ux = (b.x - a.x) / len;
          const uy = (b.y - a.y) / len;
          while (next <= len && count < MAX_ARROWS) {
            const x = a.x + ux * next;
            const y = a.y + uy * next;
            const tip = L.point(x + ux * 4, y + uy * 4);
            const w1 = L.point(x - ux * 4 - uy * 4.5, y - uy * 4 + ux * 4.5);
            const w2 = L.point(x - ux * 4 + uy * 4.5, y - uy * 4 - ux * 4.5);
            arrowLayer.addLayer(L.polyline([w1, tip, w2].map((pt) => map.layerPointToLatLng(pt)), {
              renderer: routeRenderer, color: line.color, weight: 2.5, opacity: 1,
              interactive: false, lineCap: "round", lineJoin: "round",
            }));
            count++;
            next += ARROW_SPACING;
          }
          next -= len;
        }
      });
    }
  }

  let arrowFrame = 0;
  map.on("moveend", () => {
    cancelAnimationFrame(arrowFrame);
    arrowFrame = requestAnimationFrame(drawArrows);
  });

  // --- drawing: everything -----------------------------------------------------

  function redrawAll() {
    for (const p of state.people) {
      drawRoutes(p.entity_id);
      draw(p.entity_id);
    }
    drawArrows();
    drawNow();
    renderTop();
  }

  map.on("zoomend", () => {
    for (const group of Object.values(state.layers)) {
      group.eachLayer((dot) => dot.setRadius(radiusPx(dot.getLatLng().lat)));
    }
  });

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
      list.innerHTML = '<li class="empty">No places recorded in this time yet.</li>';
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
      const cells = state.cells[p.entity_id] || [];
      // Frame the places that matter, not the one-off holiday trip (except when looking at one day).
      const main = state.range === "day" ? cells : cells.filter((c) => c.visits > 1);
      for (const c of main.length ? main : cells) pts.push([c.lat, c.lon]);
      if (!pts.length && typeof p.lat === "number") pts.push([p.lat, p.lon]);
    }
    if (!pts.length) return;
    map.fitBounds(L.latLngBounds(pts), { padding: [40, 40], maxZoom: 15 });
    state.fitted = true;
  }

  // --- data --------------------------------------------------------------------

  async function getJson(url) {
    const res = await fetch(url, { cache: "no-store" });
    if (res.status === 403) {
      const body = await res.json().catch(() => ({}));
      if (body.denied) { $("denied").hidden = false; throw new Error("access denied"); }
    }
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    return res.json();
  }

  function setStatus(text, err = false) {
    $("status").textContent = text;
    $("status").classList.toggle("err", err);
  }

  function windowQuery() {
    if (state.range !== "day") return `range=${state.range}`;
    const [y, m, d] = state.day.split("-").map(Number);
    const start = new Date(y, m - 1, d); // local midnight
    const end = new Date(y, m - 1, d + 1); // next local midnight (handles daylight saving)
    return `start=${start.getTime() / 1000}&end=${end.getTime() / 1000}`;
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

  let routesTimer = 0;

  async function loadData(auto = false) {
    const q = `${windowQuery()}&cell=${state.cell}&min_stay=${state.stay}`;
    const cells = {};
    const trips = {};
    let pending = 0;
    let truncated = false;
    let routingError = null;
    await Promise.all(state.people.map(async (p) => {
      const id = encodeURIComponent(p.entity_id);
      const jobs = [getJson(`api/cells?entity=${id}&${q}`).then((d) => { cells[p.entity_id] = d.cells; })];
      if (routesOn()) {
        jobs.push(getJson(`api/routes?entity=${id}&${windowQuery()}`).then((d) => {
          trips[p.entity_id] = d.trips;
          pending += d.pending;
          truncated = truncated || d.truncated;
          routingError = routingError || d.routing_error;
        }));
      }
      await Promise.all(jobs);
    }));
    // A background refresh must not close the popup someone is reading.
    if (auto && document.querySelector(".leaflet-popup")) {
      drawNow();
      return;
    }
    state.cells = cells;
    state.trips = trips;
    redrawAll();
    fitOnce();
    updateRoutesNote(pending, truncated, routingError);
    clearTimeout(routesTimer);
    if (pending) routesTimer = setTimeout(() => loadData(true), 5000); // more streets are being matched
  }

  function updateRoutesNote(pending, truncated, routingError) {
    const notes = [];
    if (state.showRoutes) {
      if (!state.cfg.snap_to_roads) {
        notes.push("Following the streets is off, so lines connect the recorded points directly. Flights are still drawn as arcs.");
      } else if (routingError) {
        notes.push(`Matching to streets failed: ${routingError}. Showing straight lines for now.`);
      } else if (pending) {
        notes.push(`Routing ${pending} trip${pending === 1 ? "" : "s"} along the streets… (about one per second)`);
      }
      if (truncated) notes.push("Showing the latest 300 trips and jumps. Pick a shorter time to see older ones.");
    }
    $("routes-note").textContent = notes.join(" ");
    $("routes-note").hidden = !notes.length;
  }

  async function refresh(auto = false) {
    try {
      await loadPeople();
      await loadData(auto);
    } catch (err) {
      if (err.message !== "access denied") setStatus(`Could not load data: ${err.message}`, true);
    }
  }

  const reload = () => loadData().catch((err) => setStatus(`Could not load data: ${err.message}`, true));

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
        (p.points && state.cfg.can_delete ? `<button type="button" class="icon-btn del" title="Delete ${esc(p.name)}'s history" aria-label="Delete ${esc(p.name)}'s history">` +
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
        drawRoutes(p.entity_id);
        draw(p.entity_id);
        drawArrows();
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

  function setDay(iso) {
    state.day = iso > todayIso() ? todayIso() : iso;
    $("day").value = state.day;
    $("day-next").disabled = state.day >= todayIso();
    state.fitted = false;
    reload();
  }

  function shiftDay(delta) {
    const [y, m, d] = state.day.split("-").map(Number);
    const dt = new Date(y, m - 1, d + delta);
    setDay(`${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`);
  }

  function initControls() {
    const cfg = state.cfg;
    if (!state.cell) state.cell = cfg.cell_size_m;
    const cellSel = $("cell");
    if (![...cellSel.options].some((o) => +o.value === state.cell)) {
      cellSel.add(new Option(`${state.cell} m`, state.cell));
    }
    cellSel.value = state.cell;
    cellSel.addEventListener("change", () => { state.cell = +cellSel.value; store.set("cell", state.cell); reload(); });

    $("stay").value = state.stay;
    $("stay").addEventListener("change", () => {
      state.stay = +$("stay").value;
      store.set("stay", state.stay);
      reload();
    });

    // Time: a named range, or one day.
    const syncDayRow = () => { $("day-row").hidden = state.range !== "day"; };
    segmented("range", () => state.range, (v) => {
      state.range = v;
      store.set("range", v);
      syncDayRow();
      if (v === "day") state.fitted = false;
      reload();
    });
    syncDayRow();
    $("day").max = todayIso();
    $("day").value = state.day;
    $("day-next").disabled = true;
    $("day").addEventListener("change", () => { if ($("day").value) setDay($("day").value); });
    $("day-prev").addEventListener("click", () => shiftDay(-1));
    $("day-next").addEventListener("click", () => shiftDay(1));

    // Routes.
    const syncSnapRow = () => {
      $("snap-row").hidden = !routesOn();
      $("line-key").hidden = !routesOn();
    };
    $("snap").checked = !!cfg.snap_to_roads;
    $("snap").disabled = !cfg.can_delete;
    $("snap-hint").textContent = cfg.can_delete
      ? `Sends trip coordinates (no names) to ${cfg.routing_host || "the routing server"}`
      : "Only users who can see everyone can change this";
    $("snap").addEventListener("change", async () => {
      const want = $("snap").checked;
      try {
        const res = await fetch("api/settings", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ snap_to_roads: want }),
        });
        if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
        state.cfg.snap_to_roads = (await res.json()).snap_to_roads;
        reload();
      } catch (err) {
        $("snap").checked = !want;
        setStatus(`Could not change the setting: ${err.message}`, true);
      }
    });
    syncSnapRow();
    $("routes").checked = state.showRoutes;
    $("routes").addEventListener("change", () => {
      state.showRoutes = $("routes").checked;
      store.set("routes2", state.showRoutes);
      syncSnapRow();
      if (!state.showRoutes) {
        for (const p of state.people) drawRoutes(p.entity_id);
        drawArrows();
        updateRoutesNote(0, false, null);
      } else {
        reload();
      }
    });

    // Shading.
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

    // Map type.
    const basemap = $("basemap");
    for (const [key, b] of Object.entries(BASEMAPS)) {
      if (key === "custom" && !cfg.tile_url) continue;
      if (key === "ha" && cfg.demo) continue;
      basemap.add(new Option(b.label, key));
    }
    if (!BASEMAPS[state.basemap] || (state.basemap === "custom" && !cfg.tile_url)) state.basemap = cfg.tile_url ? "custom" : "ha";
    if (cfg.demo && state.basemap === "ha") state.basemap = "osm";
    basemap.value = state.basemap;
    basemap.addEventListener("change", () => {
      state.basemap = basemap.value;
      store.set("basemap", state.basemap);
      setTiles();
    });

    // Collapsible panel.
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

  // --- settings: who is recorded, who can open Footprints -------------------------

  function initSettings() {
    const dlg = $("settings");
    const btn = $("open-settings");
    if (!state.cfg.can_delete || typeof dlg.showModal !== "function") return;
    btn.hidden = false;
    let data = null;

    const showError = (msg) => { $("settings-error").textContent = msg; $("settings-error").hidden = !msg; };

    function trackedChecked() {
      return [...document.querySelectorAll("#set-track input:checked")].map((i) => i.value);
    }

    function nameOf(eid) {
      const t = data.trackers.find((x) => x.entity_id === eid);
      return t ? t.name : eid;
    }

    function renderTrack() {
      const list = $("set-track");
      list.innerHTML = "";
      const tracked = data.tracked ? new Set(data.tracked) : null;
      for (const t of data.trackers) {
        const on = tracked ? tracked.has(t.entity_id) : t.entity_id.startsWith("person.");
        const li = document.createElement("li");
        li.innerHTML = `<div class="pick-row"><label><input type="checkbox" value="${esc(t.entity_id)}"${on ? " checked" : ""}>` +
          `<span class="pick-name">${esc(t.name)}<span class="pick-sub">${esc(t.entity_id)}${t.gps ? "" : " · no GPS position right now"}</span></span></label></div>`;
        list.appendChild(li);
      }
      if (!data.trackers.length) list.innerHTML = `<li class="empty">Home Assistant has no people or GPS trackers yet.</li>`;
      list.onchange = renderAccessPeople;
    }

    // Each user: Sees everyone / Sees only… (with the people) / No access.
    function renderAccess() {
      const list = $("set-access");
      list.innerHTML = "";
      $("access-hint").textContent = (data.options_users
        ? "Right now this comes from the users option of the app. Saving here replaces it. "
        : "") + "Only Home Assistant administrators can open apps, so other users are listed for completeness only.";
      for (const u of data.users) {
        let mode = "all";
        let people = [];
        if (data.access) {
          if (!(u.id in data.access)) mode = "none";
          else if (data.access[u.id]) { mode = "some"; people = data.access[u.id]; }
        }
        const me = u.id === data.me;
        if (!u.admin) mode = "none";
        if (me) mode = "all";
        const li = document.createElement("li");
        li.dataset.uid = u.id;
        li.dataset.people = JSON.stringify(people);
        li.classList.toggle("off", !u.admin);
        const sub = [u.username && `@${u.username}`, me && "you", !u.admin && "not an administrator"].filter(Boolean).join(" · ");
        li.innerHTML = `<div class="pick-row"><span class="pick-name" style="flex:1">${esc(u.name)}<span class="pick-sub">${esc(sub)}</span></span>` +
          `<select aria-label="Access for ${esc(u.name)}"${!u.admin || me ? " disabled" : ""}>` +
          `<option value="all">Sees everyone</option><option value="some">Sees only…</option><option value="none">No access</option></select></div>` +
          `<div class="pick-people" hidden></div>`;
        const sel = li.querySelector("select");
        sel.value = mode;
        if (me) sel.title = "You can't take away your own access";
        sel.addEventListener("change", () => renderPeopleFor(li));
        list.appendChild(li);
        renderPeopleFor(li);
      }
    }

    function renderPeopleFor(li) {
      const box = li.querySelector(".pick-people");
      const some = li.querySelector("select").value === "some";
      box.hidden = !some;
      if (!some) return;
      const chosen = new Set(box.childElementCount ? [...box.querySelectorAll("input:checked")].map((i) => i.value) : JSON.parse(li.dataset.people));
      box.innerHTML = trackedChecked().map((eid) =>
        `<label><input type="checkbox" value="${esc(eid)}"${chosen.has(eid) ? " checked" : ""}>${esc(nameOf(eid))}</label>`).join("") ||
        `<span class="pick-sub">Pick who is recorded first.</span>`;
    }

    function renderAccessPeople() {
      for (const li of document.querySelectorAll("#set-access > li")) renderPeopleFor(li);
    }

    btn.addEventListener("click", async () => {
      showError("");
      $("settings-loading").hidden = false;
      $("settings-content").hidden = true;
      $("settings-save").disabled = true;
      dlg.showModal();
      try {
        data = await getJson("api/admin");
        renderTrack();
        renderAccess();
        $("settings-loading").hidden = true;
        $("settings-content").hidden = false;
        $("settings-save").disabled = false;
      } catch (err) {
        $("settings-loading").hidden = true;
        showError(errorText(err));
      }
    });

    $("settings-save").addEventListener("click", async () => {
      showError("");
      // Tracking: just the people (the default) is saved as "every person", so new ones join by themselves.
      const picked = trackedChecked();
      const persons = data.trackers.filter((t) => t.entity_id.startsWith("person.")).map((t) => t.entity_id);
      const allPersons = picked.length === persons.length && picked.every((e) => e.startsWith("person."));
      const tracked = allPersons ? [] : picked;
      if (!picked.length) { showError("Pick at least one person to record."); return; }

      // Access: everyone admin on "Sees everyone" is the default (also covers future administrators).
      const rows = [...document.querySelectorAll("#set-access > li")];
      const admins = rows.filter((li) => !li.classList.contains("off"));
      let access = null;
      if (!admins.every((li) => li.querySelector("select").value === "all")) {
        access = {};
        for (const li of admins) {
          const mode = li.querySelector("select").value;
          if (mode === "all") access[li.dataset.uid] = null;
          if (mode === "some") {
            const people = [...li.querySelectorAll(".pick-people input:checked")].map((i) => i.value);
            if (!people.length) { showError(`Pick at least one person for ${li.querySelector(".pick-name").firstChild.textContent}, or choose No access.`); return; }
            access[li.dataset.uid] = people;
          }
        }
      }
      $("settings-save").disabled = true;
      try {
        const res = await fetch("api/admin", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tracked, access }),
        });
        if (!res.ok) throw new Error(await res.text());
        dlg.close();
        setStatus("Settings saved.");
        refresh();
      } catch (err) {
        showError(errorText(err));
      } finally {
        $("settings-save").disabled = false;
      }
    });
  }

  function errorText(err) {
    // Server errors come as "502 {"error": "..."}" or plain text.
    const m = /^\d+ (\{.*\})$/s.exec(err.message);
    if (m) { try { return JSON.parse(m[1]).error || err.message; } catch { /* fall through */ } }
    return err.message;
  }

  async function start() {
    try {
      state.cfg = await getJson("api/config");
    } catch (err) {
      if (err.message === "access denied") return;
      state.cfg = { cell_size_m: 50, map_style: "auto", tile_url: "", snap_to_roads: true, can_delete: false };
      setStatus(`Could not load settings: ${err.message}`, true);
    }
    initControls();
    initSettings();
    setTiles();
    dark.addEventListener("change", applyDarkTiles);
    await refresh();
    setInterval(() => { if (!document.hidden) refresh(true); }, 60000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(true); });
  }

  start();
})();
