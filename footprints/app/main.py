"""Footprints web server (served to Home Assistant through Ingress)."""
import asyncio
import json
import logging
import os
import time
from pathlib import Path

import aiohttp
from aiohttp import web

import demo
from ha import HomeAssistant
from store import Store

log = logging.getLogger("footprints")

DATA_DIR = Path(os.environ.get("FP_DATA_DIR", "/data"))
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEMO = bool(os.environ.get("FP_DEMO"))

DEFAULT_OPTIONS = {
    "entities": [],
    "min_distance_m": 20,
    "max_accuracy_m": 200,
    "import_days": 3650,
    "keep_days": 0,
    "cell_size_m": 50,
    "map_style": "auto",
    "tile_url": "",
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


@web.middleware
async def ingress_only(request: web.Request, handler):
    # Home Assistant does the authentication; only its ingress proxy may talk to us.
    if not DEMO and request.remote not in ALLOWED_CLIENTS:
        raise web.HTTPForbidden()
    return await handler(request)


def number(request: web.Request, name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(request.query.get(name, default))
    except ValueError:
        raise web.HTTPBadRequest(text=f"{name} must be a number")
    return min(hi, max(lo, value))


async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def api_config(request: web.Request) -> web.Response:
    opts = request.app["opts"]
    return web.json_response({
        "cell_size_m": opts["cell_size_m"],
        "map_style": opts["map_style"],
        "tile_url": opts["tile_url"],
        "demo": DEMO,
    })


async def api_people(request: web.Request) -> web.Response:
    store: Store = request.app["store"]
    ha: HomeAssistant | None = request.app["ha"]
    people = demo.people() if DEMO else ha.people()
    summary = await asyncio.to_thread(store.summary)
    for p in people:
        p.update(summary.get(p["entity_id"], {"points": 0, "first": None, "last": None}))
    return web.json_response({
        "people": people,
        "connected": True if DEMO else ha.connected,
        "importing": None if DEMO else ha.importing,
        "error": None if DEMO else ha.last_error,
    })


async def api_cells(request: web.Request) -> web.Response:
    store: Store = request.app["store"]
    entity = request.query.get("entity", "")
    if not entity:
        raise web.HTTPBadRequest(text="entity is required")
    rng = request.query.get("range", "all")
    if rng not in RANGES:
        raise web.HTTPBadRequest(text="unknown range")
    days = RANGES[rng]
    start = 0.0 if days is None else (time.time() // 3600) * 3600 - days * 86400
    cell = number(request, "cell", request.app["opts"]["cell_size_m"], 10, 2000)
    min_stay = number(request, "min_stay", 0, 0, 24 * 60) * 60
    min_away = number(request, "min_away", 30, 0, 24 * 60) * 60
    cells = await asyncio.to_thread(store.cells, entity, start, None, cell, min_away, min_stay)
    return web.json_response({"entity_id": entity, "cell": cell, "cells": cells})


async def api_delete(request: web.Request) -> web.Response:
    entity = request.match_info["entity"]
    removed = await asyncio.to_thread(request.app["store"].delete_entity, entity)
    log.info("Deleted %d points for %s", removed, entity)
    return web.json_response({"removed": removed})


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
    if DEMO:
        await asyncio.to_thread(demo.seed, app["store"])
        app["ha"] = None
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

    app = web.Application(middlewares=[ingress_only])
    app["opts"] = opts
    app["store"] = Store(DATA_DIR / "footprints.db")
    app["ha"] = None
    app.router.add_get("/", index)
    app.router.add_get("/api/config", api_config)
    app.router.add_get("/api/people", api_people)
    app.router.add_get("/api/cells", api_cells)
    app.router.add_delete("/api/history/{entity}", api_delete)
    app.router.add_static("/static", STATIC_DIR)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("FP_PORT", 8099)), print=None)


if __name__ == "__main__":
    main()
