#!/usr/bin/env python3
"""Post the latest playoff tracker snapshot to a Discord channel.

Reads docs/history.json (written by tracker.py) and posts the current
standings to the webhook in the DISCORD_WEBHOOK_URL environment variable.
Exits quietly if the webhook or data isn't available, so the workflow
never fails because of Discord.
"""
import json
import os
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
HISTORY = os.path.join(ROOT, "docs", "history.json")
PAGE_URL = "https://mattamick11.github.io/league-playoff-tracker/"


def points(rec_str):
    try:
        w, l, t = (int(x) for x in rec_str.split("-"))
        return w + 0.5 * t
    except Exception:  # noqa: BLE001
        return 0.0


def main():
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        print("DISCORD_WEBHOOK_URL not set - skipping Discord post.")
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

    order = sorted(records.items(), key=lambda kv: -points(kv[1]))
    lead = points(order[0][1])
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏆 **League of Lords Playoffs — {week} update**", ""]
    for i, (name, rec) in enumerate(order):
        tag = medals[i] if i < len(medals) else f"`{i + 1}.`"
        gb = lead - points(rec)
        gb_txt = "—" if gb == 0 else f"{gb:g} GB"
        lines.append(f"{tag} **{name}**  {rec}  ({gb_txt})")
    lines += ["", f"Full standings, categories & bracket: {PAGE_URL}"]

    body = json.dumps({"content": "\n".join(lines)[:1990]}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "LeagueOfLordsTracker/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print(f"Discord post OK (HTTP {r.status})")
    except Exception as e:  # noqa: BLE001
        print(f"Discord post failed: {e}")


if __name__ == "__main__":
    main()
