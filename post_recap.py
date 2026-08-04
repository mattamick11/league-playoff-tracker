#!/usr/bin/env python3
"""Post recap.md to Discord as a sequence of messages that respect the 2000-char limit.

Reads recap.md from the repo root. Splits on blank lines so paragraphs are never
cut mid-sentence, packs into messages of at most LIMIT characters, and posts them
in order with a small delay so Discord preserves ordering.

Fails CLOSED: if the webhook or the file is missing, or if any chunk would exceed
the hard limit, it posts NOTHING and exits non-zero. A partial recap is worse than
no recap.

Usage:
  python post_recap.py            # post for real
  python post_recap.py --dry-run  # print the chunks, post nothing
"""
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
RECAP = os.path.join(ROOT, "recap.md")
LIMIT = 1900          # our budget
HARD_LIMIT = 2000     # Discord's actual ceiling
MAX_MESSAGES = 4


def chunk(text, limit=LIMIT):
    """Pack paragraphs into <=limit-char messages without splitting a paragraph."""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    out, cur = [], ""
    for p in paras:
        if len(p) > limit:
            # a single paragraph too long for one message: split on sentence ends
            if cur:
                out.append(cur); cur = ""
            piece = ""
            for sent in p.replace("\n", " ").split(". "):
                sent = (sent + ". ") if not sent.endswith(".") else sent + " "
                if len(piece) + len(sent) > limit:
                    out.append(piece.rstrip()); piece = ""
                piece += sent
            if piece.strip():
                cur = piece.rstrip()
            continue
        candidate = (cur + "\n\n" + p) if cur else p
        if len(candidate) > limit:
            out.append(cur); cur = p
        else:
            cur = candidate
    if cur:
        out.append(cur)
    return out


def main():
    dry = "--dry-run" in sys.argv
    if not os.path.exists(RECAP):
        print("FAIL: recap.md not found - nothing posted.")
        return 1
    text = open(RECAP, encoding="utf-8").read().strip()
    if not text:
        print("FAIL: recap.md is empty - nothing posted.")
        return 1

    parts = chunk(text)
    n = len(parts)
    print(f"recap.md is {len(text)} chars -> {n} message(s)")
    for i, p in enumerate(parts, 1):
        print(f"  [{i}/{n}] {len(p)} chars")

    # fail closed on anything suspicious
    if n > MAX_MESSAGES:
        print(f"FAIL: {n} messages exceeds MAX_MESSAGES={MAX_MESSAGES}. Nothing posted.")
        return 1
    over = [i for i, p in enumerate(parts, 1) if len(p) >= HARD_LIMIT]
    if over:
        print(f"FAIL: chunk(s) {over} at/over Discord's {HARD_LIMIT}-char ceiling. Nothing posted.")
        return 1
    if "@everyone" in text or "@here" in text:
        print("FAIL: recap contains a mass mention. Nothing posted.")
        return 1

    if dry:
        print("\n--- DRY RUN, nothing sent ---")
        for i, p in enumerate(parts, 1):
            print(f"\n===== message {i}/{n} =====\n{p}")
        return 0

    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        print("FAIL: DISCORD_WEBHOOK_URL not set - nothing posted.")
        return 1

    for i, p in enumerate(parts, 1):
        body = json.dumps({"content": p, "allowed_mentions": {"parse": []}}).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json",
                     "User-Agent": "LeagueOfLordsRecap/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                print(f"  posted {i}/{n} (HTTP {r.status})")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL posting {i}/{n}: {e}")
            print("  WARNING: earlier messages may already be live.")
            return 1
        if i < n:
            time.sleep(1.2)
    print("all messages posted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
