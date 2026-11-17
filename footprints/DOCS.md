# Footprints

Footprints records where the people in your home have been and shows it on a
map in the side panel. Every place gets a dot in that person's color. A place
visited once is barely visible; the more often someone goes back, the more
solid the dot becomes. Home, work and the gym stand out, and one-off trips stay
faint. Routes between places can be shown as lines, with arrows for the
direction of travel.

## How it works

- The app listens to Home Assistant for location updates of every `person`
  entity (or the entities you list in the options) and stores each position in
  its own database in `/data`. That database is included in Home Assistant
  backups.
- Everything Footprints records is kept for good (unless you set
  `keep_days`), no matter how long the Home Assistant recorder keeps its own
  history. So the map keeps growing from the day you install it.
- When it starts for the first time, it also imports everything the Home
  Assistant recorder still has. The recorder keeps 10 days by default, so the
  map starts with up to 10 days of history and grows from there. When the app
  was stopped for a while, it fills the gap from the recorder on the next
  start.
- Locations are grouped into a grid (50 m by default). A **visit** is counted
  each time a person arrives at a spot they haven't been at for 30 minutes, so
  GPS jitter doesn't count as coming back.
- A **trip** is the movement between two stops. A stop is staying within 80 m
  for 5 minutes or more; a silence of more than 30 minutes also ends a trip.

## Using the map

- **People**: show or hide each person. The round picture is where they are
  now.
- **Time**: one **Day** (with arrows to step through the days), the last 7 or
  30 days, the last year, or all time.
- **Routes** (on by default): shows the trips as lines in the person's color,
  with small arrows pointing the way they went. Select a line to see when the
  trip started and ended, how long it was, and how far. With long time ranges,
  the latest 300 trips are shown. The kinds of lines:
  - **Solid**: along the streets (see *Routes along the streets*). A fainter
    solid line is a stretch without recorded points (the phone was silent),
    drawn along the likely route.
  - **Dashed**: a straight line. Used where no road leads (a ferry, a boat, a
    lake), and for trips that haven't been routed yet or when following the
    streets is off.
  - **Dotted arc**: a flight. When someone turns up more than 30 km away faster
    than 250 km/h in a straight line (New York at 9:00, Atlanta at 11:00), it
    can't have been by road, so the two places are connected over the globe.
- **Shade places by**: *Visits* (how often they went there) or *Time spent*
  (how long they were there in total).
- **Solid after**: how many visits (or hours) it takes for a dot to become
  fully solid. Lower it to make regular places stand out sooner.
- **Blink where they stayed longer than**: places where someone stayed
  longer than 1 hour (or 3 or 8 hours) in one go blink, so long stops are easy
  to spot. Select a place to see its longest stay. Choose *off* to stop the
  blinking. With *Reduce motion* turned on in your device settings, these places are
  shown solid instead of blinking.
- **Count a visit after**: set it to 5 minutes or more to hide streets they
  only drove through and show only places where they stopped.
- **Dead ends are left out**: a point the person went out to and came
  straight back from without stopping (a U-turn, or more often a GPS fix that
  landed off the road) doesn't get a dot, and the line doesn't go there.
  Points on the way somewhere else, and places where they stayed, are kept.- **Spot size**: how close two positions must be to count as the same place.
  Use a larger size when looking at a whole country.
- **Map**: the background map. *Home Assistant map* is the same one as the Map
  dashboard. *OpenStreetMap*, *Satellite* (Esri World Imagery) and
  *Topographic* (OpenTopoMap) come straight from those services. A *Custom*
  choice appears when you set `tile_url`.
- **Most visited**: the top places; select one to zoom in.

## Routes along the streets

Recorded points are often minutes apart, so a straight line between them cuts
through buildings. **Follow the streets** under Routes on the map is on by
default (`snap_to_roads` in the options; the switch on the map wins once it
has been used). Only users who can see everyone can change it. Footprints then works like a
navigation system: it asks an OSRM routing server for the street route from
each recorded point to the next, in order, picking car, bike or walking routes
from the trip's speed. Points closer together than about a street block are
skipped so GPS wobble doesn't cause detours, and a point that makes the route
loop out and back (a fix that landed on the wrong road) is left out. Each trip
is sent once and the result is stored, so the map stays fast. Points more than
300 m from any road or path (out on the water, for example) aren't pulled to
the nearest street; they are joined with straight lines. Flights are never
sent to the routing server.

When points are far apart, the line shows the most likely route between them,
so a short detour the person took in between can be missing.

This sends the trip's coordinates (no times, no names, no entity IDs) to the
routing server. The default is the free server run by FOSSGIS
(`routing.openstreetmap.de`), which allows light personal use. To keep
everything at home, run your own [OSRM](https://github.com/Project-OSRM/osrm-backend)
server and set `routing_url` to it, for example `http://192.168.1.20:5000`.

## Who can see it

Location history is sensitive, so access is limited in two ways:

1. Only Home Assistant **administrators** get the Footprints entry in the side
   panel; other users don't see it and can't open it.
2. Under **Settings** (the gear at the top of the panel), pick for each
   administrator: *Sees everyone*, *Sees only…* (tick the people), or *No
   access*. The list comes straight from Home Assistant's users, so there's
   nothing to type. Everyone set to *No access* gets a "No access" page. You
   can't take away your own access to everyone, so you can't lock yourself
   out. When every administrator is on *Sees everyone*, that also goes for
   administrators added later; otherwise new users start with no access.

The same Settings dialog picks **who is recorded**: every person (the
default, which also includes people added later), or only the people and GPS
device trackers you tick. History of someone newly ticked is imported from
the recorder right away.

Only users who can see everyone get the Settings gear. What is saved there
replaces the `entities` and `users` options. Those options still work for
setting things up without the map, by username:

```yaml
users:
  - username: daniel      # sees everyone
    people: []
  - username: alex        # sees only their own history
    people:
      - person.alex
```

Use the **username** (the login name, under Settings → People → Users), not
the display name: users can change their own display name, but not their
username. Only users who can see everyone can delete history.

Home Assistant doesn't offer a per-user setting for an app's side panel
entry, so an administrator who isn't listed still sees the Footprints entry,
but gets "No access" when opening it. They can hide the entry from their own
sidebar under their profile → *Change the order and hide items from the
sidebar*.

## Options

| Option | Default | What it does |
|---|---|---|
| `entities` | empty | Entity IDs to record (`person.*` or `device_tracker.*`). Empty means every person. Easier: Settings on the map, which wins once saved. |
| `users` | empty | Who can open Footprints and whom they see (see above). Empty means every administrator, seeing everyone. Easier: Settings on the map, which wins once saved. |
| `min_distance_m` | 20 | Skip updates closer than this to the last stored point (unless 15 minutes have passed). |
| `max_accuracy_m` | 200 | Ignore GPS fixes less accurate than this. |
| `import_days` | 3650 | How far back to import from the recorder. |
| `keep_days` | 0 | Delete points older than this. 0 keeps everything. |
| `cell_size_m` | 50 | Default spot size on the map. |
| `map_style` | auto | `auto` (follows light/dark mode), `light` or `dark`. Dark mode darkens the Home Assistant and OpenStreetMap maps. `osm` is the same as `light`. |
| `tile_url` | — | Optional custom tile server, for example `https://tile.example.com/{z}/{x}/{y}.png`. |
| `snap_to_roads` | true | Draw routes along streets using the routing server. |
| `routing_url` | FOSSGIS | OSRM server; `{profile}` becomes `car`, `bike` or `foot`. |
| `log_level` | info | How much detail the app writes to its log. |

## Tips

- Location only gets recorded when Home Assistant knows it. The Home Assistant
  Companion app on each phone is the usual source. Router or Bluetooth based
  trackers only report home/away, not coordinates.
- Routes look better with more frequent location updates. In the Companion
  app, the location sensor settings control how often the phone reports.
- To keep more history from before you installed the app, raise
  `purge_keep_days` of the recorder. Footprints imports whatever is there on
  first start.
- Changing `entities` later imports the history of newly added entities on the
  next start.

## Privacy

Everything stays on your Home Assistant: the points are stored in the app's
`/data` folder and are only reachable through Home Assistant's own login. The
map background is loaded from the map service you pick; it sees which area is
being viewed, but never receives the recorded locations. With **Follow the
streets** on (the default), trip coordinates are sent to the routing server;
switch it off on the map, or run your own routing server, to keep them at home.

To delete one person's recorded history, hover over their name in the People
list and select the trash icon. Uninstalling the app removes all of it. If the
app is still running, it keeps recording new locations for that person; remove
them from `entities` (or remove the person) to stop that.
