# League of Lords — 2026 Playoff Tracker

An automated standings page for our fantasy baseball playoffs. Three times a
day, a GitHub Action pulls the active lineups from Fantrax and live player
stats from MLB, recomputes every team's 16-category all-play record for each
playoff week, and republishes the results as a web page via GitHub Pages.

No server, no API keys, no cost. Everything runs inside GitHub.

## What's in here

| File | Purpose |
|---|---|
| `tracker.py` | The whole pipeline (Python standard library only) |
| `config.json` | League id, playoff teams/seeds, week schedule |
| `player_map.json` | Cache mapping Fantrax player ids to MLB ids (auto-grows) |
| `docs/index.html` | The published page (regenerated on every run) |
| `docs/history.json` | Snapshot of records after every run (auto-created) |
| `docs/data/` | Cached per-period stats so finished periods aren't refetched |
| `.github/workflows/update.yml` | The schedule that runs everything |

## One-time setup

1. **Create a public GitHub repository** (public is required for free GitHub
   Pages). Name it anything, e.g. `playoff-tracker`.
2. **Upload all of these files**, preserving the folder structure — in
   particular, `update.yml` must end up at `.github/workflows/update.yml`.
   (Easiest way: on your repo page, "Add file" → "Upload files" and drag the
   whole package in; or use `git push` from this folder.)
3. **Turn on GitHub Pages**: repo → Settings → Pages → under "Build and
   deployment" choose **Deploy from a branch**, branch **main**, folder
   **/docs**, then Save. Your page will be at
   `https://<your-username>.github.io/<repo-name>/`.
4. **Enable Actions**: click the **Actions** tab and, if prompted, click
   "I understand my workflows, go ahead and enable them."
5. **Test it**: Actions tab → "Update playoff tracker" (left sidebar) →
   "Run workflow" → Run workflow. It should finish green in a few minutes,
   commit updated files, and your Pages URL will refresh shortly after.

## When seeds are known: edit config.json

The `teams` list ships with `"TBD"` placeholders. Before Week 1, replace each
entry with the real Fantrax `teamId`, the team `name`, and its `seed` (1-7).
Team ids are the long codes Fantrax uses (e.g. `bnhdr2afmm9mb3pv`) — they
appear in the roster API response and in Fantrax team page URLs. Until the
TBDs are filled in, the tracker publishes a "waiting for config" page instead
of crashing.

If an elimination ever needs a manual correction (commissioner ruling, etc.),
add it to `eliminatedOverrides`, e.g. `{"bnhdr2afmm9mb3pv": "Week 1"}` means
that team was eliminated after Week 1 regardless of what the math says.

## Schedule

The workflow runs at **1pm, 5pm, and 10pm Pacific** every day (cron
`0 0,5,20 * * *` in UTC). You can also trigger it manually any time from the
Actions tab. Note: GitHub's scheduler can run a few minutes late — that's
normal.

## Troubleshooting

- **Page not updating?** Check the **Actions** tab. Open the latest
  "Update playoff tracker" run and read the log of the "Run tracker" step —
  it prints every fetch, every resolved player, and any per-player failures.
- **A single player failing** (name lookup or stats) is logged and skipped;
  it never kills the run. If someone's stats look missing, search the log for
  their name and add them to `player_map.json` by hand if needed
  (`"fantraxId": {"name": "Full Name", "mlbId": 123456}`).
- **Workflow didn't run on schedule?** GitHub pauses schedules on repos with
  60+ days of no activity — pushing any commit reactivates it. Manual runs
  always work.
- **Pages showing 404?** Re-check Settings → Pages is set to main branch,
  `/docs` folder, and that at least one Action run has committed.
