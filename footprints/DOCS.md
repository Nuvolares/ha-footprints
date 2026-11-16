# Footprints

Footprints records where the people in your home have been and shows it on a
map in the side panel. Every place gets a dot in that person's color. A place
visited once is barely visible; the more often someone goes back, the more
solid the dot becomes. Home, work and the gym stand out, and one-off trips stay
faint.

## How it works

- The app listens to Home Assistant for location updates of every `person`
  entity (or the entities you list in the options) and stores each position in
  its own database in `/data`. That database is included in Home Assistant
  backups.
- When it starts for the first time, it also imports everything the Home
  Assistant recorder still has. The recorder keeps 10 days by default, so the
  map starts with up to 10 days of history and grows from there. When the app
  was stopped for a while, it fills the gap from the recorder on the next
  start.
- Locations are grouped into a grid (50 m by default). A **visit** is counted
  each time a person arrives at a spot they haven't been at for 30 minutes, so
  GPS jitter doesn't count as coming back.

## Using the map

- **People**: show or hide each person. The round picture is where they are
  now.
- **Time range**: last 7 days, 30 days, 90 days, year, or all time.
- **Shade places by**: *Visits* (how often they went there) or *Time spent*
  (how long they were there in total).
- **Solid after**: how many visits (or hours) it takes for a dot to become
  fully solid. Lower it to make regular places stand out sooner.
- **Count a visit after**: set it to 5 minutes or more to hide streets they
  only drove through and show only places where they stopped.
- **Spot size**: how close two positions must be to count as the same place.
  Use a larger size when looking at a whole country.
- **Most visited**: the top places; select one to zoom in.
- Select a dot to see the number of visits, the total time spent there, and
  the first and last time.

## Options

| Option | Default | What it does |
|---|---|---|
| `entities` | empty | Entity IDs to record (`person.*` or `device_tracker.*`). Empty means every person. |
| `min_distance_m` | 20 | Skip updates closer than this to the last stored point (unless 15 minutes have passed). |
| `max_accuracy_m` | 200 | Ignore GPS fixes less accurate than this. |
| `import_days` | 3650 | How far back to import from the recorder. |
| `keep_days` | 0 | Delete points older than this. 0 keeps everything. |
| `cell_size_m` | 50 | Default spot size on the map. |
| `map_style` | auto | `auto` (follows light/dark mode), `light` or `dark`. The map is OpenStreetMap; dark mode darkens it. `osm` is the same as `light`. |
| `tile_url` | — | Optional custom tile server, for example `https://tile.example.com/{z}/{x}/{y}.png`. |
| `log_level` | info | How much detail the app writes to its log. |

## Tips

- Location only gets recorded when Home Assistant knows it. The Home Assistant
  Companion app on each phone is the usual source. Router or Bluetooth based
  trackers only report home/away, not coordinates.
- To keep more history from before you installed the app, raise
  `purge_keep_days` of the recorder. Footprints imports whatever is there on
  first start.
- Changing `entities` later imports the history of newly added entities on the
  next start.

## Privacy

Location history is personal. Everything stays on your Home Assistant: the
points are stored in the app's `/data` folder and are only reachable through
Home Assistant's own login. The map tiles are loaded from OpenStreetMap (or
your own tile server); the tile server sees which map area is being viewed,
but never receives the recorded locations.

To delete one person's recorded history, hover over their name in the People
list and select the trash icon. Uninstalling the app removes all of it. If the
app is still running, it keeps recording new locations for that person; remove
them from `entities` (or remove the person) to stop that.
