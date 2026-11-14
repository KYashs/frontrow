"""Static catalogue: events (catalog/events.json) + venue geometry (venues/*.json).

The JSON files are the single source of truth. They are packaged into the
app bundle, the seeder Lambda, and (geometry only) the frontend on S3.
"""
import json
import os
from functools import lru_cache

_HERE = os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    # Packaged layout: <bundle>/catalog, <bundle>/venues next to core/.
    # Repo layout: <repo>/catalog, <repo>/venues.
    return os.getenv("CATALOG_DIR", os.path.dirname(_HERE))


@lru_cache(maxsize=None)
def _events():
    with open(os.path.join(_data_dir(), "catalog", "events.json")) as f:
        return {e["event_id"]: e for e in json.load(f)["events"]}


@lru_cache(maxsize=None)
def venue(venue_id):
    with open(os.path.join(_data_dir(), "venues", f"{venue_id}.json")) as f:
        return json.load(f)


def events():
    return list(_events().values())


def event(event_id):
    return _events().get(event_id)


def _killed(ev, section):
    rule = ev.get("killed") or {}
    if section.get("level") not in rule.get("levels", []) or "angle" not in section:
        return False
    diff = abs((section["angle"] - rule.get("center", 0) + 180) % 360 - 180)
    return diff <= rule.get("half_angle", 0)


@lru_cache(maxsize=None)
def event_sections(event_id):
    """Every section of the event's venue with tier, price and on-sale flag.

    A section is off sale when it sits behind an end stage (the event's
    `killed` rule) or when the event has no tier for its level.
    """
    ev = event(event_id)
    if ev is None:
        return {}
    tiers = {t["tier"]: t for t in ev["tiers"]}
    out = {}
    for s in venue(ev["venue_id"])["sections"]:
        tier = tiers.get(s["level"])
        on_sale = tier is not None and not _killed(ev, s)
        out[s["id"]] = {
            "section_id": s["id"],
            "label": s["label"],
            "kind": s["kind"],
            "level": s["level"],
            "tier": s["level"] if tier else None,
            "tier_name": tier["name"] if tier else None,
            "price": tier["price"] if tier else None,
            "on_sale": on_sale,
            "capacity": s["capacity"],
            "rows": s.get("rows"),
            "seats_per_row": s.get("seats_per_row"),
        }
    return out


def seat_keys(section):
    """All seat sort keys for a seated section: R01#S01 ... Rnn#Smm."""
    for r in range(1, section["rows"] + 1):
        for n in range(1, section["seats_per_row"] + 1):
            yield f"R{r:02d}#S{n:02d}"


def capacity_summary(event_id):
    secs = [s for s in event_sections(event_id).values() if s["on_sale"]]
    return {
        "sections": len(secs),
        "seated": sum(s["capacity"] for s in secs if s["kind"] == "seated"),
        "ga": sum(s["capacity"] for s in secs if s["kind"] == "ga"),
    }
