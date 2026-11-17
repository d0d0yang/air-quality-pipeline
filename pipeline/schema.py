"""
schema.py

The keys each dashboard json file must have. Kept import-free so the pages workflow can check
downloaded files without installing the database driver.
"""

SCHEMA = {
    "summary.json": ["generated_utc", "last_run", "region", "sites", "regional", "attributions"],
    "stations.json": ["generated_utc", "hours_start_utc", "stations"],
    "forecast.json": ["generated_utc", "model_version", "metrics", "holdout", "spatial", "sites"],
    "pipeline.json": ["generated_utc", "runs", "freshness", "dq", "row_counts"],
}


def validate(name, payload):
    if name not in SCHEMA:
        raise ValueError(f"unexpected file {name}")
    missing = [k for k in SCHEMA[name] if k not in payload]
    if missing:
        raise ValueError(f"{name} is missing {missing}")
    return True
