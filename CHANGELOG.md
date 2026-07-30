# Changelog

All notable changes to Wizarr will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project uses [Calendar Versioning](https://calver.org/).



## [2026.7.12] (2026-07-30)


### ✨ Features

* **users:** the Users page now shows a red "Expired" / orange "Expiring Soon" (≤3 days) / green "Active" badge on every card, and the filter bar gained a matching status dropdown. Both read from the same `get_expiry_status()` helper so the badge and the filter can never disagree, and the filter is applied after `_group_users_for_display()` groups multi-server accounts, against each card's `earliest_expires`.
* **users:** "Recently Expired Users" now scopes to the last 30 days (it previously showed the full unbounded history, hence 690 rows for a handful of accounts — see the fix below) and gained per-row checkboxes with "Delete Selected", plus a "Clear All" button. "All Expired Users" (the full history) gained a "Delete All" button. Both sections read the same `ExpiredUser` table, so deleting from either refreshes both via a shared `refreshExpiredUsers` HTMX trigger.
* **invite:** the Jellyfin "Allow audio playback that requires transcoding" checkbox now defaults to checked for every new invitation, as the code comments already (incorrectly) claimed it did. Video transcoding stays opt-in.


### 🐛 Bug Fixes

* **expiry:** fixed the root cause of the duplicated/stale "Recently Expired Users" history and users that showed "Expires: Never" while actually being inaccessible. `disable_or_delete_user_if_expired()` runs every 15 minutes in production; in `expiry_action="disable"` mode it logged a new `ExpiredUser` row and disabled the account remotely, but never marked `is_disabled` locally or excluded the user from its own query — so the same expired user matched again on every subsequent tick, forever, each time inserting another history row. The query now excludes already-disabled users and the disable branch marks `is_disabled=True` immediately. The manual enable/disable toggle in the admin panel now also persists `is_disabled` locally (it previously only called the remote API). The Jellyfin sync additionally pulls `Policy.IsDisabled` on every poll, so an account disabled directly on Jellyfin (outside Wizarr) self-heals into the local record instead of drifting indefinitely.



### ✨ Features

* **invite:** both password fields on the public create-account form now carry an eye toggle to reveal what was typed, so a typo can be checked without retyping. Open eye = hidden (click to reveal), crossed-out eye = visible (click to hide). The two fields toggle independently and both start hidden — `type="password"` straight from the form, nothing to opt out of. Rendered from one Jinja macro; `type="button"` on the control is load-bearing, since a `<button>` inside a `<form>` defaults to submit and would otherwise post the form on click. Accessible name (`Mostrar contraseña` / `Ocultar contraseña`) and `aria-pressed` swap with the state.


### 🐛 Bug Fixes

* **invite:** the focus animation on form fields now targets the `.form-field` wrapper explicitly instead of `parentElement`. The password inputs sit inside a positioning box for their toggle, so `parentElement` would have animated that box rather than the field. Also skips inputs with no `.form-field` ancestor — `hidden_tag()`'s CSRF input hangs off `<form>` directly, and anime.js throws on a null target.
* **invite:** added a regression guard for the `:root` custom properties in `welcome-jellyfin.html`. djLint's `--format-css`, wired into `.pre-commit-config.yaml`, rewrites the `{{ ... }}` inside that `<style>` block into `{ { ... } }`; Jinja then emits it verbatim and the page silently loses its accent colour while still returning 200. Run djLint on this file with `--lint` only, never `--reformat --format-css`.



## [2026.7.10] (2026-07-27)


### ✨ Features

* **jellyfin:** the Playlists library is never granted to a provisioned account. `_set_specific_folders` filters it out of `EnabledFolders`, matching on `CollectionType == "playlists"` rather than on the display name — library names are admin-chosen and usually localised ("Peliculas", "Documentales"), so a name match would break on exactly the servers that need this. If the Playlists folder was the *only* requested library the user is now restricted to nothing, rather than falling through to `EnableAllFolders` and being granted everything.

  Known limit, stated plainly: this is correct hygiene but not a guarantee. Jellyfin exempts Playlists from the `EnabledFolders` check entirely — `Folder.IsVisible` only consults it when `this is ICollectionFolder && this is not BasePluginFolder`, and `PlaylistsFolder` derives from `BasePluginFolder`. What actually hides the library is `UserViewManager.GetUserViews`, which skips a playlists folder unless the user can see at least one playlist inside it. Fully revoking it is a Jellyfin-side configuration matter, outside Sauron's reach.


### 🐛 Bug Fixes

* **libraries:** scanning libraries no longer resets `Library.enabled`, so unchecking a library in the server settings actually sticks. Both scan paths did this: the "Scan Libraries" button (`media_servers/routes.py`) and the startup scan (`library_scanner.py`), the latter running on **every container boot** — so an admin could uncheck a library, restart, and find it silently re-enabled. New libraries still arrive enabled; only existing rows are left alone. Trade-off: a library that vanished from the server (and was auto-disabled) now stays disabled if it comes back, until the admin re-checks it — under-granting is the safer failure.



## [2026.7.9] (2026-07-27)


### ✨ Features

* **jellyfin:** every Jellyfin account Sauron creates now lands with all of its Home screen sections (User → Settings → Home) set to "None". The layout lives in DisplayPreferences (id `usersettings`, client `emby`) rather than in the user Policy, so this is a separate write on both provisioning paths: invitation redemption (`_do_join`) and the password-prompt route (`/j/<code>/password`). Jellyfin's update handler clears every stored section and re-adds only the keys it receives, so all 10 `homesection*` keys are written — the user-visible count is 7 on current clients and 10 on newer jellyfin-web, and omitting a section would let it fall back to a built-in default. The write is isolated in its own error handler and runs last: the account, its libraries and its policy already exist by then, so a DisplayPreferences failure logs a warning instead of rolling back and orphaning the account. Emby is excluded — `EmbyClient` inherits the method but stores display preferences differently.



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
