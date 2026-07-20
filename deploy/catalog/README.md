# sauron — TrueNAS custom catalog train (optional / best-effort)

This directory is a self-hosted TrueNAS Scale app catalog. Adding it lets you
install sauron from a **form** (Apps → Discover) instead of pasting Compose YAML.

> **Heads up:** this is the convenience path, not the tested one. The official
> `truenas/apps` format renders compose through a vendored ~60-file `ix_lib`
> library with a CI-validated hash. This train instead uses a **self-contained
> plain-Jinja** `templates/docker-compose.yaml` (no library, no hash) so it stays
> maintainable in your own repo. Most TrueNAS builds render plain-Jinja templates
> fine, but if your version rejects it, use the **Custom App** path in
> [`../truenas/README.md`](../truenas/README.md) — that one always works.

## Layout

```
deploy/catalog/
└── trains/
    └── community/
        └── sauron/
            ├── item.yaml                 # app-level metadata
            └── 1.0.0/
                ├── app.yaml              # manifest
                ├── ix_values.yaml        # image repo/tag (n00xx/sauron:latest)
                ├── questions.yaml        # install form (port, storage, PUID/PGID/TZ)
                ├── README.md
                └── templates/
                    └── docker-compose.yaml
```

## Add it to TrueNAS

TrueNAS needs a git repo whose root contains a `trains/` directory. Two options:

**A. Point at a subdirectory** — not supported directly; TrueNAS expects `trains/`
at the repo root. So either:

**B. Publish this `deploy/catalog/` as its own repo root.** Easiest:

```bash
# from a clone of sauron
git subtree split --prefix deploy/catalog -b catalog-train
# push that branch/dir to a repo where trains/ is at the root, e.g.:
#   github.com/n00xx/sauron-catalog  (contents = everything under deploy/catalog/)
```

Then in TrueNAS: **Apps → Discover → (⋮) → Manage Catalogs → Add Catalog**
- Catalog Name: `sauron`
- Repository: `https://github.com/n00xx/sauron-catalog`
- Branch: `main`
- Preferred Trains: `community`

After it syncs, sauron appears in Discover. Install, fill the form (data host
path, port, timezone), deploy.

## Bumping versions

Copy `1.0.0/` to a new semver dir (e.g. `1.0.1/`), update `app.yaml`'s
`version`/`app_version` and `ix_values.yaml`'s image `tag`, commit, and let
TrueNAS re-sync the catalog.
