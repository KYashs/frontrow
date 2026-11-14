"""Create the RDS schema and load the catalogue from catalog/ + venues/ JSON.

Runs on every app-server boot (systemd ExecStartPre). Safe to run in
parallel on many instances: a Postgres advisory lock serialises them and
every insert is ON CONFLICT DO NOTHING. Seat inventory in DynamoDB is
seeded separately by the inventory-seeder Lambda.
"""
from sqlalchemy import text

import database
from core import catalog

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS venues (
        venue_id TEXT PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL,
        country TEXT NOT NULL, tz TEXT NOT NULL, capacity INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY, artist TEXT NOT NULL, title TEXT NOT NULL,
        venue_id TEXT NOT NULL REFERENCES venues(venue_id), starts_at TEXT NOT NULL,
        currency TEXT NOT NULL, genre TEXT NOT NULL, art1 TEXT NOT NULL, art2 TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS price_tiers (
        event_id TEXT NOT NULL REFERENCES events(event_id), tier TEXT NOT NULL,
        name TEXT NOT NULL, price INTEGER NOT NULL, PRIMARY KEY (event_id, tier))""",
    """CREATE TABLE IF NOT EXISTS event_sections (
        event_id TEXT NOT NULL REFERENCES events(event_id), section_id TEXT NOT NULL,
        label TEXT NOT NULL, kind TEXT NOT NULL, tier TEXT, on_sale INTEGER NOT NULL,
        capacity INTEGER NOT NULL, rows_count INTEGER, seats_per_row INTEGER,
        PRIMARY KEY (event_id, section_id))""",
    "CREATE INDEX IF NOT EXISTS idx_event_sections_tier ON event_sections (event_id, tier)",
]


def seed():
    eng = database.engine()
    with eng.begin() as conn:
        if eng.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(727272)"))
        for stmt in SCHEMA:
            conn.execute(text(stmt))
        for ev in catalog.events():
            v = catalog.venue(ev["venue_id"])
            conn.execute(text(
                "INSERT INTO venues (venue_id, name, city, country, tz, capacity) "
                "VALUES (:id, :name, :city, :country, :tz, :cap) ON CONFLICT (venue_id) DO NOTHING"),
                {"id": v["venue_id"], "name": v["name"], "city": v["city"], "country": v["country"],
                 "tz": v["tz"], "cap": v["capacity"]["total"]})
            conn.execute(text(
                "INSERT INTO events (event_id, artist, title, venue_id, starts_at, currency, genre, art1, art2) "
                "VALUES (:id, :artist, :title, :venue, :starts, :cur, :genre, :a1, :a2) "
                "ON CONFLICT (event_id) DO NOTHING"),
                {"id": ev["event_id"], "artist": ev["artist"], "title": ev["title"], "venue": ev["venue_id"],
                 "starts": ev["starts_at"], "cur": ev["currency"], "genre": ev["genre"],
                 "a1": ev["art"][0], "a2": ev["art"][1]})
            for tier in ev["tiers"]:
                conn.execute(text(
                    "INSERT INTO price_tiers (event_id, tier, name, price) VALUES (:e, :t, :n, :p) "
                    "ON CONFLICT (event_id, tier) DO NOTHING"),
                    {"e": ev["event_id"], "t": tier["tier"], "n": tier["name"], "p": tier["price"]})
            for s in catalog.event_sections(ev["event_id"]).values():
                conn.execute(text(
                    "INSERT INTO event_sections (event_id, section_id, label, kind, tier, on_sale, capacity, "
                    "rows_count, seats_per_row) VALUES (:e, :s, :l, :k, :t, :o, :c, :r, :spr) "
                    "ON CONFLICT (event_id, section_id) DO NOTHING"),
                    {"e": ev["event_id"], "s": s["section_id"], "l": s["label"], "k": s["kind"],
                     "t": s["tier"], "o": 1 if s["on_sale"] else 0, "c": s["capacity"],
                     "r": s["rows"], "spr": s["seats_per_row"]})


if __name__ == "__main__":
    seed()
    print("catalogue seeded")
