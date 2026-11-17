"""
run.py

The local runner. It mirrors the ADF pipeline step for step so the whole thing can run on a laptop
with SQL Server in Docker. `--plan-only` just prints what a run would do.
"""

import argparse
import json
import sys
from collections import defaultdict

from pipeline import config, plan


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


def main(argv=None):
    parser = argparse.ArgumentParser(description="run the air quality pipeline locally")
    parser.add_argument("--mode", choices=["incremental", "full"], default="incremental")
    parser.add_argument("--plan-only", action="store_true", help="print the work-item plan and stop")
    parser.add_argument("--json", help="also write the plan to this file")
    args = parser.parse_args(argv)

    p = plan.build_plan(args.mode)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(p, fh, indent=2)
    if args.plan_only:
        print_plan(p)
        return 0
    print("full runs arrive in m3; use --plan-only for now")
    return 1


if __name__ == "__main__":
    sys.exit(main())
