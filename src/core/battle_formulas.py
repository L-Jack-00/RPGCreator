# -*- coding: utf-8 -*-
"""
Per-project battle damage formulas.

A project chooses a preset (and optional overrides) in ``ir_project.json``::

    "battle_formula": {"preset": "rmxp"}

Projects without the key use the RPG Maker XP formulas. Presets reproduce the default
damage calculation of each RPG Maker generation on top of this tool's RMXP-style data
(STR/DEX/AGI/INT + equipment ATK/PDEF/MDEF). Formulas are plain expressions evaluated by a
small sandboxed evaluator, so users can paste VX Ace / MV style formulas such as
``a.atk * 4 - b.def * 2`` or ``Math.max(0, 100 + a.mat * 2 - b.mdf * 2)``.

Formula variables
-----------------
``a`` = attacker, ``b`` = target, ``s`` = skill (skill formulas only).

Stats (``stat_model: "xp"``, used by rmxp/simple):
  atk   RMXP ATK (actor: weapon ATK + class bonus, enemy: ATK)
  str dex agi int, pdef mdef eva, hp mhp sp msp level, is_actor
  legacy_atk / legacy_def   values used by the v0.1.0 simplified formulas
``stat_model: "unified"`` (used by 2000/2003/VX/VX Ace/MV/MZ presets) maps the XP data to the
later engines' single attack stat: ``atk`` = actor STR + equipment ATK (enemy: ATK).
Aliases in both models: def=pdef, mat=int, spi=int, mdf=mdef, mmp=msp, mp=sp, luk=0.

Skill fields: power, atk_f, str_f, dex_f, agi_f, int_f (=spi_f), pdef_f, mdef_f, eva_f,
variance, hit, magic (1 when INT-based: int_f > 0 and atk_f == 0), physical, scope, id.
A skill may override its formula with a ``damage_formula`` field.

Syntax: Python-style arithmetic (``+ - * / // % **``), comparisons, ``x if cond else y``,
``and/or/not``, ``min max abs floor ceil round int sqrt`` and ``Math.*``. Several statements
may be separated by newlines or ``;`` with ``name = expr`` assignments; the last expression is
the result. ``var/let/const`` prefixes are ignored. With ``integer_division`` (Ruby-based
engines) ``/`` between integers floors like Ruby.
"""
from __future__ import annotations

import ast
import copy
import json
import math
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

DEFAULT_PRESET = "rmxp"

_RMXP_SKILL = (
    "p = s.power + a.atk * s.atk_f / 100\n"
    "p = max(p - b.pdef * s.pdef_f / 200 - b.mdef * s.mdef_f / 200, 0) if p > 0 else p\n"
    "rate = 20 + a.str * s.str_f / 100 + a.dex * s.dex_f / 100 + a.agi * s.agi_f / 100 + a.int * s.int_f / 100\n"
    "p * rate / 20"
)
_SIMPLE_ATTACK = "max(5, a.legacy_atk * 2 - b.pdef) if a.is_actor else max(1, a.atk - b.legacy_def / 2)"
_SIMPLE_SKILL = (
    "off = a.int if s.magic else (a.legacy_atk if a.is_actor else a.atk)\n"
    "mit = b.mdef if s.magic else b.legacy_def\n"
    "max(5, off * 2 + s.power / 2 - mit) if a.is_actor else max(1, off + max(0, s.power) / 2 - mit / 2)"
)
_RM2K_ATTACK = "a.atk / 2 - b.def / 4"
_RM2K_SKILL = (
    "p = s.power + a.atk * s.atk_f / 200 + a.mat * s.int_f / 400\n"
    "p - b.def * s.atk_f / 400 - b.spi * s.int_f / 800"
)
_RMVX_ATTACK = "a.atk * 4 - b.def * 2"
_RMVX_SKILL = (
    "d = s.power\n"
    "d + a.atk * 4 * s.atk_f / 100 + a.spi * 2 * s.spi_f / 100 - b.def * 2 * s.atk_f / 100 - b.spi * s.spi_f / 100 if d > 0 else d"
)
_ACE_MV_ATTACK = "a.atk * 4 - b.def * 2"
_ACE_MV_SKILL = "s.power + (a.atk * 4 - b.def * 2) * s.atk_f / 100 + (a.mat * 2 - b.mdf * 2) * s.int_f / 100"

PRESETS: Dict[str, Dict[str, Any]] = {
    "rmxp": {
        "label": "RPG Maker XP (기본값)",
        "description": "RMXP Game_Battler 기본식. 일반공격 = max(ATK - PDEF/2, 0) × (20 + STR) / 20, "
        "스킬 = (위력 + ATK×atk_f) - 방어 감쇠, × (20 + 능력치×비율)/20. 치명타 ×2, 분산 15%.",
        "stat_model": "xp",
        "integer_division": True,
        "attack": "max(a.atk - b.pdef / 2, 0) * (20 + a.str) / 20",
        "skill": _RMXP_SKILL,
        "critical_multiplier": 2.0,
        "attack_variance": 15,
        "skill_variance": "skill",
        "guard_divisor": 2,
    },
    "simple": {
        "label": "RPGCreator 단순 공식 (v0.1.0 기존 방식)",
        "description": "v0.1.0 플레이어의 자체 공식. 주인공 일반공격 = max(5, (STR+무기ATK)×2 - 적PDEF), "
        "적 일반공격 = max(1, ATK - (8+장비PDEF)/2). 치명타 ×1.5, 일반공격 분산 없음.",
        "stat_model": "xp",
        "integer_division": True,
        "attack": _SIMPLE_ATTACK,
        "skill": _SIMPLE_SKILL,
        "critical_multiplier": 1.5,
        "attack_variance": 0,
        "skill_variance": "skill",
        "guard_divisor": 2,
    },
    "rm2k": {
        "label": "RPG Maker 2000",
        "description": "일반공격 = ATK/2 - DEF/4, 스킬 = 효과량 + ATK×물리관계도/20 + 정신×마법관계도/40 - "
        "DEF×물리관계도/40 - 정신×마법관계도/80 (관계도 0~10 = atk_f/int_f ÷ 10). 치명타 ×3, 분산 20%.",
        "stat_model": "unified",
        "integer_division": True,
        "attack": _RM2K_ATTACK,
        "skill": _RM2K_SKILL,
        "critical_multiplier": 3.0,
        "attack_variance": 20,
        "skill_variance": "skill",
        "guard_divisor": 2,
    },
    "rm2k3": {
        "label": "RPG Maker 2003",
        "description": "RPG Maker 2000과 같은 기본 데미지식(ATK/2 - DEF/4)을 사용합니다. 치명타 ×3, 분산 20%.",
        "stat_model": "unified",
        "integer_division": True,
        "attack": _RM2K_ATTACK,
        "skill": _RM2K_SKILL,
        "critical_multiplier": 3.0,
        "attack_variance": 20,
        "skill_variance": "skill",
        "guard_divisor": 2,
    },
    "rmvx": {
        "label": "RPG Maker VX",
        "description": "일반공격 = ATK×4 - DEF×2, 스킬 = 기본데미지 + ATK×4×atk_f + 정신×2×spi_f - "
        "DEF×2×atk_f - 정신×spi_f (spi_f = int_f). 치명타 ×3, 분산 20%.",
        "stat_model": "unified",
        "integer_division": True,
        "attack": _RMVX_ATTACK,
        "skill": _RMVX_SKILL,
        "critical_multiplier": 3.0,
        "attack_variance": 20,
        "skill_variance": "skill",
        "guard_divisor": 2,
    },
    "rmvxace": {
        "label": "RPG Maker VX Ace",
        "description": "공격 스킬 기본식 a.atk * 4 - b.def * 2. 스킬은 XP 데이터의 위력/비율로 "
        "위력 + (a.atk*4 - b.def*2)×atk_f + (a.mat*2 - b.mdf*2)×int_f 형태로 근사. 치명타 ×3, 분산 20%.",
        "stat_model": "unified",
        "integer_division": True,
        "attack": _ACE_MV_ATTACK,
        "skill": _ACE_MV_SKILL,
        "critical_multiplier": 3.0,
        "attack_variance": 20,
        "skill_variance": "skill",
        "guard_divisor": 2,
    },
    "rmmv": {
        "label": "RPG Maker MV",
        "description": "공격 스킬 기본식 a.atk * 4 - b.def * 2 (JavaScript 실수 나눗셈, 최종 반올림). "
        "스킬은 VX Ace 프리셋과 같은 방식으로 근사. 치명타 ×3, 분산 20%.",
        "stat_model": "unified",
        "integer_division": False,
        "attack": _ACE_MV_ATTACK,
        "skill": _ACE_MV_SKILL,
        "critical_multiplier": 3.0,
        "attack_variance": 20,
        "skill_variance": "skill",
        "guard_divisor": 2,
    },
    "rmmz": {
        "label": "RPG Maker MZ",
        "description": "MV와 같은 기본식 a.atk * 4 - b.def * 2를 사용합니다. 치명타 ×3, 분산 20%.",
        "stat_model": "unified",
        "integer_division": False,
        "attack": _ACE_MV_ATTACK,
        "skill": _ACE_MV_SKILL,
        "critical_multiplier": 3.0,
        "attack_variance": 20,
        "skill_variance": "skill",
        "guard_divisor": 2,
    },
}

PRESET_ALIASES: Dict[str, str] = {
    "xp": "rmxp",
    "2000": "rm2k",
    "2k": "rm2k",
    "2003": "rm2k3",
    "2k3": "rm2k3",
    "vx": "rmvx",
    "vxace": "rmvxace",
    "ace": "rmvxace",
    "mv": "rmmv",
    "mz": "rmmz",
    "legacy": "simple",
}

OVERRIDE_KEYS = (
    "attack",
    "skill",
    "critical_multiplier",
    "attack_variance",
    "skill_variance",
    "guard_divisor",
    "integer_division",
    "stat_model",
)

# ---------------------------------------------------------------------------
# Sandboxed expression evaluator
# ---------------------------------------------------------------------------

_MAX_FORMULA_LEN = 2000
_MAX_POW_EXPONENT = 8


def _js_round(x: float) -> int:
    return int(math.floor(float(x) + 0.5))


_FUNCS: Dict[str, Callable[..., Any]] = {
    "min": min,
    "max": max,
    "abs": abs,
    "floor": lambda x: int(math.floor(x)),
    "ceil": lambda x: int(math.ceil(x)),
    "round": _js_round,
    "int": lambda x: int(x),
    "sqrt": lambda x: math.sqrt(max(0.0, float(x))),
    "pow": lambda x, y: _safe_pow(x, y),
}
_MATH_FUNCS: Dict[str, Callable[..., Any]] = dict(_FUNCS)
_MATH_FUNCS["trunc"] = lambda x: int(x)

_ALLOWED_ROOTS = {"a", "b", "s", "Math"}


class FormulaError(ValueError):
    """Raised when a battle formula cannot be parsed or evaluated."""


def _safe_pow(x: Any, y: Any) -> Any:
    if abs(float(y)) > _MAX_POW_EXPONENT:
        raise FormulaError(f"exponent too large: {y}")
    return x ** y


def _normalize_source(src: str) -> str:
    text = str(src).replace("\r\n", "\n").replace(";", "\n")
    text = re.sub(r"\b(?:var|let|const)\s+", "", text)
    text = text.replace("&&", " and ").replace("||", " or ")
    text = re.sub(r"!(?!=)", " not ", text)
    # `def` is a Python keyword; RPG Maker formulas use it as a stat (b.def).
    text = re.sub(r"\.def\b", ".def_", text)
    return "\n".join(line.strip() for line in text.split("\n") if line.strip())


_COMPILE_CACHE: Dict[str, ast.Module] = {}


def compile_formula(src: str) -> ast.Module:
    """Parses and validates a formula into a whitelisted AST (cached)."""
    if not isinstance(src, str) or not src.strip():
        raise FormulaError("formula is empty")
    if len(src) > _MAX_FORMULA_LEN:
        raise FormulaError("formula is too long")
    cached = _COMPILE_CACHE.get(src)
    if cached is not None:
        return cached
    try:
        tree = ast.parse(_normalize_source(src), mode="exec")
    except SyntaxError as exc:
        raise FormulaError(f"syntax error: {exc.msg} (line {exc.lineno})") from exc
    if not tree.body or not isinstance(tree.body[-1], ast.Expr):
        raise FormulaError("the last line must be an expression (the damage value)")
    for stmt in tree.body[:-1]:
        if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)):
            raise FormulaError("only `name = expression` assignments are allowed before the result")
        if stmt.targets[0].id in _ALLOWED_ROOTS or stmt.targets[0].id in _FUNCS:
            raise FormulaError(f"cannot assign to reserved name {stmt.targets[0].id!r}")
    for node in ast.walk(tree):
        _check_node(node)
    _COMPILE_CACHE[src] = tree
    return tree


_ALLOWED_NODES = (
    ast.Module, ast.Expr, ast.Assign, ast.Name, ast.Load, ast.Store, ast.Constant,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Call, ast.Attribute,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)


def _check_node(node: ast.AST) -> None:
    if not isinstance(node, _ALLOWED_NODES):
        raise FormulaError(f"unsupported syntax: {type(node).__name__}")
    if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, bool)):
        raise FormulaError("only numeric constants are allowed")
    if isinstance(node, ast.Attribute):
        if not isinstance(node.value, ast.Name) or node.value.id not in _ALLOWED_ROOTS:
            raise FormulaError("attributes are only allowed on a, b, s and Math")
        if node.attr.startswith("_"):
            raise FormulaError("private attributes are not allowed")
    if isinstance(node, ast.Call):
        if node.keywords:
            raise FormulaError("keyword arguments are not allowed")
        fn = node.func
        if isinstance(fn, ast.Name):
            if fn.id not in _FUNCS:
                raise FormulaError(f"unknown function {fn.id!r}")
        elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id == "Math":
            if fn.attr not in _MATH_FUNCS:
                raise FormulaError(f"unknown function Math.{fn.attr}")
        else:
            raise FormulaError("only built-in functions may be called")


class _Evaluator:
    def __init__(self, roots: Dict[str, Dict[str, Any]], integer_division: bool) -> None:
        self.roots = roots
        self.int_div = integer_division
        self.locals: Dict[str, Any] = {}

    def run(self, tree: ast.Module) -> Any:
        for stmt in tree.body[:-1]:
            assert isinstance(stmt, ast.Assign)
            self.locals[stmt.targets[0].id] = self.ev(stmt.value)  # type: ignore[union-attr]
        last = tree.body[-1]
        assert isinstance(last, ast.Expr)
        return self.ev(last.value)

    def ev(self, node: ast.AST) -> Any:  # noqa: C901 - flat dispatcher
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in self.locals:
                return self.locals[node.id]
            if node.id in ("true", "True"):
                return True
            if node.id in ("false", "False"):
                return False
            raise FormulaError(f"unknown name {node.id!r}")
        if isinstance(node, ast.Attribute):
            root = node.value.id  # type: ignore[attr-defined]
            if root == "Math":
                if node.attr in ("PI",):
                    return math.pi
                raise FormulaError(f"Math.{node.attr} is not a value")
            table = self.roots.get(root, {})
            if node.attr not in table:
                raise FormulaError(f"unknown field {root}.{node.attr}")
            return table[node.attr]
        if isinstance(node, ast.UnaryOp):
            v = self.ev(node.operand)
            if isinstance(node.op, ast.USub):
                return -v
            if isinstance(node.op, ast.UAdd):
                return +v
            return not v
        if isinstance(node, ast.BinOp):
            left = self.ev(node.left)
            right = self.ev(node.right)
            op = node.op
            if isinstance(op, ast.Add):
                return left + right
            if isinstance(op, ast.Sub):
                return left - right
            if isinstance(op, ast.Mult):
                return left * right
            if isinstance(op, ast.Div):
                if right == 0:
                    return 0
                if self.int_div and _is_int(left) and _is_int(right):
                    return int(left) // int(right)
                return left / right
            if isinstance(op, ast.FloorDiv):
                return 0 if right == 0 else left // right
            if isinstance(op, ast.Mod):
                return 0 if right == 0 else left % right
            return _safe_pow(left, right)
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                val: Any = True
                for sub in node.values:
                    val = self.ev(sub)
                    if not val:
                        return val
                return val
            val = False
            for sub in node.values:
                val = self.ev(sub)
                if val:
                    return val
            return val
        if isinstance(node, ast.Compare):
            left = self.ev(node.left)
            for op, comp in zip(node.ops, node.comparators):
                right = self.ev(comp)
                ok = {
                    ast.Eq: left == right, ast.NotEq: left != right, ast.Lt: left < right,
                    ast.LtE: left <= right, ast.Gt: left > right, ast.GtE: left >= right,
                }[type(op)]
                if not ok:
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return self.ev(node.body) if self.ev(node.test) else self.ev(node.orelse)
        if isinstance(node, ast.Call):
            args = [self.ev(arg) for arg in node.args]
            fn = node.func
            func = _FUNCS[fn.id] if isinstance(fn, ast.Name) else _MATH_FUNCS[fn.attr]  # type: ignore[attr-defined]
            return func(*args)
        raise FormulaError(f"unsupported syntax: {type(node).__name__}")


def _is_int(v: Any) -> bool:
    return isinstance(v, (int, bool)) and not isinstance(v, float)


def evaluate_formula(
    src: str,
    a: Dict[str, Any],
    b: Dict[str, Any],
    s: Optional[Dict[str, Any]] = None,
    integer_division: bool = True,
) -> Union[int, float]:
    """Evaluates a formula with attacker/target/skill namespaces."""
    tree = compile_formula(src)
    try:
        result = _Evaluator({"a": a, "b": b, "s": s or {}}, integer_division).run(tree)
    except FormulaError:
        raise
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise FormulaError(f"evaluation error: {exc}") from exc
    if isinstance(result, bool):
        return int(result)
    if not isinstance(result, (int, float)):
        raise FormulaError("formula must produce a number")
    return result


# ---------------------------------------------------------------------------
# Stats namespaces
# ---------------------------------------------------------------------------

_STAT_DEFAULTS: Dict[str, int] = {
    "is_actor": 0, "level": 1, "hp": 1, "mhp": 1, "sp": 0, "msp": 0,
    "atk": 0, "str": 10, "dex": 10, "agi": 10, "int": 10,
    "pdef": 0, "mdef": 0, "eva": 0,
}


def combatant_stats(**values: Any) -> Dict[str, int]:
    """Builds a canonical (RMXP-semantics) stat dictionary for the formula engine.

    ``atk``/``pdef``/``mdef`` follow RMXP: for actors they come from equipment (+ class bonus),
    for enemies from the enemy database. ``legacy_atk``/``legacy_def`` default to the values the
    v0.1.0 simplified formulas used.
    """
    out = dict(_STAT_DEFAULTS)
    for key, val in values.items():
        if val is None:
            continue
        out[key] = int(val)
    out["is_actor"] = 1 if out.get("is_actor") else 0
    out.setdefault("legacy_atk", out["str"] + out["atk"] if out["is_actor"] else out["atk"])
    out.setdefault("legacy_def", out["pdef"] + 8 if out["is_actor"] else out["pdef"])
    return out


def _namespace(stats: Dict[str, Any], stat_model: str) -> Dict[str, Any]:
    ns = dict(stats)
    ns["xp_atk"] = int(stats.get("atk", 0))
    if stat_model == "unified" and stats.get("is_actor"):
        ns["atk"] = int(stats.get("str", 0)) + int(stats.get("atk", 0))
    ns["def"] = ns.get("pdef", 0)
    ns["def_"] = ns["def"]
    ns["mat"] = ns.get("int", 0)
    ns["spi"] = ns.get("int", 0)
    ns["mdf"] = ns.get("mdef", 0)
    ns["mmp"] = ns.get("msp", 0)
    ns["mp"] = ns.get("sp", 0)
    ns.setdefault("luk", 0)
    return ns


def skill_namespace(skill_fields: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    sf = skill_fields or {}

    def gi(key: str, default: int) -> int:
        try:
            return int(sf.get(key, default) or 0)
        except (TypeError, ValueError):
            return default

    atk_f = gi("atk_f", 0)
    int_f = gi("int_f", 0)
    magic = 1 if (int_f > 0 and atk_f == 0) else 0
    return {
        "id": gi("id", 0),
        "power": gi("power", 0),
        "atk_f": atk_f,
        "str_f": gi("str_f", 0),
        "dex_f": gi("dex_f", 0),
        "agi_f": gi("agi_f", 0),
        "int_f": int_f,
        "spi_f": int_f,
        "pdef_f": gi("pdef_f", 0),
        "mdef_f": gi("mdef_f", 0),
        "eva_f": gi("eva_f", 0),
        "variance": gi("variance", 0),
        "hit": gi("hit", 100),
        "scope": gi("scope", 1),
        "magic": magic,
        "physical": 0 if magic else 1,
    }


# ---------------------------------------------------------------------------
# Formula configuration
# ---------------------------------------------------------------------------


def resolve_preset_name(name: Optional[str]) -> str:
    key = str(name or DEFAULT_PRESET).strip().lower()
    key = PRESET_ALIASES.get(key, key)
    if key not in PRESETS:
        raise FormulaError(f"unknown battle formula preset {name!r}; choose from {sorted(PRESETS)}")
    return key


class BattleFormula:
    """Resolved battle formula settings for one project."""

    def __init__(self, preset: str = DEFAULT_PRESET, overrides: Optional[Dict[str, Any]] = None) -> None:
        self.preset = resolve_preset_name(preset)
        self.overrides: Dict[str, Any] = {k: v for k, v in (overrides or {}).items() if k in OVERRIDE_KEYS and v is not None}
        base = PRESETS[self.preset]
        cfg = dict(base)
        cfg.update(self.overrides)
        self.attack_src = str(cfg["attack"])
        self.skill_src = str(cfg["skill"])
        self.stat_model = str(cfg.get("stat_model", "xp"))
        if self.stat_model not in ("xp", "unified"):
            raise FormulaError("stat_model must be 'xp' or 'unified'")
        self.integer_division = bool(cfg.get("integer_division", True))
        self.critical_multiplier = float(cfg.get("critical_multiplier", 2.0))
        self.attack_variance = max(0, min(100, int(cfg.get("attack_variance", 0))))
        sv = cfg.get("skill_variance", "skill")
        self.skill_variance: Union[str, int] = "skill" if str(sv) == "skill" else max(0, min(100, int(sv)))
        self.guard_divisor = max(1.0, float(cfg.get("guard_divisor", 2)))

    # -- construction -----------------------------------------------------
    @classmethod
    def from_config(cls, cfg: Optional[Dict[str, Any]]) -> "BattleFormula":
        cfg = cfg or {}
        if not isinstance(cfg, dict):
            raise FormulaError("battle_formula must be an object")
        return cls(cfg.get("preset", DEFAULT_PRESET), {k: cfg.get(k) for k in OVERRIDE_KEYS})

    @classmethod
    def from_project(cls, project: Optional[Dict[str, Any]]) -> "BattleFormula":
        return cls.from_config((project or {}).get("battle_formula"))

    @classmethod
    def from_ir_dir(cls, ir_dir: Union[str, Path]) -> "BattleFormula":
        proj_path = Path(ir_dir) / "ir_project.json"
        if not proj_path.is_file():
            return cls()
        with proj_path.open("r", encoding="utf-8") as fh:
            return cls.from_project(json.load(fh))

    @classmethod
    def safe_from_project(cls, project: Optional[Dict[str, Any]], log: Optional[List[str]] = None) -> "BattleFormula":
        """Like from_project, but falls back to the default preset on invalid settings."""
        try:
            bf = cls.from_project(project)
            bf.validate()
            return bf
        except (FormulaError, TypeError, ValueError) as exc:
            if log is not None:
                log.append(f"[BattleFormula] invalid settings, using {DEFAULT_PRESET}: {exc}")
            return cls()

    def to_config(self) -> Dict[str, Any]:
        cfg: Dict[str, Any] = {"preset": self.preset}
        cfg.update(copy.deepcopy(self.overrides))
        return cfg

    def describe(self) -> Dict[str, Any]:
        return {
            "preset": self.preset,
            "label": PRESETS[self.preset]["label"],
            "overrides": copy.deepcopy(self.overrides),
            "attack": self.attack_src,
            "skill": self.skill_src,
            "stat_model": self.stat_model,
            "integer_division": self.integer_division,
            "critical_multiplier": self.critical_multiplier,
            "attack_variance": self.attack_variance,
            "skill_variance": self.skill_variance,
            "guard_divisor": self.guard_divisor,
        }

    def validate(self) -> None:
        """Compiles both formulas and test-evaluates them with sample stats."""
        hero = combatant_stats(is_actor=1, level=5, hp=200, mhp=200, sp=50, msp=50, atk=30, str=30, dex=25, agi=25, int=25, pdef=20, mdef=15)
        foe = combatant_stats(is_actor=0, hp=150, mhp=150, atk=25, str=25, dex=15, agi=15, int=12, pdef=15, mdef=10, eva=5)
        sample_skill = {"power": 50, "atk_f": 100, "str_f": 50, "pdef_f": 100, "variance": 15, "hit": 100}
        self.attack_damage(hero, foe)
        self.attack_damage(foe, hero)
        self.skill_damage(hero, foe, sample_skill)
        self.skill_damage(foe, hero, {"power": 60, "atk_f": 0, "int_f": 100, "mdef_f": 100})

    # -- evaluation -------------------------------------------------------
    def _finish(self, value: Union[int, float]) -> int:
        if isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                raise FormulaError("formula produced an invalid number")
            value = _js_round(value) if not self.integer_division else int(value)
        return max(0, int(value))

    def attack_damage(self, attacker: Dict[str, Any], target: Dict[str, Any]) -> int:
        """Base normal-attack damage (before element, critical, variance and guard)."""
        val = evaluate_formula(
            self.attack_src,
            _namespace(attacker, self.stat_model),
            _namespace(target, self.stat_model),
            None,
            self.integer_division,
        )
        return self._finish(val)

    def skill_damage(
        self,
        attacker: Dict[str, Any],
        target: Dict[str, Any],
        skill_fields: Optional[Dict[str, Any]],
        strict: bool = False,
    ) -> int:
        """Base offensive skill damage (before element, critical, variance and guard)."""
        a_ns = _namespace(attacker, self.stat_model)
        b_ns = _namespace(target, self.stat_model)
        s_ns = skill_namespace(skill_fields)
        custom = (skill_fields or {}).get("damage_formula")
        if isinstance(custom, str) and custom.strip():
            if strict:
                return self._finish(evaluate_formula(custom, a_ns, b_ns, s_ns, self.integer_division))
            try:
                return self._finish(evaluate_formula(custom, a_ns, b_ns, s_ns, self.integer_division))
            except FormulaError:
                pass  # invalid per-skill formula (reported by `check`): fall back to the preset formula
        return self._finish(evaluate_formula(self.skill_src, a_ns, b_ns, s_ns, self.integer_division))

    def skill_variance_for(self, skill_fields: Optional[Dict[str, Any]]) -> int:
        if self.skill_variance == "skill":
            try:
                return max(0, min(100, int((skill_fields or {}).get("variance", 0) or 0)))
            except (TypeError, ValueError):
                return 0
        return int(self.skill_variance)

    @staticmethod
    def vary(value: int, variance: int, seed: int) -> int:
        """Deterministic RMXP-style variance: value ± max(value*variance/100, 1)."""
        if value <= 0 or variance <= 0:
            return value
        amp = max(value * variance // 100, 1)
        offset = (abs(int(seed)) * 7919 + 13) % (2 * amp + 1) - amp
        return max(1, value + offset)

    def apply_critical(self, value: int) -> int:
        return int(round(value * self.critical_multiplier))

    def apply_guard(self, value: int) -> int:
        if value <= 0:
            return value
        return max(1, int(value / self.guard_divisor))


def list_presets() -> List[Dict[str, Any]]:
    return [
        {"id": key, "label": cfg["label"], "description": cfg["description"], "default": key == DEFAULT_PRESET}
        for key, cfg in PRESETS.items()
    ]
