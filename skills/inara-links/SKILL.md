---
name: "inara-links"
description: "Build Inara.cz search links for Elite Dangerous (material traders now; commodities and outfitting later) whenever telling CMDR PAJAMA SUIT to go find or buy something."
---

# Inara links

Whenever you tell the user to go somewhere or find something in Elite Dangerous, include a ready-to-click Inara link for it.

## Rules

- If you don't know where the user is searching from, omit `ps1`. Inara then searches from the commander's last known location (same behavior as Inara's own nav-bar links). Add `ps1=<System Name>` only when the user names a system.
- The Elite Dangerous journal MCP now tracks fleet carrier jumps correctly, so its current system can be used for `ps1`. If its `location_timestamp` is more than a few days old, confirm with the user or omit `ps1`.
- For "where can I buy or sell a commodity", prefer the MCP's `find_commodity_market` tool, which returns live results. Add an Inara link as a second opinion, not as the answer.
- Inara fetches through Claude return cached data. Give the user the link to open; don't try to read live results yourself.
- Write material-trader trades as what you GET ← what you HAND OVER, matching the in-game trader screen.

## Material traders (confirmed working)

Base: `https://inara.cz/elite/nearest-stations/?pi17=1&pi18=3&pi19=5000&pa1%5B%5D=<code>&formbrief=1`

| Trader | `pa1[]` code |
|---|---|
| Any material trader | `25` |
| Raw | `25-10` |
| Manufactured | `25-11` |
| Encoded | `25-12` |

Examples:
- Nearest raw trader: https://inara.cz/elite/nearest-stations/?pi17=1&pi18=3&pi19=5000&pa1%5B%5D=25-10&formbrief=1
- Encoded traders near Mizar: https://inara.cz/elite/nearest-stations/?formbrief=1&ps1=Mizar&pi17=1&pi18=3&pi19=5000&pa1%5B%5D=25-12

Other station service codes seen on the nearest-stations form: `4` commodity market, `18` interstellar factors, `26` technology broker, `32` fleet carrier outfitting, `9` vendors.

Undecoded but kept from the nav-bar link: `pi17=1`, `pi18=3` (probably pad size), `pi19=5000` (probably max distance from arrival, Ls).

## Commodity buy search (to be decoded)

Example the user supplied:
`https://inara.cz/elite/commodities/?formbrief=1&pi1=1&pa1%5B%5D=10269&ps1=Hollatja&pi10=2&pi11=5000&pi3=2&pi9=0&pi4=0&pi8=0&pi13=1&pi5=48&pi12=0&pi7=0&pi14=0&ps3=`

Known so far: `ps1` = reference system, `pa1[]` = commodity id. To finish: map commodity ids, buy vs sell (`pi1`?), pad size, max Ls (`pi11`?), price age (`pi5`?), minimum supply. Decode by having the user change one form field at a time and paste the resulting links.

## Outfitting search

Not started. Get example links from the user the same way.