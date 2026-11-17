# Teardown

Everything lives in one resource group, so removing the project is one command.

## Pause it (keep everything, stop the daily runs)

ADF Studio → **Manage** → **Triggers** → `tr_refresh` → **Stop** → **Publish all**, or:

```
az datafactory trigger stop --resource-group rg-air-quality-pipeline --factory-name <factory name> --name tr_refresh
```

The public dashboard keeps showing the last snapshot. The SQL database auto-pauses when idle and is on the free
offer, so it costs nothing while stopped. Don't leave a stopped factory around for months; idle pipelines still
carry a small monthly charge.

## Delete it (for good)

```
az group delete --name rg-air-quality-pipeline --yes
```

This deletes the storage account (landing, publish and model files), the function app, the SQL server and database,
Data Factory, and the monitoring resources. It can't be undone. Then:

1. GitHub repo → **Settings** → **Secrets and variables** → **Actions** → delete `PUBLISHED_DATA_SAS_URL`.
2. Actions → **pages** → **⋯** → **Disable workflow** if you don't want the daily job to keep trying.
3. Cost Management: check the next day that nothing is still billing.

The local setup (Docker SQL Server) is separate: `docker compose down -v` removes the container and its volume.
