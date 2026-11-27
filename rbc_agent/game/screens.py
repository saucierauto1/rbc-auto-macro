"""Stage 3: turn OCR text from an RBC screen into numbers, quests, upgrades and bees.

Pure text parsing (no screen access), so it is tested with plain strings.
OCR is not perfect: every value is ``None`` when it could not be read, and
each problem is listed in ``ScreenState.problems`` for the log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .hive import BeeInfo, normalize_bee
from .rules import RBCRules

# Letters OCR often returns instead of digits inside a number.
_DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "|": "1"})
_SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}


def parse_amount(s: str) -> float | None:
    """'20,000' -> 20000, '1.5M' -> 1500000. None if there is no number."""
    m = re.match(r"\s*([\d,\.]+)\s*([kmbt]?)", s.strip().lower())
    if not m:
        return None
    digits = m.group(1).replace(",", "")
    try:
        return float(digits) * _SUFFIX.get(m.group(2), 1)
    except ValueError:
        return None


def last_int(text: str) -> int | None:
    """Last whole number in the text ('COGS: 21' -> 21, 'CO65:21' -> 21, 'Cogs: 1O' -> 10).

    Look-alike letters only count inside a group that has a real digit, so words stay words.
    """
    groups = [g for g in re.findall(r"[\dOoIl|][\dOoIl|,]*", text) if re.search(r"\d", g)]
    if not groups:
        return None
    digits = groups[-1].translate(_DIGIT_FIX).replace(",", "")
    return int(digits) if digits.isdigit() else None


def _int_after(pattern: str, text: str) -> int | None:
    m = re.search(pattern + r"\W*([\dOoIl|]+)", text, re.IGNORECASE)
    if not m:
        return None
    digits = m.group(1).translate(_DIGIT_FIX)
    return int(digits) if digits.isdigit() else None


def parse_round(text: str) -> int | None:
    return _int_after(r"round", text)


def parse_time_left(text: str) -> int | None:
    """'TIME: 4:57' -> 297 seconds."""
    m = re.search(r"time\W*(\d{1,2})\s*[:.]\s*(\d{2})", text, re.IGNORECASE)
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def parse_reroll(text: str) -> tuple[int | None, int | None]:
    """(free rerolls left, reroll cost in Cogs) from the Reroll button."""
    free = re.search(r"(\d+)\s*remaining", text, re.IGNORECASE)
    return (int(free.group(1)) if free else None), _int_after(r"cost", text)


@dataclass(frozen=True)
class Objective:
    type: str
    amount: float | None
    color: str = ""
    place: str = ""          # field name for collect_field_pollen
    mob: str = ""
    text: str = ""


def parse_objectives(text: str, rules: RBCRules) -> list[Objective]:
    """All objectives in a quest card, in the order they appear."""
    flat = " ".join(text.lower().split())
    found: list[tuple[int, int, Objective]] = []
    for qtype, rx in rules.objective_patterns:
        for m in rx.finditer(flat):
            g = m.groupdict()
            obj = Objective(qtype, parse_amount(g.get("amount") or ""), g.get("color") or "",
                            (g.get("place") or "").title(), g.get("mob") or "", m.group(0))
            found.append((m.start(), m.end(), obj))
    found.sort(key=lambda t: (t[0], -(t[1] - t[0])))
    out, end = [], -1
    for s, e, obj in found:
        if s >= end:                    # skip a shorter pattern matching inside a longer one
            out.append(obj)
            end = e
    return out


@dataclass(frozen=True)
class QuestOffer:
    objectives: tuple[Objective, ...]
    reward: int | None
    text: str


@dataclass(frozen=True)
class ShopOffer:
    name: str                 # as read, e.g. "Overfit: Red"
    upgrade_id: str | None    # None = not in data/rbc_rules.json (misread or new upgrade)
    price: int | None
    level: int | None
    effects: str


@dataclass(frozen=True)
class BeeOffer:
    name: str                 # catalog spelling when known, else as read
    known: bool               # found in data/bees.json
    level: int | None
    mutation: str


def parse_shop_row(header: str, effects: str, rules: RBCRules) -> ShopOffer | None:
    m = re.search(r"[\[(]\s*([^\]\[()]+?)\s*\]", header)
    if not m:
        return None
    name = m.group(1).strip()
    u = rules.upgrade_by_name(name)
    lvl = re.search(r"\bl\s*v\s*[lt1i]?\W*(\d+)", header, re.IGNORECASE)
    return ShopOffer(u.name if u else name, u.id if u else None, _int_after(r"cost", header),
                     int(lvl.group(1)) if lvl else None, effects.strip())


_BEE_LINE = re.compile(r"([a-z][a-z .'\-]*?bee)\s*\(\s*l\s*v\s*[l1i]?\W*(\d+)", re.IGNORECASE)


def parse_bee_list(text: str, catalog: dict[str, BeeInfo]) -> list[BeeOffer]:
    """Bee cards from the bee-pick list. Each card starts at a '<Name> Bee (Lvl N)' line."""
    out: list[BeeOffer] = []
    lines = text.splitlines()
    starts = [(i, m) for i, ln in enumerate(lines) if (m := _BEE_LINE.search(ln))]
    for k, (i, m) in enumerate(starts):
        rest = " ".join(lines[i + 1: starts[k + 1][0] if k + 1 < len(starts) else len(lines)])
        mut = re.search(r"mutation\s*[:;]?\s*([+\-]?\d+%?\s*[a-z][a-z ]*|[a-z][a-z ]*)", rest, re.IGNORECASE)
        mutation = mut.group(1).strip() if mut else ""
        raw = m.group(1).strip()
        info = catalog.get(normalize_bee(raw))
        out.append(BeeOffer(info.name if info else raw.title(), info is not None,
                            int(m.group(2)), "" if mutation.lower() == "none" else mutation))
    return out


@dataclass
class ScreenState:
    kind: str
    round_no: int | None = None
    cogs: int | None = None
    free_rerolls: int | None = None
    reroll_cost: int | None = None
    quests: list[QuestOffer] = field(default_factory=list)
    shop: list[ShopOffer] = field(default_factory=list)
    bees: list[BeeOffer] = field(default_factory=list)
    time_left_s: int | None = None
    objectives: list[Objective] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def parse_screen(kind: str, texts: dict[str, str], shop_rows: list[tuple[str, str]],
                 rules: RBCRules, catalog: dict[str, BeeInfo]) -> ScreenState:
    """Build a ScreenState from the texts ScreenReader returned."""
    st = ScreenState(kind)
    if kind == "unknown":
        return st
    if kind == "in_round":
        st.round_no = parse_round(texts.get("round_time", ""))
        st.time_left_s = parse_time_left(texts.get("round_time", ""))
        st.cogs = last_int(texts.get("cogs", ""))
        st.objectives = parse_objectives(texts.get("quest", ""), rules)
        if not st.objectives:
            st.problems.append(f"quest not understood: {texts.get('quest', '')!r}")
    else:
        st.round_no = parse_round(texts.get("title", ""))
        st.cogs = last_int(texts.get("cogs", ""))
        st.free_rerolls, st.reroll_cost = parse_reroll(texts.get("reroll", ""))
    if kind == "quest_choice":
        for side in ("a", "b"):
            card = texts.get(f"card_{side}", "")
            objs = parse_objectives(card, rules)
            if not objs:
                st.problems.append(f"quest {side.upper()} not understood: {card!r}")
            st.quests.append(QuestOffer(tuple(objs), _int_after(r"reward", texts.get(f"reward_{side}", "")),
                                        card))
    elif kind == "upgrade_shop":
        for header, effects in shop_rows:
            offer = parse_shop_row(header, effects, rules)
            if offer is None or offer.upgrade_id is None:
                st.problems.append(f"upgrade not recognised: {header!r}")
            if offer is not None:
                st.shop.append(offer)
    elif kind == "bee_pick":
        st.bees = parse_bee_list(texts.get("list", ""), catalog)
        st.problems += [f"bee not in bee list: {b.name}" for b in st.bees if not b.known]
        if not st.bees:
            st.problems.append("no bees read")
    for name, val in (("round", st.round_no), ("cogs", st.cogs)):
        if val is None:
            st.problems.append(f"{name} not read")
    return st
