# Temporal Prerequisites

devloop requires a running Temporal cluster. By default the devloop Helm chart does **not** deploy Temporal — you provide it independently, as described below. For evaluation, the chart can alternatively bundle Temporal as a subchart: `helm install devloop ... --set temporal.enabled=true` deploys the official chart (single replica, single-node Cassandra) and points the worker at it automatically — see [Getting Started Step 4](getting-started.md#step-4-install-temporal). This guide covers the independent install path via the official [temporalio/helm-charts](https://github.com/temporalio/helm-charts) chart.

## Quick Install

```bash
helm repo add temporal https://charts.temporalio.io
helm repo update
helm install temporal temporal/temporal \
  --namespace agents \
  --create-namespace \
  -f docs/reference-temporal-values.yaml
```

## Reference Values

The following `values.yaml` is known to work with devloop. It deploys Temporal with SQLite and Dynamic Config suitable for development and small-scale production. For production workloads, replace SQLite with Postgres or MySQL.

This file ships in the repo as [`docs/reference-temporal-values.yaml`](reference-temporal-values.yaml) (the path the Quick Install above assumes, run from a clone of this repository). If you're working outside a clone, save the following as `reference-temporal-values.yaml` and adjust the `-f` path accordingly:

```yaml
# Reference Temporal Helm values for devloop
# Source: https://github.com/temporalio/helm-charts
# Tested with temporalio/helm-charts v0.0.2+

persistence:
  default:
    driver: "sql"
    options:
      host: "temporal-sqlite"
      port: 5432
      user: "temporal"
      password: "temporal"
      database: "temporal"

database:
  setNull: true

sqlite:
  enabled: true
  image:
    repository: temporalio/sqlite
    tag: latest

dynamicConfig:
  - value:
      - "*..*"
      - value:
          limits.maxIDLength: 256
      - constraints: {}
  - value:
      - "*.Matching.LongPollExpirationInterval"
      - value:
          days: 1
      - constraints: {}

web:
  enabled: false

ui:
  enabled: false

frontend:
  service:
    ports:
      grpc: 7233
  config:
    publicClientHostPort: "temporal-frontend.agents.svc.cluster.local:7233"

client:
  host: "temporal-frontend.agents.svc.cluster.local"
  port: 7233
```

## Verifying Temporal

After installation, confirm the Temporal frontend is reachable:

```bash
kubectl get pods -n agents -l app=temporal
kubectl port-forward svc/temporal-frontend -n agents 7233:7233
```

You can also use the [Temporal CLI](https://docs.temporal.io/cli) to verify
connectivity (with the port-forward from above still running):

```bash
temporal operator cluster health --address 127.0.0.1:7233
```

## Connecting devloop to Temporal

Once Temporal is running, note its service address for the devloop Helm chart:

```
temporal-frontend.agents.svc.cluster.local:7233
```

This address becomes the `temporalHost` value in your devloop `values.yaml`. See [Getting Started](getting-started.md) for the next steps.

## Production Considerations

- Replace SQLite with Postgres or MySQL by setting `sqlite.enabled: false` and configuring `persistence.default` with your database credentials.
- Enable the Temporal Web UI by setting `web.enabled: true` and `ui.enabled: true`.
- Configure TLS, authentication, and resource limits per your cluster policies.
- See the [Temporal Helm chart documentation](https://docs.temporal.io/application-development/fundamentals/deploy-temporal/deploy-in-production/kubernetes) for production hardening guidance.
