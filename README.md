# Slack PFP — now-playing profile photos for Hack Club

A multi-tenant, self-hosted service: anyone in the Hack Club Slack workspace
signs in with Slack, enters their Last.fm username, and their Slack profile photo
shows their current album art (framed) while their status shows what they're
playing. The framed album cover, holiday overlays, and custom overlays are all
positioned per-user with a drag-and-drop canvas editor.

```
🎵  90210 (feat. Kacy Hill) - Travis Scott │ by slackpfp.christianwell.xyz
```

## Architecture

- **`app.py`** — Flask web app: Slack OAuth login, per-user dashboard, onboarding,
  account management, overlay editor, school-mode OOO settings, live preview, `/healthz`.

School mode (dashboard → "School mode") switches your Slack status to an OOO
message on your school days during your school hours. Pick the days + time
window, the status message/emoji (with an optional auto-response fallback), and
how a playing track mixes in. School holidays (date ranges) skip school mode
entirely and restore your normal status.
- **`main.py`** — shared worker loop: iterates active users, throttled to stay
  under Last.fm's rate limit, updating each user's Slack photo + status via their
  own token. Disables users whose tokens are revoked.
- **`core.py`** — rendering pipeline (overlays, framing, Last.fm, Slack helpers).
- **`db.py`** — per-user SQLite store (`users.db`); Slack tokens encrypted at rest
  with Fernet.
- **`slack_oauth.py`** — Slack OAuth v2 helpers, restricted to one workspace.
- **`assets/`** — bundled default art: the base `pfp.png`, the album `frame.png`,
  and the holiday overlay images (hat, cake, Halloween, …).
- **`templates/`**, **`static/`** — dashboard/editor UI and the emoji picker.

Per-user uploaded overlays live under `uploads/<slack_user_id>/`, re-encoded to
≤512px PNGs with per-user count/byte quotas.

For deploying changes to the production host, see [`AGENTS.md`](AGENTS.md).

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Create a Slack app at <https://api.slack.com/apps> with **user** scopes
   `users.profile:write`, `users.profile:read`, `users:write`, `emoji:read`, and
   redirect URL `https://<your-domain>/oauth/callback`.
3. Copy `.env.example` to `.env` and fill it in. Generate the secrets:
   ```bash
   python -c "import os;print(os.urandom(32).hex())"   # SECRET_KEY
   python -m db keygen                                  # FERNET_KEY
   ```
   Keep `FERNET_KEY` safe — losing it makes stored Slack tokens unrecoverable
   (users just re-authorize).
4. Run the two services:
   ```bash
   python app.py     # web dashboard (waitress on $WEB_PORT)
   python main.py    # background updater
   ```
   Or install the bundled `slack-pfp.service` / `slack-pfp-web.service` units and
   front the web app with HTTPS (Caddy/nginx) for the OAuth redirect.

## Operations

- **Health:** `GET /healthz` → user counts + OAuth-configured flag.
- **Backups:** back up `users.db` (and `.env` for the keys) regularly.
- **Key rotation:** to rotate `FERNET_KEY`, decrypt-and-re-encrypt tokens with
  both keys, or simply require affected users to re-run "Add to Slack".
- **Privacy:** we read only public Last.fm scrobbles and set the user's own
  Slack photo/status. Users can pause, disconnect, or delete their data from the
  account page.

## License

Released under the [MIT License](LICENSE).
