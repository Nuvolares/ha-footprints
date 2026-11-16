"""Made-up movement for previewing the map without Home Assistant (FP_DEMO=1)."""
import random
import time

from store import Store

PEOPLE = {
    "person.alex": {"name": "Alex", "lat": 0.0, "lon": 0.0},
    "person.sam": {"name": "Sam", "lat": 0.0, "lon": 0.0},
}

HOME = (52.5200, 13.4050)
PLACES = {
    "person.alex": [((52.5310, 13.3847), 0.55), ((52.5076, 13.3904), 0.15), ((52.4900, 13.4400), 0.1),
                    ((52.5450, 13.4200), 0.1), ((52.4700, 13.3300), 0.05), ((52.5600, 13.3000), 0.05)],
    "person.sam": [((52.5000, 13.4600), 0.5), ((52.5160, 13.3777), 0.2), ((52.5390, 13.4250), 0.15),
                   ((52.4820, 13.3550), 0.1), ((52.5800, 13.4800), 0.05)],
}


def _trip(rng, rows, eid, t, a, b, minutes):
    steps = max(4, int(minutes / 2))
    for i in range(1, steps + 1):
        f = i / steps
        lat = a[0] + (b[0] - a[0]) * f + rng.gauss(0, 0.0006)
        lon = a[1] + (b[1] - a[1]) * f + rng.gauss(0, 0.0009)
        rows.append((eid, t + i * 120, lat, lon, 15.0))
    return t + steps * 120


def seed(store: Store, days: int = 180) -> None:
    rng = random.Random(7)
    now = time.time()
    for eid, places in PLACES.items():
        if store.last_point(eid):
            continue
        rows = []
        day0 = now - days * 86400
        for d in range(days):
            t = day0 + d * 86400 + 7 * 3600 + rng.uniform(0, 3600)
            pos = HOME
            rows.append((eid, t - 6 * 3600, HOME[0], HOME[1], 10.0))
            for _ in range(rng.choice([1, 1, 2, 2, 3])):
                dest = rng.choices([p for p, _ in places], [w for _, w in places])[0]
                dest = (dest[0] + rng.gauss(0, 0.0002), dest[1] + rng.gauss(0, 0.0003))
                t = _trip(rng, rows, eid, t, pos, dest, rng.uniform(15, 40))
                stay = rng.uniform(0.5, 8) * 3600
                rows.append((eid, t + stay / 2, dest[0], dest[1], 10.0))
                t += stay
                pos = dest
            if rng.random() < 0.08:  # the occasional one-off outing
                far = (HOME[0] + rng.uniform(-0.12, 0.12), HOME[1] + rng.uniform(-0.18, 0.18))
                t = _trip(rng, rows, eid, t, pos, far, 45)
                t += 3600
                pos = far
            t = _trip(rng, rows, eid, t, pos, HOME, 25)
        store.add([r for r in rows if r[1] < now])
        PEOPLE[eid]["lat"], PEOPLE[eid]["lon"] = HOME


def people() -> list[dict]:
    return [{"entity_id": eid, "name": p["name"], "picture": None, "state": "home",
             "lat": HOME[0], "lon": HOME[1]} for eid, p in PEOPLE.items()]
