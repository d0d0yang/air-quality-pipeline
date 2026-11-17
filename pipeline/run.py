"""
run.py

The local runner. It mirrors the ADF pipeline step for step so the whole thing can run on a laptop
with SQL Server in Docker: start run, plan, extract -> load -> merge per item, link, match, mart, dq, end run.
`--plan-only` just prints what a run would do.
"""

import argparse
import json
import sys
import time
import traceback
from collections import defaultdict

from pipeline import config, db, extract, plan, publish, sources, storage
from pipeline import watermark as wm

# station metadata goes first so measurement merges can find their stations (adf runs these in an earlier loop)
METADATA_SOURCES = ("naps_stations", "aqhi_stations", "climate_stations", "openaq_locations")


def print_plan(p):
    print(f"mode: {p['mode']}    window starts: {p['window_start_utc']}    generated: {p['generated_utc']}")
    print(f"naps hourly ends with {p['naps_last_year']}; openaq takes over from {p['handover_utc']}")
    print(f"region bbox: {','.join(str(x) for x in config.REGION_BBOX)}    pollutants: {','.join(config.POLLUTANTS)}")
    print()
    print("stations per source")
    for name, count in p["stations"].items():
        print(f"  {name:28} {count:>6}")
    print()
    st = p["state"]
    print(f"watermarks: {'from db' if st['available'] else 'none (cold start)'}, "
          f"{st['sources']} source keys, {st['sensors']} sensors")
    print()
    by_source = defaultdict(lambda: [0, 0])
    for item in p["items"]:
        by_source[item["source"]][0] += 1
        by_source[item["source"]][1] += item["est_requests"]
    print(f"{'work items':28} {'items':>6} {'est. requests':>14}")
    for source_id, (n, req) in by_source.items():
        flag = "  (sequential)" if source_id in p["sequential_sources"] else ""
        print(f"  {source_id:26} {n:>6} {req:>14}{flag}")
    print(f"  {'total':26} {len(p['items']):>6}")
    print()
    est = p["estimates"]
    print(f"openaq: ~{est['openaq_requests']} requests, ~{est['openaq_minutes']} min at {config.OPENAQ_RPM}/min "
          f"(and under the 2,000/hour cap)")
    print(f"open-meteo: ~{est['openmeteo_weighted_calls']} weighted calls "
          f"(fair use is about {est['openmeteo_fair_use_per_day']:,}/day)")
    for note in p["notes"]:
        print(f"note: {note}")


def ordered(items):
    meta = [i for i in items if i["source"] in METADATA_SOURCES]
    rest = [i for i in items if i["source"] not in METADATA_SOURCES]
    return meta + rest


def run_item(conn, run_id, item):
    """extract -> bulk load into stg -> merge -> watermarks. The adf ForEach body, in python."""
    src = sources.get(item["source"])
    result = extract.extract(run_id, item)
    db.call(conn, "etl.usp_log_stage", run_id=run_id, stage="extract", source_id=item["source"],
            item_id=item["item_id"], seconds=result["seconds"], rows_read=result["rows_read"],
            rows_written=result["rows_written"],
            message=json.dumps({k: v for k, v in result["stats"].items() if k != "rows_read"})[:4000])
    loaded = db.bulk_load(conn, src.stg_table, result["files"][0], item["item_id"])
    merged = db.call(conn, src.merge_proc, run_id=run_id, item_id=item["item_id"])
    # watermarks move only after the merge worked, so a failed item is simply retried next run
    db.call(conn, "etl.usp_apply_watermarks", run_id=run_id, json=json.dumps(result["new_watermark"]))
    counts = merged[0] if merged else {}
    return result, loaded, counts


def limit_items(items, only=None, max_per_source=None):
    """Dev helper: keep some sources and/or the first n items of each. Never used by benchmarks."""
    kept, seen = [], defaultdict(int)
    for item in items:
        if only and item["source"] not in only:
            continue
        if max_per_source and seen[item["source"]] >= max_per_source:
            continue
        seen[item["source"]] += 1
        kept.append(item)
    return kept


def model_step(conn, run_id, retrain=None):
    """Retrain on mondays (local time) or when there's no model yet, then predict tomorrow. /api/model does the same."""
    from model import predict, train

    t_step = time.monotonic()
    if retrain is None:
        retrain = predict.today_local().weekday() == 0 or not storage.get_storage().exists("models/latest.json")
    message = {}
    if retrain:
        metrics = train.run(write=True)
        message["retrained"] = metrics["model_version"]
    message.update(predict.run(run_id=run_id))
    db.call(conn, "etl.usp_log_stage", run_id=run_id, stage="model", seconds=round(time.monotonic() - t_step, 3),
            rows_written=message.get("forecasts"), message=json.dumps(message))
    print(f"model ({'retrained, ' if retrain else ''}{message.get('forecasts')} forecasts, "
          f"{time.monotonic() - t_step:.1f}s)")


def run_pipeline(mode, trigger="manual", target="local", only=None, max_per_source=None):
    started = time.monotonic()
    with db.connect() as conn:
        run_id = db.call(conn, "etl.usp_start_run", mode=mode, target=target, trigger_name=trigger)[0]["run_id"]
        print(f"run {run_id} started ({mode})")
        try:
            t0 = time.monotonic()
            p = plan.build_plan(mode, state=wm.load_state())
            items = ordered(limit_items(p["items"], only, max_per_source))
            db.call(conn, "etl.usp_set_items_planned", run_id=run_id, items=len(items))
            db.call(conn, "etl.usp_log_stage", run_id=run_id, stage="plan", seconds=round(time.monotonic() - t0, 3),
                    rows_written=len(items), message=json.dumps(p["estimates"]))
            print(f"planned {len(items)} items")

            failures = []
            for n, item in enumerate(items, 1):
                t_item = time.monotonic()
                try:
                    result, loaded, counts = run_item(conn, run_id, item)
                    elapsed = time.monotonic() - started
                    print(f"[{n}/{len(items)}] {item['item_id']:45} read {result['rows_read']:>8} "
                          f"loaded {loaded:>8} +{counts.get('rows_inserted', 0)} ~{counts.get('rows_updated', 0)} "
                          f"({time.monotonic() - t_item:.1f}s, {elapsed / 60:.1f} min total)", flush=True)
                except Exception as exc:  # noqa: BLE001 - one bad item shouldn't stop the others, like adf's ForEach
                    failures.append(f"{item['item_id']}: {type(exc).__name__}: {exc}")
                    db.call(conn, "etl.usp_log_stage", run_id=run_id, stage="extract", source_id=item["source"],
                            item_id=item["item_id"], status="failed", seconds=round(time.monotonic() - t_item, 3),
                            message=str(exc)[:4000])
                    print(f"[{n}/{len(items)}] {item['item_id']} FAILED: {exc}", flush=True)
            if failures:
                raise RuntimeError(f"{len(failures)} item(s) failed; first: {failures[0]}")

            for label, proc, params in (
                ("link stations", "core.usp_link_stations", {}),
                ("match weather", "core.usp_match_stations", {"max_match_km": config.MAX_MATCH_KM}),
                ("refresh mart", "mart.usp_refresh", {}),
                ("dq checks", "etl.usp_run_dq_checks", {"max_match_km": config.MAX_MATCH_KM}),
            ):
                t_step = time.monotonic()
                db.call(conn, proc, run_id=run_id, **params)
                print(f"{label} ({time.monotonic() - t_step:.1f}s)")

            model_step(conn, run_id)
            t_step = time.monotonic()
            written = publish.run(run_id)
            db.call(conn, "etl.usp_log_stage", run_id=run_id, stage="publish",
                    seconds=round(time.monotonic() - t_step, 3), rows_written=len(written),
                    message=json.dumps(written)[:4000])
            print(f"publish ({time.monotonic() - t_step:.1f}s)")

            db.call(conn, "etl.usp_end_run", run_id=run_id, status="succeeded")
            print(f"run {run_id} succeeded in {(time.monotonic() - started) / 60:.1f} min")
            return run_id
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            db.call(conn, "etl.usp_end_run", run_id=run_id, status="failed", error_message=message[:4000])
            print(f"run {run_id} failed: {message}")
            traceback.print_exc()
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="run the air quality pipeline locally")
    parser.add_argument("--mode", choices=["incremental", "full"], default="incremental")
    parser.add_argument("--plan-only", action="store_true", help="print the work-item plan and stop")
    parser.add_argument("--json", help="also write the plan to this file")
    parser.add_argument("--trigger", default="manual", help="label stored with the run (benchmarks set this)")
    parser.add_argument("--only", help="dev: comma-separated source ids to run")
    parser.add_argument("--max-items-per-source", type=int, help="dev: cap items per source")
    args = parser.parse_args(argv)

    if args.plan_only:
        p = plan.build_plan(args.mode)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(p, fh, indent=2)
        print_plan(p)
        return 0
    try:
        only = set(args.only.split(",")) if args.only else None
        trigger = args.trigger if not (only or args.max_items_per_source) else f"{args.trigger} (dev subset)"
        run_pipeline(args.mode, trigger=trigger, only=only, max_per_source=args.max_items_per_source)
    except Exception:  # noqa: BLE001 - already logged to etl.pipeline_run
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
