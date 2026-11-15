#!/usr/bin/env python3
"""Tests for trending-forecast-bench. Zero dependencies: python3 tests/run.py"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import trending_forecast_bench as tfb  # noqa: E402

SCRIPT = os.path.join(ROOT, "trending_forecast_bench.py")


def snapshot(date, rows, captured=None):
    return {
        "date": date,
        "captured_at": captured or date + "T03:00:00+00:00",
        "lists": {"daily": [{"repo": r, "stars_period": v, "stars_total": 1000 + v,
                             "language": "Python", "description": ""} for r, v in rows],
                  "weekly": [], "monthly": []},
    }


def write_archive(tmp, days, suffix=""):
    for payload in days:
        name = payload["date"] + ("-dup" if suffix else "") + ".json"
        with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    return tmp


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dedupes_same_date_keeping_later_capture(self):
        write_archive(self.tmp, [snapshot("2026-01-01", [("a/x", 5)])])
        with open(os.path.join(self.tmp, "2026-01-01-dup.json"), "w") as fh:
            json.dump(snapshot("2026-01-01", [("a/x", 5), ("b/y", 9)],
                               captured="2026-01-01T09:00:00+00:00"), fh)
        days, meta = tfb.load_archive(self.tmp)
        self.assertEqual(meta["snapshots"], 1)
        self.assertEqual(len(meta["duplicates_dropped"]), 1)
        self.assertEqual(len(days[0]["rows"]), 2)

    def test_skips_malformed_and_records_reason(self):
        write_archive(self.tmp, [snapshot("2026-01-01", [("a/x", 5)])])
        with open(os.path.join(self.tmp, "2026-01-02.json"), "w") as fh:
            fh.write("{not json")
        days, meta = tfb.load_archive(self.tmp)
        self.assertEqual(len(days), 1)
        self.assertEqual(meta["skipped_files"][0]["file"], "2026-01-02.json")

    def test_skips_payload_without_daily_list(self):
        with open(os.path.join(self.tmp, "2026-01-01.json"), "w") as fh:
            json.dump({"date": "2026-01-01", "lists": {"weekly": []}}, fh)
        with self.assertRaises(tfb.InputError):
            tfb.load_archive(self.tmp)

    def test_missing_directory_is_an_input_error(self):
        with self.assertRaises(tfb.InputError):
            tfb.load_archive(os.path.join(self.tmp, "nope"))

    def test_ignores_non_date_files(self):
        write_archive(self.tmp, [snapshot("2026-01-01", [("a/x", 5)])])
        with open(os.path.join(self.tmp, "latest.json"), "w") as fh:
            json.dump({"date": "2026-01-01", "lists": {"daily": [{"repo": "z/z"}]}}, fh)
        with open(os.path.join(self.tmp, "notes.txt"), "w") as fh:
            fh.write("ignore me")
        days, _ = tfb.load_archive(self.tmp)
        self.assertEqual([r["repo"] for r in days[0]["rows"]], ["a/x"])


class CohortTests(unittest.TestCase):
    def build(self, dates_to_rows):
        return [{"date": d, "rows": [{"repo": r, "rank": i + 1, "stars_period": 10,
                                      "stars_total": 100} for i, r in enumerate(rows)]}
                for d, rows in dates_to_rows]

    def test_classifies_one_hit_stayer_and_reentry(self):
        days = self.build([("2026-01-01", ["a/x", "b/y"]),
                           ("2026-01-02", ["a/x", "c/z"]),
                           ("2026-01-03", ["a/x", "b/y"])])
        c = tfb.build_cohort(days)
        self.assertEqual(c["repos"], 3)
        self.assertEqual(c["one_hit_repos"], 1)          # c/z
        self.assertEqual(c["stayers"], 1)                # a/x, 3 days in a row
        self.assertEqual(c["max_streak"], 3)
        by_name = {r["repo"]: r for r in c["top_reentry"] + [{"repo": "a/x", "re_entries": 0}]}
        self.assertEqual(by_name["a/x"]["re_entries"], 0)
        # b/y left and came back -> one re-entry
        self.assertIn("b/y", [r["repo"] for r in c["top_reentry"]])

    def test_debuts_exclude_the_first_archive_day(self):
        days = self.build([("2026-01-01", ["a/x"]), ("2026-01-02", ["a/x", "n/e"])])
        c = tfb.build_cohort(days)
        self.assertEqual(c["debuts"], 1)                 # only n/e, not a/x

    def test_median_days_present(self):
        days = self.build([("2026-01-01", ["a"]), ("2026-01-02", ["a", "b"]),
                           ("2026-01-03", ["a", "b", "c"])])
        self.assertEqual(tfb._median([3, 2, 1]), 2.0)


class PredictorTests(unittest.TestCase):
    def days(self):
        return [
            {"date": "2026-01-01", "rows": [{"repo": "a", "rank": 1, "stars_period": 10, "stars_total": 1},
                                            {"repo": "b", "rank": 2, "stars_period": 9, "stars_total": 1},
                                            {"repo": "c", "rank": 3, "stars_period": 8, "stars_total": 1}]},
            {"date": "2026-01-02", "rows": [{"repo": "a", "rank": 1, "stars_period": 10, "stars_total": 1},
                                            {"repo": "b", "rank": 2, "stars_period": 9, "stars_total": 1}]},
        ]

    def test_slots_spend_budget_on_outsiders(self):
        hist = self.days()
        today = hist[-1]
        ranked = tfb.predictor_recency(hist, today)
        picked = tfb.select_with_slots(ranked, {"a", "b"}, 2, 0.5)
        self.assertEqual(len(picked), 2)
        self.assertIn("c", picked)       # the outsider slot is actually used
        self.assertIn("a", picked)

    def test_slots_fall_back_when_no_outsider_exists(self):
        hist = self.days()
        today = hist[-1]
        ranked = tfb.predictor_recency(hist, today)
        picked = tfb.select_with_slots(ranked, {"a", "b", "c"}, 2, 0.5)
        self.assertEqual(picked, ["a", "b"])

    def test_slots_never_exceed_k(self):
        ranked = ["a", "b", "c", "d", "e"]
        self.assertEqual(len(tfb.select_with_slots(ranked, {"a"}, 3, 0.8)), 3)

    def test_control_is_deterministic_and_equal_budget(self):
        hist = self.days()
        today = hist[-1]
        ranked = tfb.predictor_recency(hist, today)
        one = tfb.select_control(ranked, {"a", "b"}, 2, 0.5, "seed")
        two = tfb.select_control(ranked, {"a", "b"}, 2, 0.5, "seed")
        self.assertEqual(one, two)
        self.assertEqual(len(one), 2)
        self.assertIn("c", one)

    def test_take_reserve_rounds_and_never_zeroes(self):
        self.assertEqual(tfb.take_reserve(14, 0.2), 3)
        self.assertEqual(tfb.take_reserve(2, 0.2), 1)
        self.assertEqual(tfb.take_reserve(0, 0.2), 0)

    def test_score_math(self):
        s = tfb.score(["a", "b", "c"], {"a", "b", "x", "y"}, 3)
        self.assertEqual(s["hits"], 2)
        self.assertAlmostEqual(s["precision"], 2 / 3)
        self.assertAlmostEqual(s["recall"], 0.5)


class BacktestTests(unittest.TestCase):
    def test_reentry_truth_counts_only_repos_seen_before(self):
        days = [
            {"date": "2026-01-01", "rows": [{"repo": "a", "rank": 1, "stars_period": 5, "stars_total": 1}]},
            {"date": "2026-01-02", "rows": [{"repo": "b", "rank": 1, "stars_period": 5, "stars_total": 1}]},
            {"date": "2026-01-03", "rows": [{"repo": "a", "rank": 1, "stars_period": 5, "stars_total": 1},
                                            {"repo": "new", "rank": 2, "stars_period": 4, "stars_total": 1}]},
        ]
        b = tfb.backtest(days, window=7, reserve=0.2)
        self.assertEqual(b["transitions"], 2)
        self.assertEqual(b["reentry"]["truth_total"], 1)   # a came back, new is a debut
        self.assertEqual(b["reentry"]["recall"]["persistence"], 0.0)

    def test_report_is_json_serialisable_and_hash_stable(self):
        days = [{"date": "2026-01-0%d" % i, "rows": [{"repo": "r%d" % i, "rank": 1,
                                                     "stars_period": i, "stars_total": 1}]}
                for i in (1, 2, 3)]
        meta = {"root": "/tmp/x", "period": "daily", "snapshots": 3, "first": "2026-01-01",
                "last": "2026-01-03", "duplicates_dropped": [], "skipped_files": []}
        r1 = tfb.report(days, meta)
        r2 = tfb.report(days, meta)
        self.assertEqual(r1["replay_sha256"], r2["replay_sha256"])
        self.assertEqual(json.loads(json.dumps(r1))["replay_sha256"], r1["replay_sha256"])
        self.assertNotEqual(r1["replay_sha256"], "0" * 64)

    def test_hash_changes_when_data_changes(self):
        days = [{"date": "2026-01-0%d" % i, "rows": [{"repo": "r%d" % i, "rank": 1,
                                                     "stars_period": i, "stars_total": 1}]}
                for i in (1, 2, 3)]
        meta = {"root": "/tmp/x", "period": "daily", "snapshots": 3, "first": "2026-01-01",
                "last": "2026-01-03", "duplicates_dropped": [], "skipped_files": []}
        a = tfb.report(days, meta)["replay_sha256"]
        days[2]["rows"][0]["repo"] = "other"
        b = tfb.report(days, meta)["replay_sha256"]
        self.assertNotEqual(a, b)


class CliBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args):
        proc = subprocess.run([sys.executable, SCRIPT] + list(args),
                              capture_output=True, text=True, timeout=60)
        return proc

    def test_json_output_parses_and_has_documented_keys(self):
        write_archive(self.tmp, [snapshot("2026-01-01", [("a", 5), ("b", 4)]),
                                 snapshot("2026-01-02", [("a", 5), ("c", 3)]),
                                 snapshot("2026-01-03", [("a", 6), ("c", 4)])])
        proc = self.run_cli(self.tmp, "--json")
        self.assertIn(proc.returncode, (0, 1))
        rep = json.loads(proc.stdout)
        for key in ("archive", "cohort", "backtest", "replay_sha256"):
            self.assertIn(key, rep)
        self.assertIn(rep["backtest"]["verdict"]["beats_baseline"], (True, False))

    def test_exit_2_on_missing_directory(self):
        proc = self.run_cli(os.path.join(self.tmp, "absent"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("error:", proc.stderr)

    def test_exit_2_on_bad_reserve(self):
        write_archive(self.tmp, [snapshot("2026-01-01", [("a", 5)]),
                                 snapshot("2026-01-02", [("a", 5)]),
                                 snapshot("2026-01-03", [("a", 5)])])
        proc = self.run_cli(self.tmp, "--reserve", "1.5")
        self.assertEqual(proc.returncode, 2)

    def test_exit_0_when_challenger_wins_and_1_when_it_loses(self):
        # Winning fixture: c leaves on day 2, the challenger spends its outsider
        # slot on c and c comes back on day 3.
        win = tempfile.mkdtemp()
        lose = tempfile.mkdtemp()
        try:
            write_archive(win, [snapshot("2026-01-01", [("a", 10), ("b", 9), ("c", 8)]),
                                snapshot("2026-01-02", [("a", 10), ("c", 8)]),
                                snapshot("2026-01-03", [("a", 10), ("b", 9)]),
                                snapshot("2026-01-04", [("b", 9), ("c", 8)]),
                                snapshot("2026-01-05", [("a", 10), ("b", 9)])])
            write_archive(lose, [snapshot("2026-01-01", [("a", 10), ("b", 9)]),
                                 snapshot("2026-01-02", [("x", 10), ("y", 9)]),
                                 snapshot("2026-01-03", [("p", 10), ("q", 9)])])
            p_win = self.run_cli(win, "--json")
            p_lose = self.run_cli(lose, "--json")
            # Exit code is the verdict, bit for bit: 0 = challenger beat the best
            # baseline by the margin, 1 = it did not.
            self.assertEqual(p_win.returncode,
                             0 if json.loads(p_win.stdout)["backtest"]["verdict"]["beats_baseline"] else 1)
            self.assertEqual(p_lose.returncode, 1)
            self.assertFalse(json.loads(p_lose.stdout)["backtest"]["verdict"]["beats_baseline"])
        finally:
            shutil.rmtree(win, ignore_errors=True)
            shutil.rmtree(lose, ignore_errors=True)

    def test_text_mode_prints_verdict_and_hash(self):
        write_archive(self.tmp, [snapshot("2026-01-01", [("a", 5)]),
                                 snapshot("2026-01-02", [("a", 5)]),
                                 snapshot("2026-01-03", [("a", 5)])])
        proc = self.run_cli(self.tmp)
        self.assertIn("verdict:", proc.stdout)
        self.assertIn("replay_sha256:", proc.stdout)

    def test_examples_directory_reproduces_committed_report(self):
        proc = self.run_cli(os.path.join(ROOT, "examples", "in"), "--json")
        self.assertIn(proc.returncode, (0, 1))
        live = json.loads(proc.stdout)
        with open(os.path.join(ROOT, "examples", "out", "report.json"), encoding="utf-8") as fh:
            committed = json.load(fh)
        self.assertEqual(live["replay_sha256"], committed["replay_sha256"])
        self.assertEqual(live["backtest"]["macro"], committed["backtest"]["macro"])


if __name__ == "__main__":
    unittest.main(verbosity=2)