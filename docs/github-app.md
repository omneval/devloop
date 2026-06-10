# GitHub App Setup for devloop-bot (issue #81)

devloop can authenticate to GitHub either as a fine-grained personal access
token (PAT — see [Step 3 of the Getting Started guide](getting-started.md))
or, **recommended**, as a **GitHub App**. A GitHub App:

- mints short-lived **installation access tokens** (1-hour expiry, generated
  on demand from an RSA private key — no long-lived secret to leak or rotate),
- can be **installed per-repository**, scoping exactly which repos devloop can
  touch without sharing a single bot account's full permission set, and
- can be **published** so other devloop operators install it on their own
  repos directly from the GitHub Marketplace/App page — no need to create and
  manage a `devloop-bot` collaborator account at all.

This document covers registering the app (the manifest/permission set) and
wiring the resulting credentials into devloop. PAT-based deployments continue
to work unchanged — see [Backward compatibility](#backward-compatibility).

## 1. Register the App

Go to **Settings → Developer settings → GitHub Apps → New GitHub App** (for a
personal account) or your organization's equivalent page, and register an app
with the following configuration. You may also paste the JSON manifest below
into a [GitHub App manifest flow](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest)
to create it in one step.

- **GitHub App name**: `devloop` (or `devloop-<your-org>` if `devloop` is taken
  — app names are globally unique)
- **Homepage URL**: your fork/deployment's repository URL
- **Webhook**: keep this **disabled/inactive** — devloop's existing webhook
  receiver (configured in [Step 2 of Getting Started](getting-started.md#step-2-create-the-github-webhook))
  is the integration point; the App itself doesn't need its own webhook
- **Where can this GitHub App be installed?**: "Only on this account" for a
  private/single-operator deployment, or "Any account" if you intend to
  publish `devloop` for other operators to install on their own repos

### Required permission set

Configure exactly these **Repository permissions** — this is the same
permission set the PAT path documents in
[Getting Started Step 3](getting-started.md#step-3-set-up-github-authentication-for-devloop-bot),
mapped onto GitHub App permission names:

| Permission      | Access         | Why devloop needs it                                                            |
|-----------------|----------------|---------------------------------------------------------------------------------|
| **Contents**    | Read and write | clone, commit, and push agent branches                                          |
| **Pull requests** | Read and write | open PRs, request reviewers, post review comments, fetch diffs, poll merge state |
| **Issues**      | Read and write | read trigger labels, post status comments, file follow-up issues, open summarization digests |
| **Checks**      | Read           | poll CI status during the CI Fix Loop                                            |
| **Workflows**   | Read and write | push agent branches that add or edit `.github/workflows/*` files                |

Grant nothing else — devloop never needs Actions, Administration, Webhooks, or
any account-level permission.

> **Don't skip Workflows.** Without it, any agent branch that touches
> `.github/workflows/*` is rejected on push with a generic-looking failure
> (`refusing to allow a GitHub App to create or update workflow ... without
> \`workflows\` permission`) — the job fails with an opaque "exit status 1"
> and no indication that a permission, not a bug, is the cause.

### Manifest (for the manifest creation flow)

```json
{
  "name": "devloop",
  "url": "https://github.com/<your-org>/devloop",
  "hook_attributes": { "url": "" },
  "redirect_url": "https://github.com/<your-org>/devloop",
  "public": false,
  "default_permissions": {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "checks": "read",
    "workflows": "write"
  },
  "default_events": []
}
```

> Set `"public": true` (and fill in `url`/`redirect_url` with your published
> listing) if you intend to publish `devloop` for other operators to install.

## 2. Generate a Private Key

After creating the app, scroll to **Private keys** on the app's settings page
and click **Generate a private key**. GitHub downloads a `.pem` file — this is
the RSA private key devloop uses to sign the JWTs it exchanges for
installation access tokens. Treat it like any other credential: it grants
devloop-level access to every repo the app is installed on.

Note the **App ID** shown at the top of the same settings page — you'll need
both values below.

## 3. Install the App on Your Repositories

From the app's public page (or **Settings → Integrations → GitHub Apps** for an
org), click **Install**, and choose either "All repositories" or select the
specific repos you're enrolling with devloop. After installation, note the
**installation ID** — it's the numeric segment of the installation's settings
URL: `https://github.com/settings/installations/<installation_id>`.

## 4. Store the Credentials as Kubernetes Secrets

The private key is stored as a Secret and referenced by Helm value
`githubApp.privateKeySecret`; devloop reads it at runtime as
`GITHUB_APP_PRIVATE_KEY`.

```bash
kubectl create secret generic devloop-github-app-key \
  --from-file=privateKey=/path/to/devloop.<date>.private-key.pem \
  -n agents
```

## 5. Configure the Helm Chart

Set these values (see `charts/devloop/values.yaml`):

```yaml
githubApp:
  appId: "123456"                      # GITHUB_APP_ID
  installationId: "987654"             # GITHUB_APP_INSTALLATION_ID
  privateKeySecret:
    name: devloop-github-app-key       # Secret created in step 4
    key: privateKey                    # key within that Secret
```

The chart forwards these as `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` (sourced
from the Secret via `secretKeyRef`), and `GITHUB_APP_INSTALLATION_ID` on the
temporal-worker container. When `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY`
are both present, `github_ops.py` mints and uses short-lived installation
tokens automatically — no other configuration changes are required.

The choice between GitHub App auth and PAT auth is made **once, at
configuration-detection time**, based solely on whether `GITHUB_APP_ID` and
`GITHUB_APP_PRIVATE_KEY` are set — there is no runtime fallback to the PAT if
App-token minting fails (e.g. a wrong `installationId` produces a 404 on every
call). Existing `github_token_secret` entries in the project registry are no
longer consulted for the worker's API calls once GitHub App auth is
configured — but they are still mounted into Agent Execution Job pods for
`git clone`/`git push` (installation tokens expire after an hour, too short
for a job), so keep each project's token Secret in place; it only needs
**Contents** and **Workflows** read/write once the App handles everything
else. See [Backward Compatibility](#backward-compatibility) for when the PAT
path is used instead.

## How Token Generation Works

For every GitHub API call, devloop:

1. Builds a JWT signed with the app's RSA private key (`RS256`), with `iss`
   set to the App ID and a short (≤10 minute) expiry, per
   [GitHub's App authentication requirements](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app).
2. Exchanges that JWT for an installation access token via
   `POST /app/installations/{installation_id}/access_tokens`.
3. Caches the resulting token (GitHub issues these with a 1-hour expiry) and
   reuses it for subsequent calls.
4. Refreshes the token — repeating steps 1–2 — once the cached token is within
   **5 minutes** of expiring, so a long-running activity never gets caught
   mid-call with a token GitHub has just invalidated.

This all happens transparently inside `github_ops._client()` / `_resolve_token()`
— activities that talk to GitHub don't need to know which auth mode is active.

## Backward Compatibility

GitHub App auth is **opt-in**: devloop only switches to it when *both*
`GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY` are set on the worker. If either
is absent, devloop falls back to the existing per-project PAT path
(`GITHUB_TOKEN` resolved from each project's `github_token_secret`), exactly
as documented in [Getting Started Step 3](getting-started.md#step-3-set-up-github-authentication-for-devloop-bot).
Existing PAT-based deployments require **no changes** to keep working.
