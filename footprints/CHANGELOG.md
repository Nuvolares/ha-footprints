# Changelog

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
