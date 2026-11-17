"""
deploy.py

One command from an empty resource group to a scheduled pipeline: bicep (twice), sql schema + etl_app, the
function code, the model files, the adf trigger, and a read-only SAS url for the dashboard. Uses the az cli you're
logged into; secrets come from .env and never get printed.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import config, db  # noqa: E402 - needs the repo root on the path first

AZ = shutil.which("az") or r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
TEMPLATE = ROOT / "infra" / "main.bicep"
SAS_FILE = ROOT / "data" / "publish_sas_url.txt"  # data/ is gitignored


def say(message):
    print(f"\n==> {message}", flush=True)


def az(*args, capture=True):
    cmd = [AZ, *args] + (["-o", "json"] if capture else [])
    out = subprocess.run(cmd, capture_output=capture, text=True)
    if out.returncode != 0:
        # stderr from az never contains our secrets: they only travel in the temp parameters file
        raise SystemExit(f"az {' '.join(args[:3])} failed:\n{(out.stderr or '')[-2000:]}")
    return json.loads(out.stdout) if capture and out.stdout.strip() else None


def need(name):
    value = config._get(name)
    if not value:
        raise SystemExit(f"set {name} in .env first")
    return value


def deployment(group, params, what_if=False):
    """Runs the bicep with secure params passed through a temp file that's deleted right after."""
    body = {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
            "contentVersion": "1.0.0.0", "parameters": {k: {"value": v} for k, v in params.items()}}
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(body, fh)
        if what_if:
            az("deployment", "group", "what-if", "--resource-group", group, "--template-file", str(TEMPLATE),
               "--parameters", f"@{path}", capture=False)
            return None
        result = az("deployment", "group", "create", "--resource-group", group, "--name", "air-quality-pipeline",
                    "--template-file", str(TEMPLATE), "--parameters", f"@{path}")
        return {k: v["value"] for k, v in result["properties"]["outputs"].items()}
    finally:
        os.remove(path)


def package_function():
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_function.py")], check=True)
    out = Path(tempfile.gettempdir()) / "air-quality-function.zip"
    skip = {".venv", "__pycache__", ".python_packages", "local.settings.json"}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in (ROOT / "function_app").rglob("*"):
            rel = path.relative_to(ROOT / "function_app")
            if path.is_file() and not (set(rel.parts) & skip):
                zf.write(path, rel.as_posix())
    return out


def upload_models(connection_string):
    """The locally trained and evaluated model, so the first cloud run can forecast before its first refit."""
    from azure.storage.blob import BlobServiceClient

    local = config.LOCAL_DATA_DIR / "models"
    version = json.loads((local / "latest.json").read_text())["version"]
    container = BlobServiceClient.from_connection_string(connection_string).get_container_client("models")
    for path in [local / "latest.json", local / "metrics.json", *sorted((local / version).glob("*"))]:
        name = path.relative_to(local).as_posix()
        container.upload_blob(name, path.read_bytes(), overwrite=True)
        print(f"   uploaded models/{name}")


def main():
    group = config._get("AZ_RESOURCE_GROUP", "rg-air-quality-pipeline")
    location = config._get("AZ_LOCATION", "canadacentral")
    admin_user = config._get("AZ_SQL_ADMIN_USER", "aqadmin")
    admin_pw, etl_pw, openaq_key = need("AZ_SQL_ADMIN_PASSWORD"), need("AZ_SQL_ETL_PASSWORD"), need("OPENAQ_API_KEY")

    account = az("account", "show")
    say(f"subscription: {account['name']} ({account['id'][:8]}...)")
    if config._get("AZ_SUBSCRIPTION_ID"):
        az("account", "set", "--subscription", config._get("AZ_SUBSCRIPTION_ID"), capture=False)

    say(f"checking {location} offers flex consumption")
    flex = [r["name"] for r in az("functionapp", "list-flexconsumption-locations")]
    if location.replace(" ", "").lower() not in [f.replace(" ", "").lower() for f in flex]:
        raise SystemExit(f"flex consumption isn't offered in {location}; set AZ_LOCATION to one of: {', '.join(flex)}")

    deployer_ip = urllib.request.urlopen("https://api.ipify.org", timeout=20).read().decode().strip()
    params = {"location": location, "sqlAdminLogin": admin_user, "sqlAdminPassword": admin_pw,
              "sqlEtlPassword": etl_pw, "openaqApiKey": openaq_key, "deployerIp": deployer_ip,
              "refreshSchedule": config.REFRESH_SCHEDULE}

    say(f"creating resource group {group} in {location} (if it doesn't exist)")
    az("group", "create", "--name", group, "--location", location)

    say("what-if: what the bicep will create or change")
    deployment(group, params, what_if=True)
    say("creating: storage account (+5 containers), log analytics + app insights, flex consumption plan + "
        "function app, sql server + free-offer database + 2 firewall rules, data factory + linked services + datasets")
    out = deployment(group, params)

    say(f"applying sql/ to {out['databaseName']} and creating etl_app")
    os.environ["SQL_ADMIN_CONN_STR"] = (
        "Driver={ODBC Driver 18 for SQL Server};Server=tcp:" + out["sqlServerHost"] + ",1433;"
        f"Database={out['databaseName']};Uid={admin_user};Pwd={{AZ_SQL_ADMIN_PASSWORD}};Encrypt=yes;"
        "TrustServerCertificate=no;Connection Timeout=120")
    db.apply(etl_password=etl_pw)

    say(f"deploying function code to {out['functionAppName']} (remote build)")
    package = package_function()
    az("functionapp", "deployment", "source", "config-zip", "--resource-group", group, "--name",
       out["functionAppName"], "--src", str(package), "--build-remote", "true", capture=False)

    say("uploading the trained model to blob models/")
    keys = az("storage", "account", "keys", "list", "--account-name", out["storageAccount"], "--resource-group", group)
    conn = (f"DefaultEndpointsProtocol=https;AccountName={out['storageAccount']};AccountKey={keys[0]['value']};"
            "EndpointSuffix=core.windows.net")
    upload_models(conn)

    say("creating: adf linked service to the function, both pipelines and the tr_refresh trigger")
    function_key = None
    for _ in range(20):  # a fresh host takes a minute or two to create its keys
        keys_out = az("functionapp", "keys", "list", "--resource-group", group, "--name", out["functionAppName"])
        function_key = (keys_out.get("functionKeys") or {}).get("default")
        if function_key:
            break
        time.sleep(15)
    if not function_key:
        raise SystemExit("the function app has no default key yet; rerun deploy.py in a few minutes")
    out = deployment(group, {**params, "functionKey": function_key})

    say("starting the daily trigger")
    az("extension", "add", "--name", "datafactory", "--upgrade", "--yes", capture=False)
    az("datafactory", "trigger", "start", "--resource-group", group, "--factory-name", out["factoryName"],
       "--name", "tr_refresh", capture=False)

    expiry = (datetime.now(UTC) + timedelta(days=365)).strftime("%Y-%m-%dT%H:%MZ")
    sas = az("storage", "container", "generate-sas", "--account-name", out["storageAccount"], "--name", "publish",
             "--permissions", "rl", "--expiry", expiry, "--https-only", "--account-key", keys[0]["value"])
    say("done")
    print(f"   resource group : {group}")
    print(f"   data factory   : {out['factoryName']}")
    print(f"   function app   : {out['functionAppName']}")
    print(f"   sql database   : {out['sqlServerName']}/{out['databaseName']}")
    # written to a gitignored file instead of the terminal, so it never ends up in logs or chat transcripts
    SAS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SAS_FILE.write_text(f"https://{out['storageAccount']}.blob.core.windows.net/publish?{sas}\n", encoding="utf-8")
    print(f"\nread-only SAS url for the dashboard (12 months) saved to {SAS_FILE.relative_to(ROOT)}")
    print("put it in the PUBLISHED_DATA_SAS_URL repo secret, then delete the file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
