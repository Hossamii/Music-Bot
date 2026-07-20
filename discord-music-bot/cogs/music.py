"""
Slash commands for the music bot: join/leave, play/pause/resume/skip/stop,
queue display, and volume control. All music playback logic and error
handling for a single guild lives in this cog.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from utils.lyrics import LyricsNotFoundError, fetch_lyrics, split_artist_title
from utils.queue_manager import GuildMusicState, MusicManager, Track
from utils.ytdl_source import TrackUnavailableError, YTDLSource

log = logging.getLogger("music.cog")

SOURCE_CHOICES = [
    app_commands.Choice(name="YouTube", value="youtube"),
    app_commands.Choice(name="SoundCloud", value="soundcloud"),
]

# FFmpeg `-af` equalizer filter graphs for the /bassboost command. Boosts
# low frequencies (~60Hz and ~150Hz) at increasing gain per level — good
# for heavy/loud tracks.
BASS_PRESETS: dict[str, str] = {
    "off": "",
    "low": "equalizer=f=60:width_type=o:width=2:g=4,equalizer=f=150:width_type=o:width=2:g=2",
    "medium": "equalizer=f=60:width_type=o:width=2:g=8,equalizer=f=150:width_type=o:width=2:g=4",
    "high": "equalizer=f=60:width_type=o:width=2:g=12,equalizer=f=150:width_type=o:width=2:g=6",
    "extreme": "equalizer=f=60:width_type=o:width=2:g=18,equalizer=f=150:width_type=o:width=2:g=9",
}

BASS_CHOICES = [
    app_commands.Choice(name="Off", value="off"),
    app_commands.Choice(name="Low", value="low"),
    app_commands.Choice(name="Medium", value="medium"),
    app_commands.Choice(name="High", value="high"),
    app_commands.Choice(name="Extreme (very heavy bass)", value="extreme"),
]

ACTIVITY_TYPE_CHOICES = [
    app_commands.Choice(name="Playing", value="playing"),
    app_commands.Choice(name="Listening to", value="listening"),
    app_commands.Choice(name="Watching", value="watching"),
    app_commands.Choice(name="Competing in", value="competing"),
]

_ACTIVITY_TYPE_MAP = {
    "playing": discord.ActivityType.playing,
    "listening": discord.ActivityType.listening,
    "watching": discord.ActivityType.watching,
    "competing": discord.ActivityType.competing,
}


class Music(commands.Cog):
    """Music playback commands. All commands are decorated with
    @app_commands.guild_only() since playback doesn't make sense in DMs and
    interaction.guild_id / user.voice would be None there."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.manager = MusicManager()

    # ---------- helpers ----------

    def _state(self, guild_id: int) -> GuildMusicState:
        return self.manager.get(guild_id)

    async def _ensure_voice(
        self, interaction: discord.Interaction
    ) -> discord.VoiceClient | None:
        """Ensure the bot is connected to the user's voice channel. Returns
        the voice client, or None (after sending an error) if it can't join."""
        if interaction.user.voice is None or interaction.user.voice.channel is None:
            await interaction.response.send_message(
                "You need to be in a voice channel first.", ephemeral=True
            )
            return None

        channel = interaction.user.voice.channel
        state = self._state(interaction.guild_id)

        if state.voice_client and state.voice_client.is_connected():
            if state.voice_client.channel.id != channel.id:
                await state.voice_client.move_to(channel)
            return state.voice_client

        try:
            voice_client = await channel.connect()
        except discord.ClientException as exc:
            await interaction.response.send_message(
                f"Couldn't join the voice channel: {exc}", ephemeral=True
            )
            return None

        state.voice_client = voice_client
        state.text_channel = interaction.channel
        return voice_client

    def _play_next(self, guild: discord.Guild) -> None:
        """Callback invoked (from a non-async FFmpeg thread) when a track
        finishes. Schedules the next track on the bot's event loop."""
        state = self._state(guild.id)
        coro = self._advance_queue(guild)
        fut = asyncio.run_coroutine_threadsafe(coro, self.bot.loop)
        try:
            fut.result()
        except Exception:
            log.exception("Error advancing queue for guild %s", guild.id)

    async def _advance_queue(self, guild: discord.Guild) -> None:
        """Pop and start the next track. Iterates (never recurses) so a
        chain of unavailable tracks can't hold `play_next_lock` forever
        across nested awaits (asyncio.Lock is not reentrant)."""
        state = self._state(guild.id)
        async with state.play_next_lock:
            while True:
                next_track = state.pop_next()
                state.current = next_track
                if next_track is None:
                    return

                if state.voice_client is None or not state.voice_client.is_connected():
                    return

                # Never start a second stream on top of one already playing.
                if state.voice_client.is_playing() or state.voice_client.is_paused():
                    return

                try:
                    stream_url = await YTDLSource.refresh_stream_url(next_track)
                    audio_filter = BASS_PRESETS.get(state.bass_level, "")
                    source = YTDLSource.build_audio_source(stream_url, state.volume, audio_filter)
                except TrackUnavailableError as exc:
                    await self._notify(state, f"Skipping **{next_track.title}** — {exc}")
                    continue
                except Exception:  # noqa: BLE001 - report unexpected errors, never crash the bot
                    # Print the exact traceback to the console for debugging,
                    # but never let it crash the bot or a slash command.
                    log.exception("Unexpected error resolving/building audio source for %r", next_track.title)
                    await self._notify(
                        state,
                        f"Skipping **{next_track.title}** — an unexpected error occurred while loading it "
                        "(see the bot console for details).",
                    )
                    continue

                def _after(error: Exception | None) -> None:
                    if error:
                        log.error("Playback error for %r: %s", next_track.title, error)
                    self._play_next(guild)

                try:
                    state.voice_client.play(source, after=_after)
                except discord.opus.OpusNotLoaded:
                    log.exception(
                        "libopus is not loaded — voice playback cannot start. "
                        "Ensure the 'libopus' system dependency is installed."
                    )
                    await self._notify(
                        state,
                        f"Couldn't play **{next_track.title}** — the audio codec (libopus) isn't loaded on the "
                        "server. This is a bot configuration issue, not your fault.",
                    )
                    return
                except discord.ClientException:
                    # Something else started playback concurrently; don't double-start.
                    log.warning("play() rejected — playback already in progress for guild %s", guild.id)
                    return
                except Exception:  # noqa: BLE001 - never let an unexpected playback error crash the bot
                    log.exception("Unexpected error starting playback for %r", next_track.title)
                    await self._notify(
                        state,
                        f"Couldn't play **{next_track.title}** — an unexpected error occurred "
                        "(see the bot console for details).",
                    )
                    return
                await self._notify(state, f"Now playing: **{next_track.title}** ({next_track.formatted_duration()})")
                return

    async def _notify(self, state: GuildMusicState, message: str) -> None:
        if state.text_channel is not None:
            try:
                await state.text_channel.send(message)
            except discord.HTTPException:
                log.warning("Failed to send notification message")

    async def _update_presence(
        self, text: str | None, activity_type: discord.ActivityType = discord.ActivityType.listening
    ) -> None:
        """Update the bot's global status/activity. Note: Discord bots only
        have ONE presence shared across every server they're in — this
        isn't a per-guild setting (that's a platform limitation, not
        something the bot can work around)."""
        activity = discord.Activity(type=activity_type, name=text or "/play")
        try:
            await self.bot.change_presence(activity=activity)
        except discord.HTTPException:
            log.warning("Failed to update bot presence")

    async def _update_nickname(self, guild: discord.Guild | None, nick: str | None) -> None:
        """Update the bot's nickname in a single guild. Passing nick=None
        resets it back to the bot's default account name."""
        if guild is None or guild.me is None:
            return
        try:
            await guild.me.edit(nick=nick[:32] if nick else None)
        except discord.Forbidden:
            log.warning(
                "Missing permission to change nickname in guild %s "
                "(bot needs the 'Change Nickname' permission).",
                guild.id,
            )
        except discord.HTTPException:
            log.warning("Failed to update nickname in guild %s", guild.id)

    # ---------- commands ----------

    @app_commands.command(name="join", description="Join your current voice channel")
    @app_commands.guild_only()
    async def join(self, interaction: discord.Interaction):
        voice_client = await self._ensure_voice(interaction)
        if voice_client is None:
            return
        await interaction.response.send_message(f"Joined **{voice_client.channel.name}**.")

    @app_commands.command(name="leave", description="Leave the voice channel and clear the queue")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction):
        state = self._state(interaction.guild_id)
        if state.voice_client is None or not state.voice_client.is_connected():
            await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
            return
        # Mark this as an intentional disconnect so on_voice_state_update
        # doesn't try to auto-reconnect (the bot normally stays in the
        # room and only leaves when explicitly told to via /leave).
        state.expected_disconnect = True
        state.clear()
        await state.voice_client.disconnect()
        state.voice_client = None
        await interaction.response.send_message("Disconnected and cleared the queue.")

    @app_commands.command(name="play", description="Play a song by search term or URL")
    @app_commands.describe(query="Song name, artist, or a direct YouTube/SoundCloud URL", source="Where to search when not a URL")
    @app_commands.choices(source=SOURCE_CHOICES)
    @app_commands.guild_only()
    async def play(
        self,
        interaction: discord.Interaction,
        query: str,
        source: app_commands.Choice[str] | None = None,
    ):
        await interaction.response.defer()

        voice_client = await self._ensure_voice_deferred(interaction)
        if voice_client is None:
            return

        state = self._state(interaction.guild_id)
        state.text_channel = interaction.channel
        search_source = source.value if source else "youtube"

        try:
            track = await YTDLSource.resolve(
                query,
                requested_by=str(interaction.user.display_name),
                source=search_source,
                loop=self.bot.loop,
            )
        except TrackUnavailableError as exc:
            await interaction.followup.send(f"Couldn't play that: {exc}")
            return
        except Exception:  # noqa: BLE001
            log.exception("Unexpected error resolving track")
            await interaction.followup.send(
                "Something went wrong looking that up. Please try a different search or URL."
            )
            return

        # Snapshot "was something already playing" before enqueueing so the
        # followup message is accurate. The actual start decision happens
        # atomically inside _advance_queue's lock below, so even if two
        # /play calls race here, only one of them will ever start playback.
        already_playing = state.is_playing()
        position = state.add(track)

        if already_playing:
            await interaction.followup.send(
                f"Queued **{track.title}** ({track.formatted_duration()}) — position {position}."
            )
        else:
            await interaction.followup.send(f"Loading **{track.title}**...")
            # Safe to call even if another concurrent /play already started
            # playback: _advance_queue no-ops if the voice client is already
            # playing/paused by the time it acquires the lock.
            await self._advance_queue(interaction.guild)

    async def _ensure_voice_deferred(self, interaction: discord.Interaction) -> discord.VoiceClient | None:
        """Same as _ensure_voice but for an already-deferred interaction
        (used by /play, which needs time to search before responding)."""
        if interaction.user.voice is None or interaction.user.voice.channel is None:
            await interaction.followup.send("You need to be in a voice channel first.")
            return None

        channel = interaction.user.voice.channel
        state = self._state(interaction.guild_id)

        if state.voice_client and state.voice_client.is_connected():
            if state.voice_client.channel.id != channel.id:
                await state.voice_client.move_to(channel)
            return state.voice_client

        try:
            voice_client = await channel.connect()
        except discord.ClientException as exc:
            await interaction.followup.send(f"Couldn't join the voice channel: {exc}")
            return None

        state.voice_client = voice_client
        state.text_channel = interaction.channel
        return voice_client

    @app_commands.command(name="pause", description="Pause the current track")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction):
        state = self._state(interaction.guild_id)
        if not state.voice_client or not state.voice_client.is_playing():
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        state.voice_client.pause()
        await interaction.response.send_message("Paused.")

    @app_commands.command(name="resume", description="Resume playback")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction):
        state = self._state(interaction.guild_id)
        if not state.voice_client or not state.voice_client.is_paused():
            await interaction.response.send_message("Nothing is paused.", ephemeral=True)
            return
        state.voice_client.resume()
        await interaction.response.send_message("Resumed.")

    @app_commands.command(name="skip", description="Skip the current track")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction):
        state = self._state(interaction.guild_id)
        if not state.voice_client or not (state.voice_client.is_playing() or state.voice_client.is_paused()):
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        skipped = state.current.title if state.current else "current track"
        state.voice_client.stop()  # triggers the `after` callback -> plays next
        await interaction.response.send_message(f"Skipped **{skipped}**.")

    @app_commands.command(name="stop", description="Stop playback and clear the queue")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction):
        state = self._state(interaction.guild_id)
        state.clear()
        if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
            state.voice_client.stop()
        await interaction.response.send_message("Stopped and cleared the queue.")

    @app_commands.command(name="queue", description="Show the upcoming songs")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction):
        state = self._state(interaction.guild_id)
        lines = []
        if state.current:
            lines.append(f"**Now playing:** {state.current.title} ({state.current.formatted_duration()})")
        else:
            lines.append("**Now playing:** nothing")

        if state.queue:
            lines.append("")
            lines.append("**Up next:**")
            for i, track in enumerate(state.queue, start=1):
                lines.append(f"{i}. {track.title} ({track.formatted_duration()}) — requested by {track.requested_by}")
        else:
            lines.append("The queue is empty.")

        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="nowplaying", description="Show the currently playing track")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction):
        state = self._state(interaction.guild_id)
        if not state.current:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        t = state.current
        embed = discord.Embed(title=t.title, url=t.webpage_url, description=f"Requested by {t.requested_by}")
        embed.add_field(name="Duration", value=t.formatted_duration())
        embed.add_field(name="Source", value=t.source.title())
        if t.thumbnail:
            embed.set_thumbnail(url=t.thumbnail)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="volume", description="Set the playback volume (0-200%)")
    @app_commands.describe(level="Volume percentage from 0 to 200")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 200]):
        state = self._state(interaction.guild_id)
        state.volume = level / 100
        if state.voice_client and state.voice_client.source and isinstance(
            state.voice_client.source, discord.PCMVolumeTransformer
        ):
            state.voice_client.source.volume = state.volume
        await interaction.response.send_message(f"Volume set to {level}%.")

    @app_commands.command(name="lyrics", description="Show lyrics for the current song or a search")
    @app_commands.describe(query="Optional: 'Artist - Title' to look up. Defaults to the currently playing track.")
    @app_commands.guild_only()
    async def lyrics(self, interaction: discord.Interaction, query: str | None = None):
        await interaction.response.defer()
        state = self._state(interaction.guild_id)

        if query:
            artist, title = split_artist_title(query)
        elif state.current:
            artist, title = split_artist_title(state.current.title)
        else:
            await interaction.followup.send(
                "Nothing is playing, and no search was given. Try `/lyrics Artist - Title`."
            )
            return

        if not artist:
            await interaction.followup.send(
                f"Couldn't tell the artist from **{title}**. Try `/lyrics Artist - Title` explicitly."
            )
            return

        try:
            lyrics_text = await fetch_lyrics(artist, title)
        except LyricsNotFoundError as exc:
            await interaction.followup.send(str(exc))
            return

        header = f"**{title}** \u2014 {artist}\n\n"
        full_text = header + lyrics_text
        # Discord messages are capped at 2000 characters; split into pages.
        pages = [full_text[i : i + 1900] for i in range(0, len(full_text), 1900)]
        await interaction.followup.send(pages[0])
        for page in pages[1:]:
            await interaction.followup.send(page)

    @app_commands.command(name="bassboost", description="Set the bass boost level (great for heavy/loud tracks)")
    @app_commands.describe(level="Bass boost intensity")
    @app_commands.choices(level=BASS_CHOICES)
    @app_commands.guild_only()
    async def bassboost(self, interaction: discord.Interaction, level: app_commands.Choice[str]):
        state = self._state(interaction.guild_id)
        state.bass_level = level.value

        currently_playing = state.voice_client and (
            state.voice_client.is_playing() or state.voice_client.is_paused()
        )
        if currently_playing and state.current:
            # Restart the current track with the new filter applied. This
            # restarts from the beginning — FFmpeg can't resume mid-stream
            # once the filter graph changes.
            state.queue.appendleft(state.current)
            state.voice_client.stop()  # triggers `after` -> plays the requeued track
            await interaction.response.send_message(
                f"Bass boost set to **{level.name}**. Restarting the current track from the "
                "top to apply it."
            )
        else:
            await interaction.response.send_message(
                f"Bass boost set to **{level.name}**. It'll apply to the next track played."
            )

    # ---------- manual bot appearance controls (owner/admin only) ----------

    @app_commands.command(name="setnickname", description="[Admin] Change the bot's nickname in this server")
    @app_commands.describe(name="New nickname, or leave empty to reset to the default")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def setnickname(self, interaction: discord.Interaction, name: str | None = None):
        await self._update_nickname(interaction.guild, name)
        if name:
            await interaction.response.send_message(f"Nickname set to **{name}**.", ephemeral=True)
        else:
            await interaction.response.send_message("Nickname reset to default.", ephemeral=True)

    @app_commands.command(name="setstatus", description="[Admin] Change the bot's activity status")
    @app_commands.describe(type="Activity type shown before the text", text="Status text, e.g. a custom message")
    @app_commands.choices(type=ACTIVITY_TYPE_CHOICES)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def setstatus(self, interaction: discord.Interaction, type: app_commands.Choice[str], text: str):
        await self._update_presence(text, _ACTIVITY_TYPE_MAP[type.value])
        await interaction.response.send_message(
            f"Status set to **{type.name} {text}**. (Note: this is shared across every server the bot is in.)",
            ephemeral=True,
        )

    @app_commands.command(name="setavatar", description="[Admin] Change the bot's profile picture")
    @app_commands.describe(image_url="Direct URL to an image (png/jpg), e.g. from Discord's own CDN")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def setavatar(self, interaction: discord.Interaction, image_url: str):
        await interaction.response.defer(ephemeral=True)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        await interaction.followup.send(f"Couldn't download that image (HTTP {resp.status}).")
                        return
                    image_bytes = await resp.read()
        except aiohttp.ClientError as exc:
            await interaction.followup.send(f"Couldn't download that image: {exc}")
            return

        try:
            await self.bot.user.edit(avatar=image_bytes)
        except discord.HTTPException as exc:
            # Discord allows avatar changes only ~2 times per hour — this is
            # the most common failure reason here.
            await interaction.followup.send(
                f"Couldn't update the avatar: {exc}. Discord limits avatar changes to "
                "about twice per hour, so this may just need a bit more time."
            )
            return

        await interaction.followup.send("Profile picture updated.")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        """Keep the bot 'planted' in its voice room. If it gets disconnected
        unexpectedly (network blip, accidental kick, server restart, etc.)
        it rejoins the same channel and resumes the interrupted track,
        instead of silently leaving. Intentional disconnects via /leave set
        `expected_disconnect` beforehand so we don't fight those."""
        if member.id != self.bot.user.id or member.guild is None:
            return
        if before.channel is None or after.channel is not None:
            return  # not a "left a channel" event

        guild = member.guild
        state = self.manager.get(guild.id)

        if state.expected_disconnect:
            state.expected_disconnect = False
            return

        log.warning("Unexpected voice disconnect in guild %s — attempting to rejoin.", guild.id)
        channel = before.channel
        state.voice_client = None
        await asyncio.sleep(3)  # brief backoff before rejoining
        try:
            voice_client = await channel.connect()
        except discord.ClientException:
            log.warning("Auto-reconnect failed for guild %s", guild.id)
            state.clear()
            await self._update_presence(None)
            await self._update_nickname(guild, None)
            return

        state.voice_client = voice_client
        if state.current is not None:
            # Re-queue the interrupted track so it plays again from the top
            # (FFmpeg can't resume mid-stream after a full reconnect).
            state.queue.appendleft(state.current)
            state.current = None
        await self._advance_queue(guild)


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))