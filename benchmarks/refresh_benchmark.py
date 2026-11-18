"""
refresh_benchmark.py

Times full vs incremental refreshes, end to end (etl.pipeline_run started_at -> ended_at). Scenarios:
full, incremental_noop (straight after another run) and incremental_daily (watermarks rewound 24 h, so a
real day is re-pulled and merged). Runs accumulate in results.json across days; medians use all of them.
"""

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta

from pipeline import config, db
from pipeline.run import run_pipeline

RESULTS = config.ROOT / "benchmarks" / "results.json"
FRONTEND = config.ROOT / "frontend" / "data" / "benchmark.json"
SCENARIOS = ("full", "incremental_noop", "incremental_daily")
# a full backfill costs ~6,600 open-meteo weighted calls (fair use ~10,000/day), so one full run per day
MIN_HOURS_BETWEEN_FULL = 20


def load_results():
    if RESULTS.exists():
        return json.loads(RESULTS.read_text(encoding="utf-8"))
    return {"runs": []}


def summarize(results):
    targets = {}
    for r in results["runs"]:
        if r.get("status") != "succeeded":
            continue  # failed runs are kept in the file for the record but never timed
        t = targets.setdefault(r["target"], {"scenarios": {}})
        t["scenarios"].setdefault(r["scenario"], {"runs": []})["runs"].append(r["duration_s"])
    for t in targets.values():
        for s in t["scenarios"].values():
            s["median_s"] = statistics.median(s["runs"])
        sc = t["scenarios"]
        if "full" in sc and "incremental_daily" in sc:
            t["reduction_pct"] = round(100 * (1 - sc["incremental_daily"]["median_s"] / sc["full"]["median_s"]), 1)
        else:
            t["reduction_pct"] = None
    results["targets"] = targets
    results["generated_utc"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    results["definition"] = ("wall-clock duration of the whole pipeline run (etl.pipeline_run started_at -> ended_at); "
                             "median of all recorded runs per scenario; reduction = 1 - median(incremental_daily) / "
                             "median(full)")
    return results


def save(results):
    text = json.dumps(summarize(results), indent=2)
    RESULTS.write_text(text, encoding="utf-8")
    FRONTEND.parent.mkdir(parents=True, exist_ok=True)
    FRONTEND.write_text(text, encoding="utf-8")


def run_row(run_id, conn=None):
    sql = ("SELECT run_id, status, duration_s, rows_read, rows_inserted, rows_updated, started_at "
           "FROM etl.pipeline_run WHERE run_id = ?")
    own = conn is None
    conn = conn or db.connect()
    try:
        r = conn.cursor().execute(sql, run_id).fetchone()
    finally:
        if own:
            conn.close()
    return {"run_id": r[0], "status": r[1], "duration_s": float(r[2]), "rows_read": r[3], "rows_inserted": r[4],
            "rows_updated": r[5], "started_utc": r[6].strftime("%Y-%m-%dT%H:%M:%SZ")}


def last_full(results, target):
    """Most recent full run for this target, from results.json or (any trigger) from etl.pipeline_run."""
    fulls = [datetime.fromisoformat(r["started_utc"].replace("Z", "+00:00"))
             for r in results["runs"] if r["target"] == target and r["scenario"] == "full"]
    with db.connect() as conn:
        row = conn.cursor().execute("SELECT MAX(started_at) FROM etl.pipeline_run WHERE mode = 'full' AND target = ? "
                                    "AND status <> 'failed'", target).fetchone()
    if row and row[0]:
        fulls.append(row[0].replace(tzinfo=UTC))
    return max(fulls, default=None)


# --- targets ------------------------------------------------------------------------------------


def local_run(scenario):
    if scenario == "incremental_daily":
        with db.connect() as conn:
            db.call(conn, "etl.usp_prepare_benchmark_day")
    mode = "full" if scenario == "full" else "incremental"
    run_id = run_pipeline(mode, trigger=f"benchmark {scenario}", target="local")
    return run_row(run_id)


# on windows az is a .cmd, which subprocess only finds by its full path
AZ = shutil.which("az") or r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"


def az(*args):
    out = subprocess.run([AZ, *args, "-o", "json"], capture_output=True, text=True, check=True)
    return json.loads(out.stdout) if out.stdout.strip() else None


def azure_run(scenario, factory, group):
    """Triggers pl_daily_refresh in adf and waits; the duration still comes from etl.pipeline_run in azure sql."""
    if scenario == "incremental_daily":
        with db.connect() as conn:
            db.call(conn, "etl.usp_prepare_benchmark_day")
    mode = "full" if scenario == "full" else "incremental"
    with db.connect() as conn:
        before = conn.cursor().execute("SELECT ISNULL(MAX(run_id), 0) FROM etl.pipeline_run").fetchone()[0]
    # parameters go through a file: cmd.exe strips the quotes out of inline json
    fd, params = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"mode": mode}, fh)
    try:
        adf_run = az("datafactory", "pipeline", "create-run", "--factory-name", factory, "--resource-group", group,
                     "--name", "pl_daily_refresh", "--parameters", f"@{params}")["runId"]
    finally:
        os.remove(params)
    while True:
        time.sleep(30)
        status = az("datafactory", "pipeline-run", "show", "--factory-name", factory, "--resource-group", group,
                    "--run-id", adf_run)["status"]
        if status in ("Succeeded", "Failed", "Cancelled"):
            break
    with db.connect() as conn:
        cur = conn.cursor().execute("SELECT MIN(run_id) FROM etl.pipeline_run WHERE run_id > ?", before)
        run_id = cur.fetchone()[0]
    row = run_row(run_id)
    row["adf_run_id"], row["adf_status"] = adf_run, status
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description="time full vs incremental refreshes")
    parser.add_argument("--target", choices=["local", "azure"], default="local")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--scenarios", default=",".join(SCENARIOS),
                        help="comma-separated subset; full runs are limited to one per day per target")
    parser.add_argument("--factory", help="adf factory name (azure target)")
    parser.add_argument("--resource-group", default=config._get("AZ_RESOURCE_GROUP"))
    parser.add_argument("--summary-only", action="store_true", help="just rebuild the summary from results.json")
    args = parser.parse_args(argv)

    results = load_results()
    if args.summary_only:
        save(results)
        print(json.dumps(results["targets"], indent=2))
        return 0
    if config.DEV_CACHE:
        print("refusing to benchmark with DEV_CACHE=1: cached downloads would make full runs look faster than they are")
        return 2
    scenarios = [s for s in args.scenarios.split(",") if s]

    for i in range(args.repeats):
        for scenario in scenarios:
            if scenario == "full":
                last = last_full(results, args.target)
                if last and datetime.now(UTC) - last < timedelta(hours=MIN_HOURS_BETWEEN_FULL):
                    print(f"skipping full run: the last one started {last:%Y-%m-%d %H:%M} UTC "
                          "(one per day, for open-meteo fair use)")
                    continue
            print(f"--- repeat {i + 1}/{args.repeats}: {scenario} ({args.target})")
            row = local_run(scenario) if args.target == "local" else azure_run(scenario, args.factory,
                                                                              args.resource_group)
            row.update(target=args.target, scenario=scenario)
            results["runs"].append(row)
            save(results)  # after every run, so a crash never loses earlier timings
            print(f"{scenario}: {row['duration_s']:.1f} s (run {row['run_id']}, {row['status']})")

    summary = summarize(results)["targets"].get(args.target, {})
    for name, s in summary.get("scenarios", {}).items():
        print(f"{name:20} median {s['median_s']:8.1f} s over {len(s['runs'])} runs")
    if summary.get("reduction_pct") is not None:
        print(f"refresh time reduction (1 - incremental_daily / full): {summary['reduction_pct']}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
