"""Location history storage (SQLite) and the visit aggregation behind the map."""
import math
import sqlite3
import threading
import time
from pathlib import Path

EARTH_M_PER_DEG = 111_320.0
MAX_GAP = 12 * 3600  # a longer silence means the tracker was offline; don't count it as time spent


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Equirectangular approximation, accurate enough at these distances."""
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6_371_000.0 * math.hypot(x, y)


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
            self.db.commit()
        if cur.rowcount:
            self.revision = {k: v + 1 for k, v in self.revision.items()}
        return cur.rowcount

    def delete_entity(self, entity_id: str) -> int:
        with self.lock:
            cur = self.db.execute("DELETE FROM points WHERE entity_id = ?", (entity_id,))
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
                "first": int(c[5]),
                "last": int(c[6]),
            })
        out.sort(key=lambda x: x["visits"])  # most visited drawn last (on top)

        if len(self._cache) > 64:
            self._cache.clear()
        self._cache[key] = (time.time(), rev, out)
        return out
