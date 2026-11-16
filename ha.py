"""Home Assistant connection: records live location updates and imports recorder history."""
import asyncio
import itertools
import logging
import os
import time
from datetime import datetime, timezone

import aiohttp

from store import Store, Thinner

log = logging.getLogger("footprints.ha")

WS_URL = "ws://supervisor/core/websocket"
HISTORY_WINDOW = 7 * 86400
EMPTY_WINDOWS_TO_STOP = 4


def parse_ts(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class HomeAssistant:
    def __init__(self, session: aiohttp.ClientSession, store: Store, opts: dict):
        self.session = session
        self.store = store
        self.opts = opts
        self.explicit = [e.strip() for e in opts.get("entities") or [] if e and e.strip()]
        self.max_accuracy = float(opts.get("max_accuracy_m", 200))
        self.live = Thinner(float(opts.get("min_distance_m", 20)), 15 * 60)
        self.states: dict[str, dict] = {}
        self.connected = False
        self.importing: str | None = None
        self.last_error: str | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    # --- which entities ---------------------------------------------------------

    def tracked(self, entity_id: str) -> bool:
        if self.explicit:
            return entity_id in self.explicit
        return entity_id.startswith("person.")

    def tracked_entities(self) -> list[str]:
        if self.explicit:
            return list(self.explicit)
        return sorted(e for e in self.states if e.startswith("person."))

    def point_from_state(self, state: dict) -> tuple[float, float, float, float | None] | None:
        attrs = state.get("attributes") or state.get("a") or {}
        lat, lon = attrs.get("latitude"), attrs.get("longitude")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return None
        if lat == 0 and lon == 0:
            return None
        acc = attrs.get("gps_accuracy")
        acc = float(acc) if isinstance(acc, (int, float)) else None
        if acc is not None and acc > self.max_accuracy:
            return None
        ts = parse_ts(state.get("last_updated") or state.get("lu") or state.get("lc"))
        if ts is None:
            return None
        return ts, float(lat), float(lon), acc

    # --- websocket plumbing -----------------------------------------------------

    async def call(self, msg: dict, timeout: float = 120) -> object:
        msg_id = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await self._ws.send_json({"id": msg_id, **msg})
            result = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(msg_id, None)
        return result

    async def run(self) -> None:
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            log.error("SUPERVISOR_TOKEN is not set; cannot connect to Home Assistant")
            return
        delay = 5
        while True:
            try:
                await self._session(token)
                delay = 5
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - keep reconnecting whatever happens
                self.last_error = str(err) or err.__class__.__name__
                log.warning("Home Assistant connection lost (%s); retrying in %ss", self.last_error, delay)
            self.connected = False
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("disconnected"))
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)

    async def _session(self, token: str) -> None:
        async with self.session.ws_connect(WS_URL, max_msg_size=0, heartbeat=55) as ws:
            self._ws = ws
            hello = await ws.receive_json()
            if hello.get("type") != "auth_required":
                raise ConnectionError(f"unexpected greeting {hello.get('type')}")
            await ws.send_json({"type": "auth", "access_token": token})
            auth = await ws.receive_json()
            if auth.get("type") != "auth_ok":
                raise ConnectionError(f"authentication failed: {auth.get('message', auth.get('type'))}")

            reader = asyncio.create_task(self._read(ws))
            try:
                states = await self.call({"type": "get_states"})
                self.states = {s["entity_id"]: s for s in states}
                await self.call({"type": "subscribe_events", "event_type": "state_changed"})
                self.connected = True
                self.last_error = None
                log.info("Connected to Home Assistant, tracking %s", ", ".join(self.tracked_entities()) or "nobody yet")
                for eid in self.tracked_entities():
                    if eid in self.states:
                        self.record(eid, self.states[eid])
                await self.import_history()
                await reader
            finally:
                reader.cancel()
                self._ws = None

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                        break
                    continue
                data = msg.json()
                for item in data if isinstance(data, list) else [data]:
                    self._dispatch(item)
        finally:
            # Wake up anything (such as a history import) still waiting for an answer.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("websocket closed"))
        raise ConnectionError("websocket closed")

    def _dispatch(self, item: dict) -> None:
        if item.get("type") == "result":
            fut = self._pending.get(item.get("id"))
            if fut and not fut.done():
                if item.get("success"):
                    fut.set_result(item.get("result"))
                else:
                    fut.set_exception(RuntimeError((item.get("error") or {}).get("message", "request failed")))
        elif item.get("type") == "event":
            data = (item.get("event") or {}).get("data") or {}
            eid = data.get("entity_id", "")
            new = data.get("new_state")
            if new is None:
                self.states.pop(eid, None)
                return
            self.states[eid] = new
            if self.tracked(eid):
                self.record(eid, new)

    # --- recording --------------------------------------------------------------

    def record(self, entity_id: str, state: dict) -> None:
        point = self.point_from_state(state)
        if not point:
            return
        if entity_id not in self.live.prev:
            self.live.seed(entity_id, self.store.last_point(entity_id))
        ts, lat, lon, acc = point
        if self.live.keep(entity_id, ts, lat, lon):
            self.store.add([(entity_id, ts, lat, lon, acc)])

    async def import_history(self) -> None:
        """Copy what the recorder still remembers. The first time this walks back until the
        recorder runs out of data; afterwards it only fills the gap since the last stored point."""
        limit_days = int(self.opts.get("import_days", 3650))
        for eid in self.tracked_entities():
            self.importing = eid
            try:
                done = self.store.get_meta(f"imported:{eid}")
                now = time.time()
                floor = now - limit_days * 86400
                if done:
                    last = self.store.last_point(eid)
                    # Never go back before the marker: it's also set when history was deleted.
                    since = max(floor, float(done) - 3600, (last[0] if last else 0) - 3600)
                    added = 0
                    t = since
                    while t < now:
                        added += await self._import_window(eid, t, min(t + HISTORY_WINDOW, now))
                        t += HISTORY_WINDOW
                else:
                    added, empty, t = 0, 0, now
                    while t > floor and empty < EMPTY_WINDOWS_TO_STOP:
                        got = await self._import_window(eid, max(t - HISTORY_WINDOW, floor), t, count_raw=True)
                        empty = 0 if got >= 0 else empty + 1
                        added += max(got, 0)
                        t -= HISTORY_WINDOW
                    self.store.set_meta(f"imported:{eid}", str(now))
                if added:
                    log.info("Imported %d history points for %s", added, eid)
            except (RuntimeError, asyncio.TimeoutError) as err:
                log.warning("History import for %s failed: %s", eid, err)
            finally:
                self.importing = None

    async def _import_window(self, eid: str, start: float, end: float, count_raw: bool = False) -> int:
        """Returns points added; with count_raw, -1 means the recorder had nothing at all."""
        result = await self.call({
            "type": "history/history_during_period",
            "start_time": iso(start),
            "end_time": iso(end),
            "entity_ids": [eid],
            "include_start_time_state": True,
            "significant_changes_only": False,
            "minimal_response": False,
            "no_attributes": False,
        }, timeout=300) or {}
        states = result.get(eid) or []
        if count_raw and not states:
            return -1
        thinner = Thinner(self.live.min_distance, self.live.heartbeat)
        rows = []
        for st in states:
            point = self.point_from_state(st)
            if point and thinner.keep(eid, point[0], point[1], point[2]):
                rows.append((eid, *point))
        return self.store.add(rows)

    # --- for the UI -------------------------------------------------------------

    def people(self) -> list[dict]:
        out = []
        for eid in self.tracked_entities():
            st = self.states.get(eid) or {}
            attrs = st.get("attributes") or {}
            out.append({
                "entity_id": eid,
                "name": attrs.get("friendly_name") or eid.split(".", 1)[-1].replace("_", " ").title(),
                "picture": attrs.get("entity_picture"),
                "state": st.get("state"),
                "lat": attrs.get("latitude"),
                "lon": attrs.get("longitude"),
            })
        return out
