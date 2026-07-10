# Card GOL Playtest Simulator Context

This file is a handoff note for continuing the Card GOL playtest/balancing simulator in another Codex session or another machine.

## Current Goal

The project now includes a local web simulator for playtesting the physical Card GOL rules. It is intended to answer balance questions such as:

- best pattern deck composition
- best number of initial live cells
- game duration by player count
- game duration by rescue target
- which pattern cards are actually rescued
- whether players behave constructively toward completing patterns in hand

The simulator is implemented in:

- `playtest_server.py`

The usage notes are also in:

- `README.md`

## How To Run

From the project root:

```powershell
cd C:\Users\guilh\Documents\golcardgame
python playtest_server.py --port 8766
```

Then open:

```text
http://127.0.0.1:8766
```

Use port `8766` instead of `8765` because the main app/service worker may cause `8765` to show the regular `index.html`.

Stop the server with `Ctrl+C` in the terminal running it.

## Important Source Files

- `card_gol_fisico.html`: physical rulebook. Main source for turn flow, rescues, action rules, scoring and game end.
- `cards.json`: pattern/action definitions, phase counts, mirror metadata, base pattern matrices.
- `cards_active.json`: active physical deck composition by pattern/action family.
- `index.html`: main helper app and PAT/pattern detection logic. The simulator should follow its pattern recognition semantics.
- `cards-system.html`: print/card generation logic and card visual style.
- `playtest_server.py`: simulator engine, web UI, API, balancing sweep.

## Modeled Physical Rules

The simulator currently models:

- Grid size: 20 x 14.
- 2, 3, or 4 players.
- Each player starts with 4 pattern cards.
- Initial live cells default to 12, configurable.
- Each active turn:
  - roll/select quadrant side abstractly
  - draw 2 action cards as a batch
  - choose 1 action to execute
  - discard unused action and then used action
  - active player rescues matching patterns
  - optional off-turn rescues
  - refill pattern hand up to 4 cards
- End condition default: first player reaching 5 rescued patterns triggers game end.
- Score: sum of live cells in rescued patterns.
- Rescued pattern cards stay in player scoring area and do not return to the pattern deck.

## Draw / Discard Semantics

The simulator was updated to use batch draw behavior:

- If the deck can satisfy the requested draw, draw only from that deck.
- If the deck cannot satisfy the requested draw, shuffle the discard pile back into the deck before drawing the batch.
- Cards drawn in the current batch are not available again until explicitly discarded later.
- Action cards drawn during a turn enter discard only after the selection/pass logic.
- Duplicate action cards are handled correctly. Example: drawing two `add-isolated` cards discards two physical copies by end of turn.

## Pattern Variant Semantics

This is important.

Pattern cards in the simulator are not generic family names like `glider`. They are physical/pattern-detection variants generated with the same conceptual transform logic as PAT in `index.html`:

- base position
- 180-degree position
- mirror
- 180-degree mirror
- every phase of the pattern period
- duplicate shapes removed

Canonical generated ids look like:

```text
glider_t1_p1
glider_t2_p3
glider_t3_p4_mirror
beacon_t2_p2_mirror
block_t1_p1
```

`cards_active.json` still controls physical quantity by family. Example: if `cards_active.json` says `glider: 8`, the game gets 8 physical Glider cards, selected from the full PAT-recognized Glider variant pool.

Relevant functions:

- `build_pattern_card_catalog`
- `build_pattern_deck`
- `cards_payload`
- `find_matches`

## Player AI

Default policy is `greedy`.

The current AI tries to be constructive:

- Prefer valid Add actions.
- Choose add cells that help complete patterns in the current player's hand.
- Use Remove/Clear/Swap mostly as fallback.
- Removal chooses cells that least support the player's current hand.

Relevant functions:

- `choose_action`
- `evaluate_action_choice`
- `choose_action_cell`
- `score_add_cell`
- `score_cell_support`
- `card_progress_score`

## Visual Replay

The simulator can record a timeline for the first N games.

UI fields:

- `Partidas visuais`
- `Replay`

Timeline records:

- grid state
- live cell changes
- action draw
- action used
- rescues
- player hands
- player rescued/scored cards
- deck/discard counts

Relevant functions:

- `snapshot_step`
- JS functions in the embedded `HTML` string:
  - `loadJobReplay`
  - `renderBoard`
  - `renderPlayers`
  - `renderEvent`
  - `renderMiniCard`

## Balancing Sweep / Combinatorics

The UI has `Combinatoria de balanceamento`.

It can sweep:

- player counts, e.g. `2,3,4`
- initial live cells, e.g. `8,12,16,20`
- rescue targets, e.g. `4,5,6`
- pattern deck modes:
  - current deck
  - without each pattern family
  - favor each pattern family
  - reduce high-count families
  - uniform deck
  - all families x1/x2/x4/x6/x8

Each scenario reports:

- scenario label
- players
- initial live cells
- rescue target
- exact pattern deck counts
- turn distribution
- rescued-card distribution

Relevant functions:

- `pattern_deck_variations`
- `expand_scenarios`
- `aggregate_scenarios`
- `Job.run`
- JS `renderScenarioTable`

## API Endpoints

- `GET /`: web UI
- `GET /api/default-config`: default config, including active decks
- `GET /api/cards`: cards data plus generated `patternCards`
- `GET /api/jobs`: list job snapshots
- `GET /api/jobs/<id>`: full job with results/timelines
- `GET /api/jobs/<id>/results.csv`: CSV export
- `POST /api/jobs`: start a simulation job

## Validation Commands

Syntax check without writing bytecode:

```powershell
python -c "source=open('playtest_server.py', encoding='utf-8').read(); compile(source, 'playtest_server.py', 'exec'); print('syntax ok')"
```

Small scenario smoke test:

```powershell
python -c "import playtest_server as p; cfg=dict(p.DEFAULT_CONFIG); cfg.update({'games':1,'maxTurns':30,'recordTimeline':False,'maxRecordedGames':0,'sweep':{'enabled':True,'players':[2],'initialLiveCells':[8],'rescueTarget':[4,5],'patternDeckModes':['current','without_each'],'maxScenarios':4}}); job=p.Job(cfg); job.run(); print(job.status, len(job.scenarios), job.summary.get('scenarios'), job.summary.get('games'))"
```

PAT variant check:

```powershell
python -c "import collections, playtest_server as p; cat=p.build_pattern_card_catalog(p.load_json('cards.json')); counts=collections.Counter(v.family_id for k,v in cat.items() if '_t' in k); print(counts); print([k for k,v in cat.items() if v.family_id=='glider' and '_t' in k])"
```

## Known Notes / Possible Next Improvements

- The simulator is intentionally heuristic, not a perfect strategic player.
- The action model approximates physical choices; refine if exact tabletop intent changes.
- Sweep can create many scenarios quickly. Keep `maxScenarios` sane for large runs.
- Jobs are in memory only. Refreshing/restarting the server loses previous results.
- A future improvement could persist jobs/results to JSON files.
- Another useful improvement: compare scenario rankings by target metric, e.g. shortest average, highest completion rate, lowest variance, or fairer win distribution.
- Another useful improvement: allow custom manual deck count table in the UI instead of editing JSON.
- `__pycache__` may appear after Python tests; it is generated and not part of the simulator.

## Current Git/Workspace Note

At the time of this handoff, the main new/changed files are:

- `playtest_server.py` new
- `README.md` updated
- `PLAYTEST_CONTEXT.md` new
