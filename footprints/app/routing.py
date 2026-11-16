"""Draws trips along streets and paths with an OSRM routing server (only when snap_to_roads is on).

Phones usually report a location every minute or so, far too sparse for GPS map matching. So
this works like a navigation system instead: it asks for the street route from each recorded
point to the next one, in order. Before that, points closer together than about a street block
are dropped (so GPS wobble doesn't turn into detours), and a point that makes the route loop
out and back (a fix that landed on the wrong road) is dropped and the route asked for again.
Points far from any road (on a boat, say) are joined with straight lines instead of being
pulled to the nearest road. Flights are never routed; they are drawn as arcs by main.py.

Trips are routed once, in the background, at most one request per second (the public FOSSGIS
servers ask for that), and the result is stored. Only coordinates are sent: no names, no times,
no entity IDs.
"""
import asyncio
import logging

import aiohttp

from store import Store, distance_m

log = logging.getLogger("footprints.routing")

USER_AGENT = "Footprints-HomeAssistant-App (+https://github.com/Nuvolares/ha-footprints)"
ALGORITHM = "route-through-points-2"  # stored results from another algorithm are redone
CHUNK = 50  # waypoints per request
SPACING = {"car": 120.0, "bike": 50.0, "foot": 25.0}  # drop points closer than this to the last kept one
MAX_FIXES = 3  # rounds of dropping points that cause out-and-back loops
OFFROAD_M = 300.0  # a point this far from any road or path is on the water (or off the map's roads)


def profile_for(trip: dict) -> str:
    seconds = max(1.0, trip["end"] - trip["start"])
    speed = trip["dist"] / seconds  # m/s
    if speed > 7:  # faster than ~25 km/h
        return "car"
    if speed > 2.5:  # ~9 km/h
        return "bike"
    return "foot"


def thin(points: list, spacing: float) -> list:
    """Keep the first and last point, and the points at least `spacing` from the last kept one."""
    kept = [points[0]]
    for p in points[1:-1]:
        if distance_m(kept[-1][1], kept[-1][2], p[1], p[2]) >= spacing:
            kept.append(p)
    if len(points) > 1:
        last = points[-1]
        if len(kept) > 1 and distance_m(kept[-1][1], kept[-1][2], last[1], last[2]) < spacing / 2:
            kept[-1] = last  # avoid a tiny last leg that could double back
        else:
            kept.append(last)
    return kept


def is_detour(leg_m: float, straight_m: float) -> bool:
    return leg_m > 2.5 * straight_m + 400


def join(line: list, more: list) -> None:
    """Append a piece to a line, without repeating the point where they meet."""
    if line and more and line[-1] == more[0]:
        more = more[1:]
    line.extend(more)


class Router:
    def __init__(self, session: aiohttp.ClientSession, store: Store, url_template: str):
        self.session = session
        self.store = store
        self.url_template = url_template.rstrip("/")
        self.queue: asyncio.Queue = asyncio.Queue()
        self.queued: set[tuple[str, float]] = set()
        self.last_error: str | None = None
        if store.get_meta("routing_algorithm") != ALGORITHM:
            store.clear_routes()
            store.set_meta("routing_algorithm", ALGORITHM)

    def lookup(self, entity_id: str, trip: dict) -> tuple[list | None, bool]:
        """(geometry or None, still pending). Queues the trip when it hasn't been routed yet."""
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
                geometry = await self.route_trip(trip)
                self.store.save_route(entity_id, trip, geometry)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, IndexError) as err:
                # Network trouble: don't store anything, so it's tried again on a later view.
                self.last_error = str(err) or err.__class__.__name__
                log.warning("Could not route a trip along the streets: %s", self.last_error)
                await asyncio.sleep(30)
            finally:
                self.queued.discard((entity_id, trip["start"]))

    async def route_trip(self, trip: dict) -> list | dict | None:
        if trip.get("kind") == "jump":
            return await self.route_jump(trip)
        profile = profile_for(trip)
        base = self.url_template.replace("{profile}", profile)
        points = thin(trip["points"], SPACING[profile])
        if len(points) < 2:
            return None
        line: list = []
        for i in range(0, len(points) - 1, CHUNK - 1):
            part = points[i:i + CHUNK]
            if len(part) < 2:
                break
            join(line, await self._route_part(base, part))
        return line or None

    async def route_jump(self, jump: dict) -> dict:
        """A stretch without recorded points: the street route if there is a sensible one,
        otherwise a straight line (a ferry, a boat, or somewhere without roads)."""
        a, b = jump["points"][0], jump["points"][-1]
        straight = [[round(a[1], 6), round(a[2], 6)], [round(b[1], 6), round(b[2], 6)]]
        base = self.url_template.replace("{profile}", profile_for(jump))
        geometry, offroad, meters = await self._route_through(base, [a, b])
        if geometry is None or offroad or meters > 2 * jump["dist"] + 20000:
            return {"mode": "direct", "line": straight}
        return {"mode": "street", "line": geometry}

    async def _route_part(self, base: str, part: list) -> list:
        """Route through the points; points far from any road (on the water, say) are joined
        to their neighbors with straight lines instead of being pulled to the nearest road."""
        geometry, offroad, _ = await self._route_through(base, part)
        if not offroad:
            return geometry or [[round(p[1], 6), round(p[2], 6)] for p in part]
        line: list = []
        run: list = []
        for i, p in enumerate(part + [None]):
            if p is None or i in offroad:
                if len(run) >= 2:
                    sub, _, _ = await self._route_through(base, run, check_offroad=False)
                    join(line, sub or [[round(q[1], 6), round(q[2], 6)] for q in run])
                elif run:
                    join(line, [[round(run[0][1], 6), round(run[0][2], 6)]])
                if p is not None:
                    join(line, [[round(p[1], 6), round(p[2], 6)]])
                run = []
            else:
                run.append(p)
        return line

    async def _route_through(self, base: str, part: list, check_offroad: bool = True):
        """The street route through the points, in order, like navigating from one to the next.

        Returns (geometry or None, indices of points far from any road, route length in m).
        """
        geometry, meters = None, 0.0
        for _ in range(MAX_FIXES + 1):
            coords = ";".join(f"{p[2]:.6f},{p[1]:.6f}" for p in part)
            data = await self._get(f"{base}/route/v1/driving/{coords}?geometries=geojson&overview=full")
            if data.get("code") != "Ok" or not data.get("routes"):
                return geometry, set(), meters
            if check_offroad and geometry is None:
                offroad = {i for i, w in enumerate(data.get("waypoints") or [])
                           if (w.get("distance") or 0) > OFFROAD_M}
                if offroad:
                    return None, offroad, 0.0
            route = data["routes"][0]
            geometry = [[round(c[1], 6), round(c[0], 6)] for c in route["geometry"]["coordinates"]]
            meters = float(route.get("distance") or 0)
            legs = route.get("legs") or []
            if len(part) <= 2 or len(legs) != len(part) - 1:
                return geometry, set(), meters

            # A misplaced point shows up as two detours in a row: out to it, and back again.
            detour = [is_detour(leg.get("distance", 0), distance_m(a[1], a[2], b[1], b[2]))
                      for leg, a, b in zip(legs, part, part[1:])]
            bad = {i + 1 for i in range(len(detour) - 1) if detour[i] and detour[i + 1]}
            if not bad:
                return geometry, set(), meters
            part = [p for i, p in enumerate(part) if i not in bad]
            if len(part) < 2:
                return geometry, set(), meters
        return geometry, set(), meters

    async def _get(self, url: str) -> dict:
        await asyncio.sleep(1.1)  # stay under one request per second
        async with self.session.get(url, headers={"User-Agent": USER_AGENT},
                                    timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status == 429:
                raise aiohttp.ClientError("routing server says too many requests")
            return await resp.json(content_type=None)
