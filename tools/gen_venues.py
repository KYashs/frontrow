#!/usr/bin/env python3
"""Generate venue seat-map geometry + inventory definitions.

Output: venues/<venue_id>.json, consumed by
  - the frontend (SVG map, served as a static file from S3 via CloudFront),
  - the inventory seeder Lambda (one DynamoDB item per seat, GA shards),
  - the catalogue seed in the API pods (sections table in RDS).

Layouts are original drawings *inspired by* the published configurations of
MetLife Stadium, SoFi Stadium, Jawaharlal Nehru Stadium (Delhi) and
DY Patil Stadium (Navi Mumbai). Section counts and sizes are approximations
for a demo, not official seating charts.

    python3 tools/gen_venues.py
"""
import json
import math
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "venues")


# ---------------------------------------------------------------- geometry

def se_point(cx, cy, a, b, deg, n=2.0):
    """Point on a superellipse (n=2 ellipse, larger n = squarer stadium bowl).
    SVG y points down, so increasing angle moves clockwise on screen."""
    t = math.radians(deg)
    c, s = math.cos(t), math.sin(t)
    x = cx + a * math.copysign(abs(c) ** (2 / n), c)
    y = cy + b * math.copysign(abs(s) ** (2 / n), s)
    return x, y


def ring_sector(cx, cy, inner, outer, d0, d1, n=2.0, steps=6):
    """Closed path for an annular sector between two superellipses."""
    (ai, bi), (ao, bo) = inner, outer
    out = [se_point(cx, cy, ao, bo, d0 + (d1 - d0) * i / steps, n) for i in range(steps + 1)]
    inn = [se_point(cx, cy, ai, bi, d1 - (d1 - d0) * i / steps, n) for i in range(steps + 1)]
    pts = out + inn
    mid = se_point(cx, cy, (ai + ao) / 2, (bi + bo) / 2, (d0 + d1) / 2, n)
    return path(pts), mid


def path(pts):
    return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + " Z"


def rect(x, y, w, h, r=0):
    if not r:
        return path([(x, y), (x + w, y), (x + w, y + h), (x, y + h)])
    return (f"M{x + r:.1f},{y:.1f} H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} "
            f"V{y + h - r:.1f} Q{x + w:.1f},{y + h:.1f} {x + w - r:.1f},{y + h:.1f} "
            f"H{x + r:.1f} Q{x:.1f},{y + h:.1f} {x:.1f},{y + h - r:.1f} V{y + r:.1f} "
            f"Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} Z")


def ellipse_poly(cx, cy, a, b, steps=120):
    return [(cx + a * math.cos(2 * math.pi * i / steps), cy + b * math.sin(2 * math.pi * i / steps))
            for i in range(steps)]


def clip(poly, x0, y0, x1, y1):
    """Sutherland-Hodgman clip of a polygon to an axis-aligned box."""
    def edge(points, inside, intersect):
        res = []
        for i, cur in enumerate(points):
            prev = points[i - 1]
            if inside(cur):
                if not inside(prev):
                    res.append(intersect(prev, cur))
                res.append(cur)
            elif inside(prev):
                res.append(intersect(prev, cur))
        return res

    def ix(p, q, x):
        t = (x - p[0]) / (q[0] - p[0])
        return (x, p[1] + t * (q[1] - p[1]))

    def iy(p, q, y):
        t = (y - p[1]) / (q[1] - p[1])
        return (p[0] + t * (q[0] - p[0]), y)

    pts = poly
    pts = edge(pts, lambda p: p[0] >= x0, lambda p, q: ix(p, q, x0))
    pts = edge(pts, lambda p: p[0] <= x1, lambda p, q: ix(p, q, x1))
    pts = edge(pts, lambda p: p[1] >= y0, lambda p, q: iy(p, q, y0))
    pts = edge(pts, lambda p: p[1] <= y1, lambda p, q: iy(p, q, y1))
    return pts


def centroid(pts):
    xs, ys = zip(*pts)
    return sum(xs) / len(xs), sum(ys) / len(ys)


def seated(sid, label, level, d, mid, rows, seats):
    return {"id": sid, "label": label, "level": level, "kind": "seated", "rows": rows,
            "seats_per_row": seats, "capacity": rows * seats, "d": d,
            "lx": round(mid[0], 1), "ly": round(mid[1], 1)}


def ga(sid, label, level, d, mid, capacity):
    return {"id": sid, "label": label, "level": level, "kind": "ga", "capacity": capacity, "big": True,
            "d": d, "lx": round(mid[0], 1), "ly": round(mid[1], 1)}


def ring(prefix_numbers, cx, cy, inner, outer, start_deg, n, level, rows, seats, gap=0.6, span=360):
    """Evenly split a ring into numbered seated sections, clockwise from start_deg."""
    sections = []
    nums = list(prefix_numbers)
    count = len(nums)
    # Split by arc length along the ring's mid-line so every section has a
    # similar width, even on squarish superellipse bowls.
    am, bm = (inner[0] + outer[0]) / 2, (inner[1] + outer[1]) / 2
    samples = 3600
    angles = [start_deg + span * k / samples for k in range(samples + 1)]
    pts = [se_point(cx, cy, am, bm, a, n) for a in angles]
    cum = [0.0]
    for k in range(1, len(pts)):
        cum.append(cum[-1] + math.dist(pts[k - 1], pts[k]))
    total = cum[-1]

    def angle_at(frac):
        target = frac * total
        lo, hi = 0, len(cum) - 1
        while lo < hi:
            mid_i = (lo + hi) // 2
            if cum[mid_i] < target:
                lo = mid_i + 1
            else:
                hi = mid_i
        return angles[lo]

    for i, num in enumerate(nums):
        g = gap * 3.5 / total  # gap as a fraction of ring length (~2px per 0.6)
        d0 = angle_at(i / count + g)
        d1 = angle_at((i + 1) / count - g)
        d, mid = ring_sector(cx, cy, inner, outer, d0, d1, n)
        s = seated(str(num), str(num), level, d, mid, rows, seats)
        s["angle"] = round(((d0 + d1) / 2) % 360, 1)
        sections.append(s)
    return sections


# ---------------------------------------------------------------- venues

def metlife():
    cx, cy, n = 500, 430, 3.2
    sections = []
    sections += ring(range(101, 150), cx, cy, (300, 190), (372, 252), 180, n, "L100", 20, 16)
    sections += ring(range(201, 251), cx, cy, (382, 262), (432, 302), 180, n, "L200", 12, 16)
    sections += ring(range(301, 351), cx, cy, (442, 312), (496, 366), 180, n, "L300", 20, 18)
    # End-stage concert configuration: stage at the east end zone.
    stage = rect(700, 350, 70, 160, 6)
    sections.append(ga("PIT-A", "Pit A", "PIT", rect(585, 300, 105, 120, 8), (637, 360), 1800))
    sections.append(ga("PIT-B", "Pit B", "PIT", rect(585, 440, 105, 120, 8), (637, 500), 1800))
    sections.append(ga("FIELD", "Field GA", "FIELD", rect(245, 300, 325, 260, 10), (407, 430), 12000))
    decor = [
        {"type": "field", "d": rect(230, 285, 555, 290, 26)},
        {"type": "stage", "d": stage, "label": "STAGE", "x": 735, "y": 430, "rotate": 90},
        {"type": "runway", "d": rect(585, 422, 115, 16, 3)},
    ]
    return {"venue_id": "metlife", "name": "MetLife Stadium", "city": "East Rutherford, NJ",
            "country": "US", "tz": "America/New_York", "viewBox": [0, 0, 1000, 860],
            "decor": decor, "sections": sections}


def sofi():
    cx, cy, n = 500, 430, 2.6
    sections = []
    sections += ring(range(101, 141), cx, cy, (268, 158), (318, 203), 180, n, "L100", 18, 18)
    sections += ring(range(201, 241), cx, cy, (324, 209), (360, 239), 180, n, "L200", 10, 18)
    sections += ring(range(301, 341), cx, cy, (366, 245), (396, 270), 180, n, "L300", 9, 18)
    sections += ring(range(401, 441), cx, cy, (402, 276), (442, 306), 180, n, "L400", 12, 20)
    sections += ring(range(501, 541), cx, cy, (448, 312), (494, 348), 180, n, "L500", 14, 22)
    # Floor: three columns of five seated sections; A is closest to the stage.
    for col, letter in enumerate(["C", "B", "A"]):
        for r in range(5):
            x, y, w, h = 330 + col * 92, 318 + r * 46, 84, 40
            sections.append(seated(f"{letter}{r + 1}", f"{letter}{r + 1}", f"FLOOR-{letter}",
                                   rect(x, y, w, h, 5), (x + w / 2, y + h / 2), 10, 24))
    decor = [
        {"type": "field", "d": rect(300, 300, 400, 260, 30)},
        {"type": "stage", "d": rect(640, 360, 48, 140, 5), "label": "STAGE", "x": 664, "y": 430, "rotate": 90},
    ]
    return {"venue_id": "sofi", "name": "SoFi Stadium", "city": "Inglewood, CA",
            "country": "US", "tz": "America/Los_Angeles", "viewBox": [0, 0, 1000, 860],
            "decor": decor, "sections": sections}


def jln_delhi():
    cx, cy, n = 500, 560, 2.0
    sections = []
    # Horseshoe stands open towards the stage (top). 300 degrees of seating.
    lower = [f"A{i}" for i in range(1, 31)]
    upper = [f"B{i}" for i in range(1, 31)]
    for s in ring(lower, cx, cy, (338, 392), (398, 452), 300, n, "STAND-A", 18, 22, gap=0.8, span=300):
        sections.append(s)
    for s in ring(upper, cx, cy, (408, 462), (470, 524), 300, n, "STAND-B", 22, 24, gap=0.8, span=300):
        sections.append(s)
    floor = ellipse_poly(cx, cy, 322, 376)
    gold_l = clip(floor, 0, 230, 488, 420)
    gold_r = clip(floor, 512, 230, 1000, 420)
    silver = clip(floor, 0, 432, 1000, 1000)
    sections.append(ga("GOLD-L", "Gold Left", "GOLD", path(gold_l), (400, 360), 6000))
    sections.append(ga("GOLD-R", "Gold Right", "GOLD", path(gold_r), (600, 360), 6000))
    sections.append(ga("SILVER", "Silver", "SILVER", path(silver), (540, 680), 22000))
    sections.append(ga("TABLES", "Tables", "TABLES", rect(212, 470, 90, 150, 8), (257, 545), 400))
    decor = [
        {"type": "field", "d": path(floor)},
        {"type": "stage", "d": rect(410, 150, 180, 60, 6), "label": "STAGE", "x": 500, "y": 186},
        {"type": "runway", "d": "M492,210 L508,210 L516,300 L496,340 L512,390 L500,420 L488,390 L504,340 L484,300 Z"},
    ]
    return {"venue_id": "jln-delhi", "name": "Jawaharlal Nehru Stadium", "city": "New Delhi",
            "country": "IN", "tz": "Asia/Kolkata", "viewBox": [0, 0, 1000, 1110],
            "decor": decor, "sections": sections}


def dy_patil():
    cx, cy, n = 500, 560, 2.0
    sections = []
    levels = [("L1", (330, 392), (372, 434), 14, 24), ("L2", (380, 442), (420, 482), 14, 24),
              ("L3", (428, 490), (474, 536), 18, 26)]
    blocks = {"D": 300, "C": 330, "B": 0, "A": 30, "M": 212, "N": 182, "O": 152, "P": 122}
    for block, start in blocks.items():
        for lvl, inner, outer, rows, seats in levels:
            d, mid = ring_sector(cx, cy, inner, outer, start + 0.8, start + 27.2, n)
            s = seated(f"{block}-{lvl}", f"{block} {lvl}", f"STAND-{lvl}", d, mid, rows, seats)
            s["block"] = block
            sections.append(s)
    floor = ellipse_poly(cx, cy, 316, 376)
    vip_a = clip(floor, 0, 196, 480, 400)
    vip_b = clip(floor, 520, 196, 1000, 400)
    sections.append(ga("VIP-A", "VIP Floor A", "VIPFLOOR", path(vip_a), (340, 330), 4000))
    sections.append(ga("VIP-B", "VIP Floor B", "VIPFLOOR", path(vip_b), (660, 330), 4000))
    sections.append(ga("FANPIT-A", "Fan Pit A", "FANPIT", rect(408, 206, 62, 70, 6), (439, 241), 1200))
    sections.append(ga("FANPIT-B", "Fan Pit B", "FANPIT", rect(530, 206, 62, 70, 6), (561, 241), 1200))
    sections.append(ga("GA-PREMIUM", "GA Premium", "GAPREMIUM", path(clip(floor, 0, 410, 1000, 690)), (500, 550), 14000))
    sections.append(ga("DIAMOND", "Diamond Tables", "DIAMOND", path(clip(floor, 0, 698, 1000, 738)), (500, 718), 300))
    sections.append(ga("GOLDTABLE", "Gold Tables", "GOLDTABLE", path(clip(floor, 0, 744, 1000, 784)), (500, 764), 400))
    plat = clip(floor, 0, 790, 1000, 1000)
    platinum = seated("PLATINUM", "Platinum Deck", "PLATINUM", path(plat), (500, 846), 12, 40)
    platinum["big"] = True
    sections.append(platinum)
    d, mid = ring_sector(cx, cy, (360, 420), (440, 500), 75, 105, n)
    sections.append(ga("LOUNGE", "Skyline Lounge", "LOUNGE", d, mid, 350))
    decor = [
        {"type": "field", "d": path(floor)},
        {"type": "stage", "d": rect(395, 120, 210, 76, 6), "label": "STAGE", "x": 500, "y": 164},
        {"type": "runway", "d": rect(484, 196, 32, 180, 4)},
    ]
    return {"venue_id": "dy-patil", "name": "DY Patil Stadium", "city": "Navi Mumbai",
            "country": "IN", "tz": "Asia/Kolkata", "viewBox": [0, 0, 1000, 1110],
            "decor": decor, "sections": sections}


def main():
    os.makedirs(OUT, exist_ok=True)
    for build in (metlife, sofi, jln_delhi, dy_patil):
        v = build()
        seated_cap = sum(s["capacity"] for s in v["sections"] if s["kind"] == "seated")
        ga_cap = sum(s["capacity"] for s in v["sections"] if s["kind"] == "ga")
        v["capacity"] = {"seated": seated_cap, "ga": ga_cap, "total": seated_cap + ga_cap}
        with open(os.path.join(OUT, f"{v['venue_id']}.json"), "w") as f:
            json.dump(v, f, separators=(",", ":"))
        print(f"{v['venue_id']:<10} sections={len(v['sections']):>3} seated={seated_cap:>6} ga={ga_cap:>6}")


if __name__ == "__main__":
    main()
