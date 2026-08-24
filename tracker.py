#!/usr/bin/env python3
"""
Fantasy Baseball Playoff Tracker
================================
Pulls active lineups from the Fantrax public API and player stats from the
MLB Stats API, computes 16-category all-play records for each playoff week,
and regenerates docs/index.html (published via GitHub Pages).

Python 3.11+ standard library only. Run: python tracker.py
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from itertools import combinations
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- constants
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
PLAYER_MAP_PATH = os.path.join(BASE_DIR, "player_map.json")
DOCS_DIR = os.path.join(BASE_DIR, "docs")
DATA_DIR = os.path.join(DOCS_DIR, "data")
HISTORY_PATH = os.path.join(DOCS_DIR, "history.json")
INDEX_PATH = os.path.join(DOCS_DIR, "index.html")
STATE_PATH = os.path.join(DOCS_DIR, "state.json")
COMMENTARY_PATH = os.path.join(DOCS_DIR, "commentary.json")

FANTRAX_BASE = "https://www.fantrax.com/fxea/general"
MLB_BASE = "https://statsapi.mlb.com/api/v1"
USER_AGENT = "playoff-tracker/1.0 (fantasy league page; contact league admin)"
MLB_DELAY = 0.3  # polite delay between MLB API calls, seconds

PITCHER_POSITIONS = {"SP", "RP", "P"}

# (key, display label, lower_is_better)
CATEGORIES = [
    ("R", "R", False), ("HR", "HR", False), ("RBI", "RBI", False),
    ("SO", "SO", True), ("SB", "SB", False), ("AVG", "AVG", False),
    ("OPS", "OPS", False), ("CYC", "CYC", False),
    ("IP", "IP", False), ("QS", "QS", False), ("HA", "H", True),
    ("BBA", "BB", True), ("K", "K", False), ("ERA", "ERA", True),
    ("SVH3", "SVH3", False), ("NH", "NH", False),
]

RAW_KEYS = ["AB", "H", "R", "HR", "RBI", "SO", "SB", "D2", "D3", "BB", "HBP",
            "SF", "OUTS", "ER", "HA", "BBA", "K", "SV", "HLD", "QS"]


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- utilities
def http_get_json(url, retries=1):
    """GET a URL, parse JSON. One retry on failure, then raise."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - deliberate broad catch + retry
            last_err = e
            log(f"  ! fetch failed ({attempt + 1}/{retries + 1}): {url} -> {e}")
            time.sleep(1.5)
    raise last_err


def mlb_get(url):
    """MLB call with polite rate limiting."""
    time.sleep(MLB_DELAY)
    return http_get_json(url)


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, obj, indent=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=indent)


def zero_raw():
    return {k: 0 for k in RAW_KEYS}


def add_raw(total, part):
    for k in RAW_KEYS:
        total[k] += part.get(k, 0)


def ip_to_outs(ip_str):
    """Convert MLB innings-pitched string '5.2' -> 17 outs."""
    s = str(ip_str)
    if "." in s:
        whole, frac = s.split(".", 1)
        return int(whole) * 3 + int(frac[0])
    return int(s) * 3


def outs_to_ip_str(outs):
    return f"{outs // 3}.{outs % 3}"


def parse_date(s):
    return date.fromisoformat(s)


# ---------------------------------------------------------------- player map
class PlayerResolver:
    """fantraxId -> {name, mlbId}. Unknown ACTIVE players get resolved via
    Fantrax getPlayerIds (name lookup) + MLB people/search, then cached."""

    def __init__(self, league_id, pmap):
        self.league_id = league_id
        self.pmap = pmap
        self.pmap.setdefault("players", {})
        self.fantrax_names = None  # lazy-loaded
        self.dirty = False
        self.failed = set()  # don't retry same id twice per run

    def _load_fantrax_names(self):
        log("Fetching Fantrax player id map (getPlayerIds?sport=MLB)...")
        self.fantrax_names = {}
        try:
            data = http_get_json(
                f"{FANTRAX_BASE}/getPlayerIds?sport=MLB")
        except Exception as e:  # noqa: BLE001
            log(f"  !! getPlayerIds FAILED: {e} -- every unmapped player will "
                f"score ZERO. Check player_map.json coverage.")
            return
        # Parse defensively: expect a dict keyed by fantrax id (possibly
        # nested under a wrapper key), values being dicts with a name field.
        src = data
        if isinstance(data, dict):
            for key in ("allPlayers", "players", "playerIds"):
                if isinstance(data.get(key), dict):
                    src = data[key]
                    break
        if isinstance(src, dict):
            for fid, val in src.items():
                if isinstance(val, dict):
                    name = (val.get("name") or val.get("playerName")
                            or val.get("fullName"))
                    if name:
                        self.fantrax_names[fid] = name
                elif isinstance(val, str):
                    self.fantrax_names[fid] = val
        if not self.fantrax_names:
            keys = list(data)[:10] if isinstance(data, dict) else type(data)
            log(f"  ! unexpected getPlayerIds structure; top-level: {keys}")
        else:
            log(f"  mapped {len(self.fantrax_names)} fantrax player names")

    def _search_mlb(self, name):
        q = urllib.parse.quote(name)
        data = mlb_get(f"{MLB_BASE}/people/search?names={q}")
        people = data.get("people", [])
        if not people:
            return None
        for p in people:
            if p.get("active"):
                return p.get("id")
        return people[0].get("id")

    def resolve(self, fantrax_id):
        """Return {'name':..., 'mlbId':...} or None if unresolvable."""
        entry = self.pmap["players"].get(fantrax_id)
        if entry and entry.get("mlbId"):
            return entry
        if fantrax_id in self.failed:
            return None
        if self.fantrax_names is None:
            self._load_fantrax_names()
        name = self.fantrax_names.get(fantrax_id)
        if not name:
            log(f"  ! no Fantrax name for id {fantrax_id}; skipping player")
            self.failed.add(fantrax_id)
            return None
        try:
            mlb_id = self._search_mlb(name)
        except Exception as e:  # noqa: BLE001
            log(f"  ! MLB search failed for '{name}': {e}")
            self.failed.add(fantrax_id)
            return None
        if not mlb_id:
            log(f"  ! MLB search found nothing for '{name}'")
            self.failed.add(fantrax_id)
            return None
        entry = {"name": name, "mlbId": mlb_id}
        self.pmap["players"][fantrax_id] = entry
        self.dirty = True
        log(f"  resolved new player: {name} -> MLB {mlb_id}")
        return entry

    def save_if_dirty(self):
        if self.dirty:
            save_json(PLAYER_MAP_PATH, self.pmap, indent=2)
            log("player_map.json updated")


# ---------------------------------------------------------------- stat pulls
class StatFetcher:
    """Fetches/aggregates per-period raw stat components per team."""

    def __init__(self, cfg, resolver, today):
        self.cfg = cfg
        self.resolver = resolver
        self.today = today
        self.gamelog_cache = {}  # mlbId -> list of pitching game splits

    def _hitter_raw(self, mlb_id, start, end):
        url = (f"{MLB_BASE}/people/{mlb_id}/stats?stats=byDateRange"
               f"&group=hitting&startDate={start}&endDate={end}&season=2026")
        data = mlb_get(url)
        splits = (data.get("stats") or [{}])[0].get("splits") or []
        if not splits:
            return zero_raw()
        chosen = splits[0]
        for sp in splits:  # prefer the "All" split (sport.id == 0)
            if sp.get("sport", {}).get("id") == 0:
                chosen = sp
                break
        st = chosen.get("stat", {})
        raw = zero_raw()
        raw["AB"] = st.get("atBats", 0)
        raw["H"] = st.get("hits", 0)
        raw["R"] = st.get("runs", 0)
        raw["HR"] = st.get("homeRuns", 0)
        raw["RBI"] = st.get("rbi", 0)
        raw["SO"] = st.get("strikeOuts", 0)
        raw["SB"] = st.get("stolenBases", 0)
        raw["D2"] = st.get("doubles", 0)
        raw["D3"] = st.get("triples", 0)
        raw["BB"] = st.get("baseOnBalls", 0)
        raw["HBP"] = st.get("hitByPitch", 0)
        raw["SF"] = st.get("sacFlies", 0)
        return raw

    def _pitcher_games(self, mlb_id):
        if mlb_id in self.gamelog_cache:
            return self.gamelog_cache[mlb_id]
        url = (f"{MLB_BASE}/people/{mlb_id}/stats?stats=gameLog"
               f"&group=pitching&season=2026")
        data = mlb_get(url)
        splits = (data.get("stats") or [{}])[0].get("splits") or []
        self.gamelog_cache[mlb_id] = splits
        return splits

    def _pitcher_raw(self, mlb_id, start, end):
        raw = zero_raw()
        for game in self._pitcher_games(mlb_id):
            gdate = game.get("date", "")
            if not (start <= gdate <= end):
                continue
            st = game.get("stat", {})
            outs = ip_to_outs(st.get("inningsPitched", "0.0"))
            er = st.get("earnedRuns", 0)
            raw["OUTS"] += outs
            raw["ER"] += er
            raw["HA"] += st.get("hits", 0)
            raw["BBA"] += st.get("baseOnBalls", 0)
            raw["K"] += st.get("strikeOuts", 0)
            raw["SV"] += st.get("saves", 0)
            raw["HLD"] += st.get("holds", 0)
            if st.get("gamesStarted", 0) >= 1 and outs >= 18 and er <= 3:
                raw["QS"] += 1
        return raw

    def period_stats(self, period):
        """Return {teamId: raw dict} for one lineup period, using the on-disk
        cache for periods that have fully ended."""
        n = period["period"]
        start, end = period["start"], period["end"]
        cache_path = os.path.join(DATA_DIR, f"period_stats_{n}.json")
        if os.path.exists(cache_path) and parse_date(end) < self.today:
            log(f"Period {n} ({start}..{end}): using cached stats")
            return load_json(cache_path, {}).get("teams", {})

        log(f"Period {n} ({start}..{end}): fetching rosters + stats")
        rosters = http_get_json(
            f"{FANTRAX_BASE}/getTeamRosters?leagueId={self.cfg['leagueId']}"
            f"&period={n}").get("rosters", {})
        teams = {}
        players = {}
        for team in self.cfg["teams"]:
            tid = team["teamId"]
            total = zero_raw()
            teams[tid] = total
            players[tid] = {}
            roster = rosters.get(tid)
            if not roster:
                log(f"  ! no roster returned for team {tid} ({team['name']})")
                continue
            actives = [it for it in roster.get("rosterItems", [])
                       if it.get("status") == "ACTIVE"]
            for item in actives:
                fid = item.get("id")
                pos = item.get("position", "")
                info = self.resolver.resolve(fid)
                if not info:
                    continue
                try:
                    if pos in PITCHER_POSITIONS:
                        pr = self._pitcher_raw(info["mlbId"], start, end)
                        add_raw(total, pr)
                        players[tid][fid] = {"name": info.get("name"),
                                             "mlbId": info["mlbId"],
                                             "pos": pos, "side": "P", "raw": pr}
                    else:
                        hr_ = self._hitter_raw(info["mlbId"], start, end)
                        add_raw(total, hr_)
                        players[tid][fid] = {"name": info.get("name"),
                                             "mlbId": info["mlbId"],
                                             "pos": pos, "side": "H", "raw": hr_}
                except Exception as e:  # noqa: BLE001 - one player never kills the run
                    log(f"  ! stats failed for {info.get('name', fid)}: {e}")
            log(f"  {team['name']}: {len(actives)} active players tallied")
        save_json(cache_path, {"period": n, "start": start, "end": end,
                               "complete": parse_date(end) < self.today,
                               "teams": teams, "players": players})
        return teams


# ---------------------------------------------------------------- scoring
def derive_values(raw):
    """Raw components -> 16 comparable category values."""
    v = {"R": raw["R"], "HR": raw["HR"], "RBI": raw["RBI"], "SO": raw["SO"],
         "SB": raw["SB"], "CYC": 0, "NH": 0, "QS": raw["QS"],
         "HA": raw["HA"], "BBA": raw["BBA"], "K": raw["K"],
         "IP": raw["OUTS"],  # compared in outs, displayed as innings
         "SVH3": raw["SV"] + 0.5 * raw["HLD"]}
    ab = raw["AB"]
    if ab > 0:
        tb = raw["H"] + raw["D2"] + 2 * raw["D3"] + 3 * raw["HR"]
        obp_den = ab + raw["BB"] + raw["HBP"] + raw["SF"]
        obp = (raw["H"] + raw["BB"] + raw["HBP"]) / obp_den if obp_den else 0.0
        v["AVG"] = raw["H"] / ab
        v["OPS"] = obp + tb / ab
    else:  # no at-bats -> rate stats compare as worst
        v["AVG"] = -1.0
        v["OPS"] = -1.0
    v["ERA"] = (27.0 * raw["ER"] / raw["OUTS"]) if raw["OUTS"] > 0 \
        else float("inf")  # no innings -> ERA compares as worst
    return v


def all_play(values_by_team, teams):
    """Every team vs every other team across 16 categories.
    Returns {teamId: [W, L, T]}."""
    rec = {t: [0, 0, 0] for t in teams}
    for a, b in combinations(teams, 2):
        va, vb = values_by_team[a], values_by_team[b]
        for key, _label, lower in CATEGORIES:
            x, y = va[key], vb[key]
            if x == y:
                rec[a][2] += 1
                rec[b][2] += 1
            elif (x < y) == lower:  # a wins if lower-better and x<y, etc.
                rec[a][0] += 1
                rec[b][1] += 1
            else:
                rec[a][1] += 1
                rec[b][0] += 1
    return rec


def points(rec):
    return rec[0] + 0.5 * rec[2]


def fmt_value(key, val):
    """Display formatting per category."""
    if key in ("AVG", "OPS"):
        return "—" if val < 0 else f"{val:.3f}".lstrip("0") if val < 1 \
            else f"{val:.3f}"
    if key == "ERA":
        return "—" if val == float("inf") else f"{val:.2f}"
    if key == "IP":
        return outs_to_ip_str(int(val))
    if key == "SVH3":
        return f"{val:.1f}".rstrip("0").rstrip(".")
    return str(int(val))


def heat_value(key, val):
    """Numeric value used by the JS heat-coloring (finite numbers only)."""
    if key == "ERA" and val == float("inf"):
        return 9999
    return round(val, 4)


# ---------------------------------------------------------------- simulation
def run_playoffs(cfg, fetcher, today):
    """Walk the playoff structure; return a state dict for rendering."""
    teams = {t["teamId"]: t for t in cfg["teams"]}
    seed = lambda tid: teams[tid]["seed"]  # noqa: E731
    overrides = cfg.get("eliminatedOverrides", {})

    regular_weeks = [w for w in cfg["weeks"] if not w.get("championship")]
    champ_week = next((w for w in cfg["weeks"] if w.get("championship")), None)

    remaining = sorted(teams, key=seed)
    cumulative = {t: [0, 0, 0] for t in teams}
    cum_raw = {t: zero_raw() for t in teams}
    stages = []  # per regular week render data

    for idx, week in enumerate(regular_weeks):
        w_start = parse_date(week["periods"][0]["start"])
        w_end = parse_date(week["periods"][-1]["end"])
        started = w_start <= today
        complete = w_end < today
        participants = list(remaining)
        values, records = None, {t: [0, 0, 0] for t in participants}

        if started:
            log(f"--- {week['label']} ({w_start}..{w_end}) ---")
            week_raw = {t: zero_raw() for t in teams}
            for period in week["periods"]:
                if parse_date(period["start"]) > today:
                    continue
                pstats = fetcher.period_stats(period)
                for tid in teams:
                    add_raw(week_raw[tid], pstats.get(tid, zero_raw()))
            for tid in teams:
                add_raw(cum_raw[tid], week_raw[tid])
            values = {t: derive_values(week_raw[t]) for t in teams}
            records = all_play(values, participants)
            for tid, rec in records.items():
                for i in range(3):
                    cumulative[tid][i] += rec[i]

        eliminated_now = None
        if complete and len(remaining) > 4:
            forced = [tid for tid, lbl in overrides.items()
                      if lbl == week["label"] and tid in remaining]
            if forced:
                eliminated_now = forced[0]
                log(f"  override: eliminating {teams[eliminated_now]['name']}")
            else:
                # Worst cumulative record goes. Week 1 protects seeds 1-2.
                candidates = [t for t in remaining
                              if not (idx == 0 and seed(t) <= 2)]
                if not candidates:
                    candidates = list(remaining)
                # ties -> lower seed (higher seed number) eliminated
                eliminated_now = min(
                    candidates, key=lambda t: (points(cumulative[t]), -seed(t)))
                log(f"  eliminated after {week['label']}: "
                    f"{teams[eliminated_now]['name']}")
            remaining = [t for t in remaining if t != eliminated_now]

        stages.append({
            "week": week, "participants": participants,
            "values": values, "records": records,
            "cum_snapshot": {t: list(cumulative[t]) for t in teams},
            "started": started, "complete": complete,
            "eliminated": eliminated_now,
        })

    # ---- championship: records reset, one combined stat block ----
    champ = {"week": champ_week, "participants": list(remaining),
             "values": None, "records": {t: [0, 0, 0] for t in remaining},
             "started": False, "complete": False, "champion": None}
    if champ_week:
        c_start = parse_date(champ_week["periods"][0]["start"])
        c_end = parse_date(champ_week["periods"][-1]["end"])
        champ["started"] = c_start <= today
        champ["complete"] = c_end < today
        if champ["started"]:
            log(f"--- {champ_week['label']} ({c_start}..{c_end}) ---")
            block = {t: zero_raw() for t in teams}
            for period in champ_week["periods"]:
                if parse_date(period["start"]) > today:
                    continue
                pstats = fetcher.period_stats(period)
                for tid in teams:
                    add_raw(block[tid], pstats.get(tid, zero_raw()))
            champ["values"] = {t: derive_values(block[t]) for t in remaining}
            champ["records"] = all_play(champ["values"], remaining)
            if champ["complete"] and remaining:
                # leader wins; ties -> better (lower number) seed
                champ["champion"] = max(
                    remaining,
                    key=lambda t: (points(champ["records"][t]), -seed(t)))
                log(f"CHAMPION: {teams[champ['champion']]['name']}")

    return {"teams": teams, "stages": stages, "champ": champ,
            "cumulative": cumulative, "cum_raw": cum_raw,
            "remaining": remaining}


# ---------------------------------------------------------------- HTML
CSS = """
:root{--bg:#0b0f14;--panel:#151c25;--card:#151c25;--elev:#1b2430;
--accent:#4dd0c4;--blue:#58a6ff;--gold:#e8c469;
--text:#c9d1d9;--bright:#e6edf3;--dim:#8b949e;--red:#f85149;--line:#2b3542}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--text);line-height:1.5;
font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
margin:0 auto;padding:22px 16px 40px;max-width:1320px}
h1{color:var(--accent);font-size:21px;letter-spacing:.14em;text-align:center;
text-transform:uppercase;font-weight:700;margin:6px 0 4px}
.updated{text-align:center;color:var(--dim);font-size:12px;margin-bottom:26px}
.sec{margin:36px 0 14px;display:flex;align-items:center;gap:12px}
.sec:first-of-type{margin-top:6px}
.sec h2{color:var(--blue);font-size:13px;letter-spacing:.16em;font-weight:700;
text-transform:uppercase;margin:0;white-space:nowrap}
.sec .sub{color:var(--dim);font-size:11px;white-space:nowrap}
.sec .rule{flex:1;height:1px;background:var(--line)}
h2.plain{color:var(--blue);font-size:12px;letter-spacing:.14em;font-weight:700;
text-transform:uppercase;margin:22px 0 8px}
.hero{background:linear-gradient(180deg,#18232e 0%,var(--panel) 62%);
border:1px solid var(--accent);border-radius:14px;padding:18px 20px 16px;
box-shadow:0 0 0 1px rgba(77,208,196,.10),0 8px 28px rgba(0,0,0,.38)}
.hero .htop{display:flex;justify-content:space-between;align-items:baseline;
gap:10px;flex-wrap:wrap;margin-bottom:12px}
.hero .htitle{color:var(--accent);font-size:15px;font-weight:700;
letter-spacing:.14em;text-transform:uppercase}
.hero .hdates{color:var(--bright);font-size:13px}
.hero .hnote{color:var(--dim);font-size:10px;text-transform:uppercase;
letter-spacing:.10em}
.crow{display:grid;grid-template-columns:22px 1fr auto 64px;gap:10px;
align-items:center;padding:10px 0 6px;border-top:1px solid var(--line)}
.crow.first{border-top:none}
.crow .pos{color:var(--dim);font-size:12px;text-align:right;
font-variant-numeric:tabular-nums}
.crow .nm{color:var(--bright);font-size:15px;font-weight:600}
.crow .rec{color:var(--dim);font-size:12px;font-variant-numeric:tabular-nums}
.crow .pts{color:var(--accent);font-size:17px;font-weight:700;text-align:right;
font-variant-numeric:tabular-nums}
.crow.lead .pts{color:var(--gold)}
.bar{grid-column:2/-1;height:4px;background:#0d141c;border-radius:3px;
overflow:hidden}
.bar span{display:block;height:100%;background:var(--accent);border-radius:3px}
.crow.lead .bar span{background:var(--gold)}
.hero .pending{color:var(--dim);font-size:12px;text-align:center;
padding:12px 0 2px;border-top:1px solid var(--line);margin-top:6px}
.champban{margin-top:14px;border-top:1px solid var(--line);padding-top:14px;
text-align:center}
.champban .lbl{color:var(--dim);font-size:10px;letter-spacing:.16em;
text-transform:uppercase}
.champban .who{color:var(--gold);font-size:23px;font-weight:800;margin-top:5px}
.bracket{display:flex;gap:10px;flex-wrap:wrap}
.stage{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:11px 13px;min-width:190px;flex:1 1 190px;max-width:280px}
.stage h3{color:var(--blue);font-size:11px;text-transform:uppercase;
letter-spacing:.10em;margin:0 0 7px;border-bottom:1px solid var(--line);
padding-bottom:6px}
.stage .tm{display:flex;justify-content:space-between;gap:8px;font-size:13px;
padding:3px 0}
.stage .tm .rec{color:var(--dim);font-variant-numeric:tabular-nums}
.stage .out{color:var(--red)}
.stage .out span:first-child{text-decoration:line-through}
.stage .out .rec{color:var(--red)}
.seed{color:var(--accent);font-weight:600;margin-right:6px}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px;
background:var(--panel);margin-bottom:6px}
table{border-collapse:separate;border-spacing:0;width:100%;font-size:13px;
background:transparent}
th{background:#101720;color:var(--blue);padding:8px 7px;text-align:right;
font-size:10px;text-transform:uppercase;letter-spacing:.06em;white-space:nowrap;
cursor:pointer;user-select:none;-webkit-user-select:none}
th:hover{color:var(--accent)}
th.sort-asc::after{content:" \u25B2";font-size:7px;opacity:.85}
th.sort-desc::after{content:" \u25BC";font-size:7px;opacity:.85}
th:first-child,td:first-child{text-align:left;padding-left:12px;
position:sticky;left:0;background:var(--panel)}
th:first-child{background:#101720;z-index:2}
td:first-child{z-index:1}
td{padding:7px 7px;text-align:right;border-top:1px solid var(--line);
font-variant-numeric:tabular-nums;white-space:nowrap}
tbody tr:hover td,tbody tr:hover td:first-child{background:var(--elev)}
tr.out td{color:var(--red)}
tr.out td:first-child{text-decoration:line-through}
.tnote{color:var(--dim);font-size:11px;margin:0 0 16px}
footer{text-align:center;color:var(--dim);font-size:12px;margin-top:38px;
border-top:1px solid var(--line);padding-top:14px}
.commentary{background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:16px 20px;margin:0 0 4px}
.commentary h3{margin:0 0 4px;font-size:13px;letter-spacing:.10em;
text-transform:uppercase;color:var(--accent)}
.commentary .cmeta{color:var(--dim);font-size:11px;margin-bottom:10px}
.commentary p{margin:0 0 10px;line-height:1.6;font-size:14px}
.commentary p:last-child{margin-bottom:0}
.commentary strong{color:var(--bright)}
.commentary .tag{display:inline-block;font-size:10px;letter-spacing:.08em;
text-transform:uppercase;background:#1f2d3d;color:var(--accent);
border-radius:4px;padding:2px 7px;margin-left:8px;vertical-align:middle}
@media(max-width:620px){
h1{font-size:17px}.hero{padding:15px 14px}
.crow{grid-template-columns:20px 1fr auto 54px;gap:8px}
.crow .nm{font-size:14px}.crow .pts{font-size:15px}
.stage{max-width:none}}
"""

HEAT_JS = """
document.querySelectorAll('table.heat').forEach(function(tbl){
  var ths=tbl.querySelectorAll('thead th');
  ths.forEach(function(th,ci){
    if(!th.hasAttribute('data-heat'))return;
    var lower=th.getAttribute('data-dir')==='asc';
    var cells=[],vals=[];
    tbl.querySelectorAll('tbody tr').forEach(function(tr){
      var td=tr.children[ci];if(!td)return;
      var v=parseFloat(td.getAttribute('data-v'));
      if(!isNaN(v)){cells.push(td);vals.push(v);}
    });
    if(vals.length<2)return;
    var mn=Math.min.apply(null,vals),mx=Math.max.apply(null,vals);
    cells.forEach(function(td,i){
      var t=(mx>mn)?(vals[i]-mn)/(mx-mn):0.5;
      if(lower)t=1-t;               /* t=1 is best */
      td.style.color='hsl('+Math.round(120*t)+',65%,58%)';
      td.style.fontWeight=(t>0.85)?'600':'400';
    });
  });
});
document.querySelectorAll('table.sortable').forEach(function(tbl){
  var ths=Array.prototype.slice.call(tbl.querySelectorAll('thead th'));
  var body=tbl.querySelector('tbody');
  if(!body)return;
  ths.forEach(function(th,ci){
    th.addEventListener('click',function(){
      var dir;
      if(th.classList.contains('sort-desc'))dir='asc';
      else if(th.classList.contains('sort-asc'))dir='desc';
      else dir=(th.getAttribute('data-dir')==='asc')?'asc':'desc';
      if(th.getAttribute('data-sort')==='text')dir=
        th.classList.contains('sort-asc')?'desc':'asc';
      ths.forEach(function(o){o.classList.remove('sort-asc','sort-desc');});
      th.classList.add(dir==='asc'?'sort-asc':'sort-desc');
      var rows=Array.prototype.slice.call(body.querySelectorAll('tr'));
      var txt=th.getAttribute('data-sort')==='text';
      rows.sort(function(ra,rb){
        var a=ra.children[ci],b=rb.children[ci];
        if(!a||!b)return 0;
        if(txt){
          var x=(a.getAttribute('data-name')||a.textContent||'').trim();
          var y=(b.getAttribute('data-name')||b.textContent||'').trim();
          if(x===y)return 0;
          return dir==='asc'?(x<y?-1:1):(x>y?-1:1);
        }
        function num(td){
          var v=parseFloat(td.getAttribute('data-v'));
          if(isNaN(v))v=parseFloat((td.textContent||'').replace(/[^0-9.eE+-]/g,''));
          return v;
        }
        var va=num(a),vb=num(b);
        if(isNaN(va)&&isNaN(vb))return 0;
        if(isNaN(va))return 1;
        if(isNaN(vb))return -1;
        return dir==='asc'?va-vb:vb-va;
      });
      rows.forEach(function(r){body.appendChild(r);});
    });
  });
});
"""


DASH = "\u2014"


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def render_stage_card(title, rows, note=None):
    """rows: list of (seed, name, rec_str_or_dash, is_out)"""
    h = [f'<div class="stage"><h3>{esc(title)}</h3>']
    if note:
        h.append(f'<div style="color:var(--dim);font-size:11px;'
                 f'margin-bottom:6px">{esc(note)}</div>')
    for sd, name, rec, out in rows:
        cls = ' class="tm out"' if out else ' class="tm"'
        h.append(f'<div{cls}><span><span class="seed">{sd}</span>'
                 f'{esc(name)}</span><span class="rec">{rec}</span></div>')
    h.append("</div>")
    return "".join(h)


def fmt_span(week):
    """'Aug 24 - Sep 6' for a week's full period range."""
    if not week:
        return ""
    a = parse_date(week["periods"][0]["start"])
    b = parse_date(week["periods"][-1]["end"])
    f = "%b %-d" if os.name != "nt" else "%b %d"
    return f"{a.strftime(f)} \u2013 {b.strftime(f)}"


def render_champ_hero(teams, champ, seed):
    """The championship panel: the focus of the page."""
    week = champ.get("week") or {}
    label = week.get("label", "Championship")
    span = fmt_span(week)
    parts = champ.get("participants") or []
    h = ['<div class="hero"><div class="htop">',
         f'<span class="htitle">{esc(label)}</span>',
         f'<span class="hdates">{esc(span)}</span>',
         '<span class="hnote">records reset \u00b7 one combined block</span>',
         '</div>']
    if not parts:
        h.append('<div class="pending">Field is not set yet \u2014 '
                 'three knockout weeks to go.</div>')
    elif not champ.get("started") or not any(
            champ["records"][t][0] or champ["records"][t][1] for t in parts):
        for i, tid in enumerate(sorted(parts, key=seed)):
            first = ' first' if i == 0 else ''
            h.append(f'<div class="crow{first}"><span class="pos">\u2013</span>'
                     f'<span class="nm"><span class="seed">{seed(tid)}</span>'
                     f'{esc(teams[tid]["name"])}</span>'
                     f'<span class="rec">not started</span>'
                     f'<span class="pts">0</span></div>')
        started = champ.get("started")
        h.append('<div class="pending">'
                 + ('First pitch tonight' if started else
                    'Begins ' + esc(span.split(chr(8211))[0].strip()))
                 + ' \u00b7 nobody has a point yet.</div>')
    else:
        recs = champ["records"]
        order = sorted(parts, key=lambda t: (-points(recs[t]), seed(t)))
        top = points(recs[order[0]]) or 1.0
        n_top = sum(1 for t in parts if points(recs[t]) == points(recs[order[0]]))
        all_tied = n_top == len(order)
        for i, tid in enumerate(order):
            rec = recs[tid]
            p = points(rec)
            gb = top - p
            gbtxt = ("tied" if n_top > 1 else "leader") if gb == 0 \
                else f"{gb:g} back"
            pct = max(2, round(100.0 * p / top)) if top else 2
            if all_tied:
                cls = "crow first" if i == 0 else "crow"
                pos = "\u2013"
            else:
                cls = "crow first lead" if i == 0 else "crow"
                pos = str(i + 1)
            h.append(f'<div class="{cls}"><span class="pos">{pos}</span>'
                     f'<span class="nm"><span class="seed">{seed(tid)}</span>'
                     f'{esc(teams[tid]["name"])}</span>'
                     f'<span class="rec">{rec_str(rec)} \u00b7 {gbtxt}</span>'
                     f'<span class="pts">{p:g}</span>'
                     f'<div class="bar"><span style="width:{pct}%"></span></div>'
                     '</div>')
    if champ.get("champion"):
        h.append('<div class="champban"><div class="lbl">Champion</div>'
                 f'<div class="who">\U0001F3C6 '
                 f'{esc(teams[champ["champion"]]["name"])}</div></div>')
    h.append('</div>')
    return "".join(h)


def render_table(title, teams, order, records, values, out_set):
    """Standings table: Team, W, L, T, GB, PCT + 16 categories."""
    if not order:
        return ""
    lead = max(points(records[t]) for t in order)
    h = [f'<h2 class="plain">{esc(title)}</h2>', '<div class="wrap">',
         '<table class="heat sortable"><thead><tr>'
         '<th data-sort="text" title="Sort by team name">Team</th>'
         '<th data-dir="desc">W</th><th data-dir="asc">L</th>'
         '<th data-dir="desc">T</th><th data-dir="asc">GB</th>'
         '<th data-dir="desc">PCT</th>']
    for key, label, lower in CATEGORIES:
        d = "asc" if lower else "desc"
        h.append(f'<th data-heat="1" data-dir="{d}">{label}</th>')
    h.append("</tr></thead><tbody>")
    for tid in order:
        team, rec = teams[tid], records[tid]
        pts, dec = points(rec), sum(rec)
        gb = lead - pts
        pct = pts / dec if dec else 0.0
        cls = ' class="out"' if tid in out_set else ""
        h.append(f'<tr{cls}>'
                 f'<td data-name="{esc(team["name"]).lower()}">'
                 f'<span class="seed">{team["seed"]}</span>'
                 f'{esc(team["name"])}</td>'
                 f'<td data-v="{rec[0]}">{rec[0]}</td>'
                 f'<td data-v="{rec[1]}">{rec[1]}</td>'
                 f'<td data-v="{rec[2]}">{rec[2]}</td>'
                 f'<td data-v="{gb:g}">'
                 f'{DASH if gb == 0 else f"{gb:g}"}</td>'
                 f'<td data-v="{pct:.5f}">{pct:.3f}</td>')
        v = values.get(tid) if values else None
        for key, _label, _lower in CATEGORIES:
            if v is None:
                h.append("<td>—</td>")
            else:
                h.append(f'<td data-v="{heat_value(key, v[key])}">'
                         f'{fmt_value(key, v[key])}</td>')
        h.append("</tr>")
    h.append("</tbody></table></div>")
    return "".join(h)


def rec_str(rec):
    return f"{rec[0]}-{rec[1]}-{rec[2]}"


def generate_html(cfg, state, now):
    teams = state["teams"]
    seed = lambda tid: teams[tid]["seed"]  # noqa: E731
    stages, champ = state["stages"], state["champ"]
    out_all = {s["eliminated"] for s in stages if s["eliminated"]}
    body = []

    # ================= 1. CHAMPIONSHIP (the focus) =================
    body.append(render_champ_hero(teams, champ, seed))

    # ================= 2. its category board =======================
    if champ["started"] and champ["participants"]:
        order = sorted(champ["participants"],
                       key=lambda t: (-points(champ["records"][t]), seed(t)))
        body.append(render_table(
            f"{champ['week']['label']} \u2014 "
            f"{'final' if champ['complete'] else 'live'} category board",
            teams, order, champ["records"], champ["values"], set()))
        body.append('<div class="tnote">Every category, every team, over the '
                    'whole block. Green is good.</div>')
    else:
        # championship not underway: keep the live round visible up top
        current = None
        for stg in stages:
            if stg["started"]:
                current = stg
        if current is not None:
            order = sorted(current["participants"],
                           key=lambda t: (-points(current["records"][t]),
                                          seed(t)))
            body.append(render_table(
                f"Current round \u2014 {current['week']['label']} "
                f"({'final' if current['complete'] else 'live'})",
                teams, order, current["records"], current["values"], set()))
        else:
            body.append('<div class="tnote">The category board appears once '
                        'play begins.</div>')

    # ================= 3. the road here ===========================
    body.append('<div class="sec"><h2>The road here</h2>'
                '<span class="sub">Weeks 1\u20133 \u00b7 knockout rounds'
                '</span><span class="rule"></span></div>')

    cards = []
    titles = ["Week 1", "Weeks 1-2", "Weeks 1-3"]
    for i, stg in enumerate(stages):
        snap = stg["cum_snapshot"]
        rows = []
        parts = sorted(stg["participants"],
                       key=lambda t: (-points(snap[t]), seed(t)))
        for tid in parts:
            rec = rec_str(snap[tid]) if stg["started"] else "\u2014"
            rows.append((seed(tid), teams[tid]["name"], rec,
                         tid == stg["eliminated"]))
        note = None
        if stg["eliminated"]:
            note = f"out: {teams[stg['eliminated']]['name']}"
        cards.append(render_stage_card(
            titles[i] if i < 3 else stg["week"]["label"], rows, note=note))
    body.append(f'<div class="bracket">{"".join(cards)}</div>')

    # the final knockout week, only when the championship has taken over
    if champ["started"]:
        last = None
        for stg in stages:
            if stg["started"]:
                last = stg
        if last is not None:
            order = sorted(last["participants"],
                           key=lambda t: (-points(last["records"][t]), seed(t)))
            body.append(render_table(
                f"{last['week']['label']} \u2014 final standings", teams,
                order, last["records"], last["values"], set()))

    if any(s["started"] for s in stages):
        cum_vals = {t: derive_values(state["cum_raw"][t]) for t in teams}
        order = sorted(teams, key=lambda t: (-points(state["cumulative"][t]),
                                             seed(t)))
        body.append(render_table(
            "Cumulative \u2014 Weeks 1-3, all seven teams", teams, order,
            state["cumulative"], cum_vals, out_all))

    ts = now.strftime("%A, %B %-d, %Y at %-I:%M %p Pacific") \
        if os.name != "nt" else now.strftime("%A, %B %d, %Y %I:%M %p Pacific")
    html = ("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,"
            "initial-scale=1'>"
            f"<title>{esc(cfg['leagueName'])} Playoffs</title>"
            f"<style>{CSS}</style></head><body>"
            f"<h1>2026 Fantasy Baseball Playoffs \u2014 "
            f"{esc(cfg['leagueName'])}</h1>"
            f"<div class='updated'>Updated {ts}</div>"
            "<!--COMMENTARY-->"
            f"{''.join(body)}"
            "<footer>Auto-updated through the day \u00b7 Fantrax lineups "
            "\u00d7 MLB live stats</footer>"
            f"<script>{HEAT_JS}</script></body></html>")
    return html


# ---------------------------------------------------------------- history
def append_history(state, now):
    hist = load_json(HISTORY_PATH, [])
    champ = state["champ"]
    if champ["started"]:
        label = champ["week"]["label"]
        recs = {state["teams"][t]["name"]: rec_str(r)
                for t, r in champ["records"].items()}
    else:
        current = None
        for stg in state["stages"]:
            if stg["started"]:
                current = stg
        label = current["week"]["label"] if current else "pre-playoffs"
        recs = {state["teams"][t]["name"]: rec_str(r)
                for t, r in state["cumulative"].items()}
    hist.append({"timestamp": now.isoformat(timespec="seconds"),
                 "week": label, "records": recs})
    save_json(HISTORY_PATH, hist)
    log(f"history.json: appended snapshot ({label})")


def export_state(cfg, state, now):
    """Dump a JSON-serializable summary of this run for commentary.py."""
    teams = state["teams"]
    cur = None
    for stg in state["stages"]:
        if stg["started"]:
            cur = stg
    champ = state["champ"]
    active = champ if champ.get("started") else cur
    out = {
        "generated": now.isoformat(timespec="seconds"),
        "leagueName": cfg.get("leagueName"),
        "teams": {tid: {"name": t["name"], "seed": t["seed"]}
                  for tid, t in teams.items()},
        "currentWeek": (active["week"]["label"] if active else "pre-playoffs"),
        "isChampionship": bool(champ.get("started")),
        "periods": ([p for p in active["week"]["periods"]] if active else []),
        "participants": (list(active.get("participants", [])) if active else []),
        "weekRecords": ({t: list(r) for t, r in active["records"].items()}
                        if active and active.get("records") else {}),
        "weekValues": ({t: v for t, v in (active.get("values") or {}).items()}
                       if active else {}),
        "cumulative": {t: list(r) for t, r in state["cumulative"].items()},
        "eliminated": [stg.get("eliminated") for stg in state["stages"]
                       if stg.get("eliminated")],
    }
    save_json(STATE_PATH, out, indent=1)
    log(f"wrote docs/state.json")


# ---------------------------------------------------------------- main
def main():
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        log("FATAL: config.json not found")
        return 1
    tz = ZoneInfo(cfg.get("timezone", "America/Los_Angeles"))
    now = datetime.now(tz)
    today = now.date()
    log(f"=== Playoff tracker run @ {now.isoformat(timespec='seconds')} ===")

    if any(t.get("teamId") in (None, "", "TBD") for t in cfg["teams"]):
        log("config.json still has TBD team ids — writing placeholder page.")
        os.makedirs(DOCS_DIR, exist_ok=True)
        with open(INDEX_PATH, "w", encoding="utf-8") as f:
            f.write("<!DOCTYPE html><html><body style='background:#0d1117;"
                    "color:#c9d1d9;font-family:sans-serif;text-align:center;"
                    "padding-top:80px'><h1 style='color:#4dd0c4'>Tracker "
                    "waiting for config</h1><p>Fill in the playoff team ids "
                    "and seeds in config.json.</p></body></html>")
        return 0

    pmap = load_json(PLAYER_MAP_PATH, {"players": {}})
    resolver = PlayerResolver(cfg["leagueId"], pmap)
    fetcher = StatFetcher(cfg, resolver, today)

    state = run_playoffs(cfg, fetcher, today)
    resolver.save_if_dirty()

    os.makedirs(DOCS_DIR, exist_ok=True)
    html = generate_html(cfg, state, now)
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"wrote docs/index.html ({len(html)} bytes)")
    append_history(state, now)
    export_state(cfg, state, now)
    log("=== run complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
