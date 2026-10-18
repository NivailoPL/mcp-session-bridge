"""Turn proposals into candidate maps, and read a test run into plain numbers.

A proposal is only a hypothesis written by the proposing model. Before the
owner sees it, the sorting model sorts every chapter on the candidate map; the
numbers here compare where chapters were meant to go with where they landed.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.topics import POOL_KEY

PALETTE = [
    "#7a7df0", "#a5a8f6", "#5457e5", "#22d3ee", "#67e8f9", "#2dd4bf", "#38bdf8", "#0ea5e9",
    "#fbbf24", "#fcd34d", "#f59e0b", "#fb923c", "#f97316", "#fdba74", "#34d399", "#6ee7b7",
    "#10b981", "#a7f3d0", "#059669", "#bef264", "#f472b6", "#fb7185", "#c084fc", "#e879f9",
    "#94a3b8", "#fca5a5",
]
SPREAD_FLOOR = 2
SPREAD_SHARE = 0.3
WEAK_SHARE = 0.75
MOVES_SHOWN = 80


# Neighbouring topics get distant colors: step through the palette by 5 (coprime with its length).
SPREAD = [PALETTE[(index * 5) % len(PALETTE)] for index in range(len(PALETTE))]


def _hsl_hex(h: float, s: float, l: float) -> str:
    import colorsys

    r, g, b = colorsys.hls_to_rgb((h % 360) / 360, l, s)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def family_colors(topics: list[dict[str, Any]]) -> dict[str, str]:
    """One hue per family, spread around the wheel; topics of a family are close shades of it.

    Deterministic: the same families in the same order always give the same colors.
    """
    families: list[str] = []
    members: dict[str, list[str]] = {}
    for t in topics:
        if t["key"] == POOL_KEY:
            continue
        family = t.get("family") or "Other"
        if family not in members:
            families.append(family)
            members[family] = []
        members[family].append(t["key"])
    colors: dict[str, str] = {}
    count = max(1, len(families))
    for index, family in enumerate(families):
        hue = 235 + index * 360 / count
        keys = members[family]
        n = len(keys)
        spread = min(26.0, 300 / count / 2)
        for j, key in enumerate(keys):
            step = j / (n - 1) if n > 1 else 0.5
            colors[key] = _hsl_hex(hue - spread / 2 + spread * step, 0.72, 0.72 - 0.2 * step)
    return colors


def _next_colors(used: set[str]) -> list[str]:
    free = [color for color in SPREAD if color not in used]
    return free or SPREAD[:]


def build_candidate(active_topics: list[dict[str, str]], op: str, payload: dict[str, Any]) -> list[dict[str, str]]:
    """The full topic list (without the pool) a proposal would create."""
    base = [dict(t) for t in active_topics if t["key"] != POOL_KEY]
    used = {t.get("color", "") for t in base}
    colors = iter(_next_colors(used) * 3)

    def topic(item: dict[str, Any], family: str | None = None, color: str | None = None) -> dict[str, str]:
        return {
            "key": item["key"],
            "name": item["name"],
            "family": item.get("family") or family or "Other",
            "description": item["description"],
            "color": item.get("color") or color or next(colors),
        }

    if op == "first_map":
        kept = [topic(item) for item in payload["topics"] if item.get("keep", True)]
        shades = family_colors(kept)
        return [dict(t, color=shades[t["key"]]) for t in kept]
    if op == "edit":
        return [topic(item) for item in payload["topics"]]
    if op == "new_topic":
        source = next((t for t in base if t["key"] == payload.get("target")), None)
        family = payload.get("family") or (source["family"] if source else None)
        return _shade_new(base + [topic(payload["parts"][0], family=family)], {payload["parts"][0]["key"]})
    if op == "split":
        out = []
        for t in base:
            if t["key"] != payload["target"]:
                out.append(t)
                continue
            for index, part in enumerate(payload["parts"]):
                out.append(topic(part, family=t["family"], color=t["color"] if index == 0 else None))
        return _shade_new(out, {part["key"] for part in payload["parts"][1:]})
    if op == "redescribe":
        return [dict(t, description=payload["new_description"]) if t["key"] == payload["target"] else t for t in base]
    raise ValueError(f"Unknown proposal op {op}")


def _shade_new(topics: list[dict[str, str]], new_keys: set[str]) -> list[dict[str, str]]:
    """New topics take their family's shade; existing topics keep the color they have."""
    shades = family_colors(topics)
    return [dict(t, color=shades[t["key"]]) if t["key"] in new_keys else t for t in topics]


def intended_assignments(op: str, payload: dict[str, Any], before: dict[int, dict[str, Any]]) -> dict[int, str]:
    if op == "first_map":
        return {cid: t["key"] for t in payload["topics"] if t.get("keep", True) for cid in t["chapter_ids"]}
    if op in ("split", "new_topic"):
        return {cid: part["key"] for part in payload["parts"] for cid in part["chapter_ids"]}
    if op == "redescribe":
        return {cid: item["topic_key"] for cid, item in before.items() if item["topic_key"] == payload["target"]}
    return {}


def changed_keys(active_topics: list[dict[str, str]], candidate: list[dict[str, str]]) -> set[str]:
    old = {t["key"]: t["description"] for t in active_topics if t["key"] != POOL_KEY}
    return {t["key"] for t in candidate if old.get(t["key"]) != t["description"]}


def evaluate_test(
    *,
    op: str,
    payload: dict[str, Any],
    active_topics: list[dict[str, str]],
    candidate: list[dict[str, str]],
    before: dict[int, dict[str, Any]],
    after: dict[int, dict[str, Any]],
    chapters: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Compare intent, before and after; all lists name chapters and sessions so a person can read them."""
    intended = intended_assignments(op, payload, before)
    keys = changed_keys(active_topics, candidate)
    names = {t["key"]: t["name"] for t in candidate}
    names.update({t["key"]: t["name"] for t in active_topics if t["key"] not in names})
    names[POOL_KEY] = "Unsorted"

    def label(cid: int) -> dict[str, Any]:
        chapter = chapters.get(cid, {})
        return {"strand_id": cid, "title": chapter.get("title", ""), "session_title": chapter.get("session_title", "")}

    def topic_of(mapping: dict[int, dict[str, Any]], cid: int) -> str | None:
        item = mapping.get(cid)
        return item["topic_key"] if item else None

    landed = [cid for cid, key in intended.items() if topic_of(after, cid) == key]
    missed = [
        {**label(cid), "intended": key, "landed": topic_of(after, cid)}
        for cid, key in intended.items() if topic_of(after, cid) != key and cid in after
    ]
    pulled = [
        {**label(cid), "from": topic_of(before, cid), "to": item["topic_key"]}
        for cid, item in after.items()
        if item["topic_key"] in keys and cid not in intended and topic_of(before, cid) != item["topic_key"]
    ]
    area = set(intended) | {cid for cid, item in after.items() if item["topic_key"] in keys}
    area |= {cid for cid, item in before.items() if item["topic_key"] in keys}

    def average(mapping: dict[int, dict[str, Any]]) -> float | None:
        values = [mapping[cid]["confidence"] for cid in area if cid in mapping and mapping[cid]["confidence"] is not None]
        return round(sum(values) / len(values), 3) if values else None

    moves = [
        {**label(cid), "from": topic_of(before, cid), "to": item["topic_key"]}
        for cid, item in after.items() if topic_of(before, cid) != item["topic_key"]
    ]
    per_topic: dict[str, Any] = {}
    if op == "first_map":
        after_counts = Counter(item["topic_key"] for item in after.values())
        for t in payload["topics"]:
            if not t.get("keep", True):
                continue
            ids = t["chapter_ids"]
            got = sum(1 for cid in ids if topic_of(after, cid) == t["key"])
            extra = after_counts.get(t["key"], 0) - got
            per_topic[t["key"]] = {
                "intended": len(ids),
                "landed": got,
                "pulled": extra,
                "confidence": round(
                    sum(after[cid]["confidence"] or 0 for cid in after if after[cid]["topic_key"] == t["key"])
                    / max(1, after_counts.get(t["key"], 0)), 3),
                "verdict": _verdict(len(ids), got, extra),
            }
    pool_before = sum(1 for item in before.values() if item["topic_key"] == POOL_KEY)
    pool_after = sum(1 for item in after.values() if item["topic_key"] == POOL_KEY)
    verdict = (
        _verdict(len(intended), len(landed), len(pulled)) if intended and op != "first_map"
        else ("ok" if not any(v["verdict"] != "ok" for v in per_topic.values()) else "mixed") if per_topic
        else "ok"
    )
    return {
        "verdict": verdict,
        "intended": len(intended),
        "landed": len(landed),
        "missed": missed[:MOVES_SHOWN],
        "pulled": pulled[:MOVES_SHOWN],
        "pulled_count": len(pulled),
        "confidence_before": average(before),
        "confidence_after": average(after),
        "pool_before": pool_before,
        "pool_after": pool_after,
        "sorted": len(after),
        "moves": moves[:MOVES_SHOWN],
        "moves_count": len(moves),
        "per_topic": per_topic,
        "changed_keys": sorted(keys),
        "names": names,
    }


def _verdict(intended: int, landed: int, pulled: int) -> str:
    if intended and pulled > max(SPREAD_FLOOR, SPREAD_SHARE * intended):
        return "spreads"
    if intended and landed < WEAK_SHARE * intended:
        return "weak"
    return "ok"
