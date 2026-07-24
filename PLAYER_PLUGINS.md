# Player plugins

Slack PFP can now read now-playing data from multiple sources. Existing users keep using Last.fm. The default `auto` mode prefers a fresh linked-player update and falls back to Last.fm when the link expires.

## Link a CLI or self-hosted player

On the Slack PFP host, create a token for the user's Slack member ID:

```bash
python player_cli.py link U0123456789 --label bedroom-player
```

Only the token hash is stored in `player_links.json`. Copy the printed token to the player machine.

Expose the bridge safely through your reverse proxy or private network. It listens on `127.0.0.1:8092` by default. Example environment settings:

```dotenv
LINK_PLAYER_ENABLED=1
LINK_PLAYER_HOST=127.0.0.1
LINK_PLAYER_PORT=8092
PLAYER_LINKS_PATH=/home/christian/vscode/slack-pfp/player_links.json
```

Send a track:

```bash
python player_cli.py send \
  --server https://slackpfp.example.com/player-link \
  --user U0123456789 \
  --token 'TOKEN_FROM_LINK_COMMAND' \
  --song 'Digital Love' \
  --artist 'Daft Punk' \
  --album 'Discovery' \
  --album-art 'https://example.com/cover.jpg'
```

Report playback stopped:

```bash
python player_cli.py clear \
  --server https://slackpfp.example.com/player-link \
  --user U0123456789 \
  --token 'TOKEN_FROM_LINK_COMMAND'
```

A player can also POST JSON directly:

```http
POST /v1/now-playing/U0123456789
Authorization: Bearer TOKEN
Content-Type: application/json

{
  "playing": true,
  "song": "Digital Love",
  "artist": "Daft Punk",
  "album": "Discovery",
  "album_art": "https://example.com/cover.jpg",
  "track_id": "optional-stable-id",
  "ttl": 90,
  "client": "navidrome-hook"
}
```

The payload expires after `ttl` seconds. In `auto` mode, Slack PFP then falls back to Last.fm. Send updates periodically while a track is playing.

Revoke a token:

```bash
python player_cli.py revoke U0123456789
```

## External Python plugins

Set a comma-separated module list:

```dotenv
SLACK_PFP_PLAYER_PLUGINS=my_plugins.mpd,my_plugins.jellyfin
```

A module may export `PLUGIN`, or a `register(registry)` function. A plugin needs an `id`, optional numeric `priority`, and `poll(user, context)`. Return:

- `None` when the plugin is not configured for that user.
- `PollResult(plugin_id, None)` when configured but nothing is playing.
- `PollResult(plugin_id, Track(...))` when playing.

See `examples/player_plugin.py`.

## Per-user source selection

The setting lives in `user.config["player_plugin"]`:

- `auto` (default): linked player first, then Last.fm.
- `linked`: only linked pushes.
- `lastfm`: only Last.fm.
- Any registered external plugin ID.

The database config backfill makes `auto` the effective default without requiring a schema migration.
