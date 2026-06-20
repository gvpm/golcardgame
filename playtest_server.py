#!/usr/bin/env python3
"""Playtest simulator for the physical Card GOL rules.

Run:
    python playtest_server.py

Then open:
    http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import random
import statistics
import threading
import time
import traceback
import urllib.parse
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
ROWS = 14
COLS = 20
QUADRANTS = {
    0: (0, 7, 0, 10),
    1: (0, 7, 10, 20),
    2: (7, 14, 0, 10),
    3: (7, 14, 10, 20),
}


DEFAULT_CONFIG = {
    "games": 500,
    "players": 2,
    "seed": "",
    "initialLiveCells": 12,
    "handSize": 5,
    "rescueTarget": 5,
    "maxTurns": 300,
    "wrapPatterns": False,
    "allowOverlappingPatterns": True,
    "offTurnRescue": True,
    "finishActiveTurnOnly": True,
    "actionPolicy": "greedy",
    "rescuePolicy": "all",
    "quadrantPolicy": "best",
    "recordTimeline": True,
    "maxRecordedGames": 20,
    "sweep": {
        "enabled": False,
        "players": [],
        "initialLiveCells": [],
        "rescueTarget": [],
        "patternDeckModes": [],
        "maxScenarios": 120,
    },
    "patternDeck": None,
    "actionDeck": None,
}


ACTION_OFFSETS = {
    "diagonal": [(-1, -1), (-1, 1), (1, -1), (1, 1)],
    "orthogonal": [(-1, 0), (0, -1), (0, 1), (1, 0)],
    "any": [
        (-1, -1), (-1, 0), (-1, 1),
        (0, -1), (0, 1),
        (1, -1), (1, 0), (1, 1),
    ],
}


def load_json(path: str) -> dict[str, Any]:
    with (ROOT / path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def merge_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if raw:
        config.update(raw)
    config["games"] = int(config.get("games") or 1)
    config["players"] = max(2, min(4, int(config.get("players") or 2)))
    config["initialLiveCells"] = max(0, int(config.get("initialLiveCells") or 0))
    config["handSize"] = max(1, int(config.get("handSize") or 5))
    config["rescueTarget"] = max(1, int(config.get("rescueTarget") or 5))
    config["maxTurns"] = max(1, int(config.get("maxTurns") or 300))
    config["maxRecordedGames"] = max(0, int(config.get("maxRecordedGames") or 0))
    return config


def default_decks() -> dict[str, list[dict[str, Any]]]:
    active = load_json("cards_active.json")
    return {
        "patternDeck": active.get("patternDeck", []),
        "actionDeck": active.get("actionDeck", []),
    }


def normalize_deck_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for entry in entries:
        card_id = str(entry.get("id", "")).strip()
        count = max(0, int(entry.get("count") or 0))
        if card_id and count > 0:
            normalized.append({"id": card_id, "count": count})
    return normalized


def deck_total(entries: list[dict[str, Any]]) -> int:
    return sum(max(0, int(entry.get("count") or 0)) for entry in entries)


def deck_counts(entries: list[dict[str, Any]]) -> dict[str, int]:
    return {str(entry.get("id")): int(entry.get("count") or 0) for entry in entries}


def deck_from_counts(counts: dict[str, int], order: list[str]) -> list[dict[str, Any]]:
    return [{"id": card_id, "count": counts[card_id]} for card_id in order if counts.get(card_id, 0) > 0]


def scale_deck_to_total(counts: dict[str, int], total: int, order: list[str]) -> dict[str, int]:
    positive = {key: max(0, int(value)) for key, value in counts.items() if value > 0}
    if not positive or total <= 0:
        return {}
    current_total = sum(positive.values())
    scaled: dict[str, int] = {}
    remainders: list[tuple[float, str]] = []
    for key in order:
        if key not in positive:
            continue
        raw = positive[key] * total / current_total
        base = max(1, int(math.floor(raw)))
        scaled[key] = base
        remainders.append((raw - base, key))
    while sum(scaled.values()) > total:
        candidates = [key for key in order if scaled.get(key, 0) > 1]
        if not candidates:
            break
        key = max(candidates, key=lambda item: scaled[item])
        scaled[key] -= 1
    for _remainder, key in sorted(remainders, reverse=True):
        if sum(scaled.values()) >= total:
            break
        scaled[key] += 1
    return scaled


def pattern_deck_variations(base_entries: list[dict[str, Any]], requested_modes: list[str] | None = None) -> list[dict[str, Any]]:
    base_entries = normalize_deck_entries(base_entries)
    order = [entry["id"] for entry in base_entries]
    base_counts = deck_counts(base_entries)
    total = deck_total(base_entries)
    modes = requested_modes or ["current"]
    variations: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(label: str, mode: str, counts: dict[str, int]) -> None:
        entries = deck_from_counts(counts, order)
        key = json.dumps(entries, sort_keys=True)
        if key in seen or not entries:
            return
        seen.add(key)
        variations.append(
            {
                "label": label,
                "mode": mode,
                "patternDeck": entries,
                "patternDeckTotal": deck_total(entries),
                "patternDeckCounts": deck_counts(entries),
            }
        )

    if "current" in modes:
        add("Atual", "current", dict(base_counts))

    if "without_each" in modes:
        for card_id in order:
            counts = dict(base_counts)
            counts[card_id] = 0
            add("Sem " + card_id, "without_each", counts)

    if "favor_each" in modes:
        for card_id in order:
            counts = {key: max(1, value // 2) for key, value in base_counts.items()}
            counts[card_id] = max(base_counts[card_id], math.ceil(total * 0.35))
            add("Mais " + card_id, "favor_each", scale_deck_to_total(counts, total, order))

    if "reduce_high" in modes:
        counts = dict(base_counts)
        if counts:
            threshold = statistics.median(counts.values())
            for card_id, count in list(counts.items()):
                if count > threshold:
                    counts[card_id] = max(1, math.ceil(count * 0.5))
        add("Reduz maiores", "reduce_high", scale_deck_to_total(counts, total, order))

    if "uniform" in modes:
        add("Uniforme", "uniform", scale_deck_to_total({card_id: 1 for card_id in order}, total, order))

    if "count_grid" in modes:
        for count in (1, 2, 4, 6, 8):
            add("Todos x" + str(count), "count_grid", {card_id: count for card_id in order})

    return variations


def parse_sweep_values(values: Any, fallback: list[int]) -> list[int]:
    if isinstance(values, str):
        parsed = [int(part.strip()) for part in values.split(",") if part.strip()]
    elif isinstance(values, list):
        parsed = [int(value) for value in values if str(value).strip()]
    else:
        parsed = []
    return parsed or fallback


def expand_scenarios(config: dict[str, Any]) -> list[dict[str, Any]]:
    config = merge_config(config)
    sweep = config.get("sweep") or {}
    decks = default_decks()
    base_pattern_deck = normalize_deck_entries(config.get("patternDeck") or decks["patternDeck"])
    if not sweep.get("enabled"):
        scenario_config = dict(config)
        scenario_config["patternDeck"] = base_pattern_deck
        return [
            {
                "id": "scenario-1",
                "label": "Config atual",
                "config": scenario_config,
                "variables": {
                    "players": scenario_config["players"],
                    "initialLiveCells": scenario_config["initialLiveCells"],
                    "rescueTarget": scenario_config["rescueTarget"],
                    "patternDeckMode": "current",
                    "patternDeckLabel": "Atual",
                    "patternDeckTotal": deck_total(base_pattern_deck),
                    "patternDeckCounts": deck_counts(base_pattern_deck),
                },
            }
        ]

    players_values = parse_sweep_values(sweep.get("players"), [config["players"]])
    cells_values = parse_sweep_values(sweep.get("initialLiveCells"), [config["initialLiveCells"]])
    rescue_values = parse_sweep_values(sweep.get("rescueTarget"), [config["rescueTarget"]])
    modes = sweep.get("patternDeckModes") or ["current"]
    deck_variations = pattern_deck_variations(base_pattern_deck, modes)
    max_scenarios = max(1, int(sweep.get("maxScenarios") or 120))

    scenarios: list[dict[str, Any]] = []
    for players in players_values:
        for cells in cells_values:
            for rescue_target in rescue_values:
                for deck_variation in deck_variations:
                    scenario_config = dict(config)
                    scenario_config["sweep"] = {"enabled": False}
                    scenario_config["players"] = max(2, min(4, players))
                    scenario_config["initialLiveCells"] = max(0, cells)
                    scenario_config["rescueTarget"] = max(1, rescue_target)
                    scenario_config["patternDeck"] = deck_variation["patternDeck"]
                    scenario_index = len(scenarios) + 1
                    label = (
                        "P"
                        + str(scenario_config["players"])
                        + " cel"
                        + str(scenario_config["initialLiveCells"])
                        + " alvo"
                        + str(scenario_config["rescueTarget"])
                        + " - "
                        + deck_variation["label"]
                    )
                    scenarios.append(
                        {
                            "id": "scenario-" + str(scenario_index),
                            "label": label,
                            "config": scenario_config,
                            "variables": {
                                "players": scenario_config["players"],
                                "initialLiveCells": scenario_config["initialLiveCells"],
                                "rescueTarget": scenario_config["rescueTarget"],
                                "patternDeckMode": deck_variation["mode"],
                                "patternDeckLabel": deck_variation["label"],
                                "patternDeckTotal": deck_variation["patternDeckTotal"],
                                "patternDeckCounts": deck_variation["patternDeckCounts"],
                            },
                        }
                    )
                    if len(scenarios) >= max_scenarios:
                        return scenarios
    return scenarios


def cards_payload() -> dict[str, Any]:
    cards = load_json("cards.json")
    catalog = build_pattern_card_catalog(cards)
    cards["patternCards"] = {
        card_id: {
            "id": card_id,
            "family": variant.family_id,
            "name": variant.name,
            "label": variant.label,
            "category": variant.category,
            "phase": variant.phase,
            "phaseTotal": variant.phase_total,
            "mirrored": variant.mirrored,
            "pattern": [list(row) for row in variant.pattern],
            "points": len(variant.live),
        }
        for card_id, variant in catalog.items()
        if "_t" in card_id
    }
    return cards


def expand_deck(entries: list[dict[str, Any]], rng: random.Random) -> list[str]:
    deck: list[str] = []
    for entry in entries:
        card_id = str(entry.get("id", "")).strip()
        count = int(entry.get("count") or 0)
        deck.extend([card_id] * max(0, count))
    rng.shuffle(deck)
    return deck


def draw_many(deck: list[str], discard: list[str], rng: random.Random, count: int) -> list[str]:
    if count <= 0:
        return []

    # Draws happen as a batch. If the deck cannot satisfy the requested
    # amount, only then the discard pile is shuffled back in before any card
    # from this batch is drawn. Cards drawn in this batch are never available
    # again until they are explicitly discarded after the turn.
    if len(deck) < count and discard:
        deck.extend(discard)
        discard.clear()
        rng.shuffle(deck)

    cards: list[str] = []
    draw_count = min(count, len(deck))
    for _ in range(draw_count):
        cards.append(deck.pop())
    return cards


def clone_pattern(pattern: list[list[int]]) -> list[list[int]]:
    return [row[:] for row in pattern]


def trim_pattern(pattern: list[list[int]]) -> list[list[int]]:
    min_row = math.inf
    min_col = math.inf
    max_row = -1
    max_col = -1
    for row, line in enumerate(pattern):
        for col, value in enumerate(line):
            if value:
                min_row = min(min_row, row)
                min_col = min(min_col, col)
                max_row = max(max_row, row)
                max_col = max(max_col, col)
    if max_row == -1:
        return [[0]]
    return [
        [1 if pattern[row][col] else 0 for col in range(int(min_col), max_col + 1)]
        for row in range(int(min_row), max_row + 1)
    ]


def rotate_pattern(pattern: list[list[int]]) -> list[list[int]]:
    rows = len(pattern)
    cols = len(pattern[0])
    return [[pattern[rows - row - 1][col] for row in range(rows)] for col in range(cols)]


def rotate_180(pattern: list[list[int]]) -> list[list[int]]:
    return rotate_pattern(rotate_pattern(pattern))


def mirror_pattern(pattern: list[list[int]]) -> list[list[int]]:
    return [row[::-1] for row in pattern]


def pattern_key(pattern: list[list[int]]) -> str:
    return "/".join("".join(str(v) for v in row) for row in pattern)


def simulate_life(pattern: list[list[int]]) -> list[list[int]]:
    margin = 6
    sim_rows = len(pattern) + margin * 2
    sim_cols = len(pattern[0]) + margin * 2
    state = [[0 for _ in range(sim_cols)] for _ in range(sim_rows)]
    for row, line in enumerate(pattern):
        for col, value in enumerate(line):
            state[row + margin][col + margin] = 1 if value else 0
    next_state = [[0 for _ in range(sim_cols)] for _ in range(sim_rows)]
    for row in range(sim_rows):
        for col in range(sim_cols):
            neighbors = 0
            for d_row in (-1, 0, 1):
                for d_col in (-1, 0, 1):
                    if d_row == 0 and d_col == 0:
                        continue
                    n_row = row + d_row
                    n_col = col + d_col
                    if 0 <= n_row < sim_rows and 0 <= n_col < sim_cols:
                        neighbors += state[n_row][n_col]
            if state[row][col]:
                next_state[row][col] = 1 if neighbors in (2, 3) else 0
            else:
                next_state[row][col] = 1 if neighbors == 3 else 0
    return next_state


def live_cells(pattern: list[list[int]]) -> list[tuple[int, int]]:
    cells: list[tuple[int, int]] = []
    for row, line in enumerate(pattern):
        for col, value in enumerate(line):
            if value:
                cells.append((row, col))
    return cells


@dataclass(frozen=True)
class PatternVariant:
    card_id: str
    family_id: str
    name: str
    label: str
    phase: int
    phase_total: int
    mirrored: bool
    category: str
    pattern: tuple[tuple[int, ...], ...]
    live: tuple[tuple[int, int], ...]
    height: int
    width: int


def build_pattern_card_catalog(cards: dict[str, Any]) -> dict[str, PatternVariant]:
    catalog: dict[str, PatternVariant] = {}
    for family_id, definition in cards.get("patterns", {}).items():
        base = [[1 if value else 0 for value in row] for row in definition.get("pattern", [])]
        if not base:
            continue
        seeds = [
            {"pattern": clone_pattern(base), "mirrored": False, "orientation": 1},
            {"pattern": rotate_180(base), "mirrored": False, "orientation": 2},
        ]
        if definition.get("mirrors"):
            mirrored = mirror_pattern(base)
            seeds.extend(
                [
                    {"pattern": mirrored, "mirrored": True, "orientation": 1},
                    {"pattern": rotate_180(mirrored), "mirrored": True, "orientation": 2},
                ]
            )

        transforms: list[dict[str, Any]] = []
        seen_transforms: set[str] = set()
        for seed in seeds:
            trimmed = trim_pattern(seed["pattern"])
            key = pattern_key(trimmed)
            if key in seen_transforms:
                continue
            seen_transforms.add(key)
            transforms.append({**seed, "pattern": trimmed})

        phase_count = int(definition.get("phaseCount") or 4)
        seen_variants: set[str] = set()
        for transform_index, seed in enumerate(transforms, start=1):
            current = trim_pattern(seed["pattern"])
            for phase in range(phase_count):
                trimmed = trim_pattern(current)
                variant_key = pattern_key(trimmed)
                if variant_key in seen_variants:
                    current = simulate_life(current)
                    continue
                seen_variants.add(variant_key)
                matrix = tuple(tuple(int(value) for value in row) for row in trimmed)
                cells = tuple(live_cells(trimmed))
                card_id = (
                    family_id
                    + "_t"
                    + str(transform_index)
                    + "_p"
                    + str(phase + 1)
                    + ("_mirror" if seed["mirrored"] else "")
                )
                label_parts = ["pos " + str(transform_index), "fase " + str(phase + 1)]
                if seed["mirrored"]:
                    label_parts.append("mirror")
                label = " ".join(label_parts)
                catalog[card_id] = PatternVariant(
                    card_id=card_id,
                    family_id=family_id,
                    name=str(definition.get("name") or family_id),
                    label=label,
                    phase=phase + 1,
                    phase_total=phase_count,
                    mirrored=bool(seed["mirrored"]),
                    category=str(definition.get("category") or ""),
                    pattern=matrix,
                    live=cells,
                    height=len(trimmed),
                    width=len(trimmed[0]),
                )
                # Backwards-compatible aliases for exact card ids used by
                # earlier simulator versions. Family deck expansion ignores
                # these aliases and uses the PAT-derived ids above.
                legacy_id = family_id + "_p" + str(phase + 1) + ("_mirror" if seed["mirrored"] else "")
                catalog.setdefault(
                    legacy_id,
                    PatternVariant(
                        card_id=legacy_id,
                        family_id=family_id,
                        name=str(definition.get("name") or family_id),
                        label=label,
                        phase=phase + 1,
                        phase_total=phase_count,
                        mirrored=bool(seed["mirrored"]),
                        category=str(definition.get("category") or ""),
                        pattern=matrix,
                        live=cells,
                        height=len(trimmed),
                        width=len(trimmed[0]),
                    )
                )
                if phase == 0 and not seed["mirrored"] and phase_count == 1 and not definition.get("mirrors"):
                    catalog.setdefault(
                        family_id,
                        PatternVariant(
                            card_id=family_id,
                            family_id=family_id,
                            name=str(definition.get("name") or family_id),
                            label="fase 1",
                            phase=1,
                            phase_total=phase_count,
                            mirrored=False,
                            category=str(definition.get("category") or ""),
                            pattern=matrix,
                            live=cells,
                            height=len(trimmed),
                            width=len(trimmed[0]),
                        )
                    )
                current = simulate_life(current)
    return catalog


def build_pattern_deck(
    entries: list[dict[str, Any]],
    catalog: dict[str, PatternVariant],
    rng: random.Random,
) -> list[str]:
    by_family: dict[str, list[str]] = {}
    for card_id, variant in catalog.items():
        if card_id == variant.family_id:
            continue
        if "_t" not in card_id:
            continue
        by_family.setdefault(variant.family_id, []).append(card_id)

    deck: list[str] = []
    for entry in entries:
        configured_id = str(entry.get("id", "")).strip()
        count = max(0, int(entry.get("count") or 0))
        variants = by_family.get(configured_id, [])
        if variants:
            variants = variants[:]
            rng.shuffle(variants)
            for index in range(count):
                deck.append(variants[index % len(variants)])
        elif configured_id in catalog:
            deck.extend([configured_id] * count)
    rng.shuffle(deck)
    return deck


@dataclass
class Match:
    card_id: str
    name: str
    phase: int
    live_cells: tuple[tuple[int, int], ...]
    points: int


@dataclass
class Player:
    hand: list[str] = field(default_factory=list)
    rescued: list[str] = field(default_factory=list)
    score: int = 0


@dataclass
class GameState:
    grid: list[list[int]]
    players: list[Player]
    pattern_deck: list[str]
    pattern_discard: list[str]
    action_deck: list[str]
    action_discard: list[str]
    turn: int = 0
    winner_triggered: bool = False


class Simulator:
    def __init__(self, config: dict[str, Any], seed: int | str | None = None):
        self.config = merge_config(config)
        self.rng = random.Random(seed)
        cards = load_json("cards.json")
        decks = default_decks()
        self.pattern_defs = cards.get("patterns", {})
        self.pattern_catalog = build_pattern_card_catalog(cards)
        self.pattern_deck_config = self.config.get("patternDeck") or decks["patternDeck"]
        self.action_deck_config = self.config.get("actionDeck") or decks["actionDeck"]
        self._match_cache: dict[tuple[tuple[tuple[int, ...], ...], str], list[Match]] = {}

    def new_state(self) -> GameState:
        pattern_deck = build_pattern_deck(self.pattern_deck_config, self.pattern_catalog, self.rng)
        action_deck = expand_deck(self.action_deck_config, self.rng)
        players = [Player() for _ in range(self.config["players"])]
        for player in players:
            player.hand.extend(draw_many(pattern_deck, [], self.rng, self.config["handSize"]))
        grid = [[0 for _ in range(COLS)] for _ in range(ROWS)]
        cells = [(row, col) for row in range(ROWS) for col in range(COLS)]
        self.rng.shuffle(cells)
        for row, col in cells[: min(len(cells), self.config["initialLiveCells"])]:
            grid[row][col] = 1
        return GameState(grid, players, pattern_deck, [], action_deck, [])

    def run_game(self, index: int = 0, record_timeline: bool = False) -> dict[str, Any]:
        state = self.new_state()
        log: list[str] = []
        timeline: list[dict[str, Any]] = []
        max_turns = self.config["maxTurns"]
        if record_timeline:
            timeline.append(self.snapshot_step(state, "setup", "Setup inicial", changed=[]))

        while state.turn < max_turns and not state.winner_triggered:
            player_index = state.turn % len(state.players)
            self.play_turn(state, player_index, log, timeline if record_timeline else None)
            state.turn += 1

        winners = self.get_winners(state)
        rounds = state.turn / len(state.players)
        result = {
            "game": index,
            "turns": state.turn,
            "rounds": rounds,
            "endedByTarget": state.winner_triggered,
            "winner": winners[0] if winners else None,
            "winners": winners,
            "scores": [player.score for player in state.players],
            "rescued": [len(player.rescued) for player in state.players],
            "rescuedByCard": self.count_rescued_by_card(state),
            "liveCellsLeft": sum(sum(row) for row in state.grid),
            "log": log[-20:],
        }
        if record_timeline:
            timeline.append(self.snapshot_step(state, "final", "Fim da partida", changed=[]))
            result["timeline"] = timeline
        return result

    def play_turn(
        self,
        state: GameState,
        player_index: int,
        log: list[str],
        timeline: list[dict[str, Any]] | None = None,
    ) -> None:
        player = state.players[player_index]
        quadrant = self.choose_quadrant(state, player_index)
        drawn_actions = draw_many(state.action_deck, state.action_discard, self.rng, 2)
        if timeline is not None:
            timeline.append(
                self.snapshot_step(
                    state,
                    "turn-start",
                    f"P{player_index + 1} inicia turno no Q{quadrant + 1}; compra ações: {', '.join(drawn_actions) or 'nenhuma'}",
                    active_player=player_index,
                    quadrant=quadrant,
                    action_draw=drawn_actions,
                    changed=[],
                )
            )
        if not drawn_actions:
            self.rescue_active(state, player_index, log, timeline)
            return

        chosen = self.choose_action(state, player_index, quadrant, drawn_actions)
        chosen_marked = False
        for card in drawn_actions:
            if chosen is not None and card == chosen and not chosen_marked:
                chosen_marked = True
            else:
                state.action_discard.append(card)

        if chosen:
            before = self.count_hand_matches(state, player)
            changed = self.apply_action(state, quadrant, chosen)
            after = self.count_hand_matches(state, player)
            state.action_discard.append(chosen)
            log.append(
                f"P{player_index + 1} Q{quadrant + 1} {chosen} matches {before}->{after}"
            )
            if timeline is not None:
                timeline.append(
                    self.snapshot_step(
                        state,
                        "action",
                        f"P{player_index + 1} usa {chosen} no Q{quadrant + 1}; matches {before}->{after}",
                        active_player=player_index,
                        quadrant=quadrant,
                        action=chosen,
                        changed=changed,
                    )
                )
        else:
            state.action_discard.extend(drawn_actions)
            log.append(f"P{player_index + 1} Q{quadrant + 1} passed")
            if timeline is not None:
                timeline.append(
                    self.snapshot_step(
                        state,
                        "pass",
                        f"P{player_index + 1} passa; nenhuma ação válida no Q{quadrant + 1}",
                        active_player=player_index,
                        quadrant=quadrant,
                        changed=[],
                    )
                )

        self.rescue_active(state, player_index, log, timeline)
        if self.config.get("offTurnRescue") and not state.winner_triggered:
            self.rescue_off_turn(state, player_index, log, timeline)

    def choose_quadrant(self, state: GameState, player_index: int) -> int:
        die = self.rng.randint(1, 6)
        orientation = "vertical"
        even_quads = [0, 2] if orientation == "vertical" else [0, 1]
        odd_quads = [1, 3] if orientation == "vertical" else [2, 3]
        candidates = even_quads if die % 2 == 0 else odd_quads
        if self.config.get("quadrantPolicy") != "best":
            return self.rng.choice(candidates)

        best_score = -1
        best_quads: list[int] = []
        for quadrant in candidates:
            # Physical play usually prefers the richer quadrant among the two
            # allowed by the die. Full lookahead here is too expensive for
            # large batches and did not add much signal in smoke tests.
            score = self.count_quadrant_live(state.grid, quadrant)
            if score > best_score:
                best_score = score
                best_quads = [quadrant]
            elif score == best_score:
                best_quads.append(quadrant)
        return self.rng.choice(best_quads)

    def choose_action(
        self,
        state: GameState,
        player_index: int,
        quadrant: int,
        actions: list[str],
    ) -> str | None:
        valid = [action for action in actions if self.action_has_candidate(state.grid, quadrant, action)]
        if not valid:
            return None
        if self.config.get("actionPolicy") != "greedy":
            return self.rng.choice(valid)

        player = state.players[player_index]
        best_score = -999999
        best_actions: list[str] = []
        for action in valid:
            score = self.evaluate_action_choice(state, player, quadrant, action)
            if score > best_score:
                best_score = score
                best_actions = [action]
            elif score == best_score:
                best_actions.append(action)
        return self.rng.choice(best_actions)

    def evaluate_action_choice(
        self,
        state: GameState,
        player: Player,
        quadrant: int,
        action_id: str,
    ) -> float:
        if action_id.startswith("add-"):
            candidates = self.action_candidates(state.grid, quadrant, action_id)
            if not candidates and self.is_quadrant_empty(state.grid, quadrant):
                candidates = [(row, col) for row, col in self.cells_in_quadrant(quadrant) if not state.grid[row][col]]
            if not candidates:
                return -999999.0
            best_cell_score = max(self.score_add_cell(state.grid, player, row, col) for row, col in candidates[:80])
            return 1000 + best_cell_score

        if action_id.startswith("remove-"):
            candidates = self.action_candidates(state.grid, quadrant, action_id)
            if not candidates:
                return -999999.0
            lowest_damage = min(self.score_cell_support(state.grid, player, row, col) for row, col in candidates[:80])
            return -40 - lowest_damage

        if action_id == "clear-grid-tile":
            support = sum(
                self.score_cell_support(state.grid, player, row, col)
                for row, col in self.cells_in_quadrant(quadrant)
                if state.grid[row][col]
            )
            return -120 - support

        if action_id == "swap-grid-tiles":
            return 5

        return -999999.0

    def action_has_candidate(self, grid: list[list[int]], quadrant: int, action_id: str) -> bool:
        if action_id == "clear-grid-tile":
            return self.count_quadrant_live(grid, quadrant) > 0
        if action_id == "swap-grid-tiles":
            return True
        if action_id.startswith("add-"):
            return bool(self.action_candidates(grid, quadrant, action_id)) or self.is_quadrant_empty(grid, quadrant)
        if action_id.startswith("remove-"):
            return bool(self.action_candidates(grid, quadrant, action_id))
        return False

    def apply_action(self, state: GameState, quadrant: int, action_id: str) -> list[dict[str, Any]]:
        grid = state.grid
        changed: list[dict[str, Any]] = []
        if action_id == "clear-grid-tile":
            r0, r1, c0, c1 = QUADRANTS[quadrant]
            for row in range(r0, r1):
                for col in range(c0, c1):
                    if grid[row][col]:
                        changed.append({"row": row, "col": col, "before": 1, "after": 0, "reason": "clear"})
                    grid[row][col] = 0
            return changed

        if action_id == "swap-grid-tiles":
            other = self.rng.choice([q for q in QUADRANTS if q != quadrant])
            return self.swap_quadrants(grid, quadrant, other)

        if action_id.startswith("add-") and self.is_quadrant_empty(grid, quadrant):
            row, col = self.rng.choice(self.cells_in_quadrant(quadrant))
            changed.append({"row": row, "col": col, "before": 0, "after": 1, "reason": "empty-quadrant-seed"})
            grid[row][col] = 1

        repeats = self.rng.randint(1, 6) if action_id.startswith(("add-", "remove-")) else 1
        did_once = False
        for _ in range(repeats):
            candidates = self.action_candidates(grid, quadrant, action_id)
            if not candidates:
                break
            row, col = self.choose_action_cell(state, candidates, action_id)
            before = grid[row][col]
            grid[row][col] = 1 if action_id.startswith("add-") else 0
            after = grid[row][col]
            if before != after:
                changed.append({"row": row, "col": col, "before": before, "after": after, "reason": action_id})
            did_once = True
        if not did_once and action_id.startswith("add-"):
            candidates = [(row, col) for row, col in self.cells_in_quadrant(quadrant) if not grid[row][col]]
            if candidates:
                row, col = self.rng.choice(candidates)
                changed.append({"row": row, "col": col, "before": 0, "after": 1, "reason": action_id})
                grid[row][col] = 1
        return changed

    def choose_action_cell(
        self,
        state: GameState,
        candidates: list[tuple[int, int]],
        action_id: str,
    ) -> tuple[int, int]:
        if self.config.get("actionPolicy") != "greedy" or len(candidates) == 1:
            return self.rng.choice(candidates)
        player = state.players[state.turn % len(state.players)] if state.players else None
        if not player:
            return self.rng.choice(candidates)
        sample = candidates if len(candidates) <= 80 else self.rng.sample(candidates, 80)
        best_score = -999999.0
        best_cells: list[tuple[int, int]] = []
        for row, col in sample:
            if action_id.startswith("add-"):
                score = self.score_add_cell(state.grid, player, row, col)
            else:
                # For removal, choose the cell that least contributes to any
                # pattern currently in hand.
                score = -self.score_cell_support(state.grid, player, row, col)
            if score > best_score:
                best_score = score
                best_cells = [(row, col)]
            elif score == best_score:
                best_cells.append((row, col))
        return self.rng.choice(best_cells or candidates)

    def score_add_cell(self, grid: list[list[int]], player: Player, row: int, col: int) -> float:
        if grid[row][col]:
            return -999999.0
        best = 0.0
        for card_id in set(player.hand):
            multiplier = player.hand.count(card_id)
            variant = self.pattern_catalog.get(card_id)
            if not variant:
                continue
            for live_row, live_col in variant.live:
                start_row = row - live_row
                start_col = col - live_col
                if not self.config.get("wrapPatterns"):
                    if start_row < 0 or start_col < 0 or start_row + variant.height > ROWS or start_col + variant.width > COLS:
                        continue
                matched_after = 1
                for other_row, other_col in variant.live:
                    if other_row == live_row and other_col == live_col:
                        continue
                    target_row = start_row + other_row
                    target_col = start_col + other_col
                    if self.config.get("wrapPatterns"):
                        target_row %= ROWS
                        target_col %= COLS
                    if grid[target_row][target_col]:
                        matched_after += 1
                total = len(variant.live)
                missing_after = total - matched_after
                if missing_after == 0:
                    candidate = 1000 + total * 10
                elif missing_after == 1:
                    candidate = 150 + matched_after * 12
                elif missing_after == 2:
                    candidate = 70 + matched_after * 8
                else:
                    candidate = matched_after * matched_after * 3 - missing_after
                best = max(best, candidate * multiplier)
        return best

    def score_cell_support(self, grid: list[list[int]], player: Player, row: int, col: int) -> float:
        if not grid[row][col]:
            return 0.0
        best = 0.0
        for card_id in set(player.hand):
            multiplier = player.hand.count(card_id)
            variant = self.pattern_catalog.get(card_id)
            if not variant:
                continue
            for live_row, live_col in variant.live:
                start_row = row - live_row
                start_col = col - live_col
                if not self.config.get("wrapPatterns"):
                    if start_row < 0 or start_col < 0 or start_row + variant.height > ROWS or start_col + variant.width > COLS:
                        continue
                matched = 0
                for other_row, other_col in variant.live:
                    target_row = start_row + other_row
                    target_col = start_col + other_col
                    if self.config.get("wrapPatterns"):
                        target_row %= ROWS
                        target_col %= COLS
                    if grid[target_row][target_col]:
                        matched += 1
                best = max(best, matched * matched * multiplier)
        return best

    def action_candidates(self, grid: list[list[int]], quadrant: int, action_id: str) -> list[tuple[int, int]]:
        mode, _, kind = action_id.partition("-")
        if kind not in ("diagonal", "orthogonal", "isolated", "any"):
            return []
        r0, r1, c0, c1 = QUADRANTS[quadrant]
        targets: set[tuple[int, int]] = set()
        if kind == "isolated":
            for row in range(r0, r1):
                for col in range(c0, c1):
                    if mode == "add" and not grid[row][col]:
                        targets.add((row, col))
                    elif mode == "remove" and grid[row][col]:
                        targets.add((row, col))
            return list(targets)

        offsets = ACTION_OFFSETS[kind]
        for row in range(r0, r1):
            for col in range(c0, c1):
                if not grid[row][col]:
                    continue
                for d_row, d_col in offsets:
                    target = (row + d_row, col + d_col)
                    t_row, t_col = target
                    if not (r0 <= t_row < r1 and c0 <= t_col < c1):
                        continue
                    if mode == "add" and not grid[t_row][t_col]:
                        targets.add(target)
                    elif mode == "remove" and grid[t_row][t_col]:
                        targets.add(target)
        return list(targets)

    def rescue_active(
        self,
        state: GameState,
        player_index: int,
        log: list[str],
        timeline: list[dict[str, Any]] | None = None,
    ) -> None:
        player = state.players[player_index]
        rescued_count = 0
        while True:
            match = self.best_rescue_match(state, player)
            if not match:
                break
            changed = self.resolve_rescue(state, player, match)
            rescued_count += 1
            log.append(f"P{player_index + 1} rescued {match.name} for {match.points}")
            if timeline is not None:
                timeline.append(
                    self.snapshot_step(
                        state,
                        "rescue",
                        f"P{player_index + 1} resgata {match.name} ({match.points} pts)",
                        active_player=player_index,
                        rescued={"player": player_index, "card": match.card_id, "name": match.name, "points": match.points},
                        changed=changed,
                    )
                )
            if self.config.get("rescuePolicy") != "all":
                break
        self.refill_hand(state, player)
        if timeline is not None and rescued_count:
            timeline.append(
                self.snapshot_step(
                    state,
                    "draw-patterns",
                    f"P{player_index + 1} recompra padrões até {self.config['handSize']} cartas",
                    active_player=player_index,
                    changed=[],
                )
            )
        if rescued_count and len(player.rescued) >= self.config["rescueTarget"]:
            state.winner_triggered = True

    def rescue_off_turn(
        self,
        state: GameState,
        active_index: int,
        log: list[str],
        timeline: list[dict[str, Any]] | None = None,
    ) -> None:
        for offset in range(1, len(state.players)):
            player_index = (active_index + offset) % len(state.players)
            player = state.players[player_index]
            match = self.best_rescue_match(state, player)
            if not match:
                continue
            changed = self.resolve_rescue(state, player, match)
            self.refill_hand(state, player, one_card=True)
            log.append(f"P{player_index + 1} off-turn rescued {match.name} for {match.points}")
            if timeline is not None:
                timeline.append(
                    self.snapshot_step(
                        state,
                        "off-turn-rescue",
                        f"P{player_index + 1} resgata fora do turno {match.name} ({match.points} pts)",
                        active_player=active_index,
                        rescued={"player": player_index, "card": match.card_id, "name": match.name, "points": match.points},
                        changed=changed,
                    )
                )
            if len(player.rescued) >= self.config["rescueTarget"]:
                state.winner_triggered = True
                if self.config.get("finishActiveTurnOnly"):
                    return

    def best_rescue_match(self, state: GameState, player: Player) -> Match | None:
        possible: list[Match] = []
        hand_counts = {card: player.hand.count(card) for card in set(player.hand)}
        for card_id, count in hand_counts.items():
            if count <= 0:
                continue
            possible.extend(self.find_matches(state.grid, card_id))
        if not possible:
            return None
        possible.sort(key=lambda match: (match.points, match.name), reverse=True)
        return possible[0]

    def resolve_rescue(self, state: GameState, player: Player, match: Match) -> list[dict[str, Any]]:
        player.hand.remove(match.card_id)
        player.rescued.append(match.card_id)
        player.score += match.points
        changed: list[dict[str, Any]] = []
        for row, col in match.live_cells:
            if state.grid[row][col]:
                changed.append({"row": row, "col": col, "before": 1, "after": 0, "reason": "rescue", "card": match.card_id})
            state.grid[row][col] = 0
        return changed

    def refill_hand(self, state: GameState, player: Player, one_card: bool = False) -> None:
        target = len(player.hand) + 1 if one_card else self.config["handSize"]
        missing = max(0, target - len(player.hand))
        player.hand.extend(draw_many(state.pattern_deck, state.pattern_discard, self.rng, missing))

    def find_matches(self, grid: list[list[int]], card_id: str) -> list[Match]:
        grid_key = tuple(tuple(row) for row in grid)
        cache_key = (grid_key, card_id)
        cached = self._match_cache.get(cache_key)
        if cached is not None:
            return cached
        if len(self._match_cache) > 20000:
            self._match_cache.clear()

        matches: list[Match] = []
        seen: set[tuple[str, tuple[tuple[int, int], ...]]] = set()
        live_grid_cells = [
            (row, col)
            for row in range(ROWS)
            for col in range(COLS)
            if grid[row][col]
        ]
        variant = self.pattern_catalog.get(card_id)
        if not variant:
            self._match_cache[cache_key] = matches
            return matches

        if self.config.get("allowOverlappingPatterns") and variant.live:
            anchor_row, anchor_col = variant.live[0]
            starts = (
                ((row - anchor_row) % ROWS, (col - anchor_col) % COLS)
                if self.config.get("wrapPatterns")
                else (row - anchor_row, col - anchor_col)
                for row, col in live_grid_cells
            )
        else:
            max_row = ROWS - 1 if self.config.get("wrapPatterns") else ROWS - variant.height
            max_col = COLS - 1 if self.config.get("wrapPatterns") else COLS - variant.width
            starts = (
                (row, col)
                for row in range(max_row + 1)
                for col in range(max_col + 1)
            )

        for row, col in starts:
            if not self.config.get("wrapPatterns"):
                if row < 0 or col < 0 or row + variant.height > ROWS or col + variant.width > COLS:
                    continue
            live_positions = self.match_variant_at(grid, variant, row, col)
            if not live_positions:
                continue
            key = (card_id, tuple(sorted(live_positions)))
            if key in seen:
                continue
            seen.add(key)
            matches.append(
                Match(
                    card_id=card_id,
                    name=variant.name + " " + variant.label,
                    phase=variant.phase,
                    live_cells=tuple(live_positions),
                    points=len(live_positions),
                )
            )
        self._match_cache[cache_key] = matches
        return matches

    def match_variant_at(
        self,
        grid: list[list[int]],
        variant: PatternVariant,
        start_row: int,
        start_col: int,
    ) -> list[tuple[int, int]] | None:
        live_positions: list[tuple[int, int]] = []
        for row in range(variant.height):
            for col in range(variant.width):
                expected = variant.pattern[row][col]
                target_row = start_row + row
                target_col = start_col + col
                if self.config.get("wrapPatterns"):
                    target_row %= ROWS
                    target_col %= COLS
                elif target_row < 0 or target_row >= ROWS or target_col < 0 or target_col >= COLS:
                    return None

                if self.config.get("allowOverlappingPatterns"):
                    if expected and not grid[target_row][target_col]:
                        return None
                elif grid[target_row][target_col] != expected:
                    return None

                if expected:
                    live_positions.append((target_row, target_col))
        return live_positions

    def count_hand_matches(self, state: GameState, player: Player | None) -> int:
        if not player:
            return 0
        return sum(len(self.find_matches(state.grid, card_id)) for card_id in set(player.hand))

    def hand_progress_score(self, grid: list[list[int]], player: Player | None) -> float:
        if not player:
            return 0.0
        score = 0.0
        for card_id in set(player.hand):
            score += self.card_progress_score(grid, card_id) * player.hand.count(card_id)
        return score

    def card_progress_score(self, grid: list[list[int]], card_id: str) -> float:
        variant = self.pattern_catalog.get(card_id)
        if not variant:
            return 0.0
        live_grid_cells = [
            (row, col)
            for row in range(ROWS)
            for col in range(COLS)
            if grid[row][col]
        ]
        best = -999999.0
        max_row = ROWS - 1 if self.config.get("wrapPatterns") else ROWS - variant.height
        max_col = COLS - 1 if self.config.get("wrapPatterns") else COLS - variant.width
        if max_row < 0 or max_col < 0:
            return 0.0
        starts: set[tuple[int, int]] = set()
        if live_grid_cells:
            for grid_row, grid_col in live_grid_cells:
                for live_row, live_col in variant.live:
                    start_row = grid_row - live_row
                    start_col = grid_col - live_col
                    if self.config.get("wrapPatterns"):
                        starts.add((start_row % ROWS, start_col % COLS))
                    elif 0 <= start_row <= max_row and 0 <= start_col <= max_col:
                        starts.add((start_row, start_col))
        else:
            starts.add((0, 0))

        for start_row, start_col in starts:
            matched = 0
            for live_row, live_col in variant.live:
                target_row = start_row + live_row
                target_col = start_col + live_col
                if self.config.get("wrapPatterns"):
                    target_row %= ROWS
                    target_col %= COLS
                if grid[target_row][target_col]:
                    matched += 1
            total = len(variant.live)
            missing = total - matched
            if missing == 0:
                candidate = 1000 + total * 10
            elif missing == 1:
                candidate = 120 + matched * 8
            elif missing == 2:
                candidate = 50 + matched * 5
            else:
                candidate = matched * matched * 2.5 - missing * 1.5
            if candidate > best:
                best = candidate
        return best if best > -999999.0 else 0.0

    def count_quadrant_live(self, grid: list[list[int]], quadrant: int) -> int:
        r0, r1, c0, c1 = QUADRANTS[quadrant]
        return sum(grid[row][col] for row in range(r0, r1) for col in range(c0, c1))

    def is_quadrant_empty(self, grid: list[list[int]], quadrant: int) -> bool:
        return self.count_quadrant_live(grid, quadrant) == 0

    def cells_in_quadrant(self, quadrant: int) -> list[tuple[int, int]]:
        r0, r1, c0, c1 = QUADRANTS[quadrant]
        return [(row, col) for row in range(r0, r1) for col in range(c0, c1)]

    def swap_quadrants(self, grid: list[list[int]], first: int, second: int) -> list[dict[str, Any]]:
        changed: list[dict[str, Any]] = []
        r0a, r1a, c0a, c1a = QUADRANTS[first]
        r0b, _r1b, c0b, _c1b = QUADRANTS[second]
        height = r1a - r0a
        width = c1a - c0a
        for d_row in range(height):
            for d_col in range(width):
                a = (r0a + d_row, c0a + d_col)
                b = (r0b + d_row, c0b + d_col)
                before_a = grid[a[0]][a[1]]
                before_b = grid[b[0]][b[1]]
                grid[a[0]][a[1]], grid[b[0]][b[1]] = grid[b[0]][b[1]], grid[a[0]][a[1]]
                if before_a != grid[a[0]][a[1]]:
                    changed.append({"row": a[0], "col": a[1], "before": before_a, "after": grid[a[0]][a[1]], "reason": "swap"})
                if before_b != grid[b[0]][b[1]]:
                    changed.append({"row": b[0], "col": b[1], "before": before_b, "after": grid[b[0]][b[1]], "reason": "swap"})
        return changed

    def snapshot_step(
        self,
        state: GameState,
        kind: str,
        message: str,
        changed: list[dict[str, Any]],
        active_player: int | None = None,
        quadrant: int | None = None,
        action: str | None = None,
        action_draw: list[str] | None = None,
        rescued: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "step": None,
            "turn": state.turn,
            "kind": kind,
            "message": message,
            "activePlayer": active_player,
            "quadrant": quadrant,
            "action": action,
            "actionDraw": action_draw or [],
            "rescued": rescued,
            "changed": changed,
            "decks": {
                "patternDeck": len(state.pattern_deck),
                "patternDiscard": len(state.pattern_discard),
                "actionDeck": len(state.action_deck),
                "actionDiscard": len(state.action_discard),
            },
            "grid": clone_grid(state.grid),
            "players": [
                {
                    "hand": player.hand[:],
                    "rescued": player.rescued[:],
                    "score": player.score,
                }
                for player in state.players
            ],
            "liveCells": sum(sum(row) for row in state.grid),
        }

    def get_winners(self, state: GameState) -> list[int]:
        best_score = max(player.score for player in state.players)
        return [index for index, player in enumerate(state.players) if player.score == best_score]

    def count_rescued_by_card(self, state: GameState) -> dict[str, int]:
        counts: dict[str, int] = {}
        for player in state.players:
            for card_id in player.rescued:
                counts[card_id] = counts.get(card_id, 0) + 1
        return counts


def clone_grid(grid: list[list[int]]) -> list[list[int]]:
    return [row[:] for row in grid]


def summarize_results(results: list[dict[str, Any]], players: int) -> dict[str, Any]:
    if not results:
        return {}
    turns = [result["turns"] for result in results]
    rounds = [result["rounds"] for result in results]
    ended = [result["endedByTarget"] for result in results]
    wins = [0 for _ in range(players)]
    scores_by_player = [[] for _ in range(players)]
    rescued_by_player = [[] for _ in range(players)]
    card_counts: dict[str, int] = {}
    for result in results:
        for winner in result["winners"]:
            wins[winner] += 1
        for index, score in enumerate(result["scores"]):
            scores_by_player[index].append(score)
        for index, rescued in enumerate(result["rescued"]):
            rescued_by_player[index].append(rescued)
        for card_id, count in result["rescuedByCard"].items():
            card_counts[card_id] = card_counts.get(card_id, 0) + count

    return {
        "games": len(results),
        "endedByTargetPct": round(sum(1 for value in ended if value) / len(ended) * 100, 2),
        "turns": describe(turns),
        "rounds": describe(rounds),
        "wins": wins,
        "avgScoreByPlayer": [round(statistics.mean(values), 2) for values in scores_by_player],
        "avgRescuedByPlayer": [round(statistics.mean(values), 2) for values in rescued_by_player],
        "rescuedByCard": dict(sorted(card_counts.items(), key=lambda item: item[1], reverse=True)),
    }


def aggregate_scenarios(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    if not scenarios:
        return {}
    scenario_summaries = [scenario["summary"] for scenario in scenarios if scenario.get("summary")]
    if not scenario_summaries:
        return {}
    return {
        "scenarios": len(scenarios),
        "games": sum(summary.get("games", 0) for summary in scenario_summaries),
        "bestByAvgTurns": min(
            scenarios,
            key=lambda scenario: scenario.get("summary", {}).get("turns", {}).get("avg", 999999),
        )["label"],
        "slowestByAvgTurns": max(
            scenarios,
            key=lambda scenario: scenario.get("summary", {}).get("turns", {}).get("avg", -1),
        )["label"],
    }


def describe(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "avg": round(statistics.mean(ordered), 2),
        "median": round(statistics.median(ordered), 2),
        "p10": round(percentile(ordered, 0.10), 2),
        "p90": round(percentile(ordered, 0.90), 2),
        "min": round(min(ordered), 2),
        "max": round(max(ordered), 2),
    }


def percentile(sorted_values: list[float], ratio: float) -> float:
    if not sorted_values:
        return 0
    index = min(len(sorted_values) - 1, max(0, round((len(sorted_values) - 1) * ratio)))
    return sorted_values[index]


class Job:
    def __init__(self, config: dict[str, Any]):
        self.id = uuid.uuid4().hex[:10]
        self.config = merge_config(config)
        self.scenario_plan = expand_scenarios(self.config)
        self.status = "queued"
        self.created_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.progress = 0
        self.error = ""
        self.results: list[dict[str, Any]] = []
        self.scenarios: list[dict[str, Any]] = []
        self.summary: dict[str, Any] = {}

    def run(self) -> None:
        self.status = "running"
        self.started_at = time.time()
        try:
            seed_text = str(self.config.get("seed") or "").strip()
            base_seed = seed_text if seed_text else int(time.time() * 1000)
            total_runs = sum(scenario["config"]["games"] for scenario in self.scenario_plan)
            completed_runs = 0
            for scenario_index, scenario in enumerate(self.scenario_plan):
                scenario_config = scenario["config"]
                scenario_seed = str(base_seed) + "|" + scenario["id"]
                simulator = Simulator(scenario_config, scenario_seed)
                scenario_results: list[dict[str, Any]] = []
                for index in range(scenario_config["games"]):
                    record_timeline = (
                        bool(scenario_config.get("recordTimeline"))
                        and len(self.results) < self.config["maxRecordedGames"]
                    )
                    result = simulator.run_game(index + 1, record_timeline=record_timeline)
                    result["scenarioId"] = scenario["id"]
                    result["scenarioLabel"] = scenario["label"]
                    if "timeline" in result:
                        for step_index, step in enumerate(result["timeline"]):
                            step["step"] = step_index
                    scenario_results.append(result)
                    self.results.append(result)
                    completed_runs += 1
                    self.progress = int(completed_runs / max(1, total_runs) * 100)
                scenario_summary = summarize_results(scenario_results, scenario_config["players"])
                scenario_payload = {
                    "id": scenario["id"],
                    "label": scenario["label"],
                    "variables": scenario["variables"],
                    "config": {
                        "games": scenario_config["games"],
                        "players": scenario_config["players"],
                        "initialLiveCells": scenario_config["initialLiveCells"],
                        "rescueTarget": scenario_config["rescueTarget"],
                        "patternDeck": scenario_config["patternDeck"],
                    },
                    "summary": scenario_summary,
                }
                self.scenarios.append(scenario_payload)
            self.summary = aggregate_scenarios(self.scenarios)
            if len(self.scenarios) == 1:
                self.summary.update(self.scenarios[0]["summary"])
            self.status = "done"
        except Exception:
            self.error = traceback.format_exc()
            self.status = "error"
        finally:
            self.finished_at = time.time()

    def snapshot(self, include_results: bool = False) -> dict[str, Any]:
        data = {
            "id": self.id,
            "status": self.status,
            "progress": self.progress,
            "createdAt": self.created_at,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "config": self.config,
            "summary": self.summary,
            "scenarioCount": len(self.scenario_plan),
            "scenarios": self.scenarios,
            "error": self.error,
        }
        if include_results:
            data["results"] = self.results
        return data


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def start_job(config: dict[str, Any]) -> Job:
    job = Job(config)
    with JOBS_LOCK:
        JOBS[job.id] = job
    thread = threading.Thread(target=job.run, name=f"playtest-{job.id}", daemon=True)
    thread.start()
    return job


class Handler(BaseHTTPRequestHandler):
    server_version = "CardGOLPlaytest/0.1"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/":
            self.send_text(HTML, "text/html; charset=utf-8")
        elif path == "/api/default-config":
            decks = default_decks()
            config = dict(DEFAULT_CONFIG)
            config["patternDeck"] = decks["patternDeck"]
            config["actionDeck"] = decks["actionDeck"]
            self.send_json(config)
        elif path == "/api/cards":
            self.send_json(cards_payload())
        elif path == "/api/jobs":
            with JOBS_LOCK:
                jobs = [job.snapshot(False) for job in JOBS.values()]
            jobs.sort(key=lambda item: item["createdAt"], reverse=True)
            self.send_json({"jobs": jobs})
        elif path.startswith("/api/jobs/"):
            parts = path.strip("/").split("/")
            if len(parts) >= 3:
                job_id = parts[2]
                job = JOBS.get(job_id)
                if not job:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if len(parts) == 4 and parts[3] == "results.csv":
                    self.send_csv(job)
                else:
                    self.send_json(job.snapshot(True))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/jobs":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8")
        try:
            payload = json.loads(raw or "{}")
            job = start_job(payload.get("config") or payload)
        except Exception as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json(job.snapshot(False), HTTPStatus.CREATED)

    def send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text: str, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_csv(self, job: Job) -> None:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["scenarioId", "scenarioLabel", "game", "turns", "rounds", "endedByTarget", "winner", "scores", "rescued", "liveCellsLeft"])
        for result in job.results:
            writer.writerow([
                result.get("scenarioId", ""),
                result.get("scenarioLabel", ""),
                result["game"],
                result["turns"],
                result["rounds"],
                result["endedByTarget"],
                "" if result["winner"] is None else result["winner"] + 1,
                json.dumps(result["scores"]),
                json.dumps(result["rescued"]),
                result["liveCellsLeft"],
            ])
        self.send_text(output.getvalue(), "text/csv; charset=utf-8")

    def log_message(self, format: str, *args: Any) -> None:
        return


HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Card GOL Playtest</title>
  <style>
    :root { color-scheme: dark; font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body { margin: 0; background: #101418; color: #edf2f7; }
    main { max-width: 1180px; margin: 0 auto; padding: 24px; }
    header { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; margin-bottom: 18px; }
    h1 { margin: 0 0 6px; font-size: 30px; }
    p { margin: 0; color: #a7b1bd; }
    .grid { display: grid; grid-template-columns: minmax(320px, 440px) 1fr; gap: 18px; align-items: start; }
    section, .job { border: 1px solid #2a3440; background: #151b22; border-radius: 8px; padding: 16px; }
    label { display: grid; gap: 6px; margin: 0 0 12px; color: #cbd5df; font-size: 14px; }
    input, select, textarea, button { font: inherit; border-radius: 6px; border: 1px solid #354150; background: #0d1117; color: #edf2f7; padding: 9px 10px; }
    textarea { min-height: 290px; resize: vertical; line-height: 1.35; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; }
    button { cursor: pointer; font-weight: 700; background: #223044; }
    button.primary { background: #2f6f55; border-color: #3f8d6d; }
    button:disabled { opacity: .55; cursor: wait; }
    .row { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
    .checks { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 12px 0; }
    .checks label { display: flex; align-items: center; gap: 8px; margin: 0; }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }
    .jobs { display: grid; gap: 12px; }
    .job h3 { margin: 0 0 8px; display: flex; justify-content: space-between; gap: 12px; }
    .metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 12px 0; }
    .metric { background: #0d1117; border: 1px solid #28313d; border-radius: 6px; padding: 10px; }
    .metric b { display: block; font-size: 20px; }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #0d1117; border: 1px solid #28313d; border-radius: 6px; padding: 10px; max-height: 360px; overflow: auto; }
    progress { width: 100%; height: 12px; }
    a { color: #8fd6b3; }
    .viewer { margin-top: 16px; border-top: 1px solid #2a3440; padding-top: 16px; }
    .viewer-toolbar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 10px 0; }
    .viewer-layout { display: grid; grid-template-columns: minmax(300px, 520px) 1fr; gap: 14px; align-items: start; }
    .board { display: grid; grid-template-columns: repeat(20, minmax(10px, 1fr)); gap: 2px; background: #0b0f14; border: 1px solid #33404d; border-radius: 8px; padding: 8px; }
    .cell { aspect-ratio: 1; border-radius: 3px; background: #1f2833; border: 1px solid #2f3a46; }
    .cell.live { background: #f2f6fb; border-color: #ffffff; }
    .cell.added { box-shadow: inset 0 0 0 3px #71e0a3; }
    .cell.removed { background: #42202a; box-shadow: inset 0 0 0 3px #ff7d8a; }
    .cell.swapped { box-shadow: inset 0 0 0 3px #f4c95d; }
    .players { display: grid; gap: 12px; }
    .player { background: #0d1117; border: 1px solid #28313d; border-radius: 8px; padding: 10px; }
    .player h4 { margin: 0 0 8px; display: flex; justify-content: space-between; gap: 8px; }
    .hand { display: flex; flex-wrap: wrap; gap: 8px; }
    .mini-card { width: 86px; min-height: 124px; border: 1px solid #222; border-radius: 8px; overflow: hidden; background: #f8fafc; color: #111827; box-shadow: 0 3px 8px rgba(0,0,0,.25); display: flex; flex-direction: column; }
    .mini-card-header { background: #111; color: white; padding: 7px; font-size: 11px; font-weight: 800; min-height: 30px; display: flex; align-items: center; justify-content: space-between; gap: 4px; }
    .mini-card-sub { color: #526070; font-size: 9px; padding: 4px 7px 0; }
    .mini-card-grid { flex: 1; display: grid; place-items: center; padding: 6px; }
    .mini-card-footer { border-top: 1px solid #d8dee6; padding: 4px 7px; font-size: 10px; display: flex; justify-content: space-between; }
    .mini-card.action .mini-card-header { background: #263241; }
    .mini-card svg { max-width: 100%; height: auto; }
    .timeline-list { max-height: 180px; overflow: auto; display: grid; gap: 6px; margin-top: 10px; }
    .timeline-list button { text-align: left; font-weight: 500; padding: 7px 9px; }
    .timeline-list button.active { background: #2f6f55; border-color: #3f8d6d; }
    .event-box { background: #0d1117; border: 1px solid #28313d; border-radius: 8px; padding: 10px; margin-bottom: 10px; }
    .sweep-panel { margin: 12px 0; padding: 12px; border: 1px solid #28313d; border-radius: 8px; background: #0d1117; }
    .sweep-panel h3 { margin: 0 0 10px; }
    .scenario-table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }
    .scenario-table th, .scenario-table td { border: 1px solid #28313d; padding: 7px; text-align: left; vertical-align: top; }
    .scenario-table th { background: #0d1117; }
    .deck-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 4px; margin-top: 6px; color: #cbd5df; font-size: 12px; }
    @media (max-width: 860px) { .grid { grid-template-columns: 1fr; } .metrics, .row, .checks { grid-template-columns: 1fr; } header { display: block; } }
    @media (max-width: 860px) { .viewer-layout { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Card GOL Playtest</h1>
      <p>Simule partidas em background, altere regras e compare composicoes do baralho.</p>
    </div>
    <button id="refresh">Atualizar jobs</button>
  </header>

  <div class="grid">
    <section>
      <h2>Nova simulacao</h2>
      <div class="row">
        <label>Partidas <input id="games" type="number" min="1" value="500"></label>
        <label>Jogadores <select id="players"><option>2</option><option>3</option><option>4</option></select></label>
      </div>
      <div class="row">
        <label>Seed <input id="seed" placeholder="vazio = aleatorio"></label>
        <label>Max turnos <input id="maxTurns" type="number" min="1" value="300"></label>
      </div>
      <div class="row">
        <label>Celulas iniciais <input id="initialLiveCells" type="number" min="0" value="12"></label>
        <label>Alvo de resgates <input id="rescueTarget" type="number" min="1" value="5"></label>
      </div>
      <div class="row">
        <label>Partidas visuais <input id="maxRecordedGames" type="number" min="0" value="20"></label>
        <label>Replay <select id="recordTimeline"><option value="true">Gravar timeline</option><option value="false">So agregados</option></select></label>
      </div>
      <div class="checks">
        <label><input id="offTurnRescue" type="checkbox" checked> Resgate fora do turno</label>
        <label><input id="allowOverlappingPatterns" type="checkbox" checked> Padroes podem sobrepor</label>
        <label><input id="wrapPatterns" type="checkbox"> Padroes atravessam borda</label>
        <label><input id="finishActiveTurnOnly" type="checkbox" checked> Finaliza turno ativo</label>
      </div>
      <div class="sweep-panel">
        <h3>Combinatoria de balanceamento</h3>
        <label><input id="sweepEnabled" type="checkbox"> Ativar varredura de variaveis</label>
        <div class="row">
          <label>Jogadores <input id="sweepPlayers" placeholder="2,3,4"></label>
          <label>Celulas iniciais <input id="sweepCells" placeholder="8,12,16,20"></label>
        </div>
        <div class="row">
          <label>Resgates para ganhar <input id="sweepTargets" placeholder="4,5,6"></label>
          <label>Max cenarios <input id="sweepMaxScenarios" type="number" min="1" value="120"></label>
        </div>
        <div class="checks">
          <label><input class="deckMode" type="checkbox" value="current" checked> Deck atual</label>
          <label><input class="deckMode" type="checkbox" value="without_each"> Sem cada padrao</label>
          <label><input class="deckMode" type="checkbox" value="favor_each"> Mais cada padrao</label>
          <label><input class="deckMode" type="checkbox" value="reduce_high"> Reduz maiores</label>
          <label><input class="deckMode" type="checkbox" value="uniform"> Uniforme</label>
          <label><input class="deckMode" type="checkbox" value="count_grid"> Todos x1/x2/x4/x6/x8</label>
        </div>
      </div>
      <label>Config JSON
        <textarea id="configJson"></textarea>
      </label>
      <div class="actions">
        <button class="primary" id="run">Rodar simulacao</button>
        <button id="loadDefault">Recarregar padrao atual</button>
      </div>
    </section>

    <section>
      <h2>Resultados</h2>
      <div id="jobs" class="jobs"></div>
      <div class="viewer" id="viewer" hidden>
        <h2>Replay visual</h2>
        <div class="viewer-toolbar">
          <label>Partida <select id="gameSelect"></select></label>
          <button id="prevStep">Anterior</button>
          <button id="playPause">Play</button>
          <button id="nextStep">Proximo</button>
          <label>Passo <input id="stepSlider" type="range" min="0" max="0" value="0"></label>
        </div>
        <div class="event-box" id="eventBox">Selecione uma partida gravada.</div>
        <div class="viewer-layout">
          <div>
            <div id="board" class="board"></div>
            <div id="timelineList" class="timeline-list"></div>
          </div>
          <div id="playersView" class="players"></div>
        </div>
      </div>
    </section>
  </div>
</main>
<script>
let latestConfig = null;
let cardsData = null;
let selectedJob = null;
let selectedGame = null;
let selectedStep = 0;
let playTimer = null;

async function getJson(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function syncFormToConfig() {
  const config = JSON.parse(document.getElementById('configJson').value || '{}');
  config.games = Number(document.getElementById('games').value || 1);
  config.players = Number(document.getElementById('players').value || 2);
  config.seed = document.getElementById('seed').value;
  config.maxTurns = Number(document.getElementById('maxTurns').value || 300);
  config.initialLiveCells = Number(document.getElementById('initialLiveCells').value || 12);
  config.rescueTarget = Number(document.getElementById('rescueTarget').value || 5);
  config.maxRecordedGames = Number(document.getElementById('maxRecordedGames').value || 0);
  config.recordTimeline = document.getElementById('recordTimeline').value === 'true';
  config.offTurnRescue = document.getElementById('offTurnRescue').checked;
  config.allowOverlappingPatterns = document.getElementById('allowOverlappingPatterns').checked;
  config.wrapPatterns = document.getElementById('wrapPatterns').checked;
  config.finishActiveTurnOnly = document.getElementById('finishActiveTurnOnly').checked;
  config.sweep = {
    enabled: document.getElementById('sweepEnabled').checked,
    players: parseNumberList(document.getElementById('sweepPlayers').value),
    initialLiveCells: parseNumberList(document.getElementById('sweepCells').value),
    rescueTarget: parseNumberList(document.getElementById('sweepTargets').value),
    maxScenarios: Number(document.getElementById('sweepMaxScenarios').value || 120),
    patternDeckModes: Array.from(document.querySelectorAll('.deckMode:checked')).map((input) => input.value)
  };
  return config;
}

function applyConfig(config) {
  latestConfig = config;
  document.getElementById('games').value = config.games;
  document.getElementById('players').value = config.players;
  document.getElementById('seed').value = config.seed || '';
  document.getElementById('maxTurns').value = config.maxTurns;
  document.getElementById('initialLiveCells').value = config.initialLiveCells;
  document.getElementById('rescueTarget').value = config.rescueTarget;
  document.getElementById('maxRecordedGames').value = config.maxRecordedGames ?? 20;
  document.getElementById('recordTimeline').value = String(config.recordTimeline !== false);
  document.getElementById('offTurnRescue').checked = Boolean(config.offTurnRescue);
  document.getElementById('allowOverlappingPatterns').checked = Boolean(config.allowOverlappingPatterns);
  document.getElementById('wrapPatterns').checked = Boolean(config.wrapPatterns);
  document.getElementById('finishActiveTurnOnly').checked = Boolean(config.finishActiveTurnOnly);
  const sweep = config.sweep || {};
  document.getElementById('sweepEnabled').checked = Boolean(sweep.enabled);
  document.getElementById('sweepPlayers').value = (sweep.players || []).join(',');
  document.getElementById('sweepCells').value = (sweep.initialLiveCells || []).join(',');
  document.getElementById('sweepTargets').value = (sweep.rescueTarget || []).join(',');
  document.getElementById('sweepMaxScenarios').value = sweep.maxScenarios || 120;
  const modes = new Set(sweep.patternDeckModes || ['current']);
  document.querySelectorAll('.deckMode').forEach((input) => {
    input.checked = modes.has(input.value);
  });
  document.getElementById('configJson').value = JSON.stringify(config, null, 2);
}

function parseNumberList(value) {
  return String(value || '')
    .split(',')
    .map((part) => Number(part.trim()))
    .filter((value) => Number.isFinite(value));
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  }[char]));
}

function metric(label, value) {
  return `<div class="metric"><span>${label}</span><b>${value}</b></div>`;
}

function renderDeckCounts(counts, limit = 20) {
  const entries = Object.entries(counts || {});
  if (!entries.length) return '-';
  return `<div class="deck-list">${entries.slice(0, limit).map(([key, value]) => `<span>${escapeHtml(key)}: ${value}</span>`).join('')}</div>`;
}

function renderScenarioTable(job) {
  const scenarios = job.scenarios || [];
  if (!scenarios.length) return '';
  return `<table class="scenario-table">
    <thead>
      <tr>
        <th>Cenario</th>
        <th>Vars</th>
        <th>Turnos</th>
        <th>Padroes disponiveis</th>
        <th>Padroes usados</th>
      </tr>
    </thead>
    <tbody>
      ${scenarios.map((scenario) => {
        const summary = scenario.summary || {};
        const turns = summary.turns || {};
        const vars = scenario.variables || {};
        return `<tr>
          <td>${escapeHtml(scenario.label)}</td>
          <td>P${vars.players}, cel ${vars.initialLiveCells}, alvo ${vars.rescueTarget}<br>${escapeHtml(vars.patternDeckLabel || '')}</td>
          <td>avg ${turns.avg ?? '-'}<br>med ${turns.median ?? '-'}<br>p90 ${turns.p90 ?? '-'}</td>
          <td>Total ${vars.patternDeckTotal ?? '-'}${renderDeckCounts(vars.patternDeckCounts, 12)}</td>
          <td>${renderDeckCounts(summary.rescuedByCard, 12)}</td>
        </tr>`;
      }).join('')}
    </tbody>
  </table>`;
}

function renderJob(job) {
  const summary = job.summary || {};
  const turns = summary.turns || {};
  const rounds = summary.rounds || {};
  const wins = summary.wins || [];
  const cards = summary.rescuedByCard || {};
  const cardLines = Object.entries(cards).slice(0, 12).map(([key, value]) => `${key}: ${value}`).join('\n');
  const scenarioNote = job.scenarioCount && job.scenarioCount > 1
    ? `<p>Cenarios: ${job.scenarioCount} | Jogos totais: ${summary.games || '-'}</p>`
    : '';
  return `<article class="job">
    <h3><span>${job.id} - ${job.status}</span><span>${job.progress || 0}%</span></h3>
    <progress value="${job.progress || 0}" max="100"></progress>
    <div class="metrics">
      ${metric('Partidas', summary.games || job.config.games)}
      ${metric('Turnos med.', turns.avg ?? '-')}
      ${metric('Rodadas med.', rounds.avg ?? '-')}
      ${metric('Fim por alvo', summary.endedByTargetPct != null ? summary.endedByTargetPct + '%' : '-')}
    </div>
    ${scenarioNote}
    <p>Vitorias: ${wins.map((v, i) => `P${i + 1}: ${v}`).join(' | ') || '-'}</p>
    <p>Score medio: ${(summary.avgScoreByPlayer || []).map((v, i) => `P${i + 1}: ${v}`).join(' | ') || '-'}</p>
    ${job.error ? `<pre>${job.error}</pre>` : ''}
    ${cardLines ? `<pre>Padroes resgatados\n${cardLines}</pre>` : ''}
    ${renderScenarioTable(job)}
    <div class="actions">
      <button onclick="loadJobReplay('${job.id}')">Visualizar partidas</button>
      <a href="/api/jobs/${job.id}/results.csv">Baixar CSV</a>
      <a href="/api/jobs/${job.id}" target="_blank">JSON completo</a>
    </div>
  </article>`;
}

async function refreshJobs() {
  const data = await getJson('/api/jobs');
  document.getElementById('jobs').innerHTML = data.jobs.length
    ? data.jobs.map(renderJob).join('')
    : '<p>Nenhuma simulacao ainda.</p>';
}

async function loadDefault() {
  const config = await getJson('/api/default-config');
  applyConfig(config);
}

async function loadCardsData() {
  cardsData = await getJson('/api/cards');
}

function buildTinyGridSvg(pattern, mode = 'pattern') {
  if (!pattern || !pattern.length || !pattern[0].length) return '';
  const rows = pattern.length;
  const cols = pattern[0].length;
  const cell = 12;
  const width = cols * cell;
  const height = rows * cell;
  let svg = `<svg viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg">`;
  svg += `<rect width="${width}" height="${height}" rx="4" fill="#f8fafc"/>`;
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const v = pattern[r][c];
      svg += `<rect x="${c * cell}" y="${r * cell}" width="${cell}" height="${cell}" fill="none" stroke="#cbd5e1" stroke-width="1"/>`;
      if (v === 1) {
        svg += `<rect x="${c * cell + 2}" y="${r * cell + 2}" width="${cell - 4}" height="${cell - 4}" rx="2" fill="#111827"/>`;
      } else if (v === 2) {
        const fill = mode === 'remove' ? '#fca5a5' : '#86efac';
        svg += `<rect x="${c * cell + 2}" y="${r * cell + 2}" width="${cell - 4}" height="${cell - 4}" rx="2" fill="${fill}" opacity=".9"/>`;
      }
    }
  }
  svg += '</svg>';
  return svg;
}

function cardInfo(cardId, kindHint = '') {
  const patternCard = cardsData?.patternCards?.[cardId];
  if (patternCard) {
    return {
      kind: 'pattern',
      title: patternCard.name || cardId,
      sub: `${patternCard.label || ''}${patternCard.category ? ' - ' + patternCard.category : ''}`,
      footer: `${patternCard.phase}/${patternCard.phaseTotal}${patternCard.mirrored ? ' mirror' : ''}`,
      svg: buildTinyGridSvg(patternCard.pattern, 'pattern')
    };
  }
  const pattern = cardsData?.patterns?.[cardId];
  if (pattern) {
    return {
      kind: 'pattern',
      title: pattern.name || cardId,
      sub: pattern.category || 'Pattern',
      footer: `${(pattern.pattern || []).flat().filter(Boolean).length} celulas`,
      svg: buildTinyGridSvg(pattern.pattern, 'pattern')
    };
  }
  const action = (cardsData?.actions || []).find((item) => item.id === cardId);
  if (action) {
    const labels = { add: 'Adicionar', remove: 'Remover', swap: 'Trocar', clear: 'Limpar' };
    return {
      kind: 'action',
      title: labels[action.mode] || action.mode || 'Acao',
      sub: cardId,
      footer: action.uses ? `x${action.uses}` : kindHint || 'Acao',
      svg: action.layout === 'swap'
        ? buildTinyGridSvg([[1,0,0],[0,0,0],[0,0,1]], 'pattern')
        : action.layout === 'clear'
          ? buildTinyGridSvg([[1,1,1],[1,1,1],[1,1,1]], 'remove')
          : buildTinyGridSvg(action.pattern, action.mode)
    };
  }
  return { kind: kindHint || 'pattern', title: cardId, sub: 'Carta', footer: '', svg: '' };
}

function renderMiniCard(cardId, kindHint = '') {
  const info = cardInfo(cardId, kindHint);
  return `<div class="mini-card ${info.kind === 'action' ? 'action' : ''}" title="${escapeHtml(cardId)}">
    <div class="mini-card-header"><span>${escapeHtml(info.title)}</span></div>
    <div class="mini-card-sub">${escapeHtml(info.sub)}</div>
    <div class="mini-card-grid">${info.svg}</div>
    <div class="mini-card-footer"><span>${escapeHtml(info.footer)}</span><span>${escapeHtml(cardId)}</span></div>
  </div>`;
}

async function loadJobReplay(jobId) {
  selectedJob = await getJson(`/api/jobs/${jobId}`);
  const recordedGames = (selectedJob.results || []).filter((game) => Array.isArray(game.timeline) && game.timeline.length);
  const viewer = document.getElementById('viewer');
  viewer.hidden = false;
  const gameSelect = document.getElementById('gameSelect');
  if (!recordedGames.length) {
    gameSelect.innerHTML = '';
    document.getElementById('eventBox').textContent = 'Este job nao gravou timeline. Rode com "Gravar timeline" e Partidas visuais maior que 0.';
    document.getElementById('board').innerHTML = '';
    document.getElementById('playersView').innerHTML = '';
    document.getElementById('timelineList').innerHTML = '';
    return;
  }
  gameSelect.innerHTML = recordedGames.map((game) => {
    const resultIndex = (selectedJob.results || []).indexOf(game);
    return `<option value="${resultIndex}">${escapeHtml(game.scenarioLabel || 'Cenario')} | Partida ${game.game} - ${game.turns} turnos</option>`;
  }).join('');
  selectGame(Number(gameSelect.value));
  viewer.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function selectGame(gameNumber) {
  selectedGame = (selectedJob?.results || [])[Number(gameNumber)];
  selectedStep = 0;
  const max = Math.max(0, (selectedGame?.timeline?.length || 1) - 1);
  const slider = document.getElementById('stepSlider');
  slider.max = String(max);
  slider.value = '0';
  renderSelectedStep();
}

function changedCellMap(step) {
  const map = new Map();
  (step.changed || []).forEach((cell) => {
    const cls = cell.reason === 'swap' ? 'swapped' : cell.after ? 'added' : 'removed';
    map.set(`${cell.row},${cell.col}`, cls);
  });
  return map;
}

function renderBoard(step) {
  const changed = changedCellMap(step);
  const cells = [];
  for (let r = 0; r < step.grid.length; r++) {
    for (let c = 0; c < step.grid[r].length; c++) {
      const classes = ['cell'];
      if (step.grid[r][c]) classes.push('live');
      const changedClass = changed.get(`${r},${c}`);
      if (changedClass) classes.push(changedClass);
      cells.push(`<div class="${classes.join(' ')}" title="${r},${c}"></div>`);
    }
  }
  document.getElementById('board').innerHTML = cells.join('');
}

function renderPlayers(step) {
  document.getElementById('playersView').innerHTML = (step.players || []).map((player, index) => `
    <article class="player">
      <h4><span>P${index + 1}${step.activePlayer === index ? ' - ativo' : ''}</span><span>${player.score} pts | ${player.rescued.length} resgates</span></h4>
      <p>Mao</p>
      <div class="hand">${player.hand.map((card) => renderMiniCard(card, 'pattern')).join('') || '<p>Sem cartas</p>'}</div>
      <p style="margin-top:8px">Resgatadas</p>
      <div class="hand">${player.rescued.map((card) => renderMiniCard(card, 'pattern')).join('') || '<p>Nenhuma</p>'}</div>
    </article>
  `).join('');
}

function renderEvent(step) {
  const actionCards = (step.actionDraw || []).map((card) => renderMiniCard(card, 'action')).join('');
  const changedSummary = (step.changed || []).length
    ? `${step.changed.length} celulas alteradas`
    : 'sem mudanca no grid';
  document.getElementById('eventBox').innerHTML = `
    <b>Passo ${step.step} | Turno ${step.turn + 1} | ${escapeHtml(step.kind)}</b>
    <p>${escapeHtml(step.message)}</p>
    <p>${changedSummary} | celulas vivas: ${step.liveCells}</p>
    <p>Acoes: deck ${step.decks?.actionDeck ?? '-'} / descarte ${step.decks?.actionDiscard ?? '-'} | Padroes: deck ${step.decks?.patternDeck ?? '-'} / descarte ${step.decks?.patternDiscard ?? '-'}</p>
    ${actionCards ? `<div class="hand" style="margin-top:8px">${actionCards}</div>` : ''}
  `;
}

function renderTimelineList() {
  const timeline = selectedGame?.timeline || [];
  document.getElementById('timelineList').innerHTML = timeline.map((step, index) => `
    <button class="${index === selectedStep ? 'active' : ''}" onclick="goToStep(${index})">
      ${index}. T${step.turn + 1} - ${escapeHtml(step.message)}
    </button>
  `).join('');
}

function renderSelectedStep() {
  const timeline = selectedGame?.timeline || [];
  if (!timeline.length) return;
  selectedStep = Math.max(0, Math.min(selectedStep, timeline.length - 1));
  const step = timeline[selectedStep];
  document.getElementById('stepSlider').value = String(selectedStep);
  renderEvent(step);
  renderBoard(step);
  renderPlayers(step);
  renderTimelineList();
}

function goToStep(index) {
  selectedStep = index;
  renderSelectedStep();
}

function stopPlayback() {
  if (playTimer) {
    clearInterval(playTimer);
    playTimer = null;
  }
  document.getElementById('playPause').textContent = 'Play';
}

function togglePlayback() {
  if (playTimer) {
    stopPlayback();
    return;
  }
  document.getElementById('playPause').textContent = 'Pausar';
  playTimer = setInterval(() => {
    const max = (selectedGame?.timeline?.length || 1) - 1;
    if (selectedStep >= max) {
      stopPlayback();
      return;
    }
    selectedStep += 1;
    renderSelectedStep();
  }, 650);
}

document.getElementById('run').addEventListener('click', async () => {
  const button = document.getElementById('run');
  button.disabled = true;
  try {
    const config = syncFormToConfig();
    document.getElementById('configJson').value = JSON.stringify(config, null, 2);
    await getJson('/api/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config })
    });
    await refreshJobs();
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
  }
});

document.getElementById('refresh').addEventListener('click', refreshJobs);
document.getElementById('loadDefault').addEventListener('click', loadDefault);
document.getElementById('gameSelect').addEventListener('change', (event) => selectGame(Number(event.target.value)));
document.getElementById('stepSlider').addEventListener('input', (event) => goToStep(Number(event.target.value)));
document.getElementById('prevStep').addEventListener('click', () => goToStep(selectedStep - 1));
document.getElementById('nextStep').addEventListener('click', () => goToStep(selectedStep + 1));
document.getElementById('playPause').addEventListener('click', togglePlayback);

Promise.all([loadCardsData(), loadDefault()]).then(refreshJobs);
setInterval(refreshJobs, 1500);
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Card GOL playtest web simulator.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind. Default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind. Default: 8765")
    args = parser.parse_args()

    host = args.host
    port = args.port
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Card GOL Playtest running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


if __name__ == "__main__":
    main()
