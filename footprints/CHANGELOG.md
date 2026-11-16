# Changelog

## 0.4.0

- **Settings** on the map (the gear at the top): pick who is recorded and who
  can open Footprints from lists of Home Assistant's people, trackers and
  users, instead of typing names into the options.
- Dead ends are left out: points the person went out to and came straight
  back from without stopping no longer show up as dots or line spurs.
- Undid 0.3.1: routes show again while **Count a visit after** is set.

## 0.3.1

- Routes are hidden while **Count a visit after** is set, so the places only
  passed through don't show up as lines. They come back with *any stop*.

## 0.3.0

- Routes and **Follow the streets** are now on by default (also after
  updating; switch them off on the map if you don't want them).
- **Flights**: when someone shows up far away faster than any car could
  manage (New York at 9:00, Atlanta at 11:00), the two places are connected
  with a dotted arc.
- **Boats and ferries**: points out on the water, or stretches where no road
  leads, are connected with a straight dashed line instead of being pulled to
  the nearest street.
- When the phone was silent and the person turned up somewhere else, the gap
  is now drawn too: along the likely street route, or straight if there is
  none.
- A small key under Routes explains the kinds of lines. Routes are redone once
  after updating.

## 0.2.2

- New **Follow the streets** switch under Routes on the map, so routes can be
  drawn along the roads without going to the app options or restarting.

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
