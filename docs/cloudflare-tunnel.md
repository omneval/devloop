# Exposing the Webhook Endpoint with Cloudflare Tunnel

devloop is webhook-driven: GitHub must be able to reach
`https://<your-domain>/webhook/github`, which is served by the
`devloop-temporal-worker` pod on its `webhookPort` (default `8088`). If your
cluster has no public ingress, [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
(`cloudflared`) is the recommended way to expose that endpoint without opening
inbound firewall ports — it works equally well for homelab clusters and managed
Kubernetes.

This guide walks through creating a tunnel that forwards a public hostname to
the in-cluster `devloop-temporal-worker` Service.

## Prerequisites

- A domain managed in Cloudflare (the tunnel routes DNS for you).
- `cloudflared` installed locally to create and configure the tunnel
  (`brew install cloudflared` or see the [installation docs](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)).
- `kubectl` access to the cluster where devloop is (or will be) installed.

## 1. Authenticate and create the tunnel

```bash
cloudflared tunnel login
cloudflared tunnel create devloop-webhooks
```

This writes a credentials JSON file (e.g. `~/.cloudflared/<TUNNEL_ID>.json`) and
registers the tunnel with your Cloudflare account.

## 2. Route a hostname to the tunnel

```bash
cloudflared tunnel route dns devloop-webhooks webhooks.your-domain.com
```

GitHub will deliver events to `https://webhooks.your-domain.com/webhook/github`.

## 3. Configure the tunnel to forward to the in-cluster Service

Create a `config.yml` that forwards the public hostname to the
`devloop-temporal-worker` Service's webhook port (the chart names the
container port `webhook` and defaults it to `8088`):

```yaml
tunnel: <TUNNEL_ID>
credentials-file: /etc/cloudflared/creds/<TUNNEL_ID>.json

ingress:
  - hostname: webhooks.your-domain.com
    service: http://devloop-temporal-worker.agents.svc.cluster.local:8088
  - service: http_status:404
```

## 4. Run cloudflared in the cluster

Store the tunnel credentials as a Secret and run `cloudflared` as a Deployment
in the same namespace as devloop (`agents` in the examples below):

```bash
kubectl create secret generic cloudflared-credentials \
  --from-file=credentials.json=$HOME/.cloudflared/<TUNNEL_ID>.json \
  -n agents

kubectl create configmap cloudflared-config \
  --from-file=config.yaml=./config.yml \
  -n agents
```

Then deploy `cloudflared` (e.g. via the [official Helm chart](https://github.com/cloudflare/cloudflared)
or a minimal Deployment) mounting both the ConfigMap and the credentials
Secret, running `cloudflared tunnel --config /etc/cloudflared/config.yaml run`.

## 5. Verify

```bash
curl -i https://webhooks.your-domain.com/webhook/github
```

A `405 Method Not Allowed` (GET against a POST-only endpoint) confirms the
tunnel is routing traffic to the `devloop-temporal-worker` pod. You're now
ready to configure the GitHub webhook itself — see
[Getting Started, Step 2](getting-started.md#step-2-create-the-github-webhook).

## Alternatives

If Cloudflare Tunnel doesn't fit your environment, see the **Cloud load
balancer** and **ngrok** options in [Getting Started, Step 1](getting-started.md#step-1-expose-a-webhook-ingress-endpoint).
