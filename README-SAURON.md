# sauron

A minimal fork of [Wizarr](https://github.com/wizarrrr/wizarr) (pinned to
`v2026.7.1`) with one functional change: the invitation **REST API** now accepts
`max_active_sessions`.

## Why

Upstream Wizarr exposes "Max Active Sessions" in the web UI, but its REST create
handler (`POST /api/invitations`) whitelists fields and **drops**
`max_active_sessions`. So an integration that provisions invites over the API
(e.g. a Stripe checkout → invite flow) can't set a Jellyfin device limit and has
to reconcile it out-of-band (a cron that pokes the Jellyfin user Policy after the
fact). sauron closes that gap: the API sets the limit at creation, Wizarr's
existing redemption flow applies it to the Jellyfin user's Policy
(`MaxActiveSessions`) when the invite is used — **no external cron required.**

## The delta from upstream

One commit, ~18 lines across two files (plus tests):

- `app/blueprints/api/models.py` — add `max_active_sessions` to the API request
  model and to the invitation response model (Swagger + echo).
- `app/blueprints/api/api_routes.py` — pass `max_active_sessions` through to
  `create_invite`, **stringified** (upstream `create_invite` calls `.strip()`, so
  a raw JSON integer would crash), and echo the stored value on create + in the
  GET list.
- `tests/test_api_restx.py` — `TestAPIMaxActiveSessions` (persist+echo,
  integer-doesn't-500, zero-preserved, omitted-stays-none).

The invitation model field, the web-form parsing, and the Jellyfin policy
application at redemption **already exist upstream** and are unchanged. This keeps
the fork trivial to rebase onto future Wizarr releases:

```bash
git fetch upstream
git rebase upstream/main        # or the next vYYYY.M.P tag
```

## API usage

```bash
curl -X POST https://<host>/api/invitations \
  -H "X-API-Key: <key>" -H "Content-Type: application/json" \
  -d '{"server_ids":[1],"duration":"30","unlimited":false,"max_active_sessions":2}'
# -> 201, .invitation.max_active_sessions == 2   (0 = unlimited; omit = unset)
```

## Building & publishing the image

CI (`.github/workflows/docker-publish.yml`) builds `linux/amd64` (the TrueNAS
target) and pushes to Docker Hub on every push to `sauron` and on `v*` tags.

**One-time setup — add two repo secrets** (Settings → Secrets and variables →
Actions):

| Secret | Value |
| --- | --- |
| `DOCKERHUB_USERNAME` | `n00xx` |
| `DOCKERHUB_TOKEN` | a Docker Hub access token (hub.docker.com → Account Settings → Personal access tokens) |

Then push `sauron` (or run the workflow manually). It publishes:
`n00xx/sauron:latest` and `n00xx/sauron:<version>`.

## Deploying on TrueNAS Scale

See [`deploy/truenas/README.md`](deploy/truenas/README.md). Custom App (Compose
YAML) is the supported path; an optional custom catalog train lives in
[`deploy/catalog/`](deploy/catalog/README.md).

## Running the tests locally

```bash
uv run --group dev pytest tests/test_api_restx.py -q
```
