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


def day_standouts(cfg, state, resolver, target, period_n, limit=6):
    """Best and worst single-day performances among ACTIVE players."""
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
def build_brief(mode, cfg, state, resolver, tz):
    now = datetime.now(tz)
    today = now.date()
    yesterday = today - timedelta(days=1)
    target = today if mode == "preview" else yesterday
    per = period_for(state, target) or period_for(state, today)
    period_n = per["period"] if per else None

    brief = {
        "mode": mode,
        "league": state.get("leagueName"),
        "generatedLocal": now.strftime("%A %B %-d, %Y %-I:%M %p Pacific"),
        "targetDate": target.isoformat(),
        "currentWeek": state.get("currentWeek"),
        "isChampionship": state.get("isChampionship"),
        "format": ("All-play: every team plays every other team each week across "
                   "16 categories. A category win is 1 point, a tie 0.5. "
                   "Seeds 1-2 are protected from elimination after Week 1."),
        "standings": [{"team": n, "seed": s, "record": f"{w}-{l}-{t}",
                       "points": p} for _, n, s, w, l, t, p in standings(state)],
        "eliminated": [team_name(state, t) for t in state.get("eliminated", [])],
    }
    if period_n is None:
        brief["note"] = "No active lineup period covers the target date."
        return brief

    if mode == "preview":
        log("building preview: probable starters + close categories")
        brief["probableStarters"] = probable_starters(cfg, state, resolver,
                                                     target, period_n)
        brief["closeCategories"] = close_categories(state, today)
    else:
        log("building recap: standouts + movers + close categories")
        brief["standouts"] = day_standouts(cfg, state, resolver, target, period_n)
        brief["movers"] = movers(state, tz)
        brief["closeCategories"] = close_categories(state, today)
    return brief


# ------------------------------------------------------------------ the writer
SYSTEM = """You write the daily commentary for a 12-team fantasy baseball league
called The League of Lords, now in its 2026 playoffs. You are writing for the
seven managers still alive, who have played together for eleven years and give
each other a lot of grief.

Voice: a sharp beat writer who actually watches the games. Dry, confident, a
little needling. Name managers and teams directly. Short paragraphs. No preamble,
no "here's your update", no bullet lists, no emoji, no headings.

Hard rules:
- Every number you cite must come from the JSON provided. Never invent or round
  in a way that changes a value.
- If a fact isn't in the JSON, don't assert it. No made-up injuries, no
  speculation about trades or lineups you can't see.
- Team names are the fantasy team names in the JSON. Use them.
- 'All-play' means each team faces all others weekly, so one category can swing
  several points at once. Treat close categories as genuinely consequential.
- Lower is better for SO (hitting), H allowed, BB allowed, and ERA.

Length: 150-220 words. Two or three paragraphs. Lead with the single most
interesting thing, not a summary."""

PREVIEW_HINT = """Write the MORNING PREVIEW for today. Focus on: who has pitching
going today and who doesn't (a team with two probable starters has a real edge in
IP, QS and K), and which categories are close enough that today decides them.
Call out any team with zero probable starters."""

RECAP_HINT = """Write the NIGHTLY RECAP for yesterday. Focus on: the best
individual performances and who they belong to, who gained or lost ground in the
standings, and which categories are still on a knife edge. If someone had a
disaster outing, say so."""


def write_commentary(brief):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        log("  ! ANTHROPIC_API_KEY not set — falling back to a plain summary")
        return fallback_text(brief), "fallback"
    hint = PREVIEW_HINT if brief["mode"] == "preview" else RECAP_HINT
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
    """Deterministic prose if the model is unavailable. Never blocks a run."""
    s = brief.get("standings") or []
    bits = []
    if s:
        bits.append(f"{s[0]['team']} leads {brief.get('currentWeek','the week')} "
                    f"at {s[0]['record']}, with {s[-1]['team']} last at "
                    f"{s[-1]['record']}.")
    if brief["mode"] == "preview":
        ps = brief.get("probableStarters") or []
        have = [p for p in ps if p["count"]]
        none = [p["team"] for p in ps if not p["count"]]
        if have:
            top = have[0]
            names = ", ".join(x["player"] for x in top["probableStarters"])
            bits.append(f"{top['team']} has {top['count']} probable starter"
                        f"{'s' if top['count'] != 1 else ''} today ({names}).")
        if none:
            bits.append("No probable starters for " + ", ".join(none) + ".")
    else:
        so = (brief.get("standouts") or {})
        th = so.get("topHitters") or []
        tp = so.get("topPitchers") or []
        if th:
            h = th[0]
            bits.append(f"{h['player']} ({h['team']}) went {h['h']}-for-{h['ab']}"
                        f" with {h['hr']} HR and {h['rbi']} RBI.")
        if tp:
            p = tp[0]
            bits.append(f"{p['player']} ({p['team']}) threw {p['ip']} IP with "
                        f"{p['k']} K and {p['er']} ER.")
        mv = brief.get("movers") or []
        if mv and mv[0]["pointsGained"]:
            bits.append(f"{mv[0]['team']} gained the most ground "
                        f"({mv[0]['pointsGained']:+} pts).")
    cc = brief.get("closeCategories") or []
    if cc:
        bits.append(f"{cc[0]['category']} is the tightest category, with "
                    f"{cc[0]['closePairs']} matchups within a hair.")
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
    title = "Morning Preview" if mode == "preview" else "Nightly Recap"
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
    head = ("**Morning Preview**" if mode == "preview" else "**Nightly Recap**")
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
    ap.add_argument("--mode", choices=["preview", "recap"], required=True)
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
