"""Snaps trips to streets and paths with an OSRM routing server (only when snap_to_roads is on).

Trips are matched once, in the background, at most one request per second (the public FOSSGIS
servers ask for that), and the result is stored, so each trip is sent at most once. Only the
trip's coordinates and times are sent: no names or entity IDs.
"""
import asyncio
import logging

import aiohttp

from store import Store

log = logging.getLogger("footprints.routing")

USER_AGENT = "Footprints-HomeAssistant-App (+https://github.com/Nuvolares/ha-footprints)"
CHUNK = 90  # coordinates per request (servers allow up to 100)


def profile_for(trip: dict) -> str:
    seconds = max(1.0, trip["end"] - trip["start"])
    speed = trip["dist"] / seconds  # m/s
    if speed > 7:  # faster than ~25 km/h: a car (or a train, which matches badly either way)
        return "car"
    if speed > 2.5:  # ~9 km/h
        return "bike"
    return "foot"


class Router:
    def __init__(self, session: aiohttp.ClientSession, store: Store, url_template: str):
        self.session = session
        self.store = store
        self.url_template = url_template.rstrip("/")
        self.queue: asyncio.Queue = asyncio.Queue()
        self.queued: set[tuple[str, float]] = set()
        self.last_error: str | None = None

    def lookup(self, entity_id: str, trip: dict) -> tuple[list | None, bool]:
        """(geometry or None, still pending). Queues the trip when it hasn't been matched yet."""
        known, geometry = self.store.route_geometry(entity_id, trip)
        if known:
            return geometry, False
        key = (entity_id, trip["start"])
        if key not in self.queued:
            self.queued.add(key)
            self.queue.put_nowait((entity_id, trip))
        return None, True

    def pending(self) -> int:
        return len(self.queued)

    async def run(self) -> None:
        while True:
            entity_id, trip = await self.queue.get()
            try:
                geometry = await self.match(trip)
                self.store.save_route(entity_id, trip, geometry)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as err:
                # Network trouble: don't store anything, so it's tried again on a later view.
                self.last_error = str(err) or err.__class__.__name__
                log.warning("Could not snap a trip to roads: %s", self.last_error)
                await asyncio.sleep(30)
            finally:
                self.queued.discard((entity_id, trip["start"]))

    async def match(self, trip: dict) -> list | None:
        base = self.url_template.replace("{profile}", profile_for(trip))
        points = trip["points"]
        line: list = []
        for i in range(0, max(1, len(points) - 1), CHUNK - 1):
            part = points[i:i + CHUNK]
            if len(part) < 2:
                break
            geometry = await self._match_chunk(base, part)
            if geometry is None:
                geometry = await self._route_chunk(base, part)
            if geometry is None:
                geometry = [[p[1], p[2]] for p in part]  # nothing found: keep the straight line
            if line and geometry and line[-1] == geometry[0]:
                geometry = geometry[1:]
            line.extend(geometry)
        return line or None

    async def _get(self, url: str) -> dict:
        await asyncio.sleep(1.1)  # stay under one request per second
        async with self.session.get(url, headers={"User-Agent": USER_AGENT},
                                    timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status == 429:
                raise aiohttp.ClientError("routing server says too many requests")
            data = await resp.json(content_type=None)
        return data

    async def _match_chunk(self, base: str, part: list) -> list | None:
        """Map matching: the streets that best explain the recorded points."""
        coords = ";".join(f"{p[2]:.6f},{p[1]:.6f}" for p in part)
        stamps, prev = [], 0
        for p in part:
            prev = max(prev + 1, int(p[0]))
            stamps.append(str(prev))
        radiuses = ";".join(str(int(min(100, max(15, (p[3] or 25) * 1.5)))) for p in part)
        url = (f"{base}/match/v1/driving/{coords}?geometries=geojson&overview=full&gaps=ignore"
               f"&tidy=true&timestamps={';'.join(stamps)}&radiuses={radiuses}")
        data = await self._get(url)
        if data.get("code") != "Ok" or not data.get("matchings"):
            return None
        line = []
        for m in data["matchings"]:
            line.extend([round(c[1], 6), round(c[0], 6)] for c in m["geometry"]["coordinates"])
        return line

    async def _route_chunk(self, base: str, part: list) -> list | None:
        """Fallback for sparse points: the route through them."""
        if len(part) > 25:  # spread 25 waypoints evenly, keeping the first and last
            step = (len(part) - 1) / 24
            part = [part[round(i * step)] for i in range(25)]
        coords = ";".join(f"{p[2]:.6f},{p[1]:.6f}" for p in part)
        data = await self._get(f"{base}/route/v1/driving/{coords}?geometries=geojson&overview=full")
        if data.get("code") != "Ok" or not data.get("routes"):
            return None
        return [[round(c[1], 6), round(c[0], 6)] for c in data["routes"][0]["geometry"]["coordinates"]]
