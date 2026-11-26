"""Build data/rbc_rules.json from a human-readable upgrade table.

Source: Bee Swarm Simulator Wiki, "Robo Bear Challenge" page (fetched 2026-11-26).
Effects are parsed into structured modifiers so the upgrade scorer (Stage 11)
can reason about them, e.g. {"stat": "capacity", "op": "mul", "value": 1.5}.
Re-run after editing the table:  python tools/build_rules.py
"""
from __future__ import annotations
import json, re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# name | rarity | cap | first purchase | at cap ("" if cap 1)
UPGRADES = """
Botnet|common|1|+25% Bee Gather Pollen; +1 Cogs Per Round|
Credit|common|3|+2 Cogs Per Round|+6 Cogs Per Round
Defragment|common|10|x1.5 Capacity; x0.9 Critical Power|x6 Capacity; x0.5 Critical Power
Homepage|common|10|x1.25 Field Pollen [Sunflower Field, Dandelion Field, Mushroom Field, Blue Flower Field]|x3.5 Field Pollen [Sunflower Field, Dandelion Field, Mushroom Field, Blue Flower Field]
Iterate|common|100|x1.05 Pollen|x5 Pollen
Overfit: Blue|common|10|x1.25 Blue Pollen; x0.95 Player Movespeed; -5% Bee Movespeed|x4 Blue Pollen; x0.75 Player Movespeed; -15% Bee Movespeed
Overfit: Red|common|10|x1.25 Red Pollen; x0.9 Capacity|x4 Red Pollen; x0.5 Capacity
Overfit: White|common|10|x1.25 White Pollen; x0.9 Convert Rate|x4 White Pollen; x0.5 Convert Rate
Sharpen|common|10|x1.2 Bee Attack; x0.9 Capacity|x3 Bee Attack; x0.5 Capacity
APM|rare|1|SPECIAL Focus tokens grant x1.03 Pollen from Tools and Collector Tool Speed for 20s (stacks 10x)|
Blue Screen|rare|1|SPECIAL Blue Boost tokens grant x1.03 Attack and x1.03 Blue Bee Attack for 15s (stacks 10x)|
Crypto|rare|3|+3 Cogs Per Round; x0.8 Pollen; x0.8 Capacity|+9 Cogs Per Round; x0.5 Pollen; x0.5 Capacity
Commit|rare|2|+5% Critical Chance; -10% Instant Conversion; -10% Red Conversion; -10% White Conversion|+10% Critical Chance; -20% Instant Conversion; -20% Red Conversion; -20% White Conversion
Dynamo|rare|10|x1.25 Bomb Pollen; x0.9 Bee Gather Pollen|x2.5 Bomb Pollen; x0.4 Bee Gather Pollen
Equalize|rare|1|+1 Bee Attack; x1.25 Ungifted Bee Attack|
Expansion|rare|100|x1.25 Capacity|x25 Capacity
GPU|rare|3|x1.25 Pollen; -4 Cogs Per Round|x1.75 Pollen; -12 Cogs Per Round
Multithread|rare|1|SPECIAL All Ability Tokens can be created During Battle; -1 Cogs Per Round|
Nullify|rare|2|x1.5 Critical Power; x0.8 Pollen|x2 Critical Power; x0.6 Pollen
Outsource|rare|3|x1.5 Bee Gather Pollen; x0.85 Pollen From Tools|x2.5 Bee Gather Pollen; x0.55 Pollen From Tools
RAM|rare|10|+75000 Capacity; +1 Cogs Per Round|+750000 Capacity; +10 Cogs Per Round
Router|rare|10|x1.3 Field Pollen [Strawberry Field, Spider Field, Bamboo Field, Pineapple Patch]|x4 Field Pollen [Strawberry Field, Spider Field, Bamboo Field, Pineapple Patch]
Saturate|rare|3|x1.25 Blue Bee Attack; x1.25 Colorless Bee Attack; x0.8 Red Bee Attack|x1.75 Blue Bee Attack; x1.75 Colorless Bee Attack; x0.4 Red Bee Attack
SSD: Blue|rare|3|x2 Blue Field Capacity; x1.5 Blue Bee Convert Rate; -3% Critical Chance|x4 Blue Field Capacity; x2.5 Blue Bee Convert Rate; -6% Critical Chance
SSD: Red|rare|3|x2 Red Field Capacity; x1.5 Red Bee Convert Rate; x0.75 Bomb Pollen|x4 Red Field Capacity; x2.5 Red Bee Convert Rate; x0.5 Bomb Pollen
SSD: White|rare|3|x2 White Field Capacity; x1.5 Colorless Bee Convert Rate; x0.8 Bee Attack|x4 White Field Capacity; x2.5 Colorless Bee Convert Rate; x0.6 Bee Attack
Subscribe|rare|1|+1 Cogs Per Round; x1.1 Event Bee Pollen|
Virus|rare|3|+1 Bee Attack; +1% Critical Chance; x0.9 Pollen|+3 Bee Attack; +3% Critical Chance; x0.7 Pollen
VPN|rare|3|+10% Dodge Chance|+30% Dodge Chance
Bandwidth|epic|1|x1.25 Convert Rate At Hive; x1.1 Mark Ability Pollen; x3 Crimson Cobalt Ability Pollen|
Base-15|epic|10|x1.25 Field Pollen [Cactus Field, Pumpkin Patch, Pine Tree Forest, Rose Field]|x3.5 Field Pollen [Cactus Field, Pumpkin Patch, Pine Tree Forest, Rose Field]
beeBay|epic|1|+1 Bee Choice Options; +1 Cogs Per Round|
Client-Side|epic|1|x1.25 Player Movespeed; x1.25 Pollen From Tools; x0.75 Convert Rate|
Demarcate|epic|1|SPECIAL Mark tokens grant x1.03 Critical Power for 15s (stacks 10x)|
F5|epic|1|+1 Quest Reroll; -3 Cogs Per Round|
Fission|epic|1|x4 Bomb Pollen; +1 Cogs Per Round; +1 Bee Attack; x0.75 Pollen|
FOV|epic|1|+1 Upgrade Choice Options; -2% Critical Chance|
Furnace|epic|1|x1.5 Flame Pollen; x1.5 Flame Duration; x1.5 Flame Damage; -4 Cogs Per Round|
HDD|epic|1|x3 Capacity; x4 Convert Rate At Hive; x0.5 Convert Rate; x0 Instant Conversion; x0.5 Goo Conversion|
Inject|epic|1|+2 Blue Bee Attack; +1 Colorless Bee Attack; x1.25 Impale Damage|
Invert|epic|1|SPECIAL Bubbles collect x4 from Red Flowers, Flames collect x4 from Blue Flowers|
Malware|epic|3|+3 Bee Attack; x0.75 Capacity; x0.75 Convert Rate|+9 Bee Attack; x0.25 Capacity; x0.25 Convert Rate
NFT|epic|1|+4 Cogs Per Round; -3 Bee Attack|
Normalize|epic|1|x2 Pollen; x2 Bee Attack; x0 Critical Chance|
Pop-Up|epic|1|x2 Bubble Pollen; x1.5 Bubble Lifespan; +2 Blue Bee Attack; -4 Cogs Per Round|
Proxy|epic|1|SPECIAL Haste tokens grant +2% Dodge Chance for 20s (stacks 10x)|
Refractor|epic|10|x1.1 Bee Ability Pollen; x0.9 Convert Rate|x2 Bee Ability Pollen; x0.75 Convert Rate
Respec: Blue|epic|2|x0.6 Blue Pollen; x1.25 White Pollen; x1.25 Red Pollen|x0.4 Blue Pollen; x1.5 White Pollen; x1.5 Red Pollen
Respec: Red|epic|2|x0.6 Red Pollen; x1.25 Blue Pollen; x1.25 White Pollen|x0.4 Red Pollen; x1.5 Blue Pollen; x1.5 White Pollen
Respec: White|epic|2|x0.6 White Pollen; x1.25 Blue Pollen; x1.25 Red Pollen|x0.4 White Pollen; x1.5 Blue Pollen; x1.5 Red Pollen
RGB|epic|1|+1% Critical Chance; x1.25 Flame Pollen; x1.25 Bubble Pollen; x0.8 Bee Gather Pollen|
Synchronize|epic|1|x1.2 Bomb Power; SPECIAL Red Bomb Sync always active with Crimson Bee, Blue with Cobalt Bee|
Torrent|epic|1|+10% Instant Conversion; x1.5 Tornado Pollen; x1.5 Beamstorm Pollen|
Trojan|epic|10|x1.25 Bee Attack; -10% Bee Movespeed|x2.5 Bee Attack; -50% Bee Movespeed
White Noise|epic|1|x1.1 White Pollen; x1.5 Buzz Bomb Pollen; -1 Cogs Per Round|
Virtual Pet|epic|1|+10% Bee Movespeed; x1.5 Scratch Pollen; x3 Fetch Pollen|
Bluetooth|legendary|1|x2 Blue Bee Attack; x1.25 Blue Pollen|
Bruteforce|legendary|1|+6% Super-Crit Chance; x1.25 Red Pollen|
Codec|legendary|1|+20% Instant Conversion; x0.8 Attack|
Corrupt|legendary|1|+1% Ability Duplication Chance; x2 Duped Ability Pollen|
Fluid Simulation|legendary|1|x1.25 Goo Pollen; x1.25 White Pollen|
Optimize|legendary|25|x1.1 Pollen; x1.1 Convert Rate; x1.1 Capacity|x2.5 Pollen; x2.5 Convert Rate; x2.5 Capacity
Overclock|legendary|1|+10% Bee Movespeed; +10% Bee Ability Rate; x0.75 Capacity|
Pseudo-RNG|legendary|1|+3% Critical Chance; x1.25 Super-Crit Power; x2 Field Pollen [Clover Field]|
Reboot|legendary|1|+1 Quest Reroll|
Stack Overflow|legendary|1|+10 Cogs Per Round; x0.75 Player Movespeed; -25% Bee Movespeed|
The Cloud|legendary|3|x2.5 Capacity|x5.5 Capacity
Wifi|legendary|10|x1.5 Field Pollen [Stump Field, Mountain Top Field, Coconut Field, Pepper Patch]|x6 Field Pollen [Stump Field, Mountain Top Field, Coconut Field, Pepper Patch]
"""

# Tags drive strategy.tag_bias in config. Derived from the stats an upgrade touches.
TAG_RULES = [
    ("cogs", re.compile(r"cogs|reroll|choice_options")),
    ("pollen", re.compile(r"pollen")),
    ("capacity", re.compile(r"capacity")),
    ("conversion", re.compile(r"convert|conversion")),
    ("attack", re.compile(r"attack|crit|damage|bomb_power|dodge")),
    ("speed", re.compile(r"movespeed|ability_rate")),
    ("token", re.compile(r"token|ability_pollen|mark|bomb_pollen|bubble|flame")),
]
COLOR_RULES = [("blue", re.compile(r"blue|cobalt|bubble")), ("red", re.compile(r"red|crimson|flame")),
               ("white", re.compile(r"white|colorless"))]

EFFECT_RE = re.compile(r"^(?P<op>x|\+|-)(?P<num>[\d.]+)(?P<pct>%)?\s+(?P<stat>.+?)(?:\s*\[(?P<fields>[^\]]+)\])?$")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def parse_effect(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith("SPECIAL"):
        return [{"op": "special", "text": text[len("SPECIAL"):].strip()}]
    m = EFFECT_RE.match(text)
    if not m:
        raise ValueError(f"cannot parse effect: {text!r}")
    num = float(m["num"])
    if m["op"] == "x":
        op, val = "mul", num
    else:
        op = "add_pct" if m["pct"] else "add"
        val = num if m["op"] == "+" else -num
    stat = slug(m["stat"])
    if m["fields"]:
        return [{"stat": "field_pollen", "field": f.strip(), "op": op, "value": val}
                for f in m["fields"].split(",")]
    return [{"stat": stat, "op": op, "value": val}]


def parse_effects(s: str) -> list[dict]:
    return [e for part in s.split(";") if part.strip() for e in parse_effect(part)]


def classify(effects: list[dict]) -> tuple[list[str], str]:
    keys = " ".join(e.get("stat", "") + " " + e.get("text", "").lower() for e in effects)
    tags = [t for t, rx in TAG_RULES if rx.search(keys)]
    colors = {c for c, rx in COLOR_RULES if rx.search(keys)}
    color = colors.pop() if len(colors) == 1 else "any"
    return tags, color


def build() -> dict:
    ups = []
    for line in UPGRADES.strip().splitlines():
        name, rarity, cap, first, at_cap = line.split("|")
        eff = parse_effects(first)
        tags, color = classify(eff)
        ups.append({"id": slug(name), "name": name, "rarity": rarity, "cap": int(cap),
                    "effects": eff, "effects_at_cap": parse_effects(at_cap) if at_cap else None,
                    "tags": tags, "color": color})
    return ups


if __name__ == "__main__":
    rules_path = ROOT / "data" / "rbc_rules.json"
    rules = json.loads(rules_path.read_text(encoding="utf-8"))
    rules["upgrades"] = build()
    rules_path.write_text(json.dumps(rules, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(rules['upgrades'])} upgrades written to {rules_path}")
