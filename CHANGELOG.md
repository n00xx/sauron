# Changelog

All notable changes to Wizarr will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project uses [Calendar Versioning](https://calver.org/).



## [2026.7.8] (2026-07-25)


### ✨ Features

* **invite:** the public create-account form now states the password rules up front — "Mínimo 8 caracteres, con al menos una mayúscula, una minúscula y un número." — wired to the field with `aria-describedby`. The wording mirrors `JoinForm.password` exactly, including the lowercase requirement, so nothing that reads as valid gets rejected on submit.
* **invite:** the invitation code arrives prefilled from the link and is now `readonly`, so it cannot be edited by accident. `readonly` rather than `disabled` — a disabled input is not submitted and would break redemption. All three render paths for `welcome-jellyfin.html` populate the field before rendering.


### 🐛 Bug Fixes

* **invite:** drop the "Secure invitation system powered by Wizarr" footer from the public invite page, along with the `pageFooter` references in the reveal/back animations that would otherwise hand anime.js a null target on mobile.



## [2026.7.6] (2026-07-22)


### 🐛 Bug Fixes

* **invite:** the "Create account" button is no longer clipped below the fold on the public create-account screen (`welcome-jellyfin.html`) when several validation errors stack up and grow the form past the card height. The card now grows with its content (`h-auto` instead of a fixed `md:h-[520px]`), and when the form renders with errors the page allows vertical scrolling on every breakpoint (drops `lg:overflow-hidden`) with extra padding, so the submit button is always reachable at 100% zoom without resizing the window. Scoped to the server-rendered form-with-errors path; the animated welcome→form flow is unchanged.



## [2026.7.5] (2026-07-22)


### 🌐 Internationalization

* **invite:** the public invite landing and create-account screens (`welcome-jellyfin.html`) are now served in Mexican Spanish (`es_MX`), including every validation/error message — password policy, invalid email, "please correct the highlighted fields", and the "user or e-mail already exists" banner. Scoped to the public invite endpoints only (via the locale selector); the rest of the app keeps its normal locale. Added an `es_MX` catalog, made the form/validator messages translatable with `lazy_gettext`, and set `novalidate` on the form so native browser tooltips route through the translated server-side messages.


## [2026.7.4] (2026-07-22)


### 🚀 Features

* **invite:** validate that the email domain actually resolves in DNS when a user creates their account. Emails on non-existent domains (e.g. `user@1232as.com`) are rejected with "Please enter a valid email address." Checks MX → A → AAAA records and **fails open** on DNS timeouts / unreachable nameservers so a transient DNS issue never blocks a legitimate signup.


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
