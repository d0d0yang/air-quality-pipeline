"""
function_app.py

The HTTP endpoints Data Factory calls: plan, extract, model, publish and health. Each one is a thin wrapper around
the same pipeline/ and model/ code run.py uses locally, and logs what it did to etl.stage_run.
"""

import json
import logging
import time

import azure.functions as func

from pipeline import db, extract, plan, publish, sources
from pipeline import watermark as wm

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

# station metadata first, then rate-limited sources one at a time, everything else in parallel
METADATA_SOURCES = {"naps_stations", "aqhi_stations", "climate_stations", "openaq_locations"}


def ok(payload, status=200):
    return func.HttpResponse(json.dumps(payload, default=str), status_code=status, mimetype="application/json")


def fail(exc, status=500):
    logging.exception("request failed")
    return ok({"error": f"{type(exc).__name__}: {exc}"[:2000]}, status)


def body(req):
    try:
        return req.get_json()
    except ValueError:
        return {}


def with_targets(item):
    # adf needs to know which stg table to copy into and which proc merges it
    src = sources.get(item["source"])
    return {**item, "stg_table": src.stg_table.split(".")[1], "merge_proc": src.merge_proc}


@app.route(route="plan", methods=["POST"])
def plan_endpoint(req: func.HttpRequest) -> func.HttpResponse:
    """{run_id, mode} -> work items split into the three loops adf runs."""
    try:
        args = body(req)
        started = time.monotonic()
        p = plan.build_plan(args.get("mode", "incremental"), state=wm.load_state())
        items = [with_targets(i) for i in p["items"]]
        sequential = set(p["sequential_sources"])
        result = {
            "items_meta": [i for i in items if i["source"] in METADATA_SOURCES],
            "items_sequential": [i for i in items if i["source"] not in METADATA_SOURCES and i["source"] in sequential],
            "items_parallel": [i for i in items
                               if i["source"] not in METADATA_SOURCES and i["source"] not in sequential],
            "sequential_sources": p["sequential_sources"],
            "estimates": p["estimates"],
        }
        with db.connect() as conn:
            db.call(conn, "etl.usp_set_items_planned", run_id=args["run_id"], items=len(items))
            db.call(conn, "etl.usp_log_stage", run_id=args["run_id"], stage="plan",
                    seconds=round(time.monotonic() - started, 3), rows_written=len(items),
                    message=json.dumps(p["estimates"]))
        return ok(result)
    except Exception as exc:  # noqa: BLE001 - adf gets a readable error, the run log gets it too
        return fail(exc)


@app.route(route="extract", methods=["POST"])
def extract_endpoint(req: func.HttpRequest) -> func.HttpResponse:
    """{run_id, item} -> landing csv in blob plus the watermark to apply after the merge."""
    args = body(req)
    item = args.get("item") or {k: args.get(k) for k in ("source", "item_id", "params")}
    try:
        result = extract.extract(args["run_id"], item)
        with db.connect() as conn:
            db.call(conn, "etl.usp_log_stage", run_id=args["run_id"], stage="extract", source_id=item["source"],
                    item_id=item["item_id"], seconds=result["seconds"], rows_read=result["rows_read"],
                    rows_written=result["rows_written"],
                    message=json.dumps({k: v for k, v in result["stats"].items() if k != "rows_read"})[:4000])
        src = sources.get(item["source"])
        # adf passes new_watermark straight to etl.usp_apply_watermarks as a string
        return ok({**result, "new_watermark": json.dumps(result["new_watermark"]),
                   "stg_table": src.stg_table.split(".")[1], "merge_proc": src.merge_proc,
                   "landing_container": "landing", "landing_path": result["files"][0].split("/", 1)[1]})
    except Exception as exc:  # noqa: BLE001
        try:
            with db.connect() as conn:
                db.call(conn, "etl.usp_log_stage", run_id=args.get("run_id"), stage="extract",
                        source_id=item.get("source"), item_id=item.get("item_id"), status="failed",
                        message=str(exc)[:4000])
        except Exception:  # noqa: BLE001 - the original error matters more
            logging.exception("couldn't log the failed extract")
        return fail(exc)


@app.route(route="model", methods=["POST"])
def model_endpoint(req: func.HttpRequest) -> func.HttpResponse:
    """{run_id, retrain} -> retrains when asked (adf asks on mondays), then forecasts tomorrow."""
    from model import predict, train

    try:
        args = body(req)
        started = time.monotonic()
        summary = {}
        if args.get("retrain"):
            summary["retrained"] = train.refit()
        summary.update(predict.run(run_id=args.get("run_id")))
        with db.connect() as conn:
            db.call(conn, "etl.usp_log_stage", run_id=args.get("run_id"), stage="model",
                    seconds=round(time.monotonic() - started, 3), rows_written=summary.get("forecasts"),
                    message=json.dumps(summary))
        return ok(summary)
    except Exception as exc:  # noqa: BLE001
        return fail(exc)


@app.route(route="publish", methods=["POST"])
def publish_endpoint(req: func.HttpRequest) -> func.HttpResponse:
    """{run_id} -> the dashboard json in blob publish/."""
    try:
        args = body(req)
        started = time.monotonic()
        written = publish.run(args.get("run_id"), local_copy=False)
        with db.connect() as conn:
            db.call(conn, "etl.usp_log_stage", run_id=args.get("run_id"), stage="publish",
                    seconds=round(time.monotonic() - started, 3), rows_written=len(written),
                    message=json.dumps(written)[:4000])
        return ok({"files": written})
    except Exception as exc:  # noqa: BLE001
        return fail(exc)


@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    # lists the odbc drivers the image has: pyodbc needs "ODBC Driver 18 for SQL Server"
    try:
        import pyodbc

        drivers = pyodbc.drivers()
    except Exception as exc:  # noqa: BLE001 - this endpoint reports problems, it doesn't fail on them
        drivers = [f"pyodbc unavailable: {type(exc).__name__}"]
    return ok({"status": "ok", "odbc_drivers": drivers})
