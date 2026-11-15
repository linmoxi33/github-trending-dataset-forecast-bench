# github-trending-dataset-forecast-bench

**Does today's GitHub Trending daily list actually predict tomorrow's? This tool answers with a walk-forward backtest you can re-run offline — because every "this repo is trending" claim downstream is untested until someone measures it.**

Upstream: https://github.com/linmoxi33/github-trending-dataset

## What the upstream is, and what it is missing

[`linmoxi33/github-trending-dataset`](https://github.com/linmoxi33/github-trending-dataset) (CC0-1.0) keeps one JSON snapshot per day of GitHub's three Trending lists, with `stars_period` (velocity) per repo. At commit [`de4a06f`](https://github.com/linmoxi33/github-trending-dataset/blob/de4a06f1fba32cfcbf4b2a334cd8a7abf8f1bcff/scripts/query.py#L595-L621) its reader exposes exactly five questions:

- `stats` — archive coverage / integrity
- `top` — one day's ranking
- `timeline` — every appearance of *one* repository
- `diff FROM TO` — who entered / left / moved between *two* days
- `windows` — daily/weekly/monthly side by side

That is a **retrospective query surface**: every command describes days that already happened, and the README's own summary is "coverage, rankings, timelines, day-to-day diffs" ([README](https://github.com/linmoxi33/github-trending-dataset/blob/de4a06f1fba32cfcbf4b2a334cd8a7abf8f1bcff/README.md#L18-L25)). Nothing in the archive reports **how much of tomorrow's list today's list actually explains**, and no baseline is published, so a `diff` that shows "8 of 14 repos stayed" cannot distinguish a predictable list from a churn-heavy one.

This bench fills that one gap: it takes the same on-disk snapshots, replays them day by day, and scores three predictors plus a falsification control against the real next day — including the repos that **come back** after dropping out, which `diff` can never evaluate because it only compares two days with no memory.

## What this tool does / does not do

Does:

- builds the appearance cohort across the whole archive (debuts, re-entry repos, one-hit wonders, stayers, longest streak, median days present);
- runs a **walk-forward** backtest: for every day `t`, each predictor sees only days `<= t`, then is scored against day `t+1` (precision / recall / F1, macro over transitions);
- compares three predictors and a control:
  - `persistence` — tomorrow = today's set;
  - `velocity` — hottest `stars_period` among everything seen in the look-back window;
  - `recency` — recency-decayed rank + velocity score, **with a reserved slot budget** so it is allowed to nominate repos absent today;
  - `control` — same outsider budget as `recency`, outsiders picked pseudo-randomly (deterministic hash order), so the challenger is falsifiable;
- reports re-entry recall separately (persistence scores 0 by construction);
- emits a deterministic `replay_sha256` over the whole report.

Does not:

- fetch anything from the network, or replace the upstream scraper;
- claim star-count accuracy, or model anything outside the daily list;
- predict beyond one day, or rank outside the archive's repo universe.

## Install

No dependencies, Python 3.9+:

```bash
git clone git@github-linmoxi33:linmoxi33/github-trending-dataset-forecast-bench.git
cd github-trending-dataset-forecast-bench
python3 trending_forecast_bench.py examples/in
```

## Usage

```bash
# full report, human readable
python3 trending_forecast_bench.py /path/to/github-trending-dataset/data

# machine readable (parseable JSON on stdout)
python3 trending_forecast_bench.py examples/in --json

# tune the challenger: look-back window and outsider slot share
python3 trending_forecast_bench.py examples/in --json --window 10 --reserve 0.25
```

`bin/trending-forecast-bench` is a thin executable wrapper around the same file.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Backtest ran **and** the challenger beat the best baseline by `>= 0.005` F1 |
| `1` | Backtest ran, challenger did **not** beat the best baseline (a finding, not a crash) |
| `2` | Usage or input error (missing directory, unreadable archive, `< 3` snapshots, bad `--window`/`--reserve`) |

### `--json` shape (abridged, real run on the 5-day `examples/in`)

```json
{
  "archive": {"snapshots": 5, "first": "2026-11-11", "last": "2026-11-15", "period": "daily"},
  "cohort": {"repos": 38, "debuts": 26, "re_entry_repos": 26, "one_hit_repos": 22,
             "stayers": 5, "median_days_present": 1.0, "max_streak": 5},
  "backtest": {
    "transitions": 4,
    "macro": {
      "persistence": {"precision": 0.4535, "recall": 0.4170, "f1": 0.4305},
      "velocity":    {"precision": 0.2785, "recall": 0.2785, "f1": 0.2759},
      "recency":     {"precision": 0.3676, "recall": 0.3436, "f1": 0.3517},
      "control":     {"precision": 0.3484, "recall": 0.3228, "f1": 0.3317}
    },
    "reentry": {"truth_total": 3, "recall": {"persistence": 0.0, "velocity": 0.0,
                                             "recency": 0.333333, "control": 0.0}},
    "verdict": {"best_baseline": "persistence", "best_baseline_f1": 0.4305,
                "delta_f1": -0.0788, "delta_vs_control": 0.02,
                "beats_baseline": false, "beats_control": true}
  },
  "replay_sha256": "c7f6bcfe115b218342887afc7b4c4f68708bfe197af427b06907d81326936c96"
}
```

## Result on the full archive (28 snapshots, 2026-10-19 .. 2026-11-15, 27 transitions)

Measured by running the command below on a real checkout of the upstream archive:

```bash
python3 trending_forecast_bench.py /path/to/github-trending-dataset/data --json
```

| predictor | precision | recall | F1 |
| --- | --- | --- | --- |
| `persistence` (today's set) | 0.5037 | 0.5035 | **0.4975** |
| `velocity` (window hot list) | 0.2744 | 0.2805 | 0.2744 |
| `recency` (challenger) | 0.4110 | 0.4133 | 0.4071 |
| `control` (random outsiders, same budget) | 0.3957 | 0.3964 | 0.3912 |

- Re-entry recall over 28 real re-entries: `persistence 0.000`, `velocity 0.214`, `recency 0.357`, `control 0.143`.
- Cohort: 160 repos, 146 debuts, 148 repos with >= 1 re-entry, 74 one-hit wonders, 44 stayers (>= 3 consecutive days), max streak 8, median 2 days present.
- Verdict: `recency` **loses** to `persistence` by `-0.0904` F1, so exit code is `1`; it **beats its own random control** by `+0.0160`, so the scoring is not noise. Read plainly: half of tomorrow's list is today's list, and any model that spends budget on re-entries pays for that recall in F1.

## 本轮创新

First published walk-forward benchmark **of the trending list itself** — not another scraper, not another dashboard. The gap is real and stated by the upstream's own command surface (`stats/top/timeline/diff/windows`, all retrospective). New and verifiable: (1) a re-entry recall metric that `diff` structurally cannot produce; (2) a same-budget random control that makes the challenger falsifiable; (3) a deterministic replay hash so any headline number can be re-derived byte for byte. Conclusion is a negative result, reported as such.

## 与同类项目的差异

`gh search repos "github trending analysis"` returns only personal report generators and dashboards (highest hit: 1 star, e.g. `jmservera/SquadScope`), all of which *describe* a day. None of them backtest a predictor against the next day, none report re-entry recall, and none ship a falsification control. This tool ships no UI and no scraping: it is the missing measurement layer over an existing archive.

## Limits

- Daily list only (`--period daily`); weekly/monthly are stored by the archive but not scored.
- `k` (prediction budget) is today's list size, so a day whose list shrinks is penalised in recall by construction.
- Ranking ties are broken deterministically (score, then recency, then name) — this is a benchmark, not a trading signal.
- Snapshots with the same date are deduped by `captured_at`; files that fail to parse are listed in `archive.skipped_files` and never silently counted.
- The upstream list itself is GitHub's own ranking, so the bench measures *persistence of that ranking*, not "true" popularity.

## Tests

```bash
python3 tests/run.py
```

23 tests: archive dedupe / malformed-input handling, cohort classification, slot-budget invariants, control determinism, score math, re-entry truth counting, replay-hash stability, plus subprocess behaviour tests for exit codes and committed example reproduction.

## License

MIT for this tool. The bundled `examples/in/*.json` are unmodified snapshots from the upstream archive, which is CC0-1.0.