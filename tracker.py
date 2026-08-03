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
        log("Fetching Fantrax player id map (getPlayerIds)...")
        self.fantrax_names = {}
        try:
            data = http_get_json(
                f"{FANTRAX_BASE}/getPlayerIds?leagueId={self.league_id}")
        except Exception as e:  # noqa: BLE001
            log(f"  ! getPlayerIds failed: {e}")
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
        for team in self.cfg["teams"]:
            tid = team["teamId"]
            total = zero_raw()
            teams[tid] = total
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
                        add_raw(total, self._pitcher_raw(info["mlbId"], start, end))
                    else:
                        add_raw(total, self._hitter_raw(info["mlbId"], start, end))
                except Exception as e:  # noqa: BLE001 - one player never kills the run
                    log(f"  ! stats failed for {info.get('name', fid)}: {e}")
            log(f"  {team['name']}: {len(actives)} active players tallied")
        save_json(cache_path, {"period": n, "start": start, "end": end,
                               "complete": parse_date(end) < self.today,
                               "teams": teams})
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
:root{--bg:#0a0f16;--panel:#151d29;--panel2:#111823;--accent:#4dd0c4;
--accent-dim:rgba(77,208,196,.13);--blue:#6cb2ff;--text:#dce5f0;
--dim:#8d99a9;--red:#f87171;--line:#26313f;--line2:#1d2734}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--text);
background-image:radial-gradient(1100px 520px at 50% -160px,#14283e 0%,rgba(10,15,22,0) 70%);
background-repeat:no-repeat;
font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
margin:0 auto;padding:32px 20px 24px;max-width:1280px}
h1{color:var(--accent);font-size:26px;font-weight:800;letter-spacing:2px;
text-align:center;margin:8px 0 6px}
@supports(-webkit-background-clip:text){
h1{background:linear-gradient(90deg,var(--accent) 20%,var(--blue) 85%);
-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.updated{text-align:center;color:var(--dim);font-size:12px;
letter-spacing:.4px;margin-bottom:26px}
h2{color:var(--blue);font-size:13px;font-weight:700;letter-spacing:2px;
text-transform:uppercase;margin:36px 0 12px;display:flex;
align-items:center;gap:12px}
h2::after{content:'';flex:1;height:1px;
background:linear-gradient(90deg,var(--line),transparent)}
.bracket{display:flex;gap:14px;flex-wrap:wrap;justify-content:center}
.stage{background:linear-gradient(180deg,var(--panel),var(--panel2));
border:1px solid var(--line);border-radius:14px;padding:14px 16px 12px;
min-width:205px;flex:1 1 205px;max-width:255px;
box-shadow:0 8px 24px rgba(0,0,0,.35);
transition:transform .15s ease,border-color .15s ease}
.stage:hover{transform:translateY(-3px);border-color:#35455c}
.stage h3{color:var(--blue);font-size:11px;font-weight:700;
text-transform:uppercase;letter-spacing:1.5px;margin:0 0 10px;
border-bottom:1px solid var(--line2);padding-bottom:8px}
.stage .tm{display:flex;justify-content:space-between;align-items:center;
gap:8px;font-size:13px;padding:5px 0;
border-bottom:1px dashed rgba(255,255,255,.05)}
.stage .tm:last-child{border-bottom:none}
.stage .tm .rec{color:var(--dim);font-size:12px;
font-variant-numeric:tabular-nums}
.stage .out{text-decoration:line-through;color:var(--red);opacity:.75}
.stage .out .rec{color:var(--red)}
.seed{display:inline-flex;justify-content:center;align-items:center;
min-width:20px;height:18px;background:var(--accent-dim);
color:var(--accent);font-weight:700;font-size:11px;border-radius:5px;
padding:0 4px;margin-right:8px;font-variant-numeric:tabular-nums}
.champbox{border-color:rgba(77,208,196,.45);
box-shadow:0 0 0 1px rgba(77,208,196,.2),0 0 30px rgba(77,208,196,.12),
0 8px 24px rgba(0,0,0,.35)}
.champbox h3{color:var(--accent)}
.champbox .winner{color:var(--accent);font-size:17px;font-weight:800;
letter-spacing:.5px;text-align:center;padding:18px 0}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:14px;
box-shadow:0 8px 24px rgba(0,0,0,.3);margin-bottom:10px}
table{border-collapse:collapse;width:100%;background:var(--panel);
font-size:13px}
th{background:#0e1621;color:var(--blue);padding:10px 7px;text-align:right;
font-size:10.5px;font-weight:700;text-transform:uppercase;
letter-spacing:.8px;white-space:nowrap}
th:first-child,td:first-child{text-align:left;padding-left:14px}
td{padding:8px 7px;text-align:right;border-top:1px solid var(--line2);
font-variant-numeric:tabular-nums;white-space:nowrap}
td:first-child{font-weight:600}
tbody tr:nth-child(even) td{background:rgba(255,255,255,.015)}
tbody tr:hover td{background:rgba(108,178,255,.06)}
tr.out td{color:var(--red)}tr.out td:first-child{text-decoration:line-through}
footer{text-align:center;color:var(--dim);font-size:12px;margin-top:40px;
border-top:1px solid var(--line);padding-top:16px;letter-spacing:.3px}
@media(max-width:640px){body{padding:20px 12px}
h1{font-size:20px;letter-spacing:1px}.stage{max-width:none}}
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
"""


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


def render_table(title, teams, order, records, values, out_set):
    """Standings table: Team, W, L, T, GB, PCT + 16 categories."""
    if not order:
        return ""
    lead = max(points(records[t]) for t in order)
    h = [f"<h2>{esc(title)}</h2>", '<div class="wrap">',
         '<table class="heat"><thead><tr><th>Team</th><th>W</th><th>L</th>'
         '<th>T</th><th>GB</th><th>PCT</th>']
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
        h.append(f'<tr{cls}><td><span class="seed">{team["seed"]}</span>'
                 f'{esc(team["name"])}</td><td>{rec[0]}</td><td>{rec[1]}</td>'
                 f'<td>{rec[2]}</td>'
                 f'<td>{"—" if gb == 0 else f"{gb:g}"}</td>'
                 f'<td>{pct:.3f}</td>')
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

    # ---- bracket cards: Week 1 / Weeks 1-2 / Weeks 1-3 / Championship ----
    cards = []
    titles = ["Week 1", "Weeks 1-2", "Weeks 1-3"]
    for i, stg in enumerate(stages):
        snap = stg["cum_snapshot"]
        rows = []
        parts = sorted(stg["participants"],
                       key=lambda t: (-points(snap[t]), seed(t)))
        for tid in parts:
            rec = rec_str(snap[tid]) if stg["started"] else "—"
            rows.append((seed(tid), teams[tid]["name"], rec,
                         tid == stg["eliminated"]))
        cards.append(render_stage_card(titles[i] if i < 3 else
                                       stg["week"]["label"], rows))
    # championship card
    ch_rows = []
    if len(champ["participants"]) == 4:
        parts = sorted(champ["participants"],
                       key=lambda t: (-points(champ["records"][t]), seed(t)))
        for tid in parts:
            rec = rec_str(champ["records"][tid]) if champ["started"] else "—"
            ch_rows.append((seed(tid), teams[tid]["name"], rec, False))
    else:
        ch_rows = [("—", "TBD", "—", False)] * 4
    cards.append(render_stage_card("Championship", ch_rows,
                                   note="records reset"))
    champion_html = (esc(teams[champ["champion"]]["name"])
                     if champ.get("champion") else "TBD")
    cards.append(f'<div class="stage champbox"><h3>Champion</h3>'
                 f'<div class="winner">🏆 {champion_html}</div></div>')

    # ---- current stage tables ----
    current = None
    for stg in stages:
        if stg["started"]:
            current = stg
    if champ["started"]:
        current = None  # championship table rendered instead

    tables = []
    out_all = {t for s in stages if s["eliminated"] for t in [s["eliminated"]]}
    if champ["started"]:
        order = sorted(champ["participants"],
                       key=lambda t: (-points(champ["records"][t]), seed(t)))
        tables.append(render_table(
            f"Championship — live standings", teams, order,
            champ["records"], champ["values"], set()))
    elif current is not None:
        order = sorted(current["participants"],
                       key=lambda t: (-points(current["records"][t]), seed(t)))
        tables.append(render_table(
            f"{current['week']['label']} — "
            f"{'final' if current['complete'] else 'live'} standings",
            teams, order, current["records"], current["values"], set()))

    # cumulative table across regular weeks (all 7 teams, eliminated in red)
    if any(s["started"] for s in stages):
        cum_vals = {t: derive_values(state["cum_raw"][t]) for t in teams}
        order = sorted(teams, key=lambda t: (-points(state["cumulative"][t]),
                                             seed(t)))
        tables.append(render_table("Cumulative standings (Weeks 1-3)", teams,
                                   order, state["cumulative"], cum_vals,
                                   out_all))
    if not tables:
        tables.append('<h2>Standings</h2><p style="color:var(--dim)">'
                      'Playoffs have not started yet.</p>')

    ts = now.strftime("%A, %B %-d, %Y at %-I:%M %p Pacific") \
        if os.name != "nt" else now.strftime("%A, %B %d, %Y %I:%M %p Pacific")
    html = ("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,"
            "initial-scale=1'>"
            f"<title>{esc(cfg['leagueName'])} Playoffs</title>"
            f"<style>{CSS}</style></head><body>"
            f"<h1>2026 FANTASY BASEBALL PLAYOFFS — "
            f"{esc(cfg['leagueName'])}</h1>"
            f"<div class='updated'>Updated {ts}</div>"
            f"<div class='bracket'>{''.join(cards)}</div>"
            f"{''.join(tables)}"
            "<footer>Auto-updated 3x daily · Fantrax lineups × MLB live "
            "stats</footer>"
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
    log("=== run complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
