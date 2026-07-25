# Deploying sauron on TrueNAS Scale

sauron is a Wizarr fork that adds `max_active_sessions` to the invitation REST
API, so a Jellyfin per-invite device limit can be set at creation time — no
external reconcile cron needed.

Two ways to install. **Custom App is the supported, reliable path.** The catalog
train (`../catalog/`) is an optional convenience.

---

## Option A — Custom App (recommended)

1. **Publish the image**: push the `sauron` branch to GitHub. CI builds
   `linux/amd64` and pushes `ghcr.io/n00xx/sauron:latest` — public, so TrueNAS
   pulls it with no credentials. (Docker Hub is a second, optional target; see
   repo root `README-SAURON.md`.)
2. **Create a dataset** for persistent data, e.g. `pool/apps/sauron/data`
   (Datasets → Add Dataset). Note its host path, e.g. `/mnt/pool/apps/sauron/data`.
3. **Apps → Discover → Custom App** (the button in the top-right).
4. Choose **Install via YAML**, paste [`docker-compose.yml`](./docker-compose.yml),
   and edit:
   - `volumes:` left side → your dataset path from step 2.
   - `TZ` → your timezone.
   - host `ports` → a free port if 5690 is taken.
   - `PUID/PGID` → the owner of the dataset (TrueNAS `apps` user is `568`).
5. **Install**. When healthy, open `http://<truenas-ip>:5690` and complete the
   Wizarr setup wizard (create admin, add your Jellyfin server).

### Verify the fork works

```bash
# Create an API key in the sauron admin UI, then:
curl -s -X POST http://<truenas-ip>:5690/api/invitations \
  -H "X-API-Key: <key>" -H "Content-Type: application/json" \
  -d '{"server_ids":[1],"duration":"30","unlimited":false,"max_active_sessions":2}'
# Response .invitation.max_active_sessions should be 2 (upstream Wizarr drops it).
```

Redeem that invite against Jellyfin, then confirm the user's Policy shows
`MaxActiveSessions = 2` (Jellyfin dashboard → user → Max simultaneous streams).

---

## Option B — Custom catalog train (optional)

Adds sauron to your Apps catalog so it installs from a form instead of raw YAML.
See [`../catalog/README.md`](../catalog/README.md). This is best-effort and may
need field tweaks for your exact TrueNAS version — Custom App (Option A) is the
guaranteed path.

---

## Notes

- **Backups:** the entire app state (SQLite DB, `secrets.json`, `sessions/`) is
  under `/data/database`. Snapshot/replicate that dataset.
- **Updates:** re-run the GitHub workflow to publish a new `:latest`, then in
  TrueNAS edit the app and pull the new image (or bump to a pinned version tag).
- **Arch:** the image is `linux/amd64` to match TrueNAS Scale.
