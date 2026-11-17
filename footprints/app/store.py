"""Location history storage (SQLite) and the visit aggregation behind the map."""
import json
import math
import sqlite3
import threading
import time
from pathlib import Path

EARTH_M_PER_DEG = 111_320.0
MAX_GAP = 12 * 3600  # a longer silence means the tracker was offline; don't count it as time spent


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters (haversine), right for a street block and for a flight."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6_371_000.0 * math.asin(min(1.0, math.sqrt(h)))


def great_circle(lat1: float, lon1: float, lat2: float, lon2: float, steps: int = 48) -> list:
    """Points along the shortest path over the globe, the way flights are drawn on a map.

    Longitudes are kept continuous (they may go past ±180), so the line doesn't jump across
    the whole map when a flight crosses the date line.
    """
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = (math.cos(p1) * math.cos(l1), math.cos(p1) * math.sin(l1), math.sin(p1))
    b = (math.cos(p2) * math.cos(l2), math.cos(p2) * math.sin(l2), math.sin(p2))
    omega = math.acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b)))))
    if omega < 1e-6:
        return [[round(lat1, 6), round(lon1, 6)], [round(lat2, 6), round(lon2, 6)]]
    out, prev_lon = [], None
    for i in range(steps + 1):
        t = i / steps
        k1, k2 = math.sin((1 - t) * omega) / math.sin(omega), math.sin(t * omega) / math.sin(omega)
        x, y, z = (k1 * u + k2 * v for u, v in zip(a, b))
        lat = math.degrees(math.atan2(z, math.hypot(x, y)))
        lon = math.degrees(math.atan2(y, x))
        if prev_lon is not None:
            lon += 360 * round((prev_lon - lon) / 360)
        prev_lon = lon
        out.append([round(lat, 6), round(lon, 6)])
    return out


def route_key(entity_id: str, trip: dict) -> str:
    """Trips and jumps can start at the same point, so their stored routes are kept apart."""
    return f"{entity_id}|jump" if trip.get("kind") == "jump" else entity_id


SPIKE_MIN = 150.0       # m: how far out a point must be to count as an out-and-back
SPIKE_RETURN = 0.3      # back within this share of the way out counts as "came back"
SPIKE_STAY = 5 * 60     # s: less time than this at the far point means they didn't stop there


def drop_spikes(rows: list) -> list:
    """Leave out dead-end blips: a point the person went out to and came straight back from.

    Rows start with (ts, lat, lon). Point b between a and c is left out when it lies at least
    SPIKE_MIN away from both, c is back near a, and c came less than SPIKE_STAY after b (so
    nobody stopped at b). Most are GPS fixes that landed off the road; the others are
    U-turns that nobody wants as a place on the map. Points on the way somewhere else, and
    places where the person stayed, are kept.
    """
    if len(rows) < 3:
        return rows
    out = [rows[0]]
    for i in range(1, len(rows) - 1):
        a, b, c = rows[i - 1], rows[i], rows[i + 1]
        if c[0] - b[0] < SPIKE_STAY:
            out_m = distance_m(a[1], a[2], b[1], b[2])
            back_m = distance_m(b[1], b[2], c[1], c[2])
            if (out_m >= SPIKE_MIN and back_m >= SPIKE_MIN
                    and distance_m(a[1], a[2], c[1], c[2]) <= SPIKE_RETURN * min(out_m, back_m)):
                continue
        out.append(b)
    out.append(rows[-1])
    return out


class Thinner:
    """Drops points that add nothing: the person hasn't moved and was seen moments ago."""

    def __init__(self, min_distance_m: float, heartbeat_s: float):
        self.min_distance = min_distance_m
        self.heartbeat = heartbeat_s
        self.prev: dict[str, tuple[float, float, float]] = {}

    def seed(self, entity_id: str, last: tuple[float, float, float] | None) -> None:
        if last:
            self.prev[entity_id] = last

    def keep(self, entity_id: str, ts: float, lat: float, lon: float) -> bool:
        prev = self.prev.get(entity_id)
        if prev:
            pts, plat, plon = prev
            if ts <= pts:
                return False
            if ts - pts < self.heartbeat and distance_m(plat, plon, lat, lon) < self.min_distance:
                return False
        self.prev[entity_id] = (ts, lat, lon)
        return True


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS points(
                    entity_id TEXT NOT NULL,
                    ts REAL NOT NULL,
                    lat REAL NOT NULL,
                    lon REAL NOT NULL,
                    acc REAL,
                    PRIMARY KEY (entity_id, ts)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS routes(
                    entity_id TEXT NOT NULL,
                    start_ts REAL NOT NULL,
                    end_ts REAL NOT NULL,
                    n INTEGER NOT NULL,
                    geometry TEXT,
                    PRIMARY KEY (entity_id, start_ts)
                ) WITHOUT ROWID;
                """
            )
        self.revision: dict[str, int] = {}
        self._cache: dict[tuple, tuple[float, int, list]] = {}

    # --- writes -----------------------------------------------------------------

    def add(self, rows: list[tuple[str, float, float, float, float | None]]) -> int:
        if not rows:
            return 0
        with self.lock:
            before = self.db.total_changes
            self.db.executemany("INSERT OR IGNORE INTO points VALUES (?, ?, ?, ?, ?)", rows)
            self.db.commit()
            added = self.db.total_changes - before
        if added:
            for eid in {r[0] for r in rows}:
                self.revision[eid] = self.revision.get(eid, 0) + 1
        return added

    def purge_before(self, ts: float) -> int:
        with self.lock:
            cur = self.db.execute("DELETE FROM points WHERE ts < ?", (ts,))
            self.db.execute("DELETE FROM routes WHERE start_ts < ?", (ts,))
            self.db.commit()
        if cur.rowcount:
            self.revision = {k: v + 1 for k, v in self.revision.items()}
        return cur.rowcount

    def delete_entity(self, entity_id: str) -> int:
        with self.lock:
            cur = self.db.execute("DELETE FROM points WHERE entity_id = ?", (entity_id,))
            self.db.execute("DELETE FROM routes WHERE entity_id IN (?, ?)", (entity_id, f"{entity_id}|jump"))
            # Keep the "imported" marker and mark the deletion, so a restart doesn't pull the
            # deleted history back in from the recorder.
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"imported:{entity_id}", str(time.time())))
            self.db.commit()
        self.revision[entity_id] = self.revision.get(entity_id, 0) + 1
        return cur.rowcount

    def get_meta(self, key: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
            self.db.commit()

    # --- reads ------------------------------------------------------------------

    def last_point(self, entity_id: str) -> tuple[float, float, float] | None:
        with self.lock:
            return self.db.execute(
                "SELECT ts, lat, lon FROM points WHERE entity_id = ? ORDER BY ts DESC LIMIT 1", (entity_id,)
            ).fetchone()

    def summary(self) -> dict[str, dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT entity_id, COUNT(*), MIN(ts), MAX(ts) FROM points GROUP BY entity_id"
            ).fetchall()
        return {r[0]: {"points": r[1], "first": r[2], "last": r[3]} for r in rows}

    # --- trips (the lines between the dots) ------------------------------------

    def trips(self, entity_id: str, start: float, end: float, max_trips: int = 300) -> tuple[list[dict], bool]:
        """Split the points into trips: runs of movement between two stops.

        A trip ends when the person stays within STOP_RADIUS for STOP_TIME, or when the tracker
        is silent for longer than TRIP_GAP. Movement nobody could do by car or on foot (faster
        than AIR_SPEED over more than AIR_MIN) also ends a trip.

        Where the tracker was silent and the person turned up somewhere else, or moved too fast
        for the ground, a **jump** connects the two points (kind "jump", mode "air" or
        "ground"), so flights, ferries and phone-off stretches still show up as a line.

        Returns the most recent `max_trips` trips and jumps (oldest first) and whether older ones
        were left out.
        """
        TRIP_GAP, STOP_RADIUS, STOP_TIME, MIN_LENGTH = 30 * 60, 80.0, 5 * 60, 250.0
        JUMP_MIN = 1000.0          # a silent gap must cover at least this to get a line
        AIR_SPEED = 250 / 3.6      # m/s, straight-line speed above which it must have been a flight
        AIR_MIN = 30000.0          # and only over this distance (GPS glitches can look fast too)
        with self.lock:
            rows = self.db.execute(
                "SELECT ts, lat, lon, acc FROM points WHERE entity_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
                (entity_id, start, end),
            ).fetchall()
        rows = drop_spikes(rows)

        trips: list[dict] = []

        def close(points: list) -> None:
            if len(points) < 2:
                return
            length = sum(distance_m(a[1], a[2], b[1], b[2]) for a, b in zip(points, points[1:]))
            if length >= MIN_LENGTH:
                trips.append({"kind": "trip", "start": points[0][0], "end": points[-1][0],
                              "dist": round(length), "points": points})

        def jump(a, b, d: float, mode: str) -> None:
            last = trips[-1] if trips else None
            if (mode == "air" and last and last["kind"] == "jump" and last["mode"] == "air"
                    and last["end"] == a[0]):
                # Several fixes during one flight: one flight line.
                last["points"].append(b)
                last["end"] = b[0]
                last["dist"] = round(last["dist"] + d)
                return
            trips.append({"kind": "jump", "mode": mode, "start": a[0], "end": b[0],
                          "dist": round(d), "points": [a, b]})

        cur: list = []
        still = 0  # index in cur of the point the person may be standing still at
        for p in rows:
            if cur:
                prev = cur[-1]
                dt = p[0] - prev[0]
                d = distance_m(prev[1], prev[2], p[1], p[2])
                flying = d > AIR_MIN and d / max(dt, 1.0) > AIR_SPEED
                if flying or dt > TRIP_GAP:
                    close(cur)
                    if flying or d >= JUMP_MIN:
                        jump(prev, p, d, "air" if flying else "ground")
                    cur, still = [], 0
            cur.append(p)
            if len(cur) == 1:
                continue
            anchor = cur[still]
            if distance_m(anchor[1], anchor[2], p[1], p[2]) <= STOP_RADIUS:
                if p[0] - anchor[0] >= STOP_TIME:
                    # Stopped: the trip ended where they started standing still.
                    close(cur[: still + 1])
                    cur, still = [p], 0
            else:
                still = len(cur) - 1
        close(cur)

        truncated = len(trips) > max_trips
        return trips[-max_trips:], truncated

    def route_geometry(self, entity_id: str, trip: dict) -> tuple[bool, list | None]:
        """(known, geometry) for a trip that was snapped to roads before. known=False: not tried yet."""
        with self.lock:
            row = self.db.execute(
                "SELECT end_ts, n, geometry FROM routes WHERE entity_id = ? AND start_ts = ?",
                (route_key(entity_id, trip), trip["start"]),
            ).fetchone()
        if not row or row[0] != trip["end"] or row[1] != len(trip["points"]):
            return False, None  # never matched, or the trip grew since (it was still going on)
        return True, (json.loads(row[2]) if row[2] else None)

    def clear_routes(self) -> None:
        with self.lock:
            self.db.execute("DELETE FROM routes")
            self.db.commit()

    def save_route(self, entity_id: str, trip: dict, geometry: list | None) -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO routes VALUES (?, ?, ?, ?, ?)",
                (route_key(entity_id, trip), trip["start"], trip["end"], len(trip["points"]),
                 json.dumps(geometry, separators=(",", ":")) if geometry else None),
            )
            self.db.commit()

    def cells(self, entity_id: str, start: float, end: float | None, cell_m: float,
              min_away_s: float, min_stay_s: float) -> list[dict]:
        """Group a person's points into grid cells and count separate visits to each cell.

        A visit starts when the person enters a cell they haven't been in for `min_away_s`
        (so GPS jitter along a cell edge doesn't count as coming back). Time spent is the time
        until the next point, capped at MAX_GAP. With `min_stay_s`, visits shorter than that
        (driving through) are not counted, and cells left without visits are hidden.
        """
        key = (entity_id, start, end, cell_m, min_away_s, min_stay_s)
        rev = self.revision.get(entity_id, 0)
        hit = self._cache.get(key)
        if hit and hit[1] == rev and (end is not None or time.time() - hit[0] < 60):
            return hit[2]

        with self.lock:
            rows = self.db.execute(
                "SELECT ts, lat, lon FROM points WHERE entity_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
                (entity_id, start, end if end is not None else 1e12),
            ).fetchall()
        rows = drop_spikes(rows)

        dlat = cell_m / EARTH_M_PER_DEG
        # cell -> [sum_lat, sum_lon, n, dwell, [time per visit], first, last]
        cells: dict[tuple[int, int], list] = {}
        cur = None
        last_ts = 0.0
        for ts, lat, lon in rows:
            iy = math.floor(lat / dlat)
            dlon = cell_m / (EARTH_M_PER_DEG * max(0.01, math.cos(math.radians((iy + 0.5) * dlat))))
            k = (iy, math.floor(lon / dlon))
            if cur is not None:
                gap = min(ts - last_ts, MAX_GAP)
                c = cells[cur]
                c[3] += gap
                c[4][-1] += gap
                c[6] = max(c[6], last_ts + gap)
            c = cells.get(k)
            if c is None:
                c = cells[k] = [0.0, 0.0, 0, 0.0, [], ts, ts]
            if k != cur:
                if not c[4] or ts - c[6] > min_away_s:
                    c[4].append(0.0)
                cur = k
            c[0] += lat
            c[1] += lon
            c[2] += 1
            c[6] = max(c[6], ts)
            last_ts = ts

        # The person is (probably) still at their last known spot: count the time up to now.
        now = time.time()
        if cur is not None and (end is None or end > now):
            gap = min(now - last_ts, MAX_GAP)
            c = cells[cur]
            c[3] += gap
            c[4][-1] += gap
            c[6] = max(c[6], last_ts + gap)

        out = []
        for c in cells.values():
            stays = [v for v in c[4] if v >= min_stay_s]
            if not stays:
                continue
            out.append({
                "lat": round(c[0] / c[2], 6),
                "lon": round(c[1] / c[2], 6),
                "visits": len(stays),
                "dwell": int(c[3]),
                "longest": int(max(stays)),  # the longest single stay, for "stayed over an hour"
                "first": int(c[5]),
                "last": int(c[6]),
            })
        out.sort(key=lambda x: x["visits"])  # most visited drawn last (on top)

        if len(self._cache) > 64:
            self._cache.clear()
        self._cache[key] = (time.time(), rev, out)
        return out
