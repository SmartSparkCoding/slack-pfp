"""Minimal third-party player plugin example.

Load with:
    SLACK_PFP_PLAYER_PLUGINS=examples.player_plugin
"""
from player_plugins import PollResult, Track


class ExamplePlugin:
    id = "example"
    priority = 50

    def poll(self, user, context):
        config = user.config.get("example_player", {})
        if not config.get("enabled"):
            return None

        # Replace this with an MPD/Jellyfin/Navidrome/etc. lookup.
        if not config.get("playing"):
            return PollResult(self.id, None)
        return PollResult(self.id, Track(
            track_id="example-track",
            song="Example Song",
            artist="Example Artist",
            album="Example Album",
            album_art="",
        ))


PLUGIN = ExamplePlugin()
