# Cloud

What teams use day to day on AWS, Google Cloud and Azure, and where it lives in agentl.

| Need | AWS | Google Cloud | Azure |
| --- | --- | --- | --- |
| Secrets in the YAML | `${aws-sm:...}`, `${ssm:...}` | `${gcp-sm:...}` | `${azure-kv:...}` |
| Files | `s3://` | `gs://` | `az://`, SharePoint / OneDrive |
| Queues | `sqs.receive`, `sqs.send` | `pubsub.pull`, `pubsub.publish` | not built in (Service Bus) |
| Warehouses | `postgres.*` (Redshift), `sql.*` (Athena through its SQLAlchemy dialect) | `sql.*` with `[bigquery]` | `sql.*` with `[mssql]` (Synapse, Fabric SQL endpoint) |
| LLMs | `provider: bedrock` | Anthropic or OpenAI-compatible endpoints | `provider: azure-openai` |
| Run a pipeline | Lambda, ECS / Batch | Cloud Run jobs | Container Apps jobs |

## Secrets

Reference a secret where an `${ENV_VAR}` would go. It is read when the file loads, kept only in
memory, and never written to the run store, events or logs (unless a step outputs it and is cached).

```yaml
steps:
  - id: save
    use: postgres.write
    with:
      dsn: ${aws-sm:prod/pg_dsn}                 # AWS Secrets Manager
      # dsn: ${aws-sm:prod/db#dsn}               # one key of a JSON secret
      # dsn: ${ssm:/prod/pg_dsn}                 # SSM Parameter Store, decrypted
      # dsn: ${gcp-sm:my-project/pg_dsn}         # Google Secret Manager, latest version
      # dsn: ${gcp-sm:my-project/pg_dsn/versions/3}
      # dsn: ${azure-kv:my-vault/pg-dsn}         # Azure Key Vault
```

| Reference | Credentials | Install |
| --- | --- | --- |
| `aws-sm`, `ssm` | the usual boto3 chain (role, `AWS_PROFILE`, env vars) | `[aws]` |
| `gcp-sm` | `GOOGLE_ACCESS_TOKEN`, or a service account in `GOOGLE_APPLICATION_CREDENTIALS` | `[google]` for service accounts |
| `azure-kv` | `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` (an app with the Key Vault Secrets User role), or `AZURE_ACCESS_TOKEN` | none |

Each reference is fetched once per process. A missing secret or key fails the load with the
reference in the message, never the value.

## Azure OpenAI

```yaml
llm:
  provider: azure-openai
  model: gpt-prod                       # your deployment name
  options: { endpoint: https://my-resource.openai.azure.com }   # or AZURE_OPENAI_ENDPOINT
```

The key comes from `AZURE_OPENAI_API_KEY`. It uses the Azure OpenAI v1 API with structured
outputs, like `provider: openai`.

## Running in the cloud

The same container works everywhere: install agentl and your pipelines, and run `agentl run`.

```dockerfile
FROM python:3.13-slim
RUN pip install --no-cache-dir "agentl-etl[aws,postgres]"
WORKDIR /app
COPY pipelines/ pipelines/
ENTRYPOINT ["agentl", "run"]
CMD ["pipelines/sales.yaml"]
```

Point the run store to a shared Postgres (`AGENTL_DB=postgresql://...`) so the dashboard and
`agentl stats` see every run, and the per-record cache survives between runs.

**Google Cloud Run job**, on a schedule:

```bash
gcloud run jobs deploy sales-etl --image REGION-docker.pkg.dev/PROJECT/etl/agentl \
  --args pipelines/sales.yaml --set-env-vars AGENTL_DB=postgresql://... \
  --service-account etl@PROJECT.iam.gserviceaccount.com
gcloud scheduler jobs create http sales-etl-daily --schedule "0 7 * * 1-5" \
  --uri "https://run.googleapis.com/v2/projects/PROJECT/locations/REGION/jobs/sales-etl:run" \
  --http-method POST --oauth-service-account-email etl@PROJECT.iam.gserviceaccount.com
```

**Azure Container Apps job**, on a schedule:

```bash
az containerapp job create -n sales-etl -g etl --environment etl-env \
  --trigger-type Schedule --cron-expression "0 7 * * 1-5" \
  --image myregistry.azurecr.io/agentl:latest --args pipelines/sales.yaml \
  --mi-system-assigned --env-vars AGENTL_DB=secretref:agentl-db
```

**AWS**: see [AWS Lambda](orchestration.md#aws-lambda) for short runs; use an ECS scheduled
task or AWS Batch with the same image for runs longer than 15 minutes.

**GitHub Actions**: see [GitHub Actions](orchestration.md#github-actions).

Commands above follow each cloud's documented CLI; check the flags against your CLI version.
They have not been run from this repository's CI.
