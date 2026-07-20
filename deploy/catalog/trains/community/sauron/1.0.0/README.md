# Sauron

A [Wizarr](https://github.com/wizarrrr/wizarr) fork whose invitation REST API
(`POST /api/invitations`) accepts `max_active_sessions`, so a Jellyfin per-invite
device limit is set at creation time and applied when the invite is redeemed —
removing the need for any external reconcile job.

Runs a single container on port 5690 with one persistent volume at
`/data/database`.
