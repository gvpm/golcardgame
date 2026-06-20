# golcardgame

## Playtest simulator

Run the physical-rule simulator with:

```bash
python playtest_server.py
```

Then open `http://127.0.0.1:8765`.

If that port shows the main game instead of the playtest page, use a clean port:

```bash
python playtest_server.py --port 8766
```

Then open `http://127.0.0.1:8766`.

The page lets you run batches for 2, 3, or 4 players, edit the active pattern/action deck JSON, change rule toggles, and download CSV/JSON results. It reads the current card definitions from `cards.json` and the active deck composition from `cards_active.json`.

Use `Partidas visuais` to choose how many games keep a full replay timeline. Recorded games can be opened in `Visualizar partidas`, where you can step through the grid, actions, removals, rescues, player hands, and scored cards.

The default `greedy` player policy prioritizes constructive add actions and chooses cells that help complete patterns in that player's hand. Remove, clear, and swap actions are fallback choices unless they are the only valid options.

Action and pattern draws consume cards from their decks as batches. If a deck cannot complete the requested draw, its discard pile is shuffled back before the batch is drawn. Action cards drawn during a turn only enter the discard pile after the chosen action is resolved or the turn passes. Rescued pattern cards stay in the player's scoring area and do not return to the pattern deck.

Pattern deck entries from `cards_active.json` are expanded into physical card variants before play using the same transform logic as the pattern assistant in `index.html`: base position, 180-degree position, mirror, 180-degree mirror, and each period phase, with duplicate shapes removed. The active deck count still controls how many physical cards of each family enter a game, but those cards are selected from the full PAT-recognized variant pool.

Enable `Combinatoria de balanceamento` to run a matrix of scenarios in one job. You can sweep player counts, initial live cells, rescue targets, and pattern deck modes such as current deck, removing each pattern family, favoring each family, reducing high-count families, uniform decks, or fixed per-family counts. Each scenario reports its exact pattern deck counts, starting cells, rescue target, turn distribution, and rescued-card distribution.
