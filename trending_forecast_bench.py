#!/usr/bin/env python3
"""trending-forecast-bench — walk-forward predictability benchmark for GitHub Trending dailies.

Reads a local directory of `YYYY-MM-DD.json` snapshots (the format published by
linmoxi33/github-trending-dataset), builds the appearance cohort, then runs a
walk-forward backtest: for every day t, three predictors see only days <= t and
are scored against the real day t+1 daily list.

Zero dependencies, no network, deterministic output (replay SHA-256).
"""

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import sys

VERSION = "1.0.0"
DECAY = 0.85          # recency weight per day of absence
WINDOW = 7            # look-back days for the challenger candidate pool
MARGIN = 0.005        # challenger must beat the best baseline by this much
RESERVE = 0.2         # share of the challenger's budget reserved for outsiders
PERIODS = ("daily",)


class InputError(Exception):
    """Usage or input problem -> exit code 2."""


# ---------------------------------------------------------------- archive ----

def load_archive(root, period="daily"):
    """Load every YYYY-MM-DD.json in *root*, dedupe by snapshot date.

    Returns (days, meta). days is a list of dicts sorted by date:
        {"date": "2026-11-13", "sizes": n, "rows": [{"repo", "rank", "stars_period", "stars_total"}]}
    """
    if not os.path.isdir(root):
        raise InputError("archive directory not found: %s" % root)
    keep = {}
    dropped, skipped = [], []
    for name in sorted(os.listdir(root)):
        if not (name[:4].isdigit() and name.endswith(".json")):
            continue
        path = os.path.join(root, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as exc:
            skipped.append({"file": name, "why": str(exc)[:120]})
            continue
        lists = payload.get("lists") or {}
        rows = lists.get(period)
        if not isinstance(rows, list):
            skipped.append({"file": name, "why": "no lists.%s" % period})
            continue
        date = payload.get("date") or name[:-5]
        try:
            dt.date.fromisoformat(date)
        except ValueError:
            skipped.append({"file": name, "why": "bad date %r" % date})
            continue
        captured = payload.get("captured_at") or ""
        prev = keep.get(date)
        if prev is not None:
            # Same snapshot date twice (e.g. latest.json copied in): keep the later capture.
            dropped.append({"date": date, "kept": max(prev[0], captured),
                            "file": name})
            if captured <= prev[0]:
                continue
        clean = []
        for idx, row in enumerate(rows):
            if not isinstance(row, dict) or not isinstance(row.get("repo"), str):
                continue
            clean.append({
                "repo": row["repo"],
                "rank": idx + 1,
                "stars_period": _num(row.get("stars_period")),
                "stars_total": _num(row.get("stars_total")),
            })
        if not clean:
            skipped.append({"file": name, "why": "empty %s list" % period})
            continue
        keep[date] = (captured, {"date": date, "rows": clean, "file": name})
    days = [keep[d][1] for d in sorted(keep)]
    if not days:
        raise InputError("no usable snapshots in %s" % root)
    for d in days:
        d["sizes"] = len(d["rows"])
    meta = {"root": os.path.abspath(root), "period": period,
            "snapshots": len(days), "first": days[0]["date"], "last": days[-1]["date"],
            "duplicates_dropped": dropped, "skipped_files": skipped}
    return days, meta


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


# ----------------------------------------------------------------- cohort ----

def build_cohort(days):
    """Across-day appearance structure: debut / re-entry / one-hit / stayer."""
    seen_dates = {}
    for d in days:
        for row in d["rows"]:
            seen_dates.setdefault(row["repo"], []).append((d["date"], row["rank"], row["stars_period"]))
    archive_start = days[0]["date"]
    repos = []
    for repo, appearances in seen_dates.items():
        dates = [a[0] for a in appearances]
        # streak: longest run of consecutive archive days present
        by_date = set(dates)
        gaps, streak, best, re_entries = 0, 0, 0, 0
        prev = None
        for d in days:
            here = d["date"] in by_date
            if here:
                streak = streak + 1 if prev is True else 1
                best = max(best, streak)
                if prev is False:
                    re_entries += 1
            else:
                gaps += 1
            prev = here
        first = dates[0]
        repos.append({
            "repo": repo,
            "first_seen": first,
            "last_seen": dates[-1],
            "days_present": len(dates),
            "re_entries": re_entries,
            "longest_streak": best,
            "best_rank": min(a[1] for a in appearances),
            "peak_stars_period": max(a[2] for a in appearances),
            "debut": first != archive_start,
            "one_hit": len(dates) == 1,
            "stayer": best >= 3,
        })
    repos.sort(key=lambda r: (-r["days_present"], r["best_rank"], r["repo"]))
    reentry = [r for r in repos if r["re_entries"] > 0]
    return {
        "repos": len(repos),
        "debuts": sum(1 for r in repos if r["debut"]),
        "re_entry_repos": len(reentry),
        "one_hit_repos": sum(1 for r in repos if r["one_hit"]),
        "stayers": sum(1 for r in repos if r["stayer"]),
        "median_days_present": _median([r["days_present"] for r in repos]),
        "max_streak": max((r["longest_streak"] for r in repos), default=0),
        "top_reentry": sorted(reentry, key=lambda r: (-r["re_entries"], r["repo"]))[:10],
    }


def _median(nums):
    if not nums:
        return 0.0
    s = sorted(nums)
    mid = len(s) // 2
    return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


# ------------------------------------------------------------- predictors ----

def predictor_persistence(history, day):
    """Assume tomorrow looks like today: every repo on today's list, ranked by today."""
    return [r["repo"] for r in day["rows"]]


def predictor_velocity(history, day, window=WINDOW):
    """Baseline: every repo seen in the look-back window, hottest stars_period first.

    This is the "just rank the hot list" strategy a watcher would use without any
    per-repo memory, and unlike persistence it can name outsiders at all.
    """
    last = {}
    for i, d in enumerate(history[-window:]):
        for r in d["rows"]:
            last[r["repo"]] = (i, r["stars_period"], r["rank"])
    ranked = sorted(last, key=lambda name: (-last[name][1], -last[name][0], last[name][2], name))
    return ranked


def predictor_recency(history, day, window=WINDOW):
    """Challenger: recency-decayed rank + velocity evidence over the look-back window.

    Unlike persistence it can nominate a repo that is *not* on today's list
    (candidate pool = every repo seen in the last `window` days), which is what
    makes catching tomorrow's re-entries possible at all.
    """
    pool = {}
    recent = history[-window:] if window else []
    last_date = day["date"]
    last_idx = len(history) - 1
    for i, d in enumerate(recent):
        age = last_idx - (len(history) - len(recent) + i)
        weight = DECAY ** age
        top_vel = max((r["stars_period"] for r in d["rows"]), default=0) or 1
        for r in d["rows"]:
            slot = pool.setdefault(r["repo"], 0.0)
            slot += weight * (0.6 * (1.0 / r["rank"]) + 0.4 * (r["stars_period"] / top_vel))
    # Deterministic tie-break: score desc, then most recent appearance, then name.
    last_seen = {}
    for i, d in enumerate(history):
        for r in d["rows"]:
            last_seen[r["repo"]] = i
    ranked = sorted(pool, key=lambda name: (-round(pool[name], 12), -last_seen.get(name, -1), name))
    # K never depends on the future: size of today's list.
    return ranked


def select_with_slots(ranked, today_set, k, reserve):
    """Pick k repos, keeping round(k * reserve) slots for repos absent today.

    A pure re-ranking of today's list can never name tomorrow's re-entry, so the
    challenger is forced to spend part of its budget on repos it thinks will
    come back. Everything is computed from history <= t only.
    """
    outsiders = [r for r in ranked if r not in today_set]
    insiders = [r for r in ranked if r in today_set]
    take_out = min(max(1, int(round(k * reserve))), len(outsiders))
    out = insiders[:k - take_out]
    out += outsiders[:take_out]
    backfill = [r for r in ranked if r not in out]
    out += backfill[:max(0, k - len(out))]
    return out[:k]


# --------------------------------------------------------------- backtest ----

def take_reserve(k, ratio):
    return max(1, int(round(k * ratio))) if k else 0


def select_control(ranked, today_set, k, reserve, seed):
    """Same outsider budget as the challenger, but outsiders picked pseudo-randomly.

    This is the control that makes the challenger falsifiable: if scoring cannot
    beat random outsider picks at an equal budget, the scoring adds nothing.
    """
    outsiders = sorted((r for r in ranked if r not in today_set),
                       key=lambda r: hashlib.sha256((seed + "|" + r).encode()).hexdigest())
    insiders = [r for r in ranked if r in today_set]
    take_out = min(max(1, int(round(k * reserve))), len(outsiders))
    out = insiders[:k - take_out] + outsiders[:take_out]
    out += [r for r in ranked if r not in out][:max(0, k - len(out))]
    return out[:k]


def score(predicted, truth, k):
    pred = predicted[:k]
    hits = sum(1 for repo in pred if repo in truth)
    precision = hits / float(k) if k else 0.0
    recall = hits / float(len(truth)) if truth else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": precision, "recall": recall, "f1": f1, "hits": hits}


def backtest(days, window=WINDOW, reserve=RESERVE):
    transitions = []
    agg = {name: {"precision": 0.0, "recall": 0.0, "f1": 0.0, "hits": 0}
           for name in ("persistence", "velocity", "recency", "control")}
    reentry_truth, reentry_hits = 0, {"persistence": 0, "velocity": 0, "recency": 0, "control": 0}
    for i in range(1, len(days)):
        history = days[:i]
        today, tomorrow = days[i - 1], days[i]
        truth = [r["repo"] for r in tomorrow["rows"]]
        truth_set = set(truth)
        today_set = {r["repo"] for r in today["rows"]}
        seen_before = set()
        for d in history[:-1]:
            seen_before.update(r["repo"] for r in d["rows"])
        entrants = [repo for repo in truth if repo not in today_set]
        reentries = [repo for repo in entrants if repo in seen_before]
        reentry_truth += len(reentries)
        k = len(today["rows"])
        ranked = predictor_recency(history, today, window)
        preds = {"persistence": predictor_persistence(history, today)[:k],
                 "velocity": predictor_velocity(history, today, window)[:k],
                 "recency": select_with_slots(ranked, today_set, k, reserve),
                 "control": select_control(ranked, today_set, k, reserve, today["date"])}
        row = {"from": today["date"], "to": tomorrow["date"], "k": k, "reserve": take_reserve(k, reserve),
               "truth_size": len(truth), "entrants": len(entrants),
               "reentries": len(reentries)}
        for name, pred in preds.items():
            s = score(pred, truth_set, len(pred))
            row[name] = {"precision": round(s["precision"], 6),
                         "recall": round(s["recall"], 6),
                         "f1": round(s["f1"], 6), "hits": s["hits"]}
            for key in ("precision", "recall", "f1", "hits"):
                agg[name][key] += s[key]
            reentry_hits[name] += sum(1 for r in reentries if r in set(pred))
        transitions.append(row)
    n = len(transitions) or 1
    for name in agg:
        for key in ("precision", "recall", "f1"):
            agg[name][key] = round(agg[name][key] / n, 6)
        agg[name]["hits"] = agg[name]["hits"]
    for name in reentry_hits:
        reentry_hits[name] = round(reentry_hits[name] / reentry_truth, 6) if reentry_truth else 0.0
    best_baseline = max(agg["persistence"]["f1"], agg["velocity"]["f1"])
    return {
        "transitions": len(transitions),
        "window": window,
        "reserve": reserve,
        "per_transition": transitions,
        "macro": agg,
        "reentry": {"truth_total": reentry_truth,
                    "recall": reentry_hits},
        "verdict": {
            "challenger": "recency",
            "best_baseline": "persistence" if agg["persistence"]["f1"] >= agg["velocity"]["f1"] else "velocity",
            "best_baseline_f1": round(best_baseline, 6),
            "delta_f1": round(agg["recency"]["f1"] - best_baseline, 6),
            "delta_vs_control": round(agg["recency"]["f1"] - agg["control"]["f1"], 6),
            "beats_control": agg["recency"]["f1"] > agg["control"]["f1"] + MARGIN,
            "margin": MARGIN,
            "beats_baseline": agg["recency"]["f1"] > best_baseline + MARGIN,
        },
    }


def report(days, meta, window=WINDOW, reserve=RESERVE):
    data = {
        "tool": "trending-forecast-bench",
        "version": VERSION,
        "archive": {k: v for k, v in meta.items() if k != "root"},
        "cohort": build_cohort(days),
        "backtest": backtest(days, window, reserve),
    }
    data["replay_sha256"] = replay_hash(data)
    return data


def replay_hash(data):
    body = {k: v for k, v in data.items() if k != "replay_sha256"}
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# -------------------------------------------------------------------- CLI ----

def render_text(rep):
    a, c, b = rep["archive"], rep["cohort"], rep["backtest"]
    out = []
    out.append("trending-forecast-bench %s" % rep["version"])
    out.append("archive: %d snapshots %s..%s (%s list)" % (a["snapshots"], a["first"], a["last"], a["period"]))
    if a["duplicates_dropped"]:
        out.append("duplicates dropped: %d" % len(a["duplicates_dropped"]))
    out.append("cohort: %d repos | %d debuts | %d re-entry repos | %d one-hit | %d stayers(>=3d) | median %.1f days"
               % (c["repos"], c["debuts"], c["re_entry_repos"], c["one_hit_repos"],
                  c["stayers"], c["median_days_present"]))
    out.append("backtest: %d transitions, k = today's list size" % b["transitions"])
    out.append("  %-12s %8s %8s %8s" % ("predictor", "prec", "recall", "f1"))
    for name in ("persistence", "velocity", "recency", "control"):
        m = b["macro"][name]
        out.append("  %-12s %8.4f %8.4f %8.4f" % (name, m["precision"], m["recall"], m["f1"]))
    v = b["verdict"]
    out.append("  control = same outsider budget, pseudo-random picks (falsification check)")
    out.append("re-entry recall (of %d re-entries): persistence %.3f | velocity %.3f | recency %.3f"
               % (b["reentry"]["truth_total"], b["reentry"]["recall"]["persistence"],
                  b["reentry"]["recall"]["velocity"], b["reentry"]["recall"]["recency"]))
    out.append("verdict: recency %s %s (delta_f1 %+.4f vs %s, margin %.3f)"
               % ("beats" if v["beats_baseline"] else "does NOT beat", v["best_baseline"],
                  v["delta_f1"], v["best_baseline"], v["margin"]))
    out.append("challenger vs control: delta_f1 %+.4f -> scoring %s random outsider picks"
               % (v["delta_vs_control"], "beats" if v["beats_control"] else "does NOT beat"))
    out.append("replay_sha256: %s" % rep["replay_sha256"])
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="trending-forecast-bench",
        description="Walk-forward predictability benchmark over local GitHub Trending daily snapshots.")
    ap.add_argument("archive", help="directory containing YYYY-MM-DD.json snapshots")
    ap.add_argument("--json", action="store_true", help="emit the full report as JSON on stdout")
    ap.add_argument("--window", type=int, default=WINDOW, help="challenger look-back days (default 7)")
    ap.add_argument("--period", default="daily", choices=PERIODS)
    ap.add_argument("--reserve", type=float, default=RESERVE,
                    help="share of challenger slots kept for repos absent today (default 0.2)")
    ap.add_argument("--min-snapshots", type=int, default=3, help="require at least N snapshots (default 3)")
    ap.add_argument("--version", action="version", version="trending-forecast-bench " + VERSION)
    args = ap.parse_args(argv)
    if args.window < 1:
        print("error: --window must be >= 1", file=sys.stderr)
        return 2
    if not 0.0 <= args.reserve <= 0.8:
        print("error: --reserve must be within [0.0, 0.8]", file=sys.stderr)
        return 2
    if args.min_snapshots < 2:
        print("error: --min-snapshots must be >= 2 (one transition needs two days)", file=sys.stderr)
        return 2
    try:
        days, meta = load_archive(args.archive, args.period)
    except InputError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if len(days) < args.min_snapshots:
        print("error: need >= %d snapshots, found %d" % (args.min_snapshots, len(days)), file=sys.stderr)
        return 2
    rep = report(days, meta, args.window, args.reserve)
    if args.json:
        json.dump(rep, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print(render_text(rep))
    return 0 if rep["backtest"]["verdict"]["beats_baseline"] else 1


if __name__ == "__main__":
    sys.exit(main())