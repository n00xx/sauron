# Changelog

All notable changes to Wizarr will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project uses [Calendar Versioning](https://calver.org/).



## [2026.7.3] (2026-07-22)


### 🚀 Features

* **auth:** protect the admin login page with Cloudflare Turnstile. Configurable from Settings → General → Login Security (site key + secret key), or via `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` / `TURNSTILE_ENABLED` env vars. The `TURNSTILE_ENABLED=false` env override always wins so a bad key can never lock you out. Fails open if Cloudflare's siteverify endpoint is unreachable (missing/invalid tokens are still rejected).


## [2026.7.2] (2026-07-22)


### 🚀 Features

* **notify:** message expiring users who are actively streaming, via an on-screen Jellyfin/Emby session message ("Tu suscripción está por vencer…"). Adds a manual "Notify users who are streaming" button on the Users page and a scheduled job, both idempotent per expiry window.



## [2025.9.1](https://github.com/wizarrrr/wizarr/compare/2025.9.0rc...2025.9.1) (2025-09-05)


### 🚀 Features

* ensure equal height for cards in widget grid on desktop ([b1f8b4f](https://github.com/wizarrrr/wizarr/commit/b1f8b4f8dc302a8176a970653986e1e0dd82f62b))


### 🐛 Bug Fixes

* properly extract next version in PR updates ([13c9555](https://github.com/wizarrrr/wizarr/commit/13c9555c08672d6cdfeab9b149719735529b89fe))

## [2025.9.0](https://github.com/wizarrrr/wizarr/compare/2025.8.5rc...2025.9.0) (2025-09-05)


### 🐛 Bug Fixes

* disable Release-It branch requirement for automated workflow ([8cea746](https://github.com/wizarrrr/wizarr/commit/8cea746f1211012298cda1c9f6b45c4d2ab59e0e))
* display latest invites ([f86964a](https://github.com/wizarrrr/wizarr/commit/f86964a29d3e10d904abc6f148e4de497e6ecca3))
* improve Release-It workflow to properly create PR with changes ([1af32f9](https://github.com/wizarrrr/wizarr/commit/1af32f927f9c3986002acb876491ace25588767e))
* Readme Kwickflix ([4996bce](https://github.com/wizarrrr/wizarr/commit/4996bce2ded3b34e1325a51d85703296d715122d))
