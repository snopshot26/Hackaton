#!/usr/bin/env python3
"""DatsSol competitive bot.

Environment variables:
- DATSSOL_TOKEN (required)
- DATSSOL_BASE_URL (default: https://games-test.datsteam.dev)
- DATSSOL_ARENA_PATH (default: /api/arena)
- DATSSOL_COMMAND_PATH (default: /api/command)
- DATSSOL_LOGS_PATH (default: /api/logs)
- DATSSOL_POLL_INTERVAL (default: 0.15)
- DATSSOL_TIMEOUT (default: 5.0)
- DATSSOL_DRY_RUN (1/0, default: 0)
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import requests

Coord = Tuple[int, int]


@dataclass
class Config:
    token: str
    base_url: str = "https://games-test.datsteam.dev"
    arena_path: str = "/api/arena"
    command_path: str = "/api/command"
    logs_path: str = "/api/logs"
    poll_interval: float = 0.15
    timeout: float = 5.0
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("DATSSOL_TOKEN", "").strip()
        if not token:
            raise ValueError("DATSSOL_TOKEN is required")
        return cls(
            token=token,
            base_url=os.getenv("DATSSOL_BASE_URL", "https://games-test.datsteam.dev").rstrip("/"),
            arena_path=os.getenv("DATSSOL_ARENA_PATH", "/api/arena"),
            command_path=os.getenv("DATSSOL_COMMAND_PATH", "/api/command"),
            logs_path=os.getenv("DATSSOL_LOGS_PATH", "/api/logs"),
            poll_interval=float(os.getenv("DATSSOL_POLL_INTERVAL", "0.15")),
            timeout=float(os.getenv("DATSSOL_TIMEOUT", "5.0")),
            dry_run=os.getenv("DATSSOL_DRY_RUN", "0") == "1",
        )


class ApiClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers.update(
            {
                "X-Auth-Token": cfg.token,
                "Content-Type": "application/json",
            }
        )

    def get_arena(self) -> Dict:
        url = f"{self.cfg.base_url}{self.cfg.arena_path}"
        r = self.s.get(url, timeout=self.cfg.timeout)
        r.raise_for_status()
        return r.json()

    def send_command(self, payload: Dict) -> Dict:
        if self.cfg.dry_run:
            return {"code": 0, "errors": ["dry_run enabled"], "payload": payload}
        url = f"{self.cfg.base_url}{self.cfg.command_path}"
        r = self.s.post(url, data=json.dumps(payload), timeout=self.cfg.timeout)
        r.raise_for_status()
        return r.json()

    def get_logs(self) -> Dict:
        url = f"{self.cfg.base_url}{self.cfg.logs_path}"
        r = self.s.get(url, timeout=self.cfg.timeout)
        r.raise_for_status()
        return r.json()


# ---------- Geometry helpers ----------


def cheb(a: Coord, b: Coord) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def manhattan(a: Coord, b: Coord) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def neighbors4(c: Coord) -> Iterable[Coord]:
    x, y = c
    yield (x + 1, y)
    yield (x - 1, y)
    yield (x, y + 1)
    yield (x, y - 1)


def in_bounds(c: Coord, size: Sequence[int]) -> bool:
    return 0 <= c[0] < size[0] and 0 <= c[1] < size[1]


# ---------- Strategy ----------


UPGRADE_PRIORITY = [
    "repair_power",
    "signal_range",
    "settlement_limit",
    "max_hp",
    "vision_range",
    "earthquake_mitigation",
    "beaver_damage_mitigation",
    "decay_mitigation",
]


class SmartBot:
    def __init__(self, client: ApiClient):
        self.client = client
        self.last_turn: Optional[int] = None

    def run_forever(self) -> None:
        while True:
            try:
                arena = self.client.get_arena()
                turn_no = int(arena.get("turnNo", -1))
                if turn_no != self.last_turn:
                    payload = self.plan_turn(arena)
                    result = self.client.send_command(payload)
                    self.last_turn = turn_no
                    print(f"turn={turn_no} payload={payload} response={result}")
                sleep_for = float(arena.get("nextTurnIn", self.client.cfg.poll_interval))
                time.sleep(max(self.client.cfg.poll_interval, sleep_for * 0.2))
            except requests.HTTPError as e:
                print(f"HTTP error: {e}")
                time.sleep(0.5)
            except requests.RequestException as e:
                print(f"Network error: {e}")
                time.sleep(0.5)
            except Exception as e:  # keep bot alive in finals
                print(f"Unexpected error: {e}")
                time.sleep(0.5)

    def plan_turn(self, arena: Dict) -> Dict:
        plantations = arena.get("plantations", [])
        enemy = arena.get("enemy", [])
        beavers = arena.get("beavers", [])
        constructions = arena.get("construction", [])
        mountains = {tuple(m) for m in arena.get("mountains", [])}
        size = arena.get("size", [800, 800])

        own_pos = [tuple(p["position"]) for p in plantations]
        own_by_pos = {tuple(p["position"]): p for p in plantations}
        enemy_pos = {tuple(e["position"]): e for e in enemy}
        beaver_pos = {tuple(b["position"]): b for b in beavers}
        construction_pos = {tuple(c["position"]): c for c in constructions}

        occupied: Set[Coord] = set(own_pos) | set(enemy_pos) | set(construction_pos) | set(beaver_pos) | mountains

        payload: Dict[str, object] = {}

        upgrade = self.choose_upgrade(arena.get("plantationUpgrades", {}))
        if upgrade:
            payload["plantationUpgrade"] = upgrade

        commands: List[Dict] = []
        used_authors: Set[Coord] = set()

        # 1) Critical repairs first.
        low_hp_targets = sorted(
            [p for p in plantations if int(p.get("hp", 0)) <= 20 and not p.get("isMain", False)],
            key=lambda p: p.get("hp", 0),
        )
        for target in low_hp_targets:
            tpos = tuple(target["position"])
            author = self.find_repair_author(plantations, tpos, used_authors, arena.get("actionRange", 2))
            if author:
                used_authors.add(author)
                commands.append({"path": [list(author), list(author), list(tpos)]})

        # 2) Safe expansion towards high-value cells and connected lanes.
        free_builders = [tuple(p["position"]) for p in plantations if tuple(p["position"]) not in used_authors]
        random.shuffle(free_builders)
        for author in free_builders:
            candidate = self.best_build_cell(author, occupied, size, arena.get("actionRange", 2))
            if candidate is None:
                continue
            used_authors.add(author)
            occupied.add(candidate)
            commands.append({"path": [list(author), list(author), list(candidate)]})
            if len(commands) >= max(2, len(plantations) // 3):
                break

        # 3) PvP and beavers by leftover plantations.
        attackers = [tuple(p["position"]) for p in plantations if tuple(p["position"]) not in used_authors]
        for author in attackers:
            target = self.best_attack_target(author, enemy, beavers, arena.get("actionRange", 2))
            if not target:
                continue
            used_authors.add(author)
            commands.append({"path": [list(author), list(author), list(target)]})

        # Optional relocate: only if main isolated and adjacent healthy node exists.
        relocate = self.choose_relocate(plantations)
        if relocate:
            payload["relocateMain"] = [list(relocate[0]), list(relocate[1])]

        if commands:
            payload["command"] = commands

        # Never send empty payload: fallback to one likely-valid build.
        if not payload:
            fallback = self.fallback_action(plantations, occupied, size, arena.get("actionRange", 2))
            if fallback:
                payload["command"] = [{"path": [list(fallback[0]), list(fallback[0]), list(fallback[1])]}]
            else:
                # Absolute final fallback: request logs-side-effect upgrade attempt stub (empty string is ignored by server).
                payload["plantationUpgrade"] = "repair_power"

        return payload

    def choose_upgrade(self, upgrades: Dict) -> Optional[str]:
        if int(upgrades.get("points", 0)) <= 0:
            return None
        tiers = {t.get("name"): t for t in upgrades.get("tiers", [])}
        for name in UPGRADE_PRIORITY:
            tier = tiers.get(name)
            if not tier:
                continue
            if int(tier.get("current", 0)) < int(tier.get("max", 0)):
                return name
        return None

    def find_repair_author(
        self,
        plantations: Sequence[Dict],
        target: Coord,
        used_authors: Set[Coord],
        action_range: int,
    ) -> Optional[Coord]:
        candidates = []
        for p in plantations:
            apos = tuple(p["position"])
            if apos == target or apos in used_authors:
                continue
            if cheb(apos, target) <= int(action_range):
                hp = int(p.get("hp", 0))
                if hp > 20:
                    candidates.append((manhattan(apos, target), -hp, apos))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][2]

    def best_build_cell(
        self,
        author: Coord,
        occupied: Set[Coord],
        size: Sequence[int],
        action_range: int,
    ) -> Optional[Coord]:
        ar = int(action_range)
        candidates: List[Tuple[float, Coord]] = []
        for dx in range(-ar, ar + 1):
            for dy in range(-ar, ar + 1):
                c = (author[0] + dx, author[1] + dy)
                if not in_bounds(c, size) or c in occupied:
                    continue
                bonus = 1.0 if (c[0] % 7 == 0 and c[1] % 7 == 0) else 0.0
                edge_penalty = 0.25 if c[0] in (0, size[0] - 1) or c[1] in (0, size[1] - 1) else 0.0
                distance_cost = 0.03 * cheb(author, c)
                score = 2.0 + bonus - edge_penalty - distance_cost
                candidates.append((score, c))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    def best_attack_target(
        self,
        author: Coord,
        enemies: Sequence[Dict],
        beavers: Sequence[Dict],
        action_range: int,
    ) -> Optional[Coord]:
        ar = int(action_range)
        scored: List[Tuple[float, Coord]] = []

        for e in enemies:
            t = tuple(e["position"])
            if cheb(author, t) <= ar:
                hp = int(e.get("hp", 50))
                # Lower hp => higher score (likely secure kill).
                score = 3.0 + (55 - min(hp, 55)) * 0.04 - 0.02 * cheb(author, t)
                scored.append((score, t))

        for b in beavers:
            t = tuple(b["position"])
            if cheb(author, t) <= ar:
                hp = int(b.get("hp", 100))
                score = 2.5 + (105 - min(hp, 105)) * 0.02 - 0.02 * cheb(author, t)
                scored.append((score, t))

        if not scored:
            return None
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]

    def choose_relocate(self, plantations: Sequence[Dict]) -> Optional[Tuple[Coord, Coord]]:
        main = None
        own_pos = {tuple(p["position"]) for p in plantations}
        for p in plantations:
            if p.get("isMain", False):
                main = p
                break
        if not main:
            return None
        if not main.get("isIsolated", False):
            return None
        mpos = tuple(main["position"])
        for n in neighbors4(mpos):
            if n in own_pos:
                return mpos, n
        return None

    def fallback_action(
        self,
        plantations: Sequence[Dict],
        occupied: Set[Coord],
        size: Sequence[int],
        action_range: int,
    ) -> Optional[Tuple[Coord, Coord]]:
        if not plantations:
            return None
        # Prefer main as fallback author.
        author = tuple(plantations[0]["position"])
        for p in plantations:
            if p.get("isMain", False):
                author = tuple(p["position"])
                break
        target = self.best_build_cell(author, occupied, size, action_range)
        if target is None:
            return None
        return author, target


def main() -> None:
    cfg = Config.from_env()
    client = ApiClient(cfg)
    bot = SmartBot(client)
    bot.run_forever()


if __name__ == "__main__":
    main()
