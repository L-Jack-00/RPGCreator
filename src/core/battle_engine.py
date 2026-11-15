# -*- coding: utf-8 -*-
"""Modular RMXP battle rules: hit/crit evaluation, elements, states, enemy AI, and troop setup."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover
    from .battle_formulas import BattleFormula

RMXP_ELEMENT_MULTS: Dict[int, float] = {1: 2.0, 2: 1.5, 3: 1.0, 4: 0.5, 5: 0.0, 6: -1.0}


def evaluate_attack_roll(
    attacker_dex: int,
    skill_hit: int,
    defender_agi: int,
    defender_eva: int,
    roll_pct: int,
    is_physical: bool = True,
) -> Tuple[bool, bool]:
    """Deterministically computes (hit_success, is_critical) from RMXP hit/eva/dex parameters."""
    _ = defender_agi
    hit_chance = max(5, min(100, int(skill_hit) - max(0, int(defender_eva) - int(attacker_dex) // 4)))
    if int(defender_eva) > 0 and roll_pct >= hit_chance:
        return False, False
    crit_chance = max(0, min(95, (int(attacker_dex) - 10) // 2)) if is_physical else 0
    is_crit = is_physical and crit_chance > 0 and (roll_pct < crit_chance)
    return True, is_crit


def apply_variance(base_value: int, variance: int, seed_val: int = 0) -> int:
    """Applies deterministic RMXP percentage variance [-variance%, +variance%] around base_value."""
    if base_value <= 0 or variance <= 0:
        return base_value
    amp = (base_value * min(100, max(0, int(variance)))) // 100
    if amp <= 0:
        return base_value
    offset = (abs(int(seed_val)) % (2 * amp + 1)) - amp
    return max(1, base_value + offset)


def compute_element_multiplier(target_fields: Dict[str, Any], element_set: List[int]) -> float:
    """Computes strongest RMXP element_ranks multiplier (A=2.0..F=-1.0) for a skill's elements."""
    if not element_set:
        return 1.0
    ranks = target_fields.get("element_ranks", {}).get("data", [])
    if not ranks:
        return 1.0
    best = 1.0
    has_hit = False
    for elem_id in element_set:
        eid = int(elem_id)
        rank = int(ranks[eid]) if 0 <= eid < len(ranks) else 3
        mult = RMXP_ELEMENT_MULTS.get(rank, 1.0)
        if not has_hit or mult > best:
            best = mult
            has_hit = True
    return best


def apply_state_sets(
    target_states: List[int],
    plus_ids: List[Any],
    minus_ids: List[Any],
    guard_states: Optional[List[Any]] = None,
) -> None:
    """Applies RMXP state additions (respecting guard_states) and removals in place."""
    guarded = {int(s) for s in (guard_states or []) if int(s) > 0}
    for sid in plus_ids:
        isid = int(sid)
        if isid > 0 and isid not in guarded and isid not in target_states:
            target_states.append(isid)
    for sid in minus_ids:
        isid = int(sid)
        if isid in target_states:
            target_states.remove(isid)


def compute_guard_element_multiplier(element_set: List[Any], guard_elements: List[Any]) -> float:
    """Returns 0.5 when any incoming element is guarded by defender's armor guard_element_set, else 1.0."""
    if not element_set or not guard_elements:
        return 1.0
    guarded = {int(e) for e in guard_elements if int(e) > 0}
    if any(int(e) in guarded for e in element_set):
        return 0.5
    return 1.0


def tick_states_on_combatant(
    combatant: Dict[str, Any],
    states_db: Dict[int, Dict[str, Any]],
    roll_pct: int = 50,
) -> Tuple[bool, int]:
    """Processes status slip_damage, action restrictions, and hold_turn/auto_release_prob."""
    states = list(combatant.get("states", []))
    if not states:
        return True, 0
    can_act = True
    total_slip = 0
    max_hp = max(1, int(combatant.get("max_hp", combatant.get("maxhp", 50))))
    state_turns = combatant.setdefault("state_turns", {})
    for sid in states:
        isid = int(sid)
        sf = states_db.get(isid, {}).get("fields", {})
        if int(sf.get("restriction", 0)) >= 4:
            can_act = False
        if bool(sf.get("slip_damage", False)):
            dmg = max(1, max_hp // 10)
            total_slip += dmg
            combatant["hp"] = max(1, int(combatant["hp"]) - dmg)
        turns = int(state_turns.get(isid, 0)) + 1
        state_turns[isid] = turns
        hold_turn = int(sf.get("hold_turn", 0))
        auto_prob = int(sf.get("auto_release_prob", 0))
        if auto_prob > 0 and turns >= hold_turn and roll_pct < auto_prob:
            if isid in combatant.get("states", []):
                combatant["states"].remove(isid)
            state_turns.pop(isid, None)
    return can_act, total_slip


def shock_release_states(
    combatant: Dict[str, Any],
    states_db: Dict[int, Dict[str, Any]],
    roll_pct: int = 0,
) -> List[int]:
    """Clears states with shock_release_prob > roll_pct when taking physical damage."""
    removed: List[int] = []
    state_turns = combatant.get("state_turns", {})
    for sid in list(combatant.get("states", [])):
        isid = int(sid)
        sf = states_db.get(isid, {}).get("fields", {})
        if int(sf.get("shock_release_prob", 0)) > roll_pct:
            combatant["states"].remove(isid)
            if isinstance(state_turns, dict):
                state_turns.pop(isid, None)
            removed.append(isid)
    return removed


def get_combatant_restriction(
    combatant: Dict[str, Any],
    states_db: Dict[int, Dict[str, Any]],
) -> int:
    """Returns highest RMXP state restriction level (0=none, 1=no magic, 2=attack enemies, 3=attack allies, 4=cannot act)."""
    highest = 0
    for sid in combatant.get("states", []):
        sf = states_db.get(int(sid), {}).get("fields", {})
        highest = max(highest, int(sf.get("restriction", 0)))
    return highest


def build_single_enemy(
    eid: int,
    enemies_db: Dict[int, Dict[str, Any]],
    hidden: bool = False,
) -> Optional[Dict[str, Any]]:
    """Instantiates one enemy combatant dictionary from IR enemy ID."""
    enemy = enemies_db.get(int(eid), {}).get("fields")
    if not enemy:
        return None
    mhp = int(enemy.get("maxhp", 50))
    msp = int(enemy.get("maxsp", 100))
    return {
        "enemy_id": int(eid),
        "name": str(enemy.get("name", f"Enemy#{eid}")),
        "battler_name": str(enemy.get("battler_name", "")),
        "battler_hue": int(enemy.get("battler_hue", 0)),
        "hp": mhp,
        "max_hp": mhp,
        "sp": msp,
        "max_sp": msp,
        "atk": int(enemy.get("atk", 10)),
        "str": int(enemy.get("str", enemy.get("atk", 10))),
        "pdef": int(enemy.get("pdef", 5)),
        "mdef": int(enemy.get("mdef", 5)),
        "dex": int(enemy.get("dex", 10)),
        "agi": int(enemy.get("agi", 10)),
        "int": int(enemy.get("int", 10)),
        "eva": int(enemy.get("eva", 0)),
        "exp": int(enemy.get("exp", 0)),
        "gold": int(enemy.get("gold", 0)),
        "actions": list(enemy.get("actions", [])),
        "element_ranks": enemy.get("element_ranks", {}),
        "states": [],
        "hidden": bool(hidden),
    }


def enemy_formula_stats(enemy: Dict[str, Any]) -> Dict[str, int]:
    """Converts a runtime enemy combatant into the canonical stat dictionary for BattleFormula."""
    from .battle_formulas import combatant_stats

    return combatant_stats(
        is_actor=0,
        level=int(enemy.get("level", 1) or 1),
        hp=int(enemy.get("hp", 1)),
        mhp=int(enemy.get("max_hp", enemy.get("hp", 1))),
        sp=int(enemy.get("sp", 0)),
        msp=int(enemy.get("max_sp", enemy.get("sp", 0))),
        atk=int(enemy.get("atk", 10)),
        str=int(enemy.get("str", enemy.get("atk", 10))),
        dex=int(enemy.get("dex", 10)),
        agi=int(enemy.get("agi", 10)),
        int=int(enemy.get("int", 10)),
        pdef=int(enemy.get("pdef", 0)),
        mdef=int(enemy.get("mdef", 0)),
        eva=int(enemy.get("eva", 0)),
    )


def build_battle_enemies(
    troop: Optional[Dict[str, Any]],
    troop_id: int,
    enemies_db: Dict[int, Dict[str, Any]],
) -> Tuple[str, List[Dict[str, Any]]]:
    """Instantiates troop enemy combatant dictionaries from IR database entries."""
    if not troop:
        return str(troop_id), []
    t_fields = troop["fields"]
    troop_name = str(t_fields.get("name", troop_id))
    enemies_inst: List[Dict[str, Any]] = []
    for mem in t_fields.get("members", []):
        mf = mem.get("fields", {})
        inst = build_single_enemy(int(mf.get("enemy_id", 0)), enemies_db, hidden=bool(mf.get("hidden", False)))
        if inst is not None:
            inst["screen_x"] = int(mf.get("x", 320))
            inst["screen_y"] = int(mf.get("y", 180))
            enemies_inst.append(inst)
    return troop_name, enemies_inst


def select_enemy_action(
    enemy: Dict[str, Any],
    turn: int,
    switches: Optional[Dict[int, bool]] = None,
) -> Optional[Dict[str, Any]]:
    """Selects highest-rating eligible RMXP Enemy::Action matching turn/HP/switch conditions."""
    hp_pct = (int(enemy["hp"]) * 100) // max(1, int(enemy["max_hp"]))
    candidates: List[Tuple[int, Dict[str, Any]]] = []
    for raw_act in enemy.get("actions", []):
        af = raw_act.get("fields", raw_act) if isinstance(raw_act, dict) else {}
        cond_sw = int(af.get("condition_switch_id", 0))
        if cond_sw > 0 and (switches is None or not switches.get(cond_sw, False)):
            continue
        cond_hp = int(af.get("condition_hp", 100))
        if hp_pct > cond_hp:
            continue
        ta = int(af.get("condition_turn_a", 0))
        tb = int(af.get("condition_turn_b", 1))
        if tb == 0 and turn != ta:
            continue
        if tb > 0 and (turn < ta or (turn - ta) % tb != 0):
            continue
        candidates.append((int(af.get("rating", 5)), af))
    if not candidates:
        return None
    candidates.sort(key=lambda item: -item[0])
    return candidates[0][1]


def execute_enemy_turn_action(
    enemy: Dict[str, Any],
    bs: Dict[str, Any],
    defending: set,
    living_party: List[int],
    turn_cursor: int,
    skills_db: Dict[int, Dict[str, Any]],
    get_actor_state: Callable[[int], Dict[str, Any]],
    get_actor_defense: Callable[[int], int],
    get_actor_magic_defense: Optional[Callable[[int], int]] = None,
    switches: Optional[Dict[int, bool]] = None,
    states_db: Optional[Dict[int, Dict[str, Any]]] = None,
    get_actor_guard_elements: Optional[Callable[[int], List[int]]] = None,
    get_actor_guard_states: Optional[Callable[[int], List[int]]] = None,
    formula: Optional["BattleFormula"] = None,
    get_actor_formula_stats: Optional[Callable[[int], Dict[str, int]]] = None,
) -> str:
    """Executes one enemy AI turn action (restriction checks, skill, basic defend/wait, or physical strike).

    When ``formula`` and ``get_actor_formula_stats`` are given, damage follows the project's
    BattleFormula preset; otherwise the legacy simplified formulas are used.
    """
    use_formula = formula is not None and get_actor_formula_stats is not None
    restriction = get_combatant_restriction(enemy, states_db or {})
    if restriction >= 4 or enemy.get("hidden", False):
        return f"{enemy['name']}은(는) 움직일 수 없다."
    if restriction == 3:
        allies = [e for e in bs.get("enemies", []) if int(e.get("hp", 0)) > 0 and not e.get("hidden", False)]
        foe_target = allies[turn_cursor % len(allies)] if allies else enemy
        if use_formula:
            dmg = max(1, formula.attack_damage(enemy_formula_stats(enemy), enemy_formula_stats(foe_target)))  # type: ignore[union-attr]
        else:
            dmg = max(1, int(enemy["atk"]) - int(foe_target.get("pdef", 0)) // 2)
        foe_target["hp"] = max(0, int(foe_target["hp"]) - dmg)
        return f"{enemy['name']} 혼란! {foe_target['name']}에게 {dmg} 피해"
    af = None if restriction == 2 else select_enemy_action(enemy, int(bs.get("turn", 1)), switches=switches)
    if af and int(af.get("kind", 0)) == 1:
        sid = int(af.get("skill_id", 0))
        sf = skills_db.get(sid, {}).get("fields", {})
        is_magic = int(sf.get("int_f", 0)) > 0 and int(sf.get("atk_f", 0)) == 0
        if restriction == 1 and is_magic:
            af = None
        else:
            sname = str(sf.get("name", f"스킬#{sid}"))
            power = int(sf.get("power", 0))
            scope = int(sf.get("scope", 1))
            if power < 0 or scope in (3, 4):
                heal = max(10, abs(power) * 2)
                enemy["hp"] = min(int(enemy["max_hp"]), int(enemy["hp"]) + heal)
                apply_state_sets(enemy.setdefault("states", []), [], sf.get("minus_state_set", []))
                return f"{enemy['name']} {sname}! HP +{heal}"
            targets = list(living_party) if scope == 2 else [living_party[turn_cursor % len(living_party)]]
            total_dmg = 0
            for tid in targets:
                tst = get_actor_state(tid)
                if is_magic:
                    off = int(enemy.get("int", enemy["atk"]))
                    mitigation = get_actor_magic_defense(tid) if get_actor_magic_defense else get_actor_defense(tid)
                else:
                    off = int(enemy["atk"])
                    mitigation = get_actor_defense(tid)
                if use_formula:
                    dmg = max(1, formula.skill_damage(enemy_formula_stats(enemy), get_actor_formula_stats(tid), sf))  # type: ignore[union-attr,misc]
                    dmg = formula.vary(dmg, formula.skill_variance_for(sf), turn_cursor * 31 + tid)  # type: ignore[union-attr]
                else:
                    dmg = max(1, off + max(0, power) // 2 - mitigation // 2)
                if get_actor_guard_elements is not None:
                    elem_mult = compute_guard_element_multiplier(
                        list(sf.get("element_set", [])), get_actor_guard_elements(tid)
                    )
                    dmg = max(1, int(round(dmg * elem_mult)))
                if tid in defending:
                    dmg = formula.apply_guard(dmg) if use_formula else max(1, dmg // 2)  # type: ignore[union-attr]
                tst["hp"] = max(0, int(tst["hp"]) - dmg)
                guard_st = get_actor_guard_states(tid) if get_actor_guard_states is not None else None
                apply_state_sets(
                    tst.setdefault("states", []),
                    sf.get("plus_state_set", []),
                    sf.get("minus_state_set", []),
                    guard_states=guard_st,
                )
                if not is_magic and states_db is not None and dmg > 0:
                    shock_release_states(tst, states_db, roll_pct=(turn_cursor * 17) % 100)
                total_dmg += dmg
            tlabel = "파티 전체" if scope == 2 else get_actor_state(targets[0])["name"]
            return f"{enemy['name']} {sname}! {tlabel}에게 {total_dmg} 피해"
    if af and int(af.get("kind", 0)) == 0:
        basic = int(af.get("basic", 0))
        if basic == 1:
            bs.setdefault("defending_enemies", []).append(enemy["name"])
            return f"{enemy['name']} 방어 태세!"
        if basic == 3:
            return f"{enemy['name']}은(는) 상황을 살피고 있다."
    target_aid = living_party[turn_cursor % len(living_party)]
    tst = get_actor_state(target_aid)
    if use_formula:
        edmg = max(1, formula.attack_damage(enemy_formula_stats(enemy), get_actor_formula_stats(target_aid)))  # type: ignore[union-attr,misc]
        edmg = formula.vary(edmg, formula.attack_variance, turn_cursor * 17 + target_aid)  # type: ignore[union-attr]
    else:
        edmg = max(1, int(enemy["atk"]) - get_actor_defense(target_aid) // 2)
    if target_aid in defending:
        edmg = formula.apply_guard(edmg) if use_formula else max(1, edmg // 2)  # type: ignore[union-attr]
    tst["hp"] = max(0, int(tst["hp"]) - edmg)
    if states_db is not None and edmg > 0:
        shock_release_states(tst, states_db, roll_pct=(turn_cursor * 17) % 100)
    return f"{enemy['name']} 공격 -> {tst['name']} {edmg} 피해"
