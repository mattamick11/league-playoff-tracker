#!/usr/bin/env python3
"""
Playoff tracker commentary
==========================
Reads what tracker.py produced, works out what is actually interesting, has
Claude write it up, then publishes to docs/index.html and Discord.

Two modes:
  --mode preview   morning: today's probable starters per team + what's on a knife edge
  --mode recap     nightly: yesterday's standouts, standings movement, category flips

The analysis is done here in Python and handed to the model as facts. The model
writes prose; it does not invent numbers.

Python 3.11+ standard library only. Requires env ANTHROPIC_API_KEY (commentary)
and DISCORD_WEBHOOK_URL (posting). Missing either one degrades gracefully.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import tracker as T   # reuse the pipeline: resolver, stat fetch, scoring

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "docs", "state.json")
COMMENTARY_PATH = os.path.join(BASE_DIR, "docs", "commentary.json")
INDEX_PATH = os.path.join(BASE_DIR, "docs", "index.html")
HISTORY_PATH = os.path.join(BASE_DIR, "docs", "history.json")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-5"

# categories where a small edge is meaningful, with "how close is close"
CLOSE_THRESHOLD = {
    "R": 3, "HR": 1, "RBI": 3, "SO": 4, "SB": 1, "AVG": 0.006, "OPS": 0.020,
    "IP": 6, "QS": 1, "HA": 3, "BBA": 2, "K": 4, "ERA": 0.35, "SVH3": 0.5,
}
LABEL = {k: lbl for k, lbl, _ in T.CATEGORIES}
LOWER = {k: low for k, _, low in T.CATEGORIES}


def log(m):
    print(m, flush=True)


# ------------------------------------------------------------------ analysis
def load_state():
    st = T.load_json(STATE_PATH, None)
    if st is None:
        log("FATAL: docs/state.json missing — run tracker.py first")
    return st


def team_name(state, tid):
    return state["teams"].get(tid, {}).get("name", tid)


def standings(state):
    """Current week standings, best first: [(tid, name, seed, W,L,T, pts)]"""
    recs = state.get("weekRecords") or {}
    rows = []
    for tid, r in recs.items():
        rows.append((tid, team_name(state, tid),
                     state["teams"][tid]["seed"], r[0], r[1], r[2],
                     r[0] + 0.5 * r[2]))
    rows.sort(key=lambda x: (-x[6], x[2]))
    return rows


def week_progress(state, today):
    """Fraction of the current scoring week that has already been played."""
    pers = state.get("periods") or []
    if not pers:
        return 1.0
    start = T.parse_date(pers[0]["start"])
    end = T.parse_date(pers[-1]["end"])
    total = (end - start).days + 1
    done = (today - start).days + 1
    if total <= 0:
        return 1.0
    return max(0.0, min(1.0, done / total))


def close_categories(state, today, limit=5):
    """Which categories are genuinely on a knife edge right now.

    All-play means one category swings up to 6 head-to-heads at once, so a tight
    category is worth real points. Two guards keep this honest early in a week:

      * thresholds scale with how much of the week has been played, so on day 1
        of 7 only near-exact ties count rather than everything;
      * a category is skipped when nobody has meaningful volume in it yet
        (e.g. QS before anyone has started a game), because 0-0 ties across the
        board are not a story.
    """
    vals = state.get("weekValues") or {}
    parts = [t for t in state.get("participants", []) if t in vals]
    if len(parts) < 2:
        return []
    frac = max(week_progress(state, today), 0.15)
    out = []
    for key, base in CLOSE_THRESHOLD.items():
        thr = base * frac
        series = []
        for t in parts:
            v = vals[t].get(key)
            if v is None:
                continue
            series.append(v / 3.0 if key == "IP" else v)
        if len(series) < 2:
            continue
        spread = max(series) - min(series)
        # nothing has happened in this category yet -> not a story
        if spread == 0:
            continue
        pairs = []
        for i, a in enumerate(parts):
            for b in parts[i + 1:]:
                va, vb = vals[a].get(key), vals[b].get(key)
                if va is None or vb is None:
                    continue
                if key == "IP":
                    va, vb = va / 3.0, vb / 3.0
                gap = abs(va - vb)
                if gap > thr:
                    continue
                # "both at zero" is not a knife edge, it means nothing has
                # happened yet. Require at least one side to have real volume.
                if va == 0 and vb == 0:
                    continue
                if gap == 0:
                    lead = "tied"
                else:
                    lead = team_name(state, a if ((va > vb) != LOWER[key]) else b)
                pairs.append({"teams": [team_name(state, a), team_name(state, b)],
                              "gap": round(gap, 4), "leader": lead})
        if not pairs:
            continue
        pairs.sort(key=lambda p: p["gap"])
        out.append({"category": LABEL[key],
                    "closePairs": len(pairs),
                    "totalPairs": len(parts) * (len(parts) - 1) // 2,
                    "spread": round(spread, 4),
                    "thresholdUsed": round(thr, 4),
                    "tightest": pairs[:3]})
    # rank by swing available, then by how bunched it is
    out.sort(key=lambda c: (-c["closePairs"], c["spread"]))
    return out[:limit]


def movers(state, tz):
    """Standings movement vs the oldest snapshot from ~24h ago."""
    hist = T.load_json(HISTORY_PATH, [])
    if len(hist) < 2:
        return []
    now_recs = hist[-1].get("records", {})
    cutoff = datetime.now(tz) - timedelta(hours=26)
    prior = None
    for snap in reversed(hist[:-1]):
        try:
            ts = datetime.fromisoformat(snap["timestamp"])
        except Exception:
            continue
        if ts <= cutoff:
            prior = snap
            break
    if prior is None:
        prior = hist[0]
    if prior.get("week") != hist[-1].get("week"):
        return []          # records reset between weeks; movement is meaningless

    def pts(rs):
        try:
            w, l, t = (int(x) for x in rs.split("-"))
            return w + 0.5 * t
        except Exception:
            return None

    def rank(recs):
        order = sorted(recs.items(), key=lambda kv: -(pts(kv[1]) or 0))
        return {n: i + 1 for i, (n, _) in enumerate(order)}

    r_now, r_old = rank(now_recs), rank(prior.get("records", {}))
    out = []
    for name, rs in now_recs.items():
        if name not in prior.get("records", {}):
            continue
        p_now, p_old = pts(rs), pts(prior["records"][name])
        if p_now is None or p_old is None:
            continue
        out.append({"team": name,
                    "pointsGained": round(p_now - p_old, 1),
                    "rankNow": r_now.get(name), "rankBefore": r_old.get(name),
                    "rankChange": (r_old.get(name, 0) - r_now.get(name, 0)),
                    "recordNow": rs, "recordBefore": prior["records"][name]})
    out.sort(key=lambda m: -m["pointsGained"])
    return out


def active_rosters(cfg, period_n):
    """{teamId: [{fid,pos,status}]} for one lineup period."""
    try:
        data = T.http_get_json(
            f"{T.FANTRAX_BASE}/getTeamRosters?leagueId={cfg['leagueId']}"
            f"&period={period_n}")
    except Exception as e:
        log(f"  ! roster fetch failed: {e}")
        return {}
    out = {}
    for tid, r in (data.get("rosters") or {}).items():
        out[tid] = [it for it in r.get("rosterItems", [])
                    if it.get("status") == "ACTIVE"]
    return out


def period_for(state, d):
    for p in state.get("periods", []):
        if p["start"] <= d.isoformat() <= p["end"]:
            return p
    return None


def probable_starters(cfg, state, resolver, target, period_n):
    """Which owned, ACTIVE pitchers are probable starters on `target`."""
    try:
        data = T.mlb_get(
            f"{T.MLB_BASE}/schedule?sportId=1&date={target.isoformat()}"
            f"&hydrate=probablePitcher,team")
    except Exception as e:
        log(f"  ! schedule fetch failed: {e}")
        return []
    prob = {}
    for day in data.get("dates", []):
        for g in day.get("games", []):
            for side in ("away", "home"):
                t = (g.get("teams") or {}).get(side) or {}
                pp = t.get("probablePitcher") or {}
                opp = "home" if side == "away" else "away"
                if pp.get("id"):
                    prob[pp["id"]] = {
                        "name": pp.get("fullName"),
                        "mlbTeam": (t.get("team") or {}).get("name"),
                        "opponent": ((g.get("teams") or {}).get(opp, {})
                                     .get("team") or {}).get("name"),
                        "gameTime": g.get("gameDate"),
                    }
    rosters = active_rosters(cfg, period_n)
    out = []
    for team in cfg["teams"]:
        tid = team["teamId"]
        mine = []
        for item in rosters.get(tid, []):
            if item.get("position") not in T.PITCHER_POSITIONS:
                continue
            info = resolver.resolve(item.get("id"))
            if not info:
                continue
            p = prob.get(info["mlbId"])
            if p:
                mine.append({"player": p["name"], "mlbTeam": p["mlbTeam"],
                             "opponent": p["opponent"]})
        out.append({"team": team["name"], "seed": team["seed"],
                    "probableStarters": mine, "count": len(mine)})
    out.sort(key=lambda x: (-x["count"], x["seed"]))
    return out


def remaining_pitching(cfg, state, resolver, today):
    """Probable starts still to come for each team in the current scoring week.

    This is the number that matters. Category totals are settled at the END of
    the week, so it is irrelevant whether a team's starts land Monday or
    Saturday. A team can be last in IP on Tuesday and win it comfortably. Only a
    thin slate of REMAINING starts late in the week is a real problem.
    """
    pers = state.get("periods") or []
    if not pers:
        return {}
    week_end = T.parse_date(pers[-1]["end"])
    days = []
    d = today
    while d <= week_end:
        days.append(d)
        d = d + timedelta(days=1)
    if not days:
        return {}
    prob = {}   # mlbId -> list of {date, mlbTeam, opponent}
    for d in days:
        try:
            data = T.mlb_get(f"{T.MLB_BASE}/schedule?sportId=1&date={d.isoformat()}"
                             f"&hydrate=probablePitcher,team")
        except Exception as e:
            log(f"  ! schedule fetch failed for {d}: {e}")
            continue
        for day in data.get("dates", []):
            for g in day.get("games", []):
                for side in ("away", "home"):
                    t = (g.get("teams") or {}).get(side) or {}
                    pp = t.get("probablePitcher") or {}
                    if not pp.get("id"):
                        continue
                    opp = "home" if side == "away" else "away"
                    prob.setdefault(pp["id"], []).append({
                        "date": d.isoformat(),
                        "player": pp.get("fullName"),
                        "mlbTeam": (t.get("team") or {}).get("name"),
                        "opponent": ((g.get("teams") or {}).get(opp, {})
                                     .get("team") or {}).get("name")})
    # map onto fantasy rosters using the period covering each date
    out = {}
    for team in cfg["teams"]:
        out[team["name"]] = {"seed": team["seed"], "starts": []}
    seen = set()
    for per in pers:
        p_start, p_end = T.parse_date(per["start"]), T.parse_date(per["end"])
        if p_end < today:
            continue
        rosters = active_rosters(cfg, per["period"])
        for team in cfg["teams"]:
            for item in rosters.get(team["teamId"], []):
                if item.get("position") not in T.PITCHER_POSITIONS:
                    continue
                info = resolver.resolve(item.get("id"))
                if not info:
                    continue
                for st in prob.get(info["mlbId"], []):
                    if not (p_start.isoformat() <= st["date"] <= p_end.isoformat()):
                        continue
                    k = (team["name"], st["player"], st["date"])
                    if k in seen:
                        continue
                    seen.add(k)
                    out[team["name"]]["starts"].append(st)
    for v in out.values():
        v["starts"].sort(key=lambda x: x["date"])
        v["remainingStarts"] = len(v["starts"])
    return out


def team_day_report(cfg, state, resolver, target, period_n):
    """Per-team breakdown of one day. This is the spine of the write-up.

    Built the way a manager actually reads a day:
      1. every active player's line, so the standouts are FOUND not guessed;
      2. each team's own hitting day, including what the team hit WITHOUT its
         best performer -- "Burleson carried him" is only visible that way;
      3. every STARTING pitcher appearance in full. A team gets 7-10 starts a
         week, so one start moves IP/QS/K/ERA far more than any hitter moves a
         hitting category. Near-miss quality starts are flagged explicitly
         because 5.2 innings with 3 ER is the classic gut punch.
      4. bench players who produced, so "good call sitting him" is checkable.
    """
    fetcher = T.StatFetcher(cfg, resolver, date.today())
    d = target.isoformat()
    try:
        data = T.http_get_json(
            f"{T.FANTRAX_BASE}/getTeamRosters?leagueId={cfg['leagueId']}"
            f"&period={period_n}")
        rosters = data.get("rosters") or {}
    except Exception as e:
        log(f"  ! roster fetch failed: {e}")
        return {}

    report = {}
    for team in cfg["teams"]:
        tid = team["teamId"]
        items = (rosters.get(tid) or {}).get("rosterItems", [])
        hitters, starters, relievers, bench = [], [], [], []
        tot = {"ab": 0, "h": 0, "r": 0, "hr": 0, "rbi": 0, "sb": 0, "so": 0, "tb": 0}
        for item in items:
            fid, pos = item.get("id"), item.get("position", "")
            status = item.get("status")
            info = resolver.resolve(fid)
            if not info:
                continue
            is_p = pos in T.PITCHER_POSITIONS
            try:
                raw = (fetcher._pitcher_raw(info["mlbId"], d, d) if is_p
                       else fetcher._hitter_raw(info["mlbId"], d, d))
            except Exception as e:
                log(f"  ! {info.get('name')}: {e}")
                continue
            active = status == "ACTIVE"
            if is_p:
                if raw["OUTS"] == 0:
                    continue
                era = 27.0 * raw["ER"] / raw["OUTS"]
                rec = {"player": info["name"], "ip": T.outs_to_ip_str(raw["OUTS"]),
                       "outs": raw["OUTS"], "er": raw["ER"], "h": raw["HA"],
                       "bb": raw["BBA"], "k": raw["K"], "qs": raw["QS"],
                       "era": round(era, 2), "started": raw["QS"] > 0 or raw["OUTS"] >= 9,
                       "svh3": raw["SV"] + 0.5 * raw["HLD"], "status": status}
                # a start that fell one or two outs short of a QS, or had the
                # innings but gave up 4+ -- both worth calling out by name
                if raw["OUTS"] >= 14 and raw["OUTS"] < 18 and raw["ER"] <= 3:
                    rec["nearMissQS"] = f"{18 - raw['OUTS']} out(s) short of a QS"
                elif raw["OUTS"] >= 18 and raw["ER"] >= 4:
                    rec["nearMissQS"] = f"went {T.outs_to_ip_str(raw['OUTS'])} but allowed {raw['ER']} ER"
                (starters if rec["started"] else relievers).append(rec) if active \
                    else bench.append({**rec, "side": "P"})
            else:
                if raw["AB"] == 0 and raw["BB"] == 0:
                    continue
                tb = raw["H"] + raw["D2"] + 2 * raw["D3"] + 3 * raw["HR"]
                rec = {"player": info["name"], "ab": raw["AB"], "h": raw["H"],
                       "r": raw["R"], "hr": raw["HR"], "rbi": raw["RBI"],
                       "sb": raw["SB"], "so": raw["SO"], "tb": tb,
                       "bb": raw["BB"], "status": status}
                if active:
                    hitters.append(rec)
                    for k, kk in (("ab","AB"),("h","H"),("r","R"),("hr","HR"),
                                  ("rbi","RBI"),("sb","SB"),("so","SO")):
                        tot[k] += raw[kk]
                    tot["tb"] += tb
                else:
                    bench.append({**rec, "side": "H"})
        hitters.sort(key=lambda x: -(x["tb"] + x["r"] + x["rbi"] + 1.5 * x["sb"]))
        starters.sort(key=lambda x: -x["outs"])
        avg = round(tot["h"] / tot["ab"], 3) if tot["ab"] else None
        # what the team hit WITHOUT its best bat -- shows who carried whom
        rest_avg = None
        if hitters and tot["ab"] - hitters[0]["ab"] > 0:
            rest_avg = round((tot["h"] - hitters[0]["h"])
                             / (tot["ab"] - hitters[0]["ab"]), 3)
        report[team["name"]] = {
            "seed": team["seed"],
            "teamHitting": {**tot, "avg": avg, "restOfTeamAvg": rest_avg,
                            "topBat": hitters[0]["player"] if hitters else None},
            "hitters": hitters[:8],
            "startingPitchers": starters,
            "relievers": relievers,
            "benchProduced": [b for b in bench
                              if (b.get("hr") or b.get("sb") or b.get("outs", 0) >= 9)][:4],
        }
    return report


def day_standouts(cfg, state, resolver, target, period_n, limit=6):
    """Best and worst single-day performances among ACTIVE players.

    This scans EVERY active player on every team and ranks them. It must never
    be short-circuited by guessing at likely names — a 3-homer game from a $9
    outfielder is exactly the sort of thing a guess misses.
    """
    fetcher = T.StatFetcher(cfg, resolver, date.today())
    d = target.isoformat()
    rosters = active_rosters(cfg, period_n)
    hitters, pitchers = [], []
    for team in cfg["teams"]:
        tid = team["teamId"]
        for item in rosters.get(tid, []):
            info = resolver.resolve(item.get("id"))
            if not info:
                continue
            pos = item.get("position", "")
            try:
                if pos in T.PITCHER_POSITIONS:
                    raw = fetcher._pitcher_raw(info["mlbId"], d, d)
                    if raw["OUTS"] == 0:
                        continue
                    ip = raw["OUTS"] / 3.0
                    era = (27.0 * raw["ER"] / raw["OUTS"]) if raw["OUTS"] else 0
                    pitchers.append({
                        "player": info["name"], "team": team["name"],
                        "ip": T.outs_to_ip_str(raw["OUTS"]), "k": raw["K"],
                        "er": raw["ER"], "h": raw["HA"], "bb": raw["BBA"],
                        "qs": raw["QS"], "svh3": raw["SV"] + 0.5 * raw["HLD"],
                        "era": round(era, 2),
                        "_score": raw["K"] * 1.0 + raw["QS"] * 4 + ip * 0.6
                                  - raw["ER"] * 2.0 + (raw["SV"] + 0.5 * raw["HLD"]) * 2})
                else:
                    raw = fetcher._hitter_raw(info["mlbId"], d, d)
                    if raw["AB"] == 0 and raw["BB"] == 0:
                        continue
                    tb = raw["H"] + raw["D2"] + 2 * raw["D3"] + 3 * raw["HR"]
                    hitters.append({
                        "player": info["name"], "team": team["name"],
                        "ab": raw["AB"], "h": raw["H"], "r": raw["R"],
                        "hr": raw["HR"], "rbi": raw["RBI"], "sb": raw["SB"],
                        "so": raw["SO"], "tb": tb,
                        "_score": tb * 1.0 + raw["R"] * 1.0 + raw["RBI"] * 1.0
                                  + raw["SB"] * 1.5 - raw["SO"] * 0.4})
            except Exception as e:
                log(f"  ! day stats failed for {info.get('name')}: {e}")
    hitters.sort(key=lambda x: -x["_score"])
    pitchers.sort(key=lambda x: -x["_score"])
    for lst in (hitters, pitchers):
        for x in lst:
            x.pop("_score", None)
    return {"topHitters": hitters[:limit], "topPitchers": pitchers[:limit],
            "worstPitchers": pitchers[-3:][::-1] if len(pitchers) > 3 else []}


# ------------------------------------------------------------------ the brief
def season_context(resolver, names_ids, season=2026):
    """Season-to-date lines for the day's notable players only (cheap: ~10 calls).

    Lets the write-up say "one of the best hitters in baseball this year" or note
    a guy's homer total, instead of treating every performance as context-free.
    """
    out = {}
    for name, mlb_id, side in names_ids:
        try:
            grp = "pitching" if side == "P" else "hitting"
            data = T.mlb_get(f"{T.MLB_BASE}/people/{mlb_id}/stats"
                             f"?stats=season&group={grp}&season={season}")
            sp = (data.get("stats") or [{}])[0].get("splits") or []
            if not sp:
                continue
            st = sp[0].get("stat", {})
            if side == "P":
                out[name] = {"w": st.get("wins"), "l": st.get("losses"),
                             "era": st.get("era"), "ip": st.get("inningsPitched"),
                             "k": st.get("strikeOuts"), "whip": st.get("whip"),
                             "gs": st.get("gamesStarted")}
            else:
                out[name] = {"avg": st.get("avg"), "hr": st.get("homeRuns"),
                             "rbi": st.get("rbi"), "r": st.get("runs"),
                             "sb": st.get("stolenBases"), "ops": st.get("ops"),
                             "team": (sp[0].get("team") or {}).get("name")}
        except Exception as e:
            log(f"  ! season stats failed for {name}: {e}")
    return out


def build_brief(mode, cfg, state, resolver, tz):
    """One nightly brief: what happened today, where the week stands, what's left."""
    now = datetime.now(tz)
    today = now.date()
    per = period_for(state, today)
    period_n = per["period"] if per else None
    pers = state.get("periods") or []
    week_end = T.parse_date(pers[-1]["end"]) if pers else today
    days_left = max(0, (week_end - today).days)

    brief = {
        "mode": mode,
        "league": state.get("leagueName"),
        "generatedLocal": now.strftime("%A %B %-d, %Y %-I:%M %p Pacific"),
        "resultsThrough": today.isoformat(),
        "currentWeek": state.get("currentWeek"),
        "isChampionship": state.get("isChampionship"),
        "weekEnds": week_end.isoformat(),
        "daysRemainingInWeek": days_left,
        "weekProgress": round(week_progress(state, today), 2),
        "format": (
            "All-play: every team plays every other team each week across 16 "
            "categories. A category win is 1 point, a tie 0.5, so with 7 teams "
            "each manager has 6 head-to-heads and 96 category decisions a week. "
            "Category totals are settled at the END of the week."),
        "seedRules": (
            "Seeds 1 and 2 are protected from elimination after Week 1, but "
            "their records still carry into the following weeks, so a bad week "
            "still costs them."),
        "standings": [{"team": n, "seed": s_, "record": f"{w}-{l}-{t}",
                       "points": p} for _, n, s_, w, l, t, p in standings(state)],
        "eliminated": [team_name(state, t) for t in state.get("eliminated", [])],
    }
    if period_n is None:
        brief["note"] = "No active lineup period covers today."
        return brief

    log("scanning every player for today's lines, team by team")
    brief["teamDayReports"] = team_day_report(cfg, state, resolver, today, period_n)
    log("ranking league-wide standouts")
    brief["todayStandouts"] = day_standouts(cfg, state, resolver, today, period_n)
    # season-to-date context for just the players who mattered today
    notable = []
    seen_n = set()
    for tr in (brief["teamDayReports"] or {}).values():
        for h in tr["hitters"][:2]:
            if h["player"] not in seen_n and (h["hr"] or h["tb"] >= 4):
                seen_n.add(h["player"]); notable.append((h["player"], None, "H"))
        for sp_ in tr["startingPitchers"]:
            if sp_["player"] not in seen_n:
                seen_n.add(sp_["player"]); notable.append((sp_["player"], None, "P"))
    resolved = []
    pl = resolver.pmap.get("players", {})
    byname = {v.get("name"): v.get("mlbId") for v in pl.values()}
    for nm, _, side in notable[:16]:
        if byname.get(nm):
            resolved.append((nm, byname[nm], side))
    log(f"fetching season context for {len(resolved)} notable players")
    brief["seasonContext"] = season_context(resolver, resolved)
    lc = T.load_json(os.path.join(BASE_DIR, "league_context.json"), {}) or {}
    # trim provenance to the players who actually appear today, so the model sees
    # "this guy was a $9 trade pickup" only for people it is going to write about
    prov_all = lc.pop("acquisitionProvenance", {})
    mentioned = set()
    for tr in (brief.get("teamDayReports") or {}).values():
        for h in tr["hitters"][:4]:
            mentioned.add(h["player"])
        for sp_ in tr["startingPitchers"]:
            mentioned.add(sp_["player"])
        for b in tr.get("benchProduced", []):
            mentioned.add(b["player"])
    for t in (brief.get("remainingPitching") or {}).values():
        for st in t.get("starts", []):
            mentioned.add(st["player"])
    lc["acquisitionProvenance"] = {k: v for k, v in prov_all.items() if k in mentioned}
    brief["leagueContext"] = lc
    log(f"league context: {len(lc.get('acquisitionProvenance', {}))} provenance entries "
        f"for today's named players")
    log("computing standings movement")
    brief["movers"] = movers(state, tz)
    log("computing close categories")
    brief["closeCategories"] = close_categories(state, today)
    log("fetching remaining probable starts for the rest of the week")
    brief["remainingPitching"] = remaining_pitching(cfg, state, resolver, today)
    # team-level week totals so the model can see context, not just deltas
    vals = state.get("weekValues") or {}
    brief["weekTotals"] = {}
    for tid in state.get("participants", []):
        v = vals.get(tid) or {}
        brief["weekTotals"][team_name(state, tid)] = {
            "R": v.get("R"), "HR": v.get("HR"), "RBI": v.get("RBI"),
            "SB": v.get("SB"), "SO": v.get("SO"),
            "AVG": round(v["AVG"], 3) if v.get("AVG") is not None else None,
            "OPS": round(v["OPS"], 3) if v.get("OPS") is not None else None,
            "IP": round(v["IP"] / 3.0, 1) if v.get("IP") is not None else None,
            "QS": v.get("QS"), "K": v.get("K"),
            "H_allowed": v.get("HA"), "BB_allowed": v.get("BBA"),
            "ERA": round(v["ERA"], 2) if v.get("ERA") is not None else None,
            "SVH3": v.get("SVH3"),
        }
    return brief


# ------------------------------------------------------------------ the writer
SYSTEM = """You write the nightly update for The League of Lords, a 12-team
fantasy baseball league now in its 2026 playoffs. Seven managers are alive. They
have played together eleven years, know the game cold, and read this for the
detail — not for a summary.

STRUCTURE — follow this, it is the whole point
Go TEAM BY TEAM, in current standings order, one short paragraph each. Open with
a line setting the scene (which day of the week it is, how much is left). Close
with a one-line sign-off. For each team cover, in roughly this order:
  1. the day's best bat on that team, with the actual line;
  2. how the REST of the team hit -- use teamHitting.restOfTeamAvg. "X carried
     him" is only true if the rest of the roster was quiet, so check;
  3. every STARTING PITCHER who threw, by name, with the line. This matters more
     than any hitter. A team gets only 7-10 starts a week, so one bad start
     wrecks IP, K and ERA together. Always mention a nearMissQS by name -- 5.2
     innings one out short of a quality start is a genuine gut punch and the
     league will feel it.
  4. who they have on the mound over the NEXT day or two, from remainingPitching,
     and whether the slate is thin.
Do not write a standings table. Do not write bullet lists.

VOICE
Warm, funny, conversational, like a friend who watched every game. First names or
league nicknames (Cwill, Soy, Con, Cousin, Burz). Dry asides are good. You may
tease a bad day. You are NOT dramatic and NOT a hype man.

HOW TO READ A YOUNG WEEK
Check daysRemainingInWeek and weekProgress first. Early in a week almost nothing
is settled -- say someone is off to a nice start, never that anyone is running
away with it or in trouble. A big early lead in IP, K or QS just means those
starters pitched first; it evens out, so do not call it an advantage. If teams are
TIED in a category, say tied -- check weekTotals before claiming anyone leads.
AVG and OPS on a few dozen at-bats mean very little yet.

PITCHING, PROPERLY
Category totals settle at the END of the week. It does not matter whether starts
land Monday or Saturday. Use remainingPitching. A team light on innings with
starts still coming is fine -- do not imply otherwise. Only call it a problem if
they are well behind AND nearly out of remaining starts. A thin slate for the next
couple of days is worth a passing note, not alarm.

CONTEXT YOU MAY USE — this is what separates a good write-up from a box score
- seasonContext: season-to-date lines for the players you are naming. Use it to
  size a performance: "one of the best hitters in baseball this year", "that's 32
  on the season".
- leagueContext.playoffField: seed, regular-season record, and how each team got
  in. Keller finished EIGHTH and made it only by winning the July-only roto
  challenge, and he is the defending champion — both worth using.
- leagueContext.seasonTeamProfiles: each team's league rank (of 12) in every
  category across the whole season. This is how you describe a team's identity
  rather than just its day: Soy led the league in innings and quality starts but
  was last in hits and walks allowed, so he is a volume-starts team; Ryan had the
  best ERA in the league while sitting 5th in innings, so his staff is genuinely
  good; Tory and Cwill finished 1-2 in SVH3, so both have real bullpens; Will led
  in HR, RBI and OPS. Use a team's season identity to frame whether a day is
  normal or surprising for them.
- leagueContext.acquisitionProvenance: how each named player was acquired —
  draft price and pick, keeper price, a trade, or a waiver claim. When someone has
  a big game, check it. A $9 draft pick acquired in a trade, or a $1 waiver claim
  going off in the playoffs, is a genuinely fun thing to point out.
Only use what is in the JSON. Do not invent a trade, a price, or a rank.

HARD RULES
- Every number must come from the JSON. Never invent one, never round in a way
  that changes it. If a fact is not there, do not assert it.
- Lower is better for SO (hitting), H allowed, BB allowed, ERA.
- Seeds 1 and 2 cannot be eliminated after Week 1, but their records carry into
  later weeks, so a bad week still costs them. Mention protection only where it
  is relevant.
- Never claim a real-world trade, injury or call-up unless it is in the JSON.

LENGTH: 450-650 words. Seven short team paragraphs plus an opener and a sign-off."""

NIGHTLY_HINT = """Write tonight's update. It is about 9:30pm Pacific and today's
games are final. Remember the reader sees this tonight, so pitchers listed in
remainingPitching for tomorrow's date go "tomorrow", not "today"."""


def write_commentary(brief):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        log("  ! ANTHROPIC_API_KEY not set — falling back to a plain summary")
        return fallback_text(brief), "fallback"
    hint = NIGHTLY_HINT
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 900,
        "system": SYSTEM,
        "messages": [{"role": "user",
                      "content": f"{hint}\n\nDATA:\n{json.dumps(brief, indent=1)}"}],
    }).encode()
    req = urllib.request.Request(
        ANTHROPIC_URL, data=body,
        headers={"content-type": "application/json",
                 "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.load(r)
        parts = [b.get("text", "") for b in data.get("content", [])
                 if b.get("type") == "text"]
        text = "\n\n".join(p.strip() for p in parts if p.strip())
        if not text:
            raise ValueError("empty response")
        log(f"  Claude wrote {len(text)} chars")
        return text, MODEL
    except urllib.error.HTTPError as e:
        log(f"  ! Anthropic HTTP {e.code}: {e.read()[:300]!r}")
    except Exception as e:
        log(f"  ! Anthropic call failed: {e}")
    return fallback_text(brief), "fallback"


def fallback_text(brief):
    """Deterministic prose if the model is unavailable. Never blocks a run.

    Follows the same discipline as the prompt: lead with the best individual
    performance, and do not imply anything is decided while the week is young.
    """
    bits = []
    so = brief.get("todayStandouts") or {}
    th = (so.get("topHitters") or [])
    tp = (so.get("topPitchers") or [])
    if th:
        h = th[0]
        line = f"{h['player']} ({h['team']}) went {h['h']}-for-{h['ab']}"
        extra = []
        if h.get("hr"):
            extra.append(f"{h['hr']} HR")
        if h.get("rbi"):
            extra.append(f"{h['rbi']} RBI")
        if h.get("r"):
            extra.append(f"{h['r']} R")
        if h.get("sb"):
            extra.append(f"{h['sb']} SB")
        if extra:
            line += " with " + ", ".join(extra)
        bits.append(line + ".")
    if tp:
        p = tp[0]
        bits.append(f"{p['player']} ({p['team']}) threw {p['ip']} IP, "
                    f"{p['k']} K, {p['er']} ER.")
    s_ = brief.get("standings") or []
    if s_:
        bits.append(f"{s_[0]['team']} leads {brief.get('currentWeek','the week')} "
                    f"at {s_[0]['record']}.")
    dl = brief.get("daysRemainingInWeek")
    if dl is not None and dl >= 4:
        bits.append(f"{dl} days still to play, so little is settled.")
    rp = brief.get("remainingPitching") or {}
    if rp:
        ranked = sorted(rp.items(), key=lambda kv: -kv[1].get("remainingStarts", 0))
        top, low = ranked[0], ranked[-1]
        bits.append(f"{top[0]} has the most starts left ({top[1]['remainingStarts']}), "
                    f"{low[0]} the fewest ({low[1]['remainingStarts']}).")
    return " ".join(bits) or "No commentary available for this run."


# ------------------------------------------------------------------ publish
def inject_into_page(text, mode, brief):
    if not os.path.exists(INDEX_PATH):
        log("  ! docs/index.html missing — skipping page injection")
        return False
    html = open(INDEX_PATH, encoding="utf-8").read()
    if "<!--COMMENTARY-->" not in html:
        log("  ! placeholder <!--COMMENTARY--> not found in index.html")
        return False
    title = "Nightly Update"
    paras = "".join(f"<p>{T.esc(p)}</p>" for p in text.split("\n\n") if p.strip())
    tag = "" if brief.get("_model") != "fallback" else \
        '<span class="tag">auto</span>'
    block = (f'<div class="commentary"><h3>{title}{tag}</h3>'
             f'<div class="cmeta">{T.esc(brief.get("generatedLocal",""))} · '
             f'{T.esc(str(brief.get("currentWeek","")))}</div>{paras}</div>')
    open(INDEX_PATH, "w", encoding="utf-8").write(
        html.replace("<!--COMMENTARY-->", block))
    log("  injected commentary into docs/index.html")
    return True


def post_discord(text, mode, brief):
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        log("  ! DISCORD_WEBHOOK_URL not set — skipping Discord")
        return False
    head = "**Nightly Update**"
    week = brief.get("currentWeek", "")
    page = "https://mattamick11.github.io/league-playoff-tracker/"
    msg = f"{head} — {week}\n\n{text}\n\n<{page}>"
    for chunk in [msg[i:i + 1900] for i in range(0, len(msg), 1900)]:
        body = json.dumps({"content": chunk}).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json",
                     "User-Agent": "playoff-tracker-commentary/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                log(f"  Discord post OK (HTTP {r.status})")
        except Exception as e:
            log(f"  ! Discord post failed: {e}")
            return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["nightly"], default="nightly")
    ap.add_argument("--no-discord", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="build the brief and print it; no model call, no publish")
    args = ap.parse_args()

    cfg = T.load_json(os.path.join(BASE_DIR, "config.json"), None)
    if cfg is None:
        log("FATAL: config.json not found")
        return 1
    state = load_state()
    if state is None:
        return 1
    tz = ZoneInfo(cfg.get("timezone", "America/Los_Angeles"))

    pmap = T.load_json(os.path.join(BASE_DIR, "player_map.json"), {"players": {}})
    resolver = T.PlayerResolver(cfg["leagueId"], pmap)

    log(f"=== commentary ({args.mode}) ===")
    brief = build_brief(args.mode, cfg, state, resolver, tz)
    resolver.save_if_dirty()

    if args.dry_run:
        print(json.dumps(brief, indent=1))
        return 0

    text, model = write_commentary(brief)
    brief["_model"] = model
    T.save_json(COMMENTARY_PATH, {
        "mode": args.mode, "model": model,
        "generated": brief.get("generatedLocal"),
        "week": brief.get("currentWeek"),
        "text": text, "brief": brief}, indent=1)
    log(f"wrote docs/commentary.json (model={model})")
    inject_into_page(text, args.mode, brief)
    if not args.no_discord:
        post_discord(text, args.mode, brief)
    log("=== commentary complete ===")
    print("\n" + "-" * 60 + "\n" + text + "\n" + "-" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
