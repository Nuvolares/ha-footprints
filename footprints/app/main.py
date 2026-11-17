"""Footprints web server (served to Home Assistant through Ingress)."""
import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

import demo
from ha import HomeAssistant
from routing import Router
from store import Store, great_circle

log = logging.getLogger("footprints")

DATA_DIR = Path(os.environ.get("FP_DATA_DIR", "/data"))
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEMO = bool(os.environ.get("FP_DEMO"))

DEFAULT_OPTIONS = {
    "entities": [],
    "users": [],
    "min_distance_m": 20,
    "max_accuracy_m": 200,
    "import_days": 3650,
    "keep_days": 0,
    "cell_size_m": 50,
    "map_style": "auto",
    "tile_url": "",
    "snap_to_roads": True,
    "routing_url": "https://routing.openstreetmap.de/routed-{profile}",
    "log_level": "info",
}

RANGES = {"7d": 7, "30d": 30, "90d": 90, "1y": 365, "all": None}
ALLOWED_CLIENTS = {"172.30.32.2", "127.0.0.1", "::1"}  # Supervisor ingress proxy + local checks


def load_options() -> dict:
    opts = dict(DEFAULT_OPTIONS)
    try:
        opts.update(json.loads((DATA_DIR / "options.json").read_text()))
    except FileNotFoundError:
        pass
    except ValueError as err:
        log.error("Could not parse options.json: %s", err)
    return opts


# --- who may see what ----------------------------------------------------------

def user_rules(opts: dict, store: Store) -> dict[str, set[str] | None]:
    """"id:<user id>" or "name:<username>" -> the person entities they may see (None: everyone).

    What was picked under Settings on the map (by user ID) wins over the `users` option (by
    username). Empty means every administrator sees everyone.
    """
    rules: dict[str, set[str] | None] = {}
    saved = store.get_meta("access_rules")
    if saved is not None:
        for uid, rule in json.loads(saved).items():
            people = set(rule.get("people") or [])
            rules[f"id:{uid}"] = people or None
        return rules
    for entry in opts.get("users") or []:
        if isinstance(entry, str):
            name, people = entry, []
        else:
            name, people = entry.get("username", ""), entry.get("people") or []
        name = name.strip().lower()
        if name:
            people = {p.strip() for p in people if p and p.strip()}
            rules[f"name:{name}"] = people or None
    return rules


@web.middleware
async def access(request: web.Request, handler):
    # Home Assistant does the login; only its ingress proxy may talk to us.
    if not DEMO and request.remote not in ALLOWED_CLIENTS:
        raise web.HTTPForbidden()
    rules = request.app["rules"]
    request["people_allowed"] = None
    # Set by the Supervisor's ingress proxy from the signed-in Home Assistant user. Neither the
    # user ID nor the username (login name) can be changed by the user, unlike the display name.
    user_id = request.headers.get("X-Remote-User-Id", "").strip()
    username = request.headers.get("X-Remote-User-Name", "").strip().lower()
    request["user_id"] = user_id
    if rules and not DEMO:
        key = f"id:{user_id}" if user_id and f"id:{user_id}" in rules else f"name:{username}"
        if key not in rules:
            if request.path.startswith("/static/"):
                return await handler(request)
            log.warning("Denied access for user %r", username or user_id or "(unknown)")
            if request.path.startswith("/api/"):
                return web.json_response({"error": "access denied", "denied": True}, status=403)
            return web.Response(status=403, content_type="text/html", text=DENIED_PAGE)
        request["people_allowed"] = rules[key]
    return await handler(request)


def check_person(request: web.Request, entity_id: str) -> None:
    allowed = request["people_allowed"]
    if allowed is not None and entity_id not in allowed:
        raise web.HTTPForbidden(text="not allowed to see this person")


DENIED_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Footprints</title>
<style>:root{--bg:#f4f5f7;--text:#1b1f24;--muted:#5f6873}
@media (prefers-color-scheme: dark){:root{--bg:#111418;--text:#e8eaed;--muted:#9aa3ad}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:16px;box-sizing:border-box}
main{max-width:420px;text-align:center}h1{font-size:18px;margin:0 0 8px}p{color:var(--muted);margin:0}</style>
</head><body><main><h1>No access</h1><p>Your Home Assistant user isn't allowed to see Footprints.
An administrator can give you access under Settings in Footprints.</p>
<p style="margin-top:12px;font-size:13px">To remove Footprints from your sidebar, select your
name at the bottom of the sidebar, then <b>Change the order and hide items from the sidebar</b>.</p>
</main></body></html>"""


# --- requests ------------------------------------------------------------------

def number(request: web.Request, name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(request.query.get(name, default))
    except ValueError:
        raise web.HTTPBadRequest(text=f"{name} must be a number")
    return min(hi, max(lo, value))


def time_window(request: web.Request) -> tuple[float, float | None]:
    """Either an explicit start/end (one day) or a named range up to now."""
    if "start" in request.query and "end" in request.query:
        start = number(request, "start", 0, 0, 1e11)
        end = number(request, "end", 0, 0, 1e11)
        if end <= start:
            raise web.HTTPBadRequest(text="end must be after start")
        return start, end
    rng = request.query.get("range", "all")
    if rng not in RANGES:
        raise web.HTTPBadRequest(text="unknown range")
    days = RANGES[rng]
    return (0.0 if days is None else (time.time() // 3600) * 3600 - days * 86400), None


def static_version() -> str:
    """A fingerprint of the page's own files, so a browser never mixes old and new ones."""
    h = hashlib.sha256()
    for name in ("app.js", "style.css"):
        h.update((STATIC_DIR / name).read_bytes())
    return h.hexdigest()[:12]


INDEX_HTML: str | None = None


async def index(request: web.Request) -> web.StreamResponse:
    global INDEX_HTML
    if INDEX_HTML is None:
        v = static_version()
        INDEX_HTML = ((STATIC_DIR / "index.html").read_text(encoding="utf-8")
                      .replace('src="static/app.js"', f'src="static/app.js?v={v}"')
                      .replace('href="static/style.css"', f'href="static/style.css?v={v}"'))
    return web.Response(text=INDEX_HTML, content_type="text/html", headers={"Cache-Control": "no-cache"})


@web.middleware
async def no_stale_static(request: web.Request, handler):
    """Make the browser check its saved copy of the page's files with every load."""
    response = await handler(request)
    if request.path.startswith("/static/") and not request.path.startswith("/static/vendor/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


async def api_config(request: web.Request) -> web.Response:
    opts = request.app["opts"]
    return web.json_response({
        "cell_size_m": opts["cell_size_m"],
        "map_style": opts["map_style"],
        "tile_url": opts["tile_url"],
        "snap_to_roads": request.app["snap"],
        "routing_host": urlparse(opts["routing_url"].replace("{profile}", "car")).hostname,
        "can_delete": request["people_allowed"] is None,
        "demo": DEMO,
    })


async def api_people(request: web.Request) -> web.Response:
    store: Store = request.app["store"]
    ha: HomeAssistant | None = request.app["ha"]
    people = demo.people() if DEMO else ha.people()
    allowed = request["people_allowed"]
    if allowed is not None:
        people = [p for p in people if p["entity_id"] in allowed]
    summary = await asyncio.to_thread(store.summary)
    for p in people:
        p.update(summary.get(p["entity_id"], {"points": 0, "first": None, "last": None}))
    return web.json_response({
        "people": people,
        "connected": True if DEMO else ha.connected,
        "importing": None if DEMO else ha.importing,
        "error": None if DEMO else ha.last_error,
    })


def entity_param(request: web.Request) -> str:
    entity = request.query.get("entity", "")
    if not entity:
        raise web.HTTPBadRequest(text="entity is required")
    check_person(request, entity)
    return entity


async def api_cells(request: web.Request) -> web.Response:
    store: Store = request.app["store"]
    entity = entity_param(request)
    start, end = time_window(request)
    cell = number(request, "cell", request.app["opts"]["cell_size_m"], 1, 2000)
    min_stay = number(request, "min_stay", 0, 0, 24 * 60) * 60
    min_away = number(request, "min_away", 30, 0, 24 * 60) * 60
    cells = await asyncio.to_thread(store.cells, entity, start, end, cell, min_away, min_stay)
    return web.json_response({"entity_id": entity, "cell": cell, "cells": cells})


async def api_routes(request: web.Request) -> web.Response:
    store: Store = request.app["store"]
    router: Router | None = request.app["router"]
    entity = entity_param(request)
    start, end = time_window(request)
    trips, truncated = await asyncio.to_thread(store.trips, entity, start, end or 1e12)
    out, pending = [], 0
    for trip in trips:
        line, snapped, mode = None, False, "direct"
        if trip["kind"] == "jump" and trip["mode"] == "air":
            # Flights are drawn as arcs over the globe, never along roads.
            line, mode = [], "air"
            for a, b in zip(trip["points"], trip["points"][1:]):
                line.extend(great_circle(a[1], a[2], b[1], b[2])[1 if line else 0:])
        elif router and request.app["snap"]:
            found, waiting = router.lookup(entity, trip)  # quick, and it touches the asyncio queue
            pending += waiting
            if isinstance(found, dict):  # a jump: routed along streets, or a straight line
                line, mode = found["line"], found["mode"]
            elif found is not None:
                line, mode = found, "street"
            snapped = mode == "street"
        if line is None:
            line = [[round(p[1], 6), round(p[2], 6)] for p in trip["points"]]
        out.append({"start": int(trip["start"]), "end": int(trip["end"]), "dist": trip["dist"],
                    "kind": trip["kind"], "mode": mode, "snapped": snapped, "line": line})
    return web.json_response({
        "entity_id": entity, "trips": out, "truncated": truncated, "pending": pending,
        "routing_error": router.last_error if router and request.app["snap"] else None,
    })


async def api_settings(request: web.Request) -> web.Response:
    """Settings changed from the map. Only users who can see everyone may change them."""
    if request["people_allowed"] is not None:
        raise web.HTTPForbidden(text="only users who can see everyone can change settings")
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="expected JSON")
    if "snap_to_roads" in body:
        snap = bool(body["snap_to_roads"])
        request.app["snap"] = snap
        await asyncio.to_thread(request.app["store"].set_meta, "snap_to_roads", "1" if snap else "0")
        log.info("Following the streets %s (changed on the map)", "on" if snap else "off")
    return web.json_response({"snap_to_roads": request.app["snap"]})


def require_full_access(request: web.Request) -> None:
    if request["people_allowed"] is not None:
        raise web.HTTPForbidden(text="only users who can see everyone can change this")


def current_access(app: web.Application) -> dict[str, list[str] | None]:
    """The access picked under Settings, as {user id: people or None}; {} when none was."""
    saved = app["store"].get_meta("access_rules")
    if saved is None:
        return {}
    return {uid: (rule.get("people") or None) for uid, rule in json.loads(saved).items()}


async def api_admin(request: web.Request) -> web.Response:
    """Who can be tracked and who can open Footprints, for the Settings dialog."""
    require_full_access(request)
    ha: HomeAssistant | None = request.app["ha"]
    store: Store = request.app["store"]
    if DEMO:
        trackers = [{"entity_id": p["entity_id"], "name": p["name"], "gps": True} for p in demo.people()]
        users = [{"id": "demo-owner", "username": "owner", "name": "Owner", "admin": True},
                 {"id": "demo-alex", "username": "alex", "name": "Alex", "admin": True},
                 {"id": "demo-kid", "username": "kid", "name": "Kid", "admin": False}]
        tracked = None
    else:
        trackers = ha.trackers()
        try:
            users = await ha.users()
        except (ConnectionError, RuntimeError, asyncio.TimeoutError) as err:
            return web.json_response({"error": f"Could not get the users from Home Assistant: {err}"}, status=502)
        tracked = ha.explicit or None
    saved_access = store.get_meta("access_rules") is not None
    access = current_access(request.app) if saved_access else None
    if access is None and request.app["rules"]:
        # Still set by the `users` option (by username): show it the same way.
        by_name = {u["username"].lower(): u["id"] for u in users if u["username"]}
        access = {by_name[k[5:]]: (sorted(v) if v else None)
                  for k, v in request.app["rules"].items() if k.startswith("name:") and k[5:] in by_name}
    return web.json_response({
        "trackers": trackers,
        "tracked": tracked,                 # None: every person
        "users": users,
        "access": access or None,           # None: every administrator sees everyone
        "options_users": bool(request.app["opts"].get("users")) and not saved_access,
        "me": request["user_id"] or ("demo-owner" if DEMO else ""),
    })


async def api_admin_save(request: web.Request) -> web.Response:
    require_full_access(request)
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="expected JSON")
    store: Store = request.app["store"]
    ha: HomeAssistant | None = request.app["ha"]

    if "tracked" in body:
        tracked = body["tracked"] or []
        if not isinstance(tracked, list) or not all(isinstance(e, str) and "." in e for e in tracked):
            raise web.HTTPBadRequest(text="tracked must be a list of entity IDs")
        await asyncio.to_thread(store.set_meta, "tracked_entities", json.dumps(tracked))
        if ha:
            ha.set_tracked(tracked)

    if "access" in body:
        access = body["access"]
        if access is None:  # back to the default: every administrator sees everyone
            new = {}
        elif isinstance(access, dict):
            new = {}
            for uid, people in access.items():
                if people is not None and not (isinstance(people, list) and all(isinstance(p, str) for p in people)):
                    raise web.HTTPBadRequest(text="people must be a list of entity IDs")
                new[str(uid)] = {"people": people or []}
        else:
            raise web.HTTPBadRequest(text="access must be an object")
        me = request["user_id"] or ("demo-owner" if DEMO else "")
        if new and (not me or me not in new or new[me]["people"]):
            raise web.HTTPBadRequest(text="You can't take away your own access to everyone; "
                                          "keep yourself on Sees everyone.")
        # Saved even when empty ("every administrator"), so the `users` option no longer applies.
        await asyncio.to_thread(store.set_meta, "access_rules", json.dumps(new))
        request.app["rules"] = user_rules(request.app["opts"], store)
        log.info("Access changed on the map: %s", f"{len(new)} users" if new else "every administrator")
    return web.json_response({"ok": True})


async def api_map_token(request: web.Request) -> web.Response:
    ha: HomeAssistant | None = request.app["ha"]
    if ha is None:
        return web.json_response({"error": "demo mode has no Home Assistant map"}, status=404)
    try:
        return web.json_response({"token": await ha.map_token()}, headers={"Cache-Control": "no-store"})
    except (ConnectionError, RuntimeError, asyncio.TimeoutError) as err:
        log.warning("Could not get a map token from Home Assistant: %s", err)
        return web.json_response({"error": str(err)}, status=502)


async def api_delete(request: web.Request) -> web.Response:
    if request["people_allowed"] is not None:
        raise web.HTTPForbidden(text="only users who can see everyone can delete history")
    entity = request.match_info["entity"]
    removed = await asyncio.to_thread(request.app["store"].delete_entity, entity)
    log.info("Deleted %d points for %s", removed, entity)
    return web.json_response({"removed": removed})


# --- lifecycle -----------------------------------------------------------------

async def retention(app: web.Application) -> None:
    keep = int(app["opts"].get("keep_days") or 0)
    while keep > 0:
        removed = await asyncio.to_thread(app["store"].purge_before, time.time() - keep * 86400)
        if removed:
            log.info("Removed %d points older than %d days", removed, keep)
        await asyncio.sleep(6 * 3600)


async def on_startup(app: web.Application) -> None:
    app["http"] = aiohttp.ClientSession()
    app["tasks"] = [asyncio.create_task(retention(app))]
    # The switch on the map wins over the option, once someone has used it.
    store: Store = app["store"]
    saved = store.get_meta("snap_to_roads")
    if store.get_meta("snap_default_on") is None:
        # 0.3.0 turned following the streets on by default, also for existing installs whose
        # options still have the old default (off), unless someone switched it off on the map.
        store.set_meta("snap_default_on", "1")
        if saved is None:
            saved = "1"
            store.set_meta("snap_to_roads", saved)
    if DEMO and not os.environ.get("FP_DEMO_SNAP"):
        saved = "0"  # previews and the build check don't send made-up trips to the public server
    app["snap"] = (saved == "1") if saved is not None else bool(app["opts"]["snap_to_roads"])
    app["router"] = Router(app["http"], app["store"], app["opts"]["routing_url"])
    app["tasks"].append(asyncio.create_task(app["router"].run()))
    if app["snap"]:
        log.info("Following the streets with %s", app["opts"]["routing_url"])
    if DEMO:
        await asyncio.to_thread(demo.seed, app["store"])
    else:
        app["ha"] = HomeAssistant(app["http"], app["store"], app["opts"])
        tracked = store.get_meta("tracked_entities")
        if tracked is not None:  # picked under Settings on the map; wins over the option
            app["ha"].explicit = json.loads(tracked)
        app["tasks"].append(asyncio.create_task(app["ha"].run()))


async def on_cleanup(app: web.Application) -> None:
    for task in app["tasks"]:
        task.cancel()
    await app["http"].close()


def main() -> None:
    opts = load_options()
    logging.basicConfig(level=str(opts["log_level"]).upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    app = web.Application(middlewares=[access, no_stale_static])
    app["opts"] = opts
    app["store"] = Store(DATA_DIR / "footprints.db")
    app["rules"] = user_rules(opts, app["store"])
    app["ha"] = None
    app["router"] = None
    app["snap"] = False
    if app["rules"]:
        log.info("Access limited to: %s", ", ".join(sorted(app["rules"])))
    app.router.add_get("/", index)
    app.router.add_get("/api/config", api_config)
    app.router.add_get("/api/people", api_people)
    app.router.add_get("/api/cells", api_cells)
    app.router.add_get("/api/routes", api_routes)
    app.router.add_get("/api/map_token", api_map_token)
    app.router.add_post("/api/settings", api_settings)
    app.router.add_get("/api/admin", api_admin)
    app.router.add_post("/api/admin", api_admin_save)
    app.router.add_delete("/api/history/{entity}", api_delete)
    app.router.add_static("/static", STATIC_DIR)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("FP_PORT", 8099)), print=None)


if __name__ == "__main__":
    main()
