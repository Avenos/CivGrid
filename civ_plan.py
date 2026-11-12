#!/usr/bin/env python3
"""
civ_plan.py - fit as many cities as possible on a Civ V map by razing a few.

1) In-game, paste this ONE command into the FireTuner Lua console (InGame):

local w,h=Map.GetGridSize() print("SIZE",w,h,Map.IsWrapX(),Game.GetActivePlayer()) local A="0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_" for y=0,h-1 do local s="" local t="" for x=0,w-1 do local p=Map.GetPlot(x,y) s=s..(p:IsWater() and "0" or ((p:IsMountain() or p:IsImpassable()) and "2" or "1")) local o=p:GetOwner() t=t..(o<0 and "." or A:sub(o+1,o+1)) end print("ROW",y,s) print("OWN",y,t) end local W={} for b in GameInfo.Buildings() do local bc=GameInfo.BuildingClasses[b.BuildingClass] if bc and bc.MaxGlobalInstances>0 then W[#W+1]=b end end print("CITIES") for i=0,63 do local p=Players[i] if p and p:IsAlive() then local ok,cr,cg,cb=pcall(function() local k=GameInfo.Colors[GameInfo.PlayerColors[p:GetPlayerColor()].PrimaryColor] return k.Red,k.Green,k.Blue end) if not ok then cr,cg,cb=-1,-1,-1 end print("PLAYER",i,cr,cg,cb,p:GetCivilizationShortDescription()) for c in p:Cities() do local wn="" for _,b in ipairs(W) do if c:IsHasBuilding(b.ID) then wn=wn..b.Type.."," end end local cap=c:IsCapital() or (pcall(c.IsOriginalCapital,c) and c:IsOriginalCapital()) local holy=pcall(c.IsHolyCityAnyReligion,c) and c:IsHolyCityAnyReligion() local rz=pcall(c.IsRazing,c) and (c:IsRazing() and 1 or 0) or -1 print("CITY",c:GetX(),c:GetY(),i,p:IsMinorCiv() and 1 or 0,cap and 1 or 0,holy and 1 or 0,c:GetPopulation(),rz,wn=="" and "-" or wn,c:GetName()) end end end print("RESOURCES") for y=0,h-1 do for x=0,w-1 do local p=Map.GetPlot(x,y) local r=p:GetResourceType() if r>=0 then local t=GameInfo.Resources[r].Type if t=="RESOURCE_OIL" or t=="RESOURCE_ALUMINUM" then print("RES",x,y,t,p:GetNumResource(),p:GetOwner()) end end end end

2) python civ_plan.py --rerun            (compute from Lua.log in the Civ V folder)
   python civ_plan.py dump.txt --rerun   (or from a file with the console output pasted in)
   python civ_plan.py                    (open the saved run, instantly)

The first pass is a quick search that gives an estimate for every plan button. Click a
plan to polish it with OR-Tools (a few seconds; click it again to keep optimizing).
Right-click a city in the window to mark it Keep or Raze, then click "Apply marks &
recompute" to redo the quick search with your marks.

The results, raze sets and marks are saved. A saved run is never recomputed on its
own: after a new export, changed options or an edited KEEP list, pass --rerun. (With
no saved run yet, the first run computes anyway.)

Options: --max-razes 36  --min-dist 4 (3 empty tiles between cities)
         --keep Delhi --keep "Kuala Lumpur"   (never raze these; or edit KEEP below)
         --raze Rome --raze Paris   (always razed, overriding every protection: your own,
                            capital, wonders, city-state; the plans add extra razes on top)
         --allow-raze-own   (by default your own cities are never razed)
         --me 0             (your player number, if the dump doesn't say)
         --time 3           (OR-Tools seconds each time you click a plan)
The window's "Empty Lua.log" button, or "python civ_plan.py --clear-log", empties the
game's log so the next export starts clean. Both ask first, and both report an error
if the game has the file locked.

pip install matplotlib ortools     (ortools optional: without it you only get the quick estimates)
"""
import argparse
import hashlib
import pickle
import math
import os
import random
import re
import sys
import time

# ---------------------------------------------------------------------------
# Cities you never want razed, by name (not case-sensitive). Example:
#   KEEP = ["Delhi", "Mumbai", "Kuala Lumpur"]
KEEP = []
# ---------------------------------------------------------------------------

DEFAULT_LOG = os.path.normpath(os.path.expanduser(
    "~/Documents/My Games/Sid Meier's Civilization 5/Logs/Lua.log"))

def log_size():
    try:
        return f" ({os.path.getsize(DEFAULT_LOG) / 1e6:.1f} MB)"
    except OSError:
        return ""


def empty_log():
    """Empty the game's Lua.log. Returns (ok, message)."""
    try:
        with open(DEFAULT_LOG, "r+b") as fh:
            fh.truncate(0)
    except FileNotFoundError:
        return False, f"Lua.log not found: {DEFAULT_LOG}"
    except OSError as e:
        if getattr(e, "winerror", None) == 32:
            why = "it's in use by another program (probably Civ V). Close the game and try again."
        elif isinstance(e, PermissionError):
            why = "access denied, or the file is read-only."
        else:
            why = e.strerror or str(e)
        return False, f"Couldn't empty Lua.log: {why}"
    return True, "Lua.log emptied."


def clear_log_cli():
    try:
        ans = input(f"Empty {DEFAULT_LOG}{log_size()}?  This can't be undone. [y/N] ")
    except EOFError:
        ans = ""
    if ans.strip().lower() not in ("y", "yes"):
        print("Cancelled.")
        return 0
    ok, msg = empty_log()
    print(msg)
    return 0 if ok else 1


# ================================================================= reading
FLAG = ("0", "1")


def parse(path):
    size, rows, cities, players, me = None, {}, [], {}, None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.search(r"\b(SIZE|ROW|CITIES|CITY|PLAYER)\b\s*(.*)$", line.rstrip())
            if not m:
                continue
            kind, rest = m.group(1), m.group(2).split()
            try:
                if kind == "SIZE":                 # newest dump wins
                    size = (int(rest[0]), int(rest[1]),
                            len(rest) < 3 or rest[2].lower() == "true")
                    me = int(rest[3]) if len(rest) > 3 and rest[3].isdigit() else None
                    rows, cities = {}, []
                elif kind == "ROW":
                    rows[int(rest[0])] = rest[1]
                elif kind == "CITIES":
                    cities = []
                elif kind == "PLAYER":
                    players[int(rest[0])] = parse_player(rest)
                elif kind == "CITY":
                    cities.append(parse_city(rest))
            except (ValueError, IndexError):
                continue
    if size is None or not rows:
        sys.exit(f"No SIZE/ROW lines found in {path}. Run the Lua command first "
                 "(it's at the top of this script).")
    W, H, wrap = size
    tiles = {}
    for y, s in rows.items():
        for x, ch in enumerate(s[:W]):
            if ch in "12":
                tiles[(x, y)] = ch
    return W, H, wrap, tiles, cities, players, me


def parse_resources(path, W=None, H=None):
    """Newest RESOURCES block: {(x, y): (kind, amount, owner)}, kind "oil" / "aluminum".
    Returns None if the file has no resource lines."""
    found, res = False, {}
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            m = re.search(r"\b(SIZE|RESOURCES|RES)\b\s*(.*)$", line.rstrip())
            if not m:
                continue
            kind, rest = m.group(1), m.group(2).split()
            if kind in ("SIZE", "RESOURCES"):       # a new block starts
                found, res = kind == "RESOURCES", {}
                continue
            try:
                x, y, t, amt, owner = int(rest[0]), int(rest[1]), rest[2], int(rest[3]), int(rest[4])
            except (ValueError, IndexError):
                continue
            if W is not None and not (0 <= x < W and 0 <= y < H):
                continue
            found = True
            res[(x, y)] = ("oil" if "OIL" in t else "aluminum", amt, owner)
    return res if found else None


TERR_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"   # owner 0..63


def parse_territory(path, W=None, H=None):
    """Newest OWN rows: {(x, y): owner} for every owned tile (land and water), or None if
    the file has none. Each row is one character per tile: "." unowned, else TERR_CHARS."""
    rows = {}
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            m = re.search(r"\b(SIZE|OWN)\b\s*(.*)$", line.rstrip())
            if not m:
                continue
            rest = m.group(2).split()
            if m.group(1) == "SIZE":                 # a new export starts
                rows = {}
                continue
            try:
                rows[int(rest[0])] = rest[1]
            except (ValueError, IndexError):
                continue
    terr = {}
    for y, s in rows.items():
        if H is not None and not 0 <= y < H:
            continue
        for x, ch in enumerate(s[:W] if W else s):
            if ch != "." and ch in TERR_CHARS:
                terr[(x, y)] = TERR_CHARS.index(ch)
    return terr or None


def parse_player(r):
    """PLAYER id [r g b] name  ->  {"name": ...}  (the dump's r g b are ignored)"""
    def is_num(t):
        try:
            float(t)
            return True
        except ValueError:
            return False
    rest = r[1:]
    if len(rest) >= 4 and all(is_num(t) for t in rest[:3]):
        rest = rest[3:]
    return {"name": " ".join(rest)}


def pretty_wonder(t):
    return t.replace("BUILDING_", "").replace("_", " ").title()


def parse_city(r):
    x, y, owner = int(r[0]), int(r[1]), int(r[2])
    c = dict(pos=(x, y), owner=owner, minor=owner >= 22, cap=None, holy=None,
             pop=None, wonders=None, razing=None, name=" ".join(r[3:]))
    if (len(r) >= 10 and r[3] in FLAG and r[4] in FLAG and r[5] in FLAG and r[6].isdigit()
            and r[7] in ("0", "1", "-1")
            and (r[8] == "-" or r[8].startswith("BUILDING_"))):          # newest: + razing flag
        c.update(minor=r[3] == "1", cap=r[4] == "1", holy=r[5] == "1", pop=int(r[6]),
                 razing={"1": True, "0": False, "-1": None}[r[7]],      # -1: game can't say
                 wonders=[pretty_wonder(t) for t in r[8].split(",") if t and t != "-"],
                 name=" ".join(r[9:]))
    elif (len(r) >= 9 and r[3] in FLAG and r[4] in FLAG and r[5] in FLAG and r[6].isdigit()
            and (r[7] == "-" or r[7].startswith("BUILDING_"))):          # current format
        c.update(minor=r[3] == "1", cap=r[4] == "1", holy=r[5] == "1", pop=int(r[6]),
                 wonders=[pretty_wonder(t) for t in r[7].split(",") if t and t != "-"],
                 name=" ".join(r[8:]))
    elif len(r) >= 7 and r[3] in FLAG and r[4] in FLAG and re.fullmatch(r"-?\d+", r[5]):
        n = int(r[5])                                                      # previous format
        c.update(minor=r[3] == "1", cap=r[4] == "1",
                 wonders=(["(wonder)"] * n if n > 0 else []) if n >= 0 else None,
                 name=" ".join(r[6:]))
    return c


# ================================================================ hex math
class Hex:
    """Civ V offset grid: odd rows shifted half a tile east."""

    def __init__(self, W, H, wrap):
        self.W, self.H, self.wrap = W, H, wrap

    @staticmethod
    def axial(x, y):
        return x - (y - (y & 1)) // 2, y

    def dist(self, a, b):
        q1, r1 = self.axial(*a)
        q2, r2 = self.axial(*b)
        dr = r2 - r1
        best = 10 ** 9
        for k in ((-1, 0, 1) if self.wrap else (0,)):
            dq = q2 + k * self.W - q1
            best = min(best, (abs(dq) + abs(dr) + abs(dq + dr)) // 2)
        return best

    def ball(self, p, r):
        x, y = p
        out = set()
        for dy in range(-r, r + 1):
            y2 = y + dy
            if not 0 <= y2 < self.H:
                continue
            for dx in range(-r - 1, r + 2):
                x2 = x + dx
                if self.wrap:
                    x2 %= self.W
                elif not 0 <= x2 < self.W:
                    continue
                if (x2, y2) != p and self.dist(p, (x2, y2)) <= r:
                    out.add((x2, y2))
        return out




# ================================================================= problem
def protect_reasons(c, keep_names, me=None, allow_own=False):
    why = []
    if me is not None and not allow_own and c["owner"] == me:
        why.append("your city")
    if c["name"].lower() in keep_names:
        why.append("on your keep list")
    if c["minor"]:
        why.append("city-state")
    if c["cap"]:
        why.append("capital")
    if c["holy"]:
        why.append("holy city")
    if c["wonders"]:
        why.append("has wonders")
    return why


class Problem:
    def __init__(self, hexg, tiles, cities, min_dist, keep_names, me=None, allow_own=False,
                 raze_names=(), keep_pos=(), raze_pos=()):
        self.hex, self.D = hexg, min_dist
        self.cities = [c["pos"] for c in cities]
        city_set = set(self.cities)
        # forced razes (--raze / a Raze mark): always razed, whatever would normally protect
        # them. A Keep mark wins over --raze; a Raze mark wins over a keep list.
        keep_pos, raze_pos = set(keep_pos), set(raze_pos)
        # A city that is already being razed (burning) counts as razed too: it's going away.
        burning = {c["pos"] for c in cities if c.get("razing")}
        self.forced = [c["pos"] for c in cities
                       if (c["name"].lower() in raze_names or c["pos"] in raze_pos
                           or c["pos"] in burning)
                       and c["pos"] not in keep_pos]
        self.forced_set = set(self.forced)
        self.burning = burning & self.forced_set
        # the razes you chose (--raze / Raze marks); burning cities are just treated as gone
        self.user_forced = [p for p in self.forced if p not in burning]
        reasons = {c["pos"]: protect_reasons(c, keep_names, me, allow_own)
                   + (["kept by your mark"] if c["pos"] in keep_pos else []) for c in cities}
        self.forced_why = {p: (["already being razed"] if p in burning else reasons[p])
                           for p in self.forced}
        self.why = {p: ([] if p in self.forced_set else w) for p, w in reasons.items()}
        self.pop = {c["pos"]: c["pop"] or 0 for c in cities}
        self.protected = {p for p, w in self.why.items() if w}
        self.razable = [p for p in self.cities
                        if p not in self.protected and p not in self.forced_set]
        self.sites = [p for p, ch in tiles.items() if ch == "1" and p not in city_set]
        self.site_set = set(self.sites)
        nodes = set(self.sites) | city_set
        # existing cities never conflict with each other; spacing applies to new ones
        self.adj = {n: {m for m in hexg.ball(n, min_dist - 1)
                        if m in nodes and not (n in city_set and m in city_set)}
                    for n in nodes}
        D, M = min_dist, min_dist * min_dist - min_dist + 1
        base = sorted(self.sites, key=lambda s: (len(self.adj[s]), s))
        self.orders = [base]
        for chir in (0, 1):                      # ideal hex lattices for this spacing
            for off in range(M):
                def on(s, chir=chir, off=off):
                    q, r = Hex.axial(*s)
                    return ((D * q + r) if chir == 0 else (q + D * r)) % M == off
                self.orders.append([s for s in base if on(s)] + [s for s in base if not on(s)])


def fill(order, adj, kept):
    blocked = set()
    for k in kept:
        blocked.add(k)
        blocked |= adj[k]
    new = []
    for s in order:
        if s not in blocked:
            new.append(s)
            blocked.add(s)
            blocked |= adj[s]
    return new


def improve(new, kept, sites, adj):
    """Local search: drop one new city for two, until stuck."""
    site_set = set(sites)
    chosen = set(new) | set(kept)
    cnt = {s: 0 for s in site_set}
    for c in chosen:
        for t in adj[c]:
            if t in cnt:
                cnt[t] += 1

    def add(t):
        chosen.add(t)
        for u in adj[t]:
            if u in cnt:
                cnt[u] += 1

    def rem(t):
        chosen.discard(t)
        for u in adj[t]:
            if u in cnt:
                cnt[u] -= 1

    changed = True
    while changed:
        changed = False
        for t in sites:
            if t not in chosen and cnt[t] == 0:
                add(t)
                changed = True
        for s in [c for c in chosen if c in site_set]:
            if s not in chosen:
                continue
            cands = [t for t in adj[s] if t in cnt and t not in chosen and cnt[t] == 1]
            done = False
            for i, a in enumerate(cands):
                for b in cands[i + 1:]:
                    if b not in adj[a]:
                        rem(s)
                        add(a)
                        add(b)
                        changed = done = True
                        break
                if done:
                    break
    return [s for s in sites if s in chosen]


def best_fill(pr, kept, orders):
    best = None
    for o in orders:
        f = improve(fill(o, pr.adj, kept), kept, pr.sites, pr.adj)
        if best is None or len(f) > len(best):
            best = f
    return best


class Layout:
    """A plan: kept existing cities + chosen new sites, with fast local edits."""

    def __init__(self, pr, kept, new, cnt=None):
        self.pr = pr
        self.kept = set(kept)
        self.chosen = set(new)
        if cnt is None:
            cnt = {s: 0 for s in pr.sites}
            for c in self.kept | self.chosen:
                for t in pr.adj[c]:
                    if t in cnt:
                        cnt[t] += 1
        self.cnt = cnt

    def copy(self):
        return Layout(self.pr, self.kept, self.chosen, dict(self.cnt))

    def total(self):
        return len(self.kept) + len(self.chosen)

    def _bump(self, p, d):
        for t in self.pr.adj[p]:
            if t in self.cnt:
                self.cnt[t] += d

    def add(self, t):
        self.chosen.add(t)
        self._bump(t, 1)

    def rem(self, t):
        self.chosen.discard(t)
        self._bump(t, -1)

    def unraze(self, c):
        """Bring a razed city back, evicting planned settles that crowd it."""
        for u in [u for u in self.pr.adj[c] if u in self.chosen]:
            self.rem(u)
        self.kept.add(c)
        self._bump(c, 1)

    def raze(self, c):
        self.kept.discard(c)
        self._bump(c, -1)

    def force(self, t):
        """Put a city on t, evicting the new sites in its way (never existing cities)."""
        if t in self.chosen or any(u in self.kept for u in self.pr.adj[t]):
            return False
        for u in [u for u in self.pr.adj[t] if u in self.chosen]:
            self.rem(u)
        self.add(t)
        return True

    def settle(self, region):
        """Fill free spots and do 1-out/2-in swaps inside region until stuck."""
        cnt, adj, chosen = self.cnt, self.pr.adj, self.chosen
        changed = True
        while changed:
            changed = False
            for t in region:
                if t not in chosen and cnt[t] == 0:
                    self.add(t)
                    changed = True
            for s in [x for x in region if x in chosen]:
                if s not in chosen:
                    continue
                cands = [t for t in adj[s] if t in cnt and t not in chosen and cnt[t] == 1]
                done = False
                for i, a in enumerate(cands):
                    for b in cands[i + 1:]:
                        if b not in adj[a]:
                            self.rem(s)
                            self.add(a)
                            self.add(b)
                            changed = done = True
                            break
                    if done:
                        break


def region_of(pr, centers, radius):
    """Planned-city tiles within radius of any center (cached per center)."""
    cache = pr.__dict__.setdefault("_reg", {})
    out = set()
    sites = pr.site_set
    for c in centers:
        key = (c, radius)
        if key not in cache:
            cache[key] = [t for t in pr.hex.ball(c, radius) if t in sites] + ([c] if c in sites else [])
        out.update(cache[key])
    return out


def shake(lay, pool, iters, rng):
    """Iterated local search: force a city somewhere, re-settle around it, keep if no worse.
    This is what lets planned settles slide around. All changes stay inside pool."""
    pool_set = set(pool)
    pool = list(pool)
    if not pool:
        return lay
    best, cur = lay.copy(), lay
    R = 2 * (lay.pr.D - 1)
    adj = lay.pr.adj
    for _ in range(iters):
        t = rng.choice(pool)
        if any(u in cur.chosen and u not in pool_set for u in adj[t]):
            continue                      # would disturb settles outside the area
        trial = cur.copy()
        if not trial.force(t):
            continue
        trial.settle(region_of(lay.pr, [t], R) & pool_set)
        if trial.total() >= cur.total():
            cur = trial
            if cur.total() > best.total():
                best = cur.copy()
    return best


def tidy(lay, baseline):
    """Undo settle moves that don't gain anything.

    For every no-raze settle spot that's no longer used, try putting the settle back
    there (removing whatever moved settles block it, then refilling the area). Keep the
    change if the city total doesn't drop and fewer settles differ from the no-raze plan.
    """
    pr = lay.pr
    base = set(baseline)
    adj = pr.adj
    moved = lambda L: sum(1 for s in L.chosen if s not in base)
    changed = True
    while changed:
        changed = False
        for b in sorted(base - lay.chosen):
            if b not in lay.cnt or any(u in lay.kept for u in adj[b]):
                continue
            t = lay.copy()
            for u in [u for u in adj[b] if u in t.chosen]:
                t.rem(u)
            t.add(b)
            near = region_of(pr, [b], 2 * (pr.D - 1))
            for x in sorted(near, key=lambda x: (x not in base, x)):   # no-raze spots first
                if x not in t.chosen and t.cnt[x] == 0:
                    t.add(x)
            if t.total() >= lay.total() and moved(t) < moved(lay):
                lay, changed = t, True
    return lay


def heuristic_all(pr, max_n, log=print, seed=1):
    """Raze search where planned settles near each raze are free to move.

    Returns {n: (razed, kept, new)} for n = 0..max_n.
    """
    rng = random.Random(seed)
    D = pr.D
    nf = len(pr.forced)                  # forced razes; plan n = n extra razes on top of them
    kept0 = [c for c in pr.cities if c not in pr.forced_set]
    start = best_fill(pr, kept0, pr.orders)
    lay = Layout(pr, kept0, start)
    lay = shake(lay, pr.sites, 3000, rng)              # polish the baseline plan
    plans = {0: (list(pr.forced), sorted(lay.kept), sorted(lay.chosen))}
    baseline_sites = set(lay.chosen)
    log(f"   {'forced razes only' if pr.user_forced else 'no razes'} -> {lay.total()} cities")
    razed = list(pr.forced)

    def try_raze(base, group, iters):
        t = base.copy()
        for c in group:
            t.raze(c)
        area = region_of(pr, group, 3 * D)            # settles here may all shift
        t.settle(area)
        return shake(t, area, iters, rng)

    while len(razed) - nf < max_n:
        cur = lay.total()
        cand = [c for c in lay.kept if c not in pr.protected]
        best = None                                   # (gain per raze, group, layout)
        for c in cand:
            t = try_raze(lay, [c], 30)
            g = t.total() - cur
            # equal gain -> raze the smaller city
            if g > 0 and (best is None or (g, -pr.pop[c]) > (best[0], -sum(pr.pop[x] for x in best[1]))):
                best = (g, [c], t)
        if best is None and max_n - (len(razed) - nf) >= 2:
            for i, a in enumerate(cand):
                for b in cand[i + 1:]:
                    if pr.hex.dist(a, b) > 2 * D + 1:
                        continue
                    t = try_raze(lay, [a, b], 30)
                    g = t.total() - cur
                    key = (g / 2, -(pr.pop[a] + pr.pop[b]))
                    if g > 0 and (best is None or key > (best[0], -sum(pr.pop[x] for x in best[1]))):
                        best = (g / 2, [a, b], t)
        if best is None:
            break
        razed += best[1]
        lay = shake(best[2], region_of(pr, razed, 3 * D), 600, rng)
        lay = tidy(lay, baseline_sites)
        plans[len(razed) - nf] = (list(razed), sorted(lay.kept), sorted(lay.chosen))
        log(f"   {len(razed) - nf} extra razed -> {lay.total()} cities" if pr.user_forced
            else f"   {len(razed) - nf} razed -> {lay.total()} cities")
    out, last = {}, plans[0]
    for n in range(max_n + 1):
        last = plans.get(n, last)
        out[n] = last
    return out


class AltSearch:
    """Finds other raze sets of the same size that reach at least the same city total.

    Each candidate swaps ONE razed city for another razable city. Everything else in
    the plan stays exactly as it is; only the area around the two swapped cities is
    re-planned: the un-razed city's area goes back to the no-raze layout, and only
    the newly razed city's area is re-planned, so cyan stars always sit by a raze. Search state is kept between clicks.
    """

    def __init__(self, pr, known_plans, baseline, seed=0):
        """known_plans: the raze sets already found for this plan size (first = main plan)."""
        self.pr = pr
        self.rng = random.Random(seed + len(known_plans))
        self.baseline = set(baseline)
        first = known_plans[0]
        self.target = len(first[1]) + len(first[2])
        self.tried = {tuple(sorted(p[0])) for p in known_plans}
        self.queue = [(tuple(sorted(p[0])), Layout(pr, p[1], p[2])) for p in known_plans]
        self.exhausted = False

    def swap(self, lay, c, d):
        pr = self.pr
        t = lay.copy()
        t.unraze(c)
        near_c = region_of(pr, [c], 3 * pr.D)
        for s in [s for s in t.chosen if s in near_c]:      # reset c's area ...
            t.rem(s)
        for s in sorted(near_c & self.baseline):            # ... to the no-raze layout
            if s not in t.chosen and t.cnt[s] == 0:
                t.add(s)
        t.raze(d)
        # re-plan around both swapped cities; if that leaves a moved settle that isn't
        # next to any raze, redo it re-planning only around the new raze
        for centers in ([c, d], [d]):
            area = region_of(pr, centers, 3 * pr.D)
            u = t.copy()
            u.settle(area)
            u = shake(u, area, 150, self.rng)
            razed = [x for x in pr.cities if x not in u.kept]
            if centers == [d] or all(s in self.baseline or
                                     min(pr.hex.dist(s, r) for r in razed) <= 3 * pr.D
                                     for s in u.chosen if s in area):
                return u

    def next(self, seconds=8.0):
        """Return the next raze set as (razed, kept, new), or None.
        After None, self.exhausted says whether every one-city swap has been tried."""
        pr, t0 = self.pr, time.time()
        while self.queue:
            rset, lay = self.queue[0]
            for c in rset:
                if c in pr.forced_set:
                    continue                                         # forced razes stay razed
                cands = sorted((d for d in pr.razable if d not in rset),
                               key=lambda d: pr.hex.dist(c, d))      # nearby swaps first
                for d in cands:
                    key = tuple(sorted((set(rset) - {c}) | {d}))
                    if key in self.tried:
                        continue
                    if time.time() - t0 > seconds:
                        return None                                  # out of time, not exhausted
                    self.tried.add(key)
                    t = self.swap(lay, c, d)
                    if t.total() >= self.target:
                        t = tidy(t, self.baseline)
                        self.queue.append((key, t))
                        return (list(key), sorted(t.kept), sorted(t.chosen))
            self.queue.pop(0)
        self.exhausted = True
        return None


def cpsat(pr, n, hint, baseline, seconds):
    from ortools.sat.python import cp_model
    m = cp_model.CpModel()
    v = {p: m.NewBoolVar("") for p in pr.adj}
    for p in pr.protected:
        m.Add(v[p] == 1)
    for p in pr.forced:
        m.Add(v[p] == 0)
    seen = set()
    for a, nb in pr.adj.items():
        for b in nb:
            if (b, a) not in seen:
                seen.add((a, b))
                m.AddBoolOr([v[a].Not(), v[b].Not()])
    if pr.razable:
        m.Add(sum(v[c] for c in pr.razable) >= len(pr.razable) - n)
    bs = set(baseline)
    # most cities first, then fewest razes, then stay close to the no-raze plan.
    # BIG is larger than every tie-break combined, so one more city always wins.
    keep_bonus = {c: 1000 + min(pr.pop[c], 900) for c in pr.razable}
    BIG = sum(keep_bonus.values()) + len(pr.sites) + 1
    m.Maximize(sum((BIG + (1 if s in bs else 0)) * v[s] for s in pr.sites)
               + sum((BIG + keep_bonus[c]) * v[c] for c in pr.razable))
    razed, kept, new = hint
    on = set(kept) | set(new) | pr.protected
    for p, var in v.items():
        m.AddHint(var, 1 if p in on else 0)
    sv = cp_model.CpSolver()
    sv.parameters.max_time_in_seconds = seconds
    sv.parameters.num_search_workers = 8
    st = sv.Solve(m)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return hint, False
    return ([c for c in pr.cities if not sv.Value(v[c])],
            [c for c in pr.cities if sv.Value(v[c])],
            [s for s in pr.sites if sv.Value(v[s])]), st == cp_model.OPTIMAL


def optimize_plan(pr, results, n, seconds, tried=()):
    """Run OR-Tools on plan n, hinted by the best plan known for n razes or fewer.

    Updates results in place, including later plans that haven't been optimized and are
    now beaten by this one. Returns True if plan n changed."""
    tot = lambda r: len(r[1]) + len(r[2])
    old = results[n]
    hint = old
    if n > 0 and tot(results[n - 1]) > tot(hint):
        hint = results[n - 1]
    baseline = results[0][2]
    plan, opt = cpsat(pr, n, hint[:3], baseline, seconds)
    if tot(plan) >= tot(old):
        lay = tidy(Layout(pr, plan[1], plan[2]), baseline)
        results[n] = ([c for c in pr.cities if c not in lay.kept],
                      sorted(lay.kept), sorted(lay.chosen), opt)
    else:                                            # nothing better found: keep ours
        results[n] = (*old[:3], opt)
    for m in range(n + 1, max(results) + 1):
        if m not in tried and tot(results[m]) < tot(results[n]):
            results[m] = (*results[n][:3], False)
    return results[n][:3] != old[:3]




# =================================================================== popup
CIV_COLORS = {   # name/adjective/leader -> (background colour = dot fill, icon colour = ring)
    ('america', 'american', 'washington'): ((0.122, 0.2, 0.471), (1.0, 1.0, 1.0)),
    ('arabia', 'arabian', 'harun al-rashid'): ((0.169, 0.345, 0.18), (0.573, 0.867, 0.039)),
    ('ashurbanipal', 'assyria', 'assyrian'): ((1.0, 0.957, 0.682), (1.0, 0.663, 0.051)),
    ('austria', 'austrian', 'maria theresa'): ((0.922, 0.0, 0.0), (1.0, 1.0, 1.0)),
    ('aztec', 'aztecs', 'montezuma'): ((0.633, 0.224, 0.137), (0.537, 0.937, 0.835)),
    ('babylon', 'babylonian', 'nebuchadnezzar ii'): ((0.169, 0.318, 0.384), (0.788, 0.973, 1.0)),
    ('brazil', 'brazilian', 'pedro ii'): ((0.588, 0.871, 0.039), (0.164, 0.329, 0.176)),
    ('byzantine', 'byzantium', 'theodora'): ((0.447, 0.635, 0.914), (0.239, 0.0, 0.427)),
    ('carthage', 'carthaginian', 'dido'): ((0.804, 0.804, 0.804), (0.318, 0.0, 0.537)),
    ('boudicca', 'celtic', 'celts'): ((0.086, 0.361, 0.247), (0.58, 0.667, 1.0)),
    ('china', 'chinese', 'wu zetian'): ((0.0, 0.584, 0.322), (1.0, 1.0, 1.0)),
    ('danish', 'denmark', 'harald bluetooth'): ((0.427, 0.167, 0.082), (0.941, 0.906, 0.706)),
    ('dutch', 'netherlands', 'william'): ((1.0, 0.565, 0.0), (1.0, 1.0, 1.0)),
    ('egypt', 'egyptian', 'ramesses ii'): ((1.0, 0.988, 0.012), (0.325, 0.0, 0.816)),
    ('elizabeth', 'england', 'english'): ((0.427, 0.008, 0.0), (1.0, 1.0, 1.0)),
    ('ethiopia', 'ethiopian', 'haile selassie'): ((0.007, 0.156, 0.057), (1.0, 0.18, 0.18)),
    ('france', 'french', 'napoleon'): ((0.255, 0.553, 0.996), (0.922, 0.922, 0.545)),
    ('bismarck', 'german', 'germany'): ((0.702, 0.698, 0.722), (0.145, 0.169, 0.129)),
    ('alexander', 'greece', 'greek'): ((1.0, 1.0, 1.0), (0.255, 0.553, 0.996)),
    ('attila', 'hunnic', 'huns'): ((0.706, 0.698, 0.643), (0.275, 0.0, 0.016)),
    ('inca', 'incan', 'pachacuti'): ((1.0, 0.725, 0.133), (0.027, 0.627, 0.467)),
    ('gandhi', 'india', 'indian'): ((0.071, 0.533, 0.027), (1.0, 0.6, 0.196)),
    ('gajah mada', 'indonesia', 'indonesian'): ((0.435, 0.827, 0.855), (0.624, 0.184, 0.114)),
    ('hiawatha', 'iroquois'): ((0.255, 0.341, 0.341), (0.988, 0.792, 0.506)),
    ('japan', 'japanese', 'oda nobunaga'): ((1.0, 1.0, 1.0), (0.722, 0.0, 0.0)),
    ('korea', 'korean', 'sejong'): ((0.106, 0.129, 0.377), (1.0, 0.0, 0.0)),
    ('maya', 'mayan', 'pacal'): ((0.776, 0.553, 0.388), (0.094, 0.247, 0.259)),
    ('genghis khan', 'mongolia', 'mongolian'): ((0.318, 0.0, 0.035), (1.0, 0.471, 0.0)),
    ('ahmad al-mansur', 'moroccan', 'morocco'): ((0.567, 0.008, 0.0), (0.157, 0.702, 0.314)),
    ('ottoman', 'ottomans', 'suleiman'): ((0.989, 0.976, 0.784), (0.071, 0.322, 0.118)),
    ('darius i', 'persia', 'persian'): ((0.694, 0.031, 0.012), (0.961, 0.902, 0.216)),
    ('casimir iii', 'poland', 'polish'): ((0.968, 0.02, 0.0), (0.224, 0.0, 0.0)),
    ('kamehameha', 'polynesia', 'polynesian'): ((0.851, 0.349, 0.0), (1.0, 1.0, 0.294)),
    ('maria i', 'portugal', 'portuguese'): ((1.0, 1.0, 1.0), (0.016, 0.078, 0.49)),
    ('augustus caesar', 'roman', 'rome'): ((0.275, 0.0, 0.463), (0.941, 0.78, 0.0)),
    ('catherine', 'russia', 'russian'): ((0.937, 0.706, 0.0), (0.0, 0.0, 0.0)),
    ('pocatello', 'shoshone'): ((0.29, 0.231, 0.18), (0.098, 0.941, 0.808)),
    ('ramkhamhaeng', 'siam', 'siamese'): ((0.961, 0.602, 0.216), (0.694, 0.031, 0.012)),
    ('askia', 'songhai'): ((0.839, 0.569, 0.075), (0.353, 0.0, 0.039)),
    ('isabella', 'spain', 'spanish'): ((0.329, 0.102, 0.102), (1.0, 0.659, 0.659)),
    ('gustavus adolphus', 'sweden', 'swedish'): ((0.031, 0.031, 0.651), (0.976, 0.969, 0.012)),
    ('enrico dandolo', 'venetian', 'venice'): ((0.4, 0.134, 0.635), (1.0, 0.996, 0.847)),
    ('shaka', 'zulu', 'zulus'): ((1.0, 0.909, 0.839), (0.417, 0.196, 0.098)),
}

_COLOR_LOOKUP = {k: v for keys, v in CIV_COLORS.items() for k in keys}


UNKNOWN_COLOR = ((0.5, 0.5, 0.5), (1.0, 1.0, 1.0))   # only for a civ missing from the table
CITY_STATE_COLOR = ((0.62, 0.62, 0.62), (0.12, 0.12, 0.12))   # territory of every city-state


def civ_color(players, owner):
    """(fill, ring) for a major civ from the table above, or None if it isn't listed."""
    name = ((players.get(owner) or {}).get("name") or "").lower().strip()
    if name.startswith("the "):
        name = name[4:]
    return _COLOR_LOOKUP.get(name)


def show(W, H, tiles, cities, players, pr, results, options, exhausted, save_cb=lambda: None,
         resources=None, me=None, tried=None, marks=None, recompute=None, opt_seconds=0,
         territory=None, ui=None):
    """The map window. territory is {(x, y): owner} for every owned tile (or None). results / options / exhausted / tried / marks are the saved run's own
    dicts and sets: the window edits them in place, and save_cb() stores them.
      tried        plans OR-Tools has already been run on (it runs when you click a plan)
      marks        your applied right-click marks, {pos: "keep" | "raze"}
      recompute    fn(marks, log) -> (problem, results): the quick search with those marks
      opt_seconds  seconds of OR-Tools per plan click (0: OR-Tools isn't available)"""
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection, PolyCollection
    from matplotlib.lines import Line2D
    from matplotlib.widgets import Button

    tried = set() if tried is None else tried
    applied = {} if marks is None else marks         # saved with the run
    marks = dict(applied)                            # working copy that right-click edits
    S3 = math.sqrt(3)
    hx = [math.cos(math.radians(60 * i - 30)) for i in range(6)]
    hy = [math.sin(math.radians(60 * i - 30)) for i in range(6)]
    X = lambda p: S3 * (p[0] + 0.5 * (p[1] & 1))
    Y = lambda p: 1.5 * p[1]
    info = {c["pos"]: c for c in cities}
    baseline = lambda: set(results[0][2])
    ref = lambda: "forced razes only" if pr.user_forced else "no razing"
    base_total = lambda: len(results[0][1]) + len(results[0][2])
    owner_name = lambda o: (players.get(o) or {}).get("name") or f"player {o}"
    color_of, unknown = {}, set()
    for c in cities:
        col = None if c["minor"] else civ_color(players, c["owner"])
        if col is None and not c["minor"]:
            unknown.add(owner_name(c["owner"]))
        color_of[c["pos"]] = col or UNKNOWN_COLOR
    if unknown:
        print("No colour for: " + ", ".join(sorted(unknown)) + "  (add to CIV_COLORS)")

    fig = plt.figure(figsize=(16, 9.5))
    try:
        fig.canvas.manager.set_window_title("Civ V raze & resettle")
    except Exception:
        pass
    ax = fig.add_axes([0.2, 0.03, 0.79, 0.86])
    ax.set_facecolor("#1d4e66")
    polys, cols = [], []
    for p, ch in tiles.items():
        polys.append([(X(p) + hx[i] * 1.01, Y(p) + hy[i] * 1.01) for i in range(6)])
        cols.append("#7da355" if ch == "1" else "#77706a")
    ax.add_collection(PolyCollection(polys, facecolors=cols, edgecolors="#00000022", linewidths=0.3))
    ax.set_xlim(-2, S3 * (W + 1))
    ax.set_ylim(-2, 1.5 * H + 1)
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    title = ax.set_title("", fontsize=13)

    territory = territory or {}
    ui = {} if ui is None else ui                    # saved with the run: toggles, plan, raze set
    state = {"n": 0, "opt": 0, "plan": ui.get("plan", True), "res": ui.get("res", True),
             "terr": bool(territory) and ui.get("terr", True)}
    layer = []
    resources = resources or {}
    searchers, notes, optnote = {}, {}, {}

    # ---- territory: tinted tiles and double-line borders in each civ's own colours
    minor_ids = {c["owner"] for c in cities if c["minor"]}
    terr_artists = []
    if territory:
        def neighbor(x, y, k):               # the tile across edge k: E, NE, NW, W, SW, SE
            odd = y & 1
            return ((x + 1, y), (x + odd, y + 1), (x + odd - 1, y + 1),
                    (x - 1, y), (x + odd - 1, y - 1), (x + odd, y - 1))[k]
        polys_t, fills, edges, dark, rings = [], [], [], [], []
        for (x, y), o in territory.items():
            fill, ring = CITY_STATE_COLOR if o in minor_ids else (civ_color(players, o) or UNKNOWN_COLOR)
            cx, cy = X((x, y)), Y((x, y))
            polys_t.append([(cx + hx[i], cy + hy[i]) for i in range(6)])
            fills.append((*fill, 0.34))
            for k in range(6):
                nx, ny = neighbor(x, y, k)
                if pr.hex.wrap:
                    nx %= W                  # borders also close across the map seam
                if territory.get((nx, ny)) != o:         # edge facing another owner, or nobody
                    k2 = (k + 1) % 6
                    edges.append([(cx + hx[k] * 0.9, cy + hy[k] * 0.9),
                                  (cx + hx[k2] * 0.9, cy + hy[k2] * 0.9)])
                    dark.append(tuple(v * 0.8 for v in fill))
                    rings.append(ring)
        terr_artists.append(ax.add_collection(PolyCollection(
            polys_t, facecolors=fills, edgecolors="none", linewidths=0, antialiased=False, zorder=1.2)))
        terr_artists.append(ax.add_collection(LineCollection(
            edges, colors=dark, linewidths=3.4, capstyle="round", zorder=1.5)))
        terr_artists.append(ax.add_collection(LineCollection(
            edges, colors=rings, linewidths=1.2, capstyle="round", zorder=1.6)))
        for a in terr_artists:
            a.set_visible(state["terr"])

    # ---- oil & aluminum (drawn once, toggled on/off)
    # centred on the tile, drawn under cities and stars (turn off "Plans & cities" to see them all)
    res_artists = []
    for kind, mk, fc, ec in (("oil", "s", "#111111", "#ffffff"), ("aluminum", "h", "#cfd8dc", "#263238")):
        pts = [p for p, v in resources.items() if v[0] == kind]
        if pts:
            res_artists.append(ax.scatter([X(p) for p in pts], [Y(p) for p in pts],
                                          s=70 if kind == "oil" else 95, marker=mk, c=fc,
                                          edgecolors=ec, linewidths=1.2, zorder=3.5))

    def scat(pts, **kw):
        if pts:
            layer.append(ax.scatter([X(p) for p in pts], [Y(p) for p in pts], **kw))

    def tag(n):
        if results[n][3]:
            return ""
        if n in tried:
            return "   [not proven optimal]"
        return "   [quick estimate]" if opt_seconds else "   [quick search]"

    def draw(n):
        state["n"] = n
        if (ui.get("n"), ui.get("opt")) != (n, state["opt"]):
            ui["n"], ui["opt"] = n, state["opt"]
            save_cb()                                # reopen on this plan and raze set
        for a in layer:
            a.remove()
        layer.clear()
        razed, kept, new = options[n][state["opt"]]
        base = baseline()
        newset = set(new)
        bonus = [s for s in new if s not in base]
        same = [s for s in new if s in base]
        lost = [s for s in base if s not in newset]
        majors = [p for p in kept if not info[p]["minor"]]
        minors = [p for p in kept if info[p]["minor"]]

        scat([p for p in majors if info[p]["wonders"]], s=430, facecolors="none",
             edgecolors="#e6c200", linewidths=2.4, zorder=3)
        if majors:
            scat(majors, s=150, c=[color_of[p][0] for p in majors],
                 edgecolors=[color_of[p][1] for p in majors], linewidths=2.4, zorder=4)
        if minors:
            scat(minors, s=110, marker="D", c="#111111", edgecolors="#ffffff",
                 linewidths=0.8, zorder=4)
        scat(lost, s=170, marker="*", facecolors="none", edgecolors="#cfd8dc", linewidths=1, zorder=4)
        scat(same, s=170, marker="*", c="#ffc107", edgecolors="k", linewidths=0.6, zorder=5)
        if bonus:
            scat(bonus, s=1100, c="#00e5ff", alpha=0.22, edgecolors="none", zorder=5)
            scat(bonus, s=480, marker="*", c="#00e5ff", edgecolors="w", linewidths=1.4, zorder=6)
            for s in bonus:
                if razed:
                    r = min(razed, key=lambda c: pr.hex.dist(c, s))
                    if pr.hex.dist(r, s) <= 3 * pr.D:
                        xr, xs, span = X(r), X(s), S3 * W
                        legs = [(xr, xs)]
                        if abs(xr - xs) > span / 2:      # link crosses the map seam: draw it
                            shift = span if xs < xr else -span   # leaving each edge
                            legs = [(xr, xs + shift), (xr - shift, xs)]
                        for a, b in legs:
                            layer.extend(ax.plot([a, b], [Y(r), Y(s)], ls="--", lw=1.4,
                                                 color="#00e5ff", alpha=0.85, zorder=5))
        scat(razed, s=260, marker="X", c="#e53935", edgecolors="w", linewidths=1, zorder=7)
        # your own Keep / Raze marks (applied or not yet)
        scat([p for p, v in marks.items() if v == "keep" and p in info], s=330, facecolors="none",
             edgecolors="#00c853", linewidths=3, zorder=4.5)
        scat([p for p, v in marks.items() if v == "raze" and p in info], s=400, facecolors="none",
             edgecolors="#d50000", linewidths=3, zorder=6.5)
        for a in layer:
            a.set_visible(state["plan"])
        total = len(kept) + len(new)
        title.set_text(
            f"{len(set(razed) - pr.burning)} razed  ->  {total} cities   ({total - base_total():+d} vs {ref()})    "
            f"{len(bonus)} cyan settles, {len(lost)} spots moved away    "
            f"pop razed: {sum(info[p]['pop'] or 0 for p in razed)}" + tag(n))
        update_opt_label()
        tip.set_visible(False)
        fig.canvas.draw_idle()

    # ---- plan buttons (one per plan)
    keys = sorted(results)
    # big clickable buttons (arrow keys also step through plans)
    ncol = 3 if len(keys) > 18 else (2 if len(keys) > 8 else 1)
    top_row = ncol == 3                    # the 0-razes plan gets its own full-width row on top
    gridrows = math.ceil((len(keys) - top_row) / ncol)
    nrow = gridrows + top_row
    top, gap = 0.95, 0.006
    bh = min(0.042, 0.42 / nrow - gap)   # shrink rows when there are many plans
    colw = 0.185 / ncol
    hdr = fig.text(0.008, top + 0.012, "", fontsize=10, weight="bold")
    buttons = []

    def label_for(n):
        t = len(results[n][1]) + len(results[n][2])
        return f"{n}: {t} ({t - base_total():+d})"

    def refresh_labels():
        for b, n in zip(buttons, keys):
            b.label.set_text(label_for(n))
        hdr.set_text("Plans  (extra razes: cities, vs forced only)" if pr.user_forced
                     else "Plans  (razed: total cities, vs no razing)")
        fig.canvas.draw_idle()

    def select(i, click=False, optimize=True):
        again = click and state.get("idx") == i      # clicking the selected plan again re-optimizes
        state["idx"] = i
        for j, b in enumerate(buttons):
            on = j == i
            b.color = "#1565c0" if on else "#eceff1"
            b.hovercolor = "#1e88e5" if on else "#cfd8dc"
            b.ax.set_facecolor(b.color)
            b.label.set_color("w" if on else "#222")
            b.label.set_weight("bold" if on and ncol < 3 else "normal")
        state["opt"] = 0
        if optimize:
            maybe_optimize(keys[i], force=again)
        draw(keys[i])

    for i, n in enumerate(keys):
        if top_row and i == 0:
            x, rw, w = 0.005, 0, 0.185 - 0.004
        else:
            j = i - top_row
            x, rw, w = 0.005 + (j // gridrows) * colw, j % gridrows + top_row, colw - 0.004
        bax = fig.add_axes([x, top - (rw + 1) * (bh + gap), w, bh])
        b = Button(bax, label_for(n))
        b.label.set_fontsize({1: 12, 2: 10.5, 3: 8.5}[ncol])
        b.on_clicked(lambda ev, i=i: select(i, click=True))
        buttons.append(b)
    fig._buttons = buttons
    fig._select = select

    def on_key(ev):
        i = state.get("idx", 0)
        if ev.key in ("right", "down"):
            select(min(len(keys) - 1, i + 1))
        elif ev.key in ("left", "up"):
            select(max(0, i - 1))
        elif ev.key == "escape":
            close_menu()
    fig.canvas.mpl_connect("key_press_event", on_key)
    row = bh + gap

    # ---- cycle through other raze sets for the same plan size
    oy = top - nrow * row - 0.012 - bh
    bprev = Button(fig.add_axes([0.005, oy, 0.06, bh]), "< Prev")
    bnext = Button(fig.add_axes([0.068, oy, 0.122, bh]), "Next option >")
    for b in (bprev, bnext):
        b.label.set_fontsize(11)
    opt_text = fig.text(0.008, oy - 0.008, "", fontsize=9, va="top", color="#333")

    def update_opt_label():
        n = state["n"]
        extra = notes.get(n) or optnote.get(n) or ""
        if not set(options[n][0][0]) - pr.burning:
            line = "No razes in this plan."
        else:
            more = "" if n in exhausted else "+"
            line = (f"Raze set {state['opt'] + 1} of {len(options[n])}{more} "
                    f"for {len(set(options[n][0][0]) - pr.burning)} razes")
        opt_text.set_text(line + ("\n" + extra if extra else ""))

    def next_opt(ev=None):
        n = state["n"]
        if not set(options[n][0][0]) - pr.burning:
            return
        notes[n] = ""
        if state["opt"] + 1 < len(options[n]):
            state["opt"] += 1
            draw(n)
            return
        if n in exhausted:
            notes[n] = "No more found (all one-city swaps tried)."
            draw(n)
            return
        opt_text.set_text("Searching for another raze set...")
        fig.canvas.draw()
        fig.canvas.flush_events()
        if n not in searchers:
            searchers[n] = AltSearch(pr, options[n], results[0][2], seed=n)
        alt = searchers[n].next(seconds=8.0)
        if alt:
            options[n].append(alt)
            state["opt"] = len(options[n]) - 1
            save_cb()
        elif searchers[n].exhausted:
            exhausted.add(n)
            save_cb()
            notes[n] = "No more found (all one-city swaps tried)."
        else:
            notes[n] = "None found in 8 s. Click again to keep searching."
        draw(n)

    def prev_opt(ev=None):
        n = state["n"]
        notes[n] = ""
        if len(options[n]) > 1:
            state["opt"] = (state["opt"] - 1) % len(options[n])
            draw(n)

    bnext.on_clicked(next_opt)
    bprev.on_clicked(prev_opt)
    fig._opt_buttons = (bprev, bnext)
    fig._next_opt = next_opt

    def on_key2(ev):
        if ev.key == "n":
            next_opt()
        elif ev.key == "p":
            prev_opt()
    fig.canvas.mpl_connect("key_press_event", on_key2)

    # ---- the top strip: apply marks, clear marks, a status line, empty Lua.log
    HINT = "Right-click a city to mark it Keep or Raze."
    SY, SH = 0.948, 0.04
    b_apply = Button(fig.add_axes([0.2, SY, 0.17, SH]), "Apply marks & recompute")
    b_reset = Button(fig.add_axes([0.375, SY, 0.1, SH]), "Clear marks")
    for b in (b_apply, b_reset):
        b.label.set_fontsize(10)
    strip_msg = fig.text(0.485, SY + SH / 2, HINT, fontsize=10, va="center", color="#444")

    def hint_say(text, color="#444"):
        strip_msg.set_text(text)
        strip_msg.set_color(color)
        fig.canvas.draw_idle()

    def busy(text):
        """Show a message right now, even while a long step is running."""
        strip_msg.set_text(text)
        strip_msg.set_color("#e65100")
        fig.canvas.draw()
        fig.canvas.flush_events()

    def n_changes():
        return sum(1 for p in set(marks) | set(applied) if marks.get(p) != applied.get(p))

    def update_apply():
        d = n_changes()
        if d:
            b_apply.label.set_text(f"Apply {d} change{'s' if d != 1 else ''} & recompute")
            b_apply.color, b_apply.hovercolor = "#ffb74d", "#ffa726"
            hint_say(f"{d} mark change{'s' if d != 1 else ''} not applied yet.", "#e65100")
        else:
            b_apply.label.set_text("Apply marks & recompute")
            b_apply.color, b_apply.hovercolor = "#eceff1", "#cfd8dc"
            hint_say(HINT)
        b_apply.ax.set_facecolor(b_apply.color)
        fig.canvas.draw_idle()

    def apply_marks(ev=None):
        nonlocal pr
        if recompute is None or not n_changes():
            hint_say("No mark changes to apply.")
            return
        t0, last_t = time.time(), [0.0]

        def log(m):                                  # progress, a few times a second at most
            if time.time() - last_t[0] > 0.4:
                last_t[0] = time.time()
                busy("Recomputing: " + m.strip())
        busy("Recomputing with your marks...")
        try:
            new_pr, new_results = recompute(marks, log)
        except Exception as e:                       # leave everything as it was
            hint_say(f"Recompute failed: {e}", "#c62828")
            return
        pr = new_pr
        results.clear()
        results.update(new_results)
        options.clear()
        options.update({n: [results[n][:3]] for n in results})
        exhausted.clear()
        tried.clear()
        searchers.clear()
        notes.clear()
        optnote.clear()
        applied.clear()
        applied.update(marks)
        refresh_labels()
        save_cb()
        select(state.get("idx", 0))
        update_apply()
        hint_say(f"Recomputed in {time.time() - t0:.0f}s with {len(applied)} marks.", "#2e7d32")

    def clear_marks(ev=None):
        marks.clear()
        last["key"] = None
        draw(state["n"])
        update_apply()

    b_apply.on_clicked(apply_marks)
    b_reset.on_clicked(clear_marks)
    fig._marks_ui = (b_apply, b_reset, marks, applied)
    fig._apply = apply_marks

    # ---- OR-Tools on demand: the quick pass gave the estimates, clicking a plan polishes it
    def maybe_optimize(n, force=False):
        if not opt_seconds or results[n][3] or (n in tried and not force):
            return
        busy(f"Optimizing plan {n} with OR-Tools ({opt_seconds:g}s)...")
        old = results[n][:3]
        optimize_plan(pr, results, n, opt_seconds, tried)
        tried.add(n)
        if results[n][:3] != old:                    # a different plan: old alternates don't apply
            options[n] = [results[n][:3]]
            exhausted.discard(n)
            searchers.pop(n, None)
        for m in results:                            # later plans may have inherited this result
            if len(options[m]) == 1 and options[m][0] != results[m][:3]:
                options[m] = [results[m][:3]]
        optnote[n] = ("Proven optimal." if results[n][3]
                      else f"Best in {opt_seconds:g}s. Click the plan again to retry.")
        refresh_labels()
        save_cb()
        update_apply()

    # ---- empty the game's Lua.log (asks first; says why if it can't)
    b_clear = Button(fig.add_axes([0.81, SY, 0.18, SH]), "Empty Lua.log")
    b_yes = Button(fig.add_axes([0.81, SY, 0.088, SH]), "Yes, empty it")
    b_no = Button(fig.add_axes([0.902, SY, 0.088, SH]), "Cancel")
    for b in (b_clear, b_yes, b_no):
        b.label.set_fontsize(10)
    b_yes.color, b_yes.hovercolor = "#ef9a9a", "#e57373"
    b_yes.ax.set_facecolor(b_yes.color)
    log_msg = fig.text(0.205, 0.006, "", fontsize=10, va="bottom")

    def log_say(text, color="#333"):
        log_msg.set_text(text)
        log_msg.set_color(color)
        fig.canvas.draw_idle()

    def log_confirm(on):
        for b, shown in ((b_clear, not on), (b_yes, on), (b_no, on)):
            b.ax.set_visible(shown)
            b.set_active(shown)
        fig.canvas.draw_idle()

    def log_ask(ev=None):
        log_confirm(True)
        log_say(f"Empty {DEFAULT_LOG}{log_size()}?  This can't be undone.", "#e65100")

    def log_cancel(ev=None):
        log_confirm(False)
        log_say("")

    def log_empty(ev=None):
        log_confirm(False)
        ok, text = empty_log()
        log_say(text, "#2e7d32" if ok else "#c62828")

    b_clear.on_clicked(log_ask)
    b_yes.on_clicked(log_empty)
    b_no.on_clicked(log_cancel)
    log_confirm(False)
    fig._log_ui = (b_clear, b_yes, b_no, log_msg)

    # ---- layer toggles + legend under the plans list
    def tally(kind):
        v = [(a, o) for (k, a, o) in resources.values() if k == kind]
        mine = [a for a, o in v if me is not None and o == me]
        return f"{len(v)} tiles ({sum(a for a, _ in v)}), yours {len(mine)} ({sum(mine)})"

    # two big on/off buttons (green = shown)
    toggles = {}

    def paint(key):
        b, label = toggles[key]
        on = state[key]
        b.color = "#2e7d32" if on else "#eceff1"
        b.hovercolor = "#43a047" if on else "#cfd8dc"
        b.ax.set_facecolor(b.color)
        b.label.set_text(f"{label}: {'ON' if on else 'OFF'}")
        b.label.set_color("w" if on else "#222")
        b.label.set_weight("bold" if on else "normal")

    def toggle(key):
        state[key] = not state[key]
        ui[key] = state[key]
        save_cb()
        for a in {"res": res_artists, "terr": terr_artists}.get(key, layer):
            a.set_visible(state[key])
        title.set_visible(state["plan"])
        tip.set_visible(False)
        last["key"] = None
        paint(key)
        fig.canvas.draw_idle()

    for i, (key, label) in enumerate((("res", "Oil & aluminum"), ("plan", "Plans & cities"))):
        b = Button(fig.add_axes([0.005, oy - 0.085 - i * 0.055, 0.185, 0.048]), label)
        b.label.set_fontsize(12)
        b.on_clicked(lambda ev, key=key: toggle(key))
        toggles[key] = (b, label)
        paint(key)
    b_terr = Button(fig.add_axes([0.70, SY, 0.105, SH]), "Territory")
    b_terr.label.set_fontsize(10)
    b_terr.on_clicked(lambda ev: toggle("terr"))
    toggles["terr"] = (b_terr, "Territory")
    paint("terr")
    if not territory:
        b_terr.label.set_text("Territory: n/a")
        b_terr.set_active(False)
    fig._toggles = toggles
    fig._toggle = toggle

    no_res = "" if resources else " (none: re-run the export)"
    fig.legend(handles=[
        Line2D([], [], marker="o", ls="", color="#888", mec="#111", ms=10, label="city (civ colours)"),
        Line2D([], [], marker="D", ls="", color="#111", mec="#fff", ms=8, label="city-state (never razed)"),
        Line2D([], [], marker="o", ls="", mfc="none", mec="#e6c200", mew=2.4, ms=15, label="has wonder(s)"),
        Line2D([], [], marker="o", ls="", mfc="none", mec="#00c853", mew=3, ms=13, label="your mark: keep"),
        Line2D([], [], marker="o", ls="", mfc="none", mec="#d50000", mew=3, ms=13, label="your mark: raze"),
        Line2D([], [], marker="X", ls="", color="#e53935", ms=11, label="raze"),
        Line2D([], [], marker="*", ls="", color="#ffc107", mec="k", ms=13, label="new settle (same as no-raze plan)"),
        Line2D([], [], marker="*", ls="", color="#00e5ff", mec="k", ms=17, label="new or moved settle, thanks to razing"),
        Line2D([], [], marker="*", ls="", mfc="none", mec="#607d8b", ms=13, label="no-raze settle spot that moved away"),
        Line2D([], [], marker="s", ls="", color="#111", mec="#fff", ms=8,
               label="oil" + (f": {tally('oil')}" if resources else no_res)),
        Line2D([], [], marker="h", ls="", color="#cfd8dc", mec="#263238", ms=10,
               label="aluminum" + (f": {tally('aluminum')}" if resources else no_res)),
    ], loc="upper left", bbox_to_anchor=(0.003, oy - 0.15), fontsize=8.5, frameon=True,
        title="Legend", title_fontsize=10, borderpad=0.6, labelspacing=0.55)

    # ---- hover tooltip
    tip = ax.annotate("", (0, 0), xytext=(14, 14), textcoords="offset points", fontsize=9.5,
                      zorder=20, visible=False, color="#111",
                      bbox=dict(boxstyle="round,pad=0.45", fc="#fffde7", ec="#555", alpha=0.97))
    last = {"key": None}

    def describe(p):
        parts = []
        if state["plan"]:
            d = describe_plan(p)
            if d:
                parts.append(d)
        # territory only adds a line to tiles that already have a tooltip (settle spots);
        # plain tiles stay silent so dragging across the map doesn't pop a box on every hex
        if parts and state["terr"] and p in territory and p not in info:
            o = territory[p]
            parts.append(f"Territory: {owner_name(o)}" + ("  (city-state)" if o in minor_ids else ""))
        if state["res"] and p in resources:
            kind, amt, owner = resources[p]
            who = ("yours" if me is not None and owner == me else
                   "unowned" if owner < 0 else f"{owner_name(owner)}'s territory")
            line = f"{kind.capitalize()}: {amt}  ({who})"
            parts.append(line if parts else f"{line}\nTile ({p[0]}, {p[1]})")
        return "\n".join(parts) if parts else None

    def describe_plan(p):
        n = state["n"]
        razed, kept, new = options[n][state["opt"]]
        base = baseline()
        if p in info:
            c = info[p]
            lines = [f"{c['name']}  ({p[0]}, {p[1]})",
                     f"Owner: {owner_name(c['owner'])}" + ("  (city-state)" if c["minor"] else "")]
            if c["pop"] is not None:
                lines.append(f"Population: {c['pop']}")
            if c.get("razing"):
                lines.append("Being razed right now")
            if c["cap"]:
                lines.append("Capital")
            if c["holy"]:
                lines.append("Holy city")
            if c["wonders"]:
                lines.append("Wonders: " + ", ".join(c["wonders"]))
            elif c["wonders"] is not None:
                lines.append("Wonders: none")
            if p in razed:
                lines.append("PLAN: RAZE" + ("  (already burning)" if c.get("razing") else
                                              "  (forced)" if p in pr.forced_set else ""))
            elif pr.why[p]:
                lines.append("Never razed: " + ", ".join(pr.why[p]))
            else:
                lines.append("PLAN: keep")
            if p in marks:
                lines.append(f"Your mark: {marks[p].upper()}"
                             + ("" if applied.get(p) == marks[p] else "  (not applied yet)"))
            lines.append("Right-click to mark Keep / Raze")
            return "\n".join(lines)
        if p in set(new):
            kind = ("New settle, same spot as the no-raze plan" if p in base
                    else "New/moved settle, possible thanks to razing")
            return f"{kind}\nFound a city at ({p[0]}, {p[1]})"
        if p in base:
            return f"No-raze plan had a settle here ({p[0]}, {p[1]})\nIt moved to fit more cities"
        return None

    def hex_at(ev):
        """The tile nearest the mouse and its distance (tile centres are 1.0 apart or more)."""
        r = int(round(ev.ydata / 1.5))
        best, bd = None, 1e9
        for yy in (r - 1, r, r + 1):
            if not 0 <= yy < H:
                continue
            c0 = int(round(ev.xdata / S3 - 0.5 * (yy & 1)))
            for xx in (c0 - 1, c0, c0 + 1):
                d = math.hypot(X((xx, yy)) - ev.xdata, Y((xx, yy)) - ev.ydata)
                if d < bd:
                    bd, best = d, (xx, yy)
        return best, bd

    def on_move(ev):
        if ev.inaxes is not ax or ev.xdata is None:
            if tip.get_visible():
                tip.set_visible(False)
                last["key"] = None
                fig.canvas.draw_idle()
            return
        best, bd = hex_at(ev)
        text = describe(best) if best and bd < 1.0 else None
        key = (best, state["n"], state["opt"], state["plan"], state["res"], state["terr"]) if text else None
        if key == last["key"]:
            return
        last["key"] = key
        if text:
            tip.xy = (X(best), Y(best))
            tip.set_text(text)
            # flip the box to the left/below near the right/top edges
            x0, x1 = ax.get_xlim()
            y0, y1 = ax.get_ylim()
            tip.set_position((-14 if X(best) > x0 + 0.7 * (x1 - x0) else 14,
                              -14 if Y(best) > y0 + 0.75 * (y1 - y0) else 14))
            tip.set_ha("right" if X(best) > x0 + 0.7 * (x1 - x0) else "left")
            tip.set_va("top" if Y(best) > y0 + 0.75 * (y1 - y0) else "bottom")
            tip.set_visible(True)
        else:
            tip.set_visible(False)
        fig.canvas.draw_idle()

    fig.canvas.mpl_connect("motion_notify_event", on_move)
    fig._describe = describe

    # ---- right-click a city: Keep / Raze / Clear
    menu = {"axes": [], "buttons": []}

    def close_menu():
        for b in menu["buttons"]:
            b.disconnect_events()
        for a in menu["axes"]:
            a.remove()
        menu["axes"].clear()
        menu["buttons"].clear()
        fig.canvas.draw_idle()

    def set_mark(p, val):
        if val is None:
            marks.pop(p, None)
        else:
            marks[p] = val
        close_menu()
        last["key"] = None
        draw(state["n"])
        update_apply()

    def open_menu(p, ev):
        close_menu()
        cur = marks.get(p)
        w, h, th = 0.09, 0.036, 0.03
        fx, fy = fig.transFigure.inverted().transform((ev.x, ev.y))
        x0 = min(fx, 0.99 - w)
        y0 = max(fy - 3 * h - th, 0.01)
        head = fig.add_axes([x0, y0 + 3 * h, w, th])
        head.set_xticks([])
        head.set_yticks([])
        head.set_facecolor("#fff8e1")
        head.text(0.5, 0.5, info[p]["name"][:16], ha="center", va="center", fontsize=9, weight="bold")
        head.set_zorder(30)
        menu["axes"].append(head)
        for k, (label, val) in enumerate((("Keep", "keep"), ("Raze", "raze"), ("Clear mark", None))):
            bax = fig.add_axes([x0, y0 + (2 - k) * h, w, h])
            bax.set_zorder(30)
            b = Button(bax, ("> " if cur == val else "") + label)
            b.label.set_fontsize(10)
            b.on_clicked(lambda e, val=val: set_mark(p, val))
            menu["axes"].append(bax)
            menu["buttons"].append(b)
        fig.canvas.draw_idle()

    def on_press(ev):
        if menu["axes"] and ev.inaxes not in menu["axes"]:
            close_menu()
        if ev.button == 3 and ev.inaxes is ax and ev.xdata is not None:
            p, bd = hex_at(ev)
            if p in info and bd < 1.0:
                open_menu(p, ev)

    fig.canvas.mpl_connect("button_press_event", on_press)
    fig._open_menu = open_menu
    fig._set_mark = set_mark
    fig._menu = menu

    # reopen where you left off: same toggles, plan and raze set
    for a in res_artists:
        a.set_visible(state["res"])
    i0 = keys.index(ui["n"]) if ui.get("n") in keys else 0
    want = ui.get("opt", 0)
    refresh_labels()
    select(i0, optimize=False)
    if 0 < want < len(options[keys[i0]]):
        state["opt"] = want
        draw(keys[i0])
    title.set_visible(state["plan"])
    plt.show()


# ==================================================================== main
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "civ_plan_state.pkl")
STATE_VERSION = 1


def file_hash(path):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_state(path):
    try:
        with open(path, "rb") as fh:
            st = pickle.load(fh)
        return st if st.get("version") == STATE_VERSION else None
    except Exception:
        return None


def save_state(path, st):
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        pickle.dump(st, fh)
    os.replace(tmp, path)


def status(msg):
    """One updating console line instead of a scrolling log."""
    sys.stdout.write("\r" + msg.ljust(72))
    sys.stdout.flush()


def norm_names(names):
    return sorted(k.strip().lower() for k in names if k.strip())


def norm_keep(extra):
    return norm_names(KEEP + extra)


def build_problem(settings, data, marks):
    """The problem for a run's settings plus your right-click marks {pos: "keep" | "raze"}."""
    W, H, wrap, tiles, cities, players, me = data
    keep_pos = {p for p, v in marks.items() if v == "keep"}
    raze_pos = {p for p, v in marks.items() if v == "raze"}
    return Problem(Hex(W, H, wrap), tiles, cities, settings["min_dist"], set(settings["keep"]), me,
                   settings["allow_raze_own"], set(settings.get("raze", [])), keep_pos, raze_pos)


def make_recompute(settings, data):
    """The quick search again with new marks: what the window's Apply button runs."""
    def run(marks, log=lambda m: None):
        pr = build_problem(settings, data, marks)
        plans = heuristic_all(pr, settings["max_razes"], log=log)
        return pr, {n: (*p, False) for n, p in plans.items()}
    return run


def compute(settings, data, marks):
    """Quick search for every plan size (OR-Tools runs later, when you click a plan)."""
    W, H, wrap, tiles, cities, players, me = data
    keep_names, raze_names = set(settings["keep"]), set(settings["raze"])
    known = {c["name"].lower() for c in cities}
    for k in sorted(keep_names - known):
        print(f"WARNING: keep-list city '{k}' isn't on the map (check the spelling).")
    for k in sorted(raze_names - known):
        print(f"WARNING: --raze city '{k}' isn't on the map (check the spelling).")
    for k in sorted(keep_names & raze_names):
        print(f"WARNING: '{k}' is on both the keep list and --raze; razing it.")
    pr = build_problem(settings, data, marks)
    by_pos = {c["pos"]: c for c in cities}
    if pr.user_forced:
        print(f"Forced razes ({len(pr.user_forced)}): " + ", ".join(
            by_pos[p]["name"] + (f" ({', '.join(pr.forced_why[p])})" if pr.forced_why[p] else "")
            for p in pr.user_forced))
    if pr.burning:
        print(f"Being razed right now, counted as gone ({len(pr.burning)}): "
              + ", ".join(sorted(by_pos[p]["name"] for p in pr.burning)))

    print(f"Map {W}x{H}, {len(tiles)} land tiles, {len(cities)} cities "
          f"({sum(c['minor'] for c in cities)} city-states), {len(pr.protected)} never razed.")
    if me is None:
        print("NOTE: the dump doesn't say which player you are, so your own cities aren't\n"
              "      protected. Re-run the Lua command at the top of this script, or pass --me N.")
    elif not settings["allow_raze_own"]:
        mine = sum(1 for c in cities if c["owner"] == me)
        print(f"You are player {me} ({(players.get(me) or {}).get('name', '?')}): "
              f"your {mine} cities are never razed (--allow-raze-own to change).")
    if any(c["cap"] is None for c in cities):
        print("NOTE: old dump format: capitals, wonders and holy cities are unknown.\n"
              "      Re-run the Lua command at the top of this script to fix that.")
    if all(c.get("razing") is None for c in cities):
        print("NOTE: the dump doesn't say which cities are being razed right now. Re-run the Lua\n"
              "      command at the top of this script (if it still says this, the game can't report it).")

    t0 = time.time()
    results = {n: (*p, False) for n, p in heuristic_all(
        pr, settings["max_razes"], log=lambda m: status("Quick search: " + m.strip())).items()}
    print(f"\rQuick search took {time.time() - t0:.0f}s.".ljust(72))
    return pr, results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dump", nargs="?", default=None,
                    help="console output saved to a file (default: the Civ V Lua.log)")
    ap.add_argument("--rerun", action="store_true",
                    help="recompute instead of opening the saved run")
    ap.add_argument("--max-razes", type=int, default=None, help="default 36")
    ap.add_argument("--min-dist", type=int, default=None,
                    help="distance between city centres; 4 (default) = three empty tiles between")
    ap.add_argument("--keep", action="append", default=[], metavar="CITY",
                    help="city never to raze (repeat the flag for several)")
    ap.add_argument("--raze", action="append", default=[], metavar="CITY",
                    help="city that MUST be razed in every plan, overriding all protection "
                         "(repeat the flag for several)")
    ap.add_argument("--allow-raze-own", action="store_true", default=None,
                    help="allow razing cities you already own (off by default)")
    ap.add_argument("--me", type=int, default=None, help="your player number (default: from the dump)")
    ap.add_argument("--time", type=float, default=3,
                    help="seconds of OR-Tools each time you click a plan (default 3)")
    ap.add_argument("--state", default=STATE_FILE, help="where the saved run lives")
    ap.add_argument("--no-popup", action="store_true")
    ap.add_argument("--clear-log", action="store_true",
                    help="empty the game's Lua.log (asks first) and exit")
    args = ap.parse_args()
    if args.clear_log:
        sys.exit(clear_log_cli())

    dump = args.dump or DEFAULT_LOG
    saved = load_state(args.state)
    st = None if args.rerun else saved

    if st is None:                                   # first run, or --rerun
        if not os.path.exists(dump):
            sys.exit(f"File not found: {dump}\nUse: python civ_plan.py dump.txt")
        if args.max_razes is None:
            args.max_razes = 36
        if args.min_dist is None:
            args.min_dist = 4
        args.allow_raze_own = bool(args.allow_raze_own)
        settings = dict(max_razes=args.max_razes, min_dist=args.min_dist, keep=norm_keep(args.keep),
                        raze=norm_names(args.raze), allow_raze_own=args.allow_raze_own, me=args.me)
        data = list(parse(dump))
        if args.me is not None:
            data[6] = args.me
        data = tuple(data)
        on_map = {c["pos"] for c in data[4]}         # keep your right-click marks across a --rerun
        marks = {p: v for p, v in (saved or {}).get("marks", {}).items() if p in on_map}
        if marks:
            print(f"Kept {len(marks)} of your Keep/Raze marks from the saved run.")
        pr, results = compute(settings, data, marks)
        st = dict(version=STATE_VERSION, saved_at=time.time(), dump_hash=file_hash(dump),
                  settings=settings, data=data, results=results,
                  resources=parse_resources(dump, data[0], data[1]) or {},
                  territory=parse_territory(dump, data[0], data[1]) or {},
                  options={n: [results[n][:3]] for n in results}, exhausted=set(),
                  tried=set(), marks=marks)
        save_state(args.state, st)
        print(f"Saved this run to {args.state}")
    else:                                            # open the saved run, whatever else changed
        settings = st["settings"]
        print(f"Loaded saved run from {time.ctime(st['saved_at'])}.")
        diffs = []
        for flag, given, saved_val in (("--max-razes", args.max_razes, settings["max_razes"]),
                                       ("--min-dist", args.min_dist, settings["min_dist"]),
                                       ("--me", args.me, settings["me"])):
            if given is not None and given != saved_val:
                diffs.append(flag)
        if args.allow_raze_own and not settings["allow_raze_own"]:
            diffs.append("--allow-raze-own")
        if norm_keep(args.keep) != settings["keep"]:
            diffs.append("the keep list")
        if norm_names(args.raze) != settings.get("raze", []):
            diffs.append("--raze")
        if args.dump and os.path.exists(dump) and file_hash(dump) != st["dump_hash"]:
            diffs.append("the dump file")
        if diffs:
            print(f"NOTE: {', '.join(diffs)} differ from the saved run. Add --rerun to recompute.")
        st.setdefault("marks", {})
        if "tried" not in st:                        # older saves: OR-Tools had run on every plan
            st["tried"] = set(st["results"]) if st.get("solver") == "ortools" else set()
        pr = build_problem(settings, st["data"], st["marks"])

    W, H, wrap, tiles, cities, players, me = st["data"]
    results, options, exhausted = st["results"], st["options"], st["exhausted"]
    # oil/aluminum can be refreshed without recomputing: use the newest resource lines
    # from the dump file (or Lua.log on a plain run) if it has any for this map
    fresh = parse_resources(dump, W, H) if os.path.exists(dump) else None
    if fresh is not None and fresh != st.get("resources"):
        st["resources"] = fresh
        save_state(args.state, st)
        print(f"Oil/aluminum layer updated from {dump}.")
    resources = st.get("resources") or {}
    territory = st.get("territory") or {}
    if not territory:
        print("NOTE: this run has no territory data. Re-run the Lua command at the top of this "
              "script, then use --rerun.")
    base_total = len(results[0][1]) + len(results[0][2])
    print(f"\nCities now: {len(cities)}.  "
          f"{'With only the forced razes' if pr.user_forced else 'Without razing'} "
          f"you can reach {base_total}.\n")
    print("  razes  cities")
    best = None
    for n in sorted(results):                        # only the plans that add cities
        r, k, nw = results[n][:3]
        t = len(k) + len(nw)
        if best is None or t > best:
            best = t
            print(f"  {len(set(r) - pr.burning):>5}  {t:>6}  ({t - base_total:+d})")

    try:
        import ortools  # noqa: F401
        opt_seconds = args.time
        print("\nThese are quick estimates. Click a plan in the window to optimize it with OR-Tools.")
    except ImportError:
        opt_seconds = 0
        print("\n(pip install ortools to optimize plans when you click them; until then these are "
              "quick estimates)")

    def save_cb():
        st["saved_at"] = time.time()
        save_state(args.state, st)

    if not args.no_popup:
        try:
            show(W, H, tiles, cities, players, pr, results, options, exhausted, save_cb,
                 resources=resources, me=me, territory=territory,
                 tried=st["tried"], marks=st["marks"],
                 recompute=make_recompute(settings, st["data"]), opt_seconds=opt_seconds,
                 ui=st.setdefault("ui", {}))
        except ImportError:
            print("\npip install matplotlib for the map window.")


if __name__ == "__main__":
    main()
