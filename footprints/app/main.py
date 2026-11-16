"""Footprints web server (served to Home Assistant through Ingress)."""
import asyncio
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
from store import Store

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
    "snap_to_roads": False,
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

def user_rules(opts: dict) -> dict[str, set[str] | None]:
    """username (lower case) -> the person entities they may see, or None for everyone."""
    rules: dict[str, set[str] | None] = {}
    for entry in opts.get("users") or []:
        if isinstance(entry, str):
            name, people = entry, []
        else:
            name, people = entry.get("username", ""), entry.get("people") or []
        name = name.strip().lower()
        if name:
            people = {p.strip() for p in people if p and p.strip()}
            rules[name] = people or None
    return rules


@web.middleware
async def access(request: web.Request, handler):
    # Home Assistant does the login; only its ingress proxy may talk to us.
    if not DEMO and request.remote not in ALLOWED_CLIENTS:
        raise web.HTTPForbidden()
    rules = request.app["rules"]
    request["people_allowed"] = None
    if rules and not DEMO:
        # Set by the Supervisor's ingress proxy from the signed-in Home Assistant user.
        # The username (login name) can't be changed by the user, unlike the display name.
        username = request.headers.get("X-Remote-User-Name", "").strip().lower()
        if username not in rules:
            if request.path.startswith("/static/"):
                return await handler(request)
            log.warning("Denied access for user %r", username or "(unknown)")
            if request.path.startswith("/api/"):
                return web.json_response({"error": "access denied", "denied": True}, status=403)
            return web.Response(status=403, content_type="text/html", text=DENIED_PAGE)
        request["people_allowed"] = rules[username]
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
An administrator can add you under <b>users</b> in the app's options.</p></main></body></html>"""


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


async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


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
    cell = number(request, "cell", request.app["opts"]["cell_size_m"], 10, 2000)
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
        line, snapped = None, False
        if router and request.app["snap"]:
            line, waiting = router.lookup(entity, trip)  # quick, and it touches the asyncio queue
            pending += waiting
            snapped = line is not None
        if line is None:
            line = [[round(p[1], 6), round(p[2], 6)] for p in trip["points"]]
        out.append({"start": int(trip["start"]), "end": int(trip["end"]), "dist": trip["dist"],
                    "snapped": snapped, "line": line})
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
    saved = app["store"].get_meta("snap_to_roads")
    app["snap"] = (saved == "1") if saved is not None else bool(app["opts"]["snap_to_roads"])
    app["router"] = Router(app["http"], app["store"], app["opts"]["routing_url"])
    app["tasks"].append(asyncio.create_task(app["router"].run()))
    if app["snap"]:
        log.info("Following the streets with %s", app["opts"]["routing_url"])
    if DEMO:
        await asyncio.to_thread(demo.seed, app["store"])
    else:
        app["ha"] = HomeAssistant(app["http"], app["store"], app["opts"])
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

    app = web.Application(middlewares=[access])
    app["opts"] = opts
    app["rules"] = user_rules(opts)
    app["store"] = Store(DATA_DIR / "footprints.db")
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
    app.router.add_delete("/api/history/{entity}", api_delete)
    app.router.add_static("/static", STATIC_DIR)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("FP_PORT", 8099)), print=None)


if __name__ == "__main__":
    main()
