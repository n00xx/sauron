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

The original change, ~18 lines across two files (plus tests):

- `app/blueprints/api/models.py` — add `max_active_sessions` to the API request
  model and to the invitation response model (Swagger + echo).
- `app/blueprints/api/api_routes.py` — pass `max_active_sessions` through to
  `create_invite`, **stringified** (upstream `create_invite` calls `.strip()`, so
  a raw JSON integer would crash), and echo the stored value on create + in the
  GET list.
- `tests/test_api_restx.py` — `TestAPIMaxActiveSessions` (persist+echo,
  integer-doesn't-500, zero-preserved, omitted-stays-none).

The invitation model field, the web-form parsing, and the Jellyfin policy
application at redemption **already exist upstream** and are unchanged.

The fork has since grown past that one commit — see "Self-service renewal API"
below and the CHANGELOG — but the same rule still applies: every addition is
additive and namespaced, so rebasing onto a future Wizarr release stays cheap:

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

## Self-service renewal API

A second group of endpoints exists so a public checkout can renew an EXISTING
account: prove the buyer owns it, then extend and reactivate it once they pay.

### `POST /api/users/verify-credentials`

Ownership proof — checks a media account's own username and password.

```bash
curl -X POST https://<host>/api/users/verify-credentials \
  -H "X-API-Key: <key>" -H "Content-Type: application/json" \
  -d '{"username":"someone","password":"their-password"}'
# -> 200 {"valid": true,  "user_id": 42}
# -> 200 {"valid": false, "user_id": null}
```

**Always 200, always the same body shape.** Unknown username, wrong password,
disabled account and unsupported server type are indistinguishable by design —
Jellyfin itself answers 401 vs 403 for those, which is a user-enumeration
oracle, and collapsing it is the whole point. Do not "improve" the error
reporting.

Two Jellyfin behaviours this has to work around, both read from
`Jellyfin.Server.Implementations/Users/UserManager.cs`:

- **A disabled account cannot authenticate at all** (403 before the password is
  even checked). Since expired accounts are exactly the ones being renewed, the
  endpoint enables the account for the duration of the check and restores it
  afterwards — under a per-account lock, restoring from sauron's own
  `user.is_disabled` column rather than a live policy read. A live read races
  against a concurrent check of the same account (one gunicorn process, 8
  threads) and can leave a lapsed account enabled without payment.
- **Failed logins lock the account out.** Jellyfin disables an account once
  `InvalidLoginAttemptCount` reaches `LoginAttemptsBeforeLockout`, so a public
  form in front of it is a remote account-disabling weapon. The endpoint detects
  a lockout it caused and reverses it, and is rate limited to 3/hour and 10/day
  per username plus 20/hour per IP. Note the counter itself has **no reset API**
  and only zeroes on a successful login — the rate limits bound how fast it can
  climb, and a successful check is what actually heals a probed account.

Jellyfin and Emby only. Anything else answers `valid: false`.

### `POST /api/users/<id>/max-sessions`

```bash
curl -X POST https://<host>/api/users/42/max-sessions \
  -H "X-API-Key: <key>" -H "Content-Type: application/json" \
  -d '{"max_active_sessions":4}'
# -> 200 {"message":"...","max_active_sessions":4}
# -> 502 when the media server refuses or does not support session limits
```

Upstream applies `max_active_sessions` only at invite redemption, which covers a
first purchase but not a renewal: a buyer upgrading tier already has an account.
Without this the money is taken for "4 devices" and the old limit stays.

### `POST /api/users/<id>/enable` — behaviour change

Now answers **502** when the media server refuses or does not support enabling.
It previously returned 200 with an `"Enable failed or not supported"` message,
so an API client could not tell a reactivated account from one still disabled —
a paid renewal would report as delivered while the buyer had no access.

Also clears any stale `ExpiredUser` record on success, so a renewed customer
stops showing up under "expired users" in the admin UI.

## Building & publishing the image

CI (`.github/workflows/docker-publish.yml`) builds `linux/amd64` (the TrueNAS
target) on every push to `sauron` and on `v*` tags.

**GHCR — the working path, no setup.** Every build publishes
`ghcr.io/n00xx/sauron:latest` and `ghcr.io/n00xx/sauron:<version>` using the
built-in `GITHUB_TOKEN`. This repo is public, so the package is public too and
TrueNAS pulls it anonymously. This is what `deploy/truenas/docker-compose.yml`
points at.

**Docker Hub — optional second target.** Skipped unless both repo secrets exist
(Settings → Secrets and variables → Actions):

| Secret | Value |
| --- | --- |
| `DOCKERHUB_USERNAME` | `n00xx` |
| `DOCKERHUB_TOKEN` | a Docker Hub access token (hub.docker.com → Account Settings → Personal access tokens) |

Without them the build still succeeds and lands in GHCR only — `n00xx/sauron`
on Docker Hub does not exist today.

## Deploying on TrueNAS Scale

See [`deploy/truenas/README.md`](deploy/truenas/README.md). Custom App (Compose
YAML) is the supported path; an optional custom catalog train lives in
[`deploy/catalog/`](deploy/catalog/README.md).

## Concurrency & rate limiting

The container runs **one gunicorn process with 8 threads** (`gunicorn.conf.py`),
not the 4 sync workers upstream uses. The reason is rate limiting:
flask-limiter's default `memory://` storage is per *process*, so 4 workers meant
a declared `10 per minute` on `/login` was really ~40 across the deployment.
Threads share that memory, so one process makes the declared limits exact — no
Redis, no second container.

Threads rather than a bare single worker because a lone `sync` worker serves one
request at a time: a slow Jellyfin call would block login and invites until the
120s timeout. The app is I/O-bound, so the GIL is released during those waits.

| Variable | Default | Notes |
| --- | --- | --- |
| `GUNICORN_WORKERS` | `1` | Above 1 the counters fragment again. `scaled_limit` compensates by dividing the declared limits, but the result is approximate — set `RATELIMIT_STORAGE_URI` instead if you need exactness. |
| `GUNICORN_THREADS` | `8` | Concurrent requests. Raising it means raising `pool_size` in `SQLALCHEMY_ENGINE_OPTIONS` too — one process now shares a single pool between these threads, the activity monitor's 10-thread executor and the scheduler. |
| `RATELIMIT_STORAGE_URI` | `memory://` | Only needed for multiple processes/replicas. **Requires adding the `redis` package to `pyproject.toml` first** — with a `redis://` URI and no such package, `limiter.init_app()` raises and the app will not boot. |

Multiple replicas are not a supported shape regardless: the database is SQLite on
a bind-mounted volume (`app/config.py`), which cannot be shared across instances.

`tests/test_ratelimit_multiworker.py` pins the `GUNICORN_WORKERS` default in
`gunicorn.conf.py` to the one in `app/extensions.py:_worker_count()`. Changing
one without the other makes every limit 4x stricter than declared, silently.

## Running the tests locally

```bash
uv run --group dev pytest tests/test_api_restx.py -q
```
