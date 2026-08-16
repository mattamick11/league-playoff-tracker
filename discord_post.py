#!/usr/bin/env python3
"""Post the playoff standings snapshot to Discord, at most 3x per day.

Reads docs/history.json (written by tracker.py) and posts current standings.
Exits quietly on any problem so the workflow never fails because of Discord.

WHY THE SLOT LOGIC (added 2026-08-04)
tracker.py now runs every 30 minutes during the MLB day (~30 runs/day). Posting
on every run would mean ~30 Discord messages a day. Matt asked for three.

The obvious fix -- gate on `github.event.schedule` -- is unreliable: GitHub's
scheduler delivers runs late (we measured 90 minutes) and drops short-interval
crons outright, so a post tied to one exact cron string silently never happens.

Instead each run asks "which of today's three slots is the most recent one that
has passed, and have I already posted for it?" A run at 13:47 still fills the
13:00 slot. A slot is never filled twice. Dropped runs self-heal on the next one.
"""
import json
import os
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
HISTORY = os.path.join(ROOT, "docs", "history.json")
STATE = os.path.join(ROOT, "docs", "state.json")
MARKER = os.path.join(ROOT, "docs", "last_discord_post.json")
PAGE_URL = "https://mattamick11.github.io/league-playoff-tracker/"

TZ = ZoneInfo("America/Los_Angeles")
# Matt chose every three hours: 1pm, 4pm, 7pm, 10pm Pacific (2026-08-04).
SLOT_HOURS = (13, 16, 19, 22)


def points(rec_str):
    try:
        w, l, t = (int(x) for x in rec_str.split("-"))
        return w + 0.5 * t
    except Exception:  # noqa: BLE001
        return 0.0


def current_slot(now):
    """The most recent slot that has already passed today, or None."""
    passed = [h for h in SLOT_HOURS if (now.hour, now.minute) >= (h, 0)]
    if not passed:
        return None
    return f"{now.date().isoformat()}T{max(passed):02d}"


def eliminated_names():
    """Team names already knocked out, so the snapshot only lists live teams.

    Reads docs/state.json, which tracker.py rewrites on every run. A team lands
    in `eliminated` once the week that knocked it out is complete, so this
    drops each week's loser by itself the morning after -- no edit needed here
    when the field shrinks. Fails open: if state is unreadable we post everyone
    rather than post nothing.
    """
    try:
        with open(STATE, encoding="utf-8") as f:
            state = json.load(f)
        teams = state.get("teams", {})
        return {teams[t]["name"] for t in state.get("eliminated", [])
                if t in teams}
    except Exception as e:  # noqa: BLE001
        print(f"! could not read eliminated list ({e}); showing all teams")
        return set()


def already_posted(slot):
    try:
        with open(MARKER, encoding="utf-8") as f:
            return json.load(f).get("slot") == slot
    except Exception:  # noqa: BLE001
        return False


def record_posted(slot):
    try:
        os.makedirs(os.path.dirname(MARKER), exist_ok=True)
        with open(MARKER, "w", encoding="utf-8") as f:
            json.dump({"slot": slot,
                       "postedAt": datetime.now(TZ).isoformat()}, f, indent=1)
    except Exception as e:  # noqa: BLE001
        print(f"! could not write slot marker: {e}")


def main():
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        print("DISCORD_WEBHOOK_URL not set - skipping Discord post.")
        return

    now = datetime.now(TZ)
    slot = current_slot(now)
    if slot is None:
        print(f"{now:%H:%M} Pacific is before the first slot "
              f"({SLOT_HOURS[0]}:00) - skipping.")
        return
    if already_posted(slot):
        print(f"Slot {slot} already posted - skipping.")
        return

    try:
        with open(HISTORY, encoding="utf-8") as f:
            hist = json.load(f)
        snap = hist[-1]
    except Exception:  # noqa: BLE001
        print("No history snapshot available - skipping Discord post.")
        return

    week = snap.get("week", "")
    records = snap.get("records", {})
    if not records or week == "pre-playoffs":
        print("Playoffs not underway - skipping Discord post.")
        return

    out = eliminated_names()
    live = {n: r for n, r in records.items() if n not in out}
    if not live:
        print("Every team reads as eliminated - skipping rather than "
              "posting an empty board.")
        return
    if out:
        print(f"Omitting eliminated: {', '.join(sorted(out))}")

    order = sorted(live.items(), key=lambda kv: -points(kv[1]))
    lead = points(order[0][1])
    medals = ["\U0001F947", "\U0001F948", "\U0001F949"]
    lines = [f"\U0001F3C6 **League of Lords Playoffs — {week} update**", ""]
    for i, (name, rec) in enumerate(order):
        tag = medals[i] if i < len(medals) else f"`{i + 1}.`"
        gb = lead - points(rec)
        gb_txt = "—" if gb == 0 else f"{gb:g} GB"
        lines.append(f"{tag} **{name}**  {rec}  ({gb_txt})")
    lines += ["", f"Full standings, categories & bracket: {PAGE_URL}"]

    content = "\n".join(lines)[:1990]
    # parse: [] means a stray @everyone in any team name can never ping the league
    body = json.dumps({"content": content,
                       "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "LeagueOfLordsTracker/1.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print(f"Discord post OK (HTTP {r.status}) for slot {slot}")
        record_posted(slot)
    except Exception as e:  # noqa: BLE001
        print(f"Discord post failed: {e}")


if __name__ == "__main__":
    main()
