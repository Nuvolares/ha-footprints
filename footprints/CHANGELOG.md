# Changelog

## 0.2.1

- Routes along the streets now work like a navigation system: the route is
  planned from each recorded point to the next, so lines no longer cut across
  the map when the phone reports only every minute or so. GPS wobble and
  misplaced points no longer cause detours. Routes snapped by 0.2.0 are
  redone automatically.
- Only coordinates are sent to the routing server now (no times).

## 0.2.0

- Routes: show the trips between places as lines with arrows for the
  direction of travel. Optionally snapped to the streets (`snap_to_roads`).
- Map types: Home Assistant map, OpenStreetMap, satellite and topographic.
- Look at a single day and step through the days.
- New `users` option: choose which Home Assistant users can open Footprints,
  and whose history each of them can see.
- App icon and logo.

## 0.1.3

- Use Home Assistant's own map (the same one as its Map dashboard) instead of
  loading tiles from OpenStreetMap directly, which blocked the requests.

## 0.1.2

- Fix the map showing "API Key Required": switch from CARTO (which now needs an
  API key) to OpenStreetMap tiles. Dark mode shows a darkened map.

## 0.1.1

- Repository clean-up and build check; no changes to the app itself.

## 0.1.0

- First version: records location history of people and trackers, imports the
  recorder history, and shows a map where often visited places are more solid.
