---
name: elite-dangerous-companion
description: Answer Elite Dangerous questions using the local elite-dangerous MCP server - where the commander is, where to buy or sell a commodity, where to mine, where to find exobiology or raw materials, what happened this session, and what to do next. Use this whenever the user mentions Elite Dangerous, their commander, ship, fleet carrier, a star system or station, credits, engineering materials, mining, exobiology, trading or hauling, or asks "where can I...", "what's near me", or "what should I do next" in a game context, even if they never name the game or the server. Also use it before charting or reporting anything from their journal data. It says which server tools to trust, which to avoid, how to shape the answer, and when a table, chart or artifact is worth making.
---

# Elite Dangerous companion

The commander is usually mid-flight, glancing at a second screen. They want one clear recommendation they can act on, with enough detail to trust it. This skill is how to get there with the `elite-dangerous` MCP server, whose tools are named `mcp__elite-dangerous__*`.

That server is a fork of someone else's project. Parts of it were rebuilt and tested against the commander's real journals. Much of the rest was never checked, and several tools have been caught returning wrong numbers. So the first skill here is knowing which tool to believe.

Read `references/tools.md` when you need the full tool list, parameters, or the trust status of a tool not covered below.

## Pick the tool by the question

| The commander asks | Use | Notes |
|---|---|---|
| Where am I? Am I docked? | `get_current_location` | Trusted. Handles fleet carrier jumps. |
| What materials do I have? Can I afford this roll? | `get_material_inventory` | Trusted. Login snapshot plus changes since. |
| What am I flying? Can this ship mine, scoop, land? | `get_ship_status` | Trusted. Pad size, jump range, modules, capability flags. |
| Where can I buy X? | `find_commodity_market`, mode `buy` | Carriers and month-old data are excluded by default. |
| Where do I sell X for the most? | `find_commodity_market`, mode `sell`, `sort_by` `price` | Set `min_quantity` to their cargo size so demand can absorb the load. |
| Where can I mine X? | `find_mining_hotspots` | Says whether the commodity is laser or core mined. |
| Where is exobiology worth doing? | `find_exobiology_targets` | Returns known species and their scan values. |
| Where do I farm raw material X? | `find_material_bodies` | Takes several materials, comma separated, to find one body with all of them. |
| What should I haul? Best trade loop from here? | `plan_trade_route` | Trade Dangerous, local database. Reads credits, hold, range and pad from the journal. State `database_age_days`. Slow: up to two minutes. |
| How's my stack? What WMMs do I have? When does it expire? | `get_wmm_stack` | Wing mining missions. Report every flagged mission and the earliest expiry. |
| What's my rep in Mbutas or Paemara? Who am I not Allied with? | `get_faction_reputation` | State `age_days`; the value only updates on a visit. |
| How do I get to a far-off system? | `plot_neutron_route` | Reads the ship from the journal and models fuel per jump. Slow: up to two minutes. Summarise long routes. |
| What did I just do? | `get_recent_events` or `search_events` | Raw events. Trusted as a record of what happened. |
| How did the session go? Earnings? | A summary tool, then verify | See "Unverified numbers" below. |
| What should I do next? | Combine the above | See "Giving advice". |

The four `find_*` tools search outward from the commander's current system. Leave `reference_system` empty unless they name a different system. Every result echoes `reference_system` and `reference_source`; read them back to the commander, because a search centred on the wrong system is a wrong answer that looks right.

### Check the starting point before you search

Everything "nearby" depends on where the server thinks the commander is, and that has been wrong more than once. Before the first search in a conversation, call `get_current_location` and look at `location_timestamp`. The commander stays wherever their last session ended, so an old timestamp is normal after a break. What matters is whether it matches the last session:

- If you can read files, compare it with the newest journal in the folder named in `references/tools.md`. If the journal is newer and names a different system, the server is stale: use the journal's system as `reference_system`, and tell the commander the server is out of date.
- If you cannot read files and the timestamp is more than a few days old, say which system and date you are working from and ask them to confirm before they act on the result.

Flagging the date is not enough on its own. A caveat under a table of stations in the wrong part of the galaxy still leaves the commander with nothing usable.

## Trust

**Trusted** - built or fixed and tested against real journals: `get_current_location`, `get_ship_status`, `get_material_inventory`, the four `find_*` tools, `server_status`, `get_recent_events`, `search_events`.

`get_ship_status` gives the ship, its landing pad size, jump range, cargo and fuel capacity, rebuy, every module with its engineering, and capability flags such as `can_laser_mine`, `fuel_scoop` and `srv_bay`. Call it before advice that depends on the ship: pad size for a market, mining gear for a mining trip, an SRV for surface prospecting.

`get_material_inventory` gives engineering materials as of the last login, plus every pickup, trade and spend since, with `snapshot_timestamp` to show how fresh it is. Keys are the journal's lower-case ids (`hybridcapacitors`); `material_names` maps them to display names. A material that is absent means zero held. If `snapshot_timestamp` is null the inventory is unknown, not empty.

**Unverified** - upstream code nobody has checked: the activity summaries (`get_trading_summary`, `get_exploration_summary`, `get_mining_summary`, `get_combat_summary`, `get_mission_summary`, `get_engineering_summary`, `get_journey_summary`, `get_performance_metrics`, `get_faction_standings`).

**Limited** - usable, with a known gap:
- `search_historical_events` returns some event types with empty contents, including the ones that hold wealth and inventory, so it cannot supply figures such as net worth over time. It works for trade events.

**Sanity check for a stale server.** The inventory and ship tools were once caught reporting a weeks-old ship with a 342 credit rebuy. If `loadout_timestamp` or `snapshot_timestamp` is far older than the commander's last session, or `get_ship_status` returns no modules, the server is running old code and needs a restart. Say so instead of using the figures.

**Not for answering questions** - the EDCoPilot chatter and theme tools, and every `generate_*_prompt` tool. The prompt generators only return a canned prompt; they hold no data. Use the EDCoPilot tools only when the commander asks for EDCoPilot chatter by name, and preview before generating, since generating overwrites files.

### Unverified numbers

When a summary tool gives you a figure the commander will act on or quote - credits earned, profit per hour, tons mined - check it before stating it. Pull the underlying events with `search_events` and see whether they support the figure. If you can read files (Claude Code), the journals themselves are the ground truth: `%USERPROFILE%\Saved Games\Frontier Developments\Elite Dangerous\Journal.*.log`, one JSON event per line.

If you cannot verify a figure, say so in the same sentence: "The server reports 412M earned, which I couldn't confirm." An unverified number stated plainly has already cost this commander one wrong answer.

Stored ships are not covered: `get_ship_status` describes only the ship the commander is in. If the advice depends on a ship parked elsewhere, ask what it is fitted with.

## Shape of an answer

Lead with the pick. Then the reason in a clause. Then a short table of alternatives.

> **Old Sharlayan in Thuleppa**, 7.9 ly. Large pad, 32,000 in stock, data under three days old.
>
> | Distance | Station | System | Stock | Price | Data age |
> |---|---|---|---|---|---|
> | 10.2 ly | Noriega Port | Arare | 11,238 | 51,077 | 0.1 d |
> | 10.9 ly | Stephan Depot | Owaha | 304,493 | 57,642 | 0.3 d |
>
> Searched from Hollatja, your current system.

Why this shape: they can act on the first line alone, the table lets them overrule you, and the last line exposes a wrong starting point.

Keep these in every answer where they apply:
- **Data age** for anything from a market. Prices are whatever a player last uploaded.
- **Distance from the arrival star** in light seconds. A station 200,000 ls out is a twenty minute supercruise, which often outweighs a better price.
- **Pad size.** Take `landing_pad` from `get_ship_status` and set `large_pad_only` when it is `large`. If they name a different ship than the one they are in, go by what they say.
- **Gravity** for anything they will land on. Above about 1 g, say so. Above 2 g, warn.

Round for reading: one decimal for light years, whole numbers for light seconds, millions and billions for credits (`18.4M`, `2.1B`).

## Giving advice

The tools answer one question each. The commander's real question usually spans two. Put them together:

- **One trip, two jobs.** If they are heading somewhere for exobiology, check the same systems for a mining ring or a needed material. A system that serves two goals beats two that each serve one slightly better.
- **Several materials.** Ask `find_material_bodies` for all of them at once first. One body with both, at lower percentages, usually beats two separate trips.
- **Selling a load.** Sort by price, but check demand covers the cargo and the station is not far out. The top price with demand for 1,000 units is no use to 700 tons in a hold when it is 60 ly away.
- **Rich versus near.** Give both the nearest option and the best option when they differ much, and say what the extra distance buys.

If the commander has shared a plan, roadmap or shopping list in this conversation or project, read it and tie the advice to it. Do not invent goals for them, and do not carry a goal over from memory without checking it still holds.

Name the catch. If the nearest ring is depleted, the best planet is 2.4 g, or the data is three weeks old, that belongs in the answer, not left for them to discover in flight.

## Tables, charts and artifacts

Most answers are a table in chat. Reach for more only when it helps:

- **Table in chat** - the default. Up to about eight rows. A ranked list of places is a table, not a chart.
- **Chart or widget** - for a trend over time, where shape is the point: net worth, earnings per session, jumps per day. Make one without being asked when they ask how something changed over time. Annotate turning points with what caused them; an emoji per annotation suits this commander. Use the session's chart or widget tool and follow its design guidance.
- **Artifact or saved page** - for something they will keep or come back to: a multi-stop route plan, a session report, an engineering shopping list. Offer it in one line; build it when they say yes or when they ask for a plan or report outright.

For anything else, offer the visual in one line at the end and let them choose. A chart nobody asked for costs them time while they are flying.

Charts need trustworthy numbers. Trends over long periods have to come from the journal files, because the historical search tool returns empty events. If you cannot read the files, say that a chart of that history is not available from this session, and why.

## When something looks wrong

- **Location is "Unknown" or plainly stale.** Ask where they are and pass it as `reference_system`. Mention it, since it means the server has a bug worth fixing.
- **"Spansh rejected the search."** The system name is not one Spansh knows. Check spelling, or use a nearby known system.
- **"Unknown commodity" with suggestions.** Retry with the closest suggestion and tell them which name you used.
- **No results.** Widen `max_distance_ly`, drop `pristine_only` or `large_pad_only`, or raise `max_data_age_days`, and say what you loosened.
- **Deep space.** Spansh only knows systems players have visited and uploaded. Far from inhabited space, an empty result means "nobody has reported this", not "nothing is here".
- **A tool disagrees with what the commander sees in game.** Believe the commander. Say the server is wrong and note which tool, so it can be fixed.
