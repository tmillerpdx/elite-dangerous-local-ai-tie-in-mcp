# WMM tooling: research and recommendation (2026-10-01)

For CMDR PAJAMA SUIT. Answers the "WMM Tooling Brief".

## Recommendation

Use two things together; build nothing standalone.

1. **OD Elite Tracker** (https://github.com/WarmedxMints/ODEliteTracker) as the
   second-monitor stack display while playing. It is the maintained successor
   to the OD Trade Mission Tracker that PTN recommends.
2. **Two new tools in the local elite-dangerous MCP server**, `get_wmm_stack`
   and `get_faction_reputation`, so Claude can answer "how's my stack?" and
   "who am I not Allied with?" and apply the PTN rules the tracker does not
   know about. These are built (see "What was built").

No EDMC plugin covers this. A custom EDMC plugin or a standalone dashboard
would only be worth building if OD Elite Tracker turns out not to fit.

## Existing tools

| Tool | Stack view | Bad-mission flags | Reputation | Live | Status |
|---|---|---|---|---|---|
| OD Trade Mission Tracker | yes | mission type only | no | yes | Archived. Last release 2022-12-06. README says it is replaced by OD Elite Tracker. |
| OD Elite Tracker | yes (no count toward 20 seen) | mission type only | shown per faction, no "not Allied" filter | yes | Active: release 1.5.30 on 2026-09-29. Windows, needs .NET 9, single exe. No licence file, no plugin API. |
| EDDiscovery | mission list with items left | no | not confirmed | yes | Active, Apache-2.0. No per-commodity totals. |
| BGS-Tally (EDMC) | stores active missions | no | not confirmed | yes | Active, MIT. Aimed at BGS work, not delivery progress. |
| EDMC-Massacres (EDMC) | massacre stacks only | no | no | yes | Last release 2023-04-15. |
| Observatory Core | no plugin found | no | no | yes | Active, MIT, has a plugin framework. |
| EDEngineer | no | no | no | yes | Archived. |
| EDWM (web) | no | no | no | no | Manual wing-share queue, no journal reading. |
| EliteMissionRadar | no | reads the board by screen capture | no | no | A board filter, not a stack tracker. |

None of them flags Bromellite, Indium or a wrong station, shows a board-refresh
timer, or tracks a share session. The OD Elite Tracker findings come from its
source (models and stores), not from running it; its screens were not reviewed.

PTN's GitHub organisation has only Discord bots and libraries: no WMM tracker
and no public stock API.

## Why the MCP server returned nothing

- **`get_mission_summary` (720 h):** the server only keeps events from about
  the last 24 hours of journal files in memory, and the summary tools read
  that memory. A 720 hour range does not load older files. The journal path
  is right and mission events are parsed.
- **`get_faction_standings`:** it never reads minor-faction reputation. It
  looks for a list inside the `Reputation` event, which is the superpower
  event (Empire, Federation, Alliance, Independent) and has no such list, so
  the result is always empty. Minor-faction reputation is in `Factions[]` on
  `FSDJump`, `Location` and `CarrierJump`, which the tool ignores.
- **Stale location:** fixed earlier (carrier jumps and restarts).

The new tools avoid both problems by reading the journal files directly.

## Journal data

Confirmed against the journal manual (v37) and the EDCD-style schemas, and
where possible against the real journal folder.

- **Location of files:** as in the brief. Confirmed on this machine.
- **MissionAccepted:** `MissionID`, `Name`, `LocalisedName`, `Faction`,
  `Reward`, `Influence`, `Reputation` and `Wing` (a boolean, present on all
  125 real events checked). `Commodity`, `Count`, `Expiry`, `DestinationSystem`
  and `DestinationStation` are optional. It does not say which station the
  mission was taken at; that has to come from the preceding `Docked` event.
- **Missions:** written at game start with `Active`, `Failed` and `Complete`
  lists of `MissionID`, `Name`, `Expires` (seconds left). No commodity or
  count, so the stack has to be rebuilt from `MissionAccepted` history.
- **CargoDepot:** `MissionID`, `UpdateType` (`Collect`, `Deliver`,
  `WingUpdate`), `ItemsCollected`, `ItemsDelivered`, `TotalItemsToDeliver`,
  `Progress`. `Progress` is goods in transit, not completion; use
  `ItemsDelivered`. Written for wing missions, including when a wingmate
  delivers.
- **MissionCompleted / MissionAbandoned / MissionFailed:** carry `MissionID`.
- **Factions[]:** `Name`, `FactionState`, `Influence`, `MyReputation` and
  more, on `Location`, `FSDJump` and `CarrierJump`.

Not confirmed:

- **Wing mining versus source-and-return names.** The commander's journals
  contain no wing mining missions yet, so there is no real sample. OD's source
  classifies `Mission_Mining*`, `Mission_Delivery*` and `Mission_Collect*`
  (source and return). The new tool uses the `Wing` boolean for the wing
  check and "Collect" in the name for source and return.
- **Allied threshold.** `MyReputation` runs -100 to 100. "90 and above is
  Allied" is documented for superpower reputation only; applying it to minor
  factions is an inference.
- **Missions received from wingmates.** Not seen in real data. They should
  appear in the `Missions` snapshot and in `CargoDepot` `WingUpdate` events
  without a `MissionAccepted`; the tool lists such missions as
  `details_unknown`.

## Discord

There is no legitimate way for an ordinary member to read `#wmm-stock`
programmatically. Discord's guidelines forbid self-bots and its terms forbid
scraping without written consent. A bot, a followed channel or a webhook all
need PTN staff to set them up. PTN publishes the stock nowhere else that could
be found. Plan on pasting the channel content in, or ask PTN staff.

## What was built

In `src/utils/wmm.py`, exposed as MCP tools (details in
`skills/elite-dangerous-companion/references/tools.md`):

- **`get_wmm_stack`** covers the stack view, per-commodity totals, count
  toward 20, earliest expiry with a 48 hour alert, bad-mission flags, a
  hauling plan by cargo capacity, and the next board refresh time.
- **`get_faction_reputation`** covers reputation per faction for Mbutas and
  Paemara (or any systems), with the not-yet-Allied list.

Both read the journal on every call, so there is nothing extra to keep running.

Not built: a board-flip timer with sound, a share-session checklist and a
received-mission tracker. The first needs an always-on display, which is OD
Elite Tracker's job or a small separate page; the other two need real wing
mission data to design against.

## State of the commander's data today

- No active missions; no wing mining mission has ever been accepted in these
  journals.
- Paemara, last visited 2026-09-11: Dijkstra PLC is Friendly (68.8); every
  other faction is Cordial or Neutral (0 to 8). None is Allied.
- Mbutas: no visit in the last 180 days of journals.

## To use

Restart the MCP server in Claude Code and Claude Desktop/Cowork, then ask
"how's my stack?" or "what's my rep in Paemara?".
