# Footprints for Home Assistant

A Home Assistant app (formerly called an add-on) that shows every place the
people in your home have been on a map in the side panel. Places visited once
are faint; places they keep coming back to become more and more solid.

![Footprints](footprints/logo.png)

## Install

1. In Home Assistant, go to **Settings → Apps → Install app**.
2. Open the menu in the top right, select **Repositories**, add
   `https://github.com/Nuvolares/ha-footprints` and select **Add**.
3. Install **Footprints**, start it and open it from the side panel.

## Test a local copy

1. Install the **Samba share** or **Terminal & SSH** app.
2. Copy the `footprints` folder into the `local_apps` folder (called `addons`
   on older installations).
3. In the app store, open the top-right menu and select **Check for updates**.
   The app appears under **Local apps**.
4. After changing files, select **Check for updates** again and then
   **Rebuild** on the app page.

## Preview without Home Assistant

With Python 3.11+ and aiohttp installed, this starts the map with made-up data
for two people:

```sh
cd footprints/app
mkdir -p static/vendor && curl -fsSL -o static/vendor/leaflet.js https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js \
  && curl -fsSL -o static/vendor/leaflet.css https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css
FP_DEMO=1 FP_DATA_DIR=../../.dev-data python3 main.py
```

Then open http://localhost:8099.

See [footprints/DOCS.md](footprints/DOCS.md) for usage and options.

## How it works

- A Python (aiohttp) service connects to the Home Assistant WebSocket API,
  records location changes into SQLite in `/data`, and on first start imports
  the recorder history.
- The page (Leaflet, served through Home Assistant Ingress) asks the service
  for each person's places grouped into a grid, with the number of visits and
  the time spent in each, and sets each dot's opacity from that.

## License

MIT, see [LICENSE](LICENSE).
