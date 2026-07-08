"""
Slash commands for the music bot: join/leave, play/pause/resume/skip/stop,
queue display, and volume control. All music playback logic and error
handling for a single guild lives in this cog.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils.queue_manager import GuildMusicState, MusicManager, Track
from utils.ytdl_source import TrackUnavailableError, YTDLSource

log = logging.getLogger("music.cog")

SOURCE_CHOICES = [
    app_commands.Choice(name="YouTube", value="youtube"),
    app_commands.Choice(name="SoundCloud", value="soundcloud"),
]


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
                    source = YTDLSource.build_audio_source(stream_url, state.volume)
                except TrackUnavailableError as exc:
                    await self._notify(state, f"Skipping **{next_track.title}** — {exc}")
                    continue
                except Exception:  # noqa: BLE001 - report unexpected errors, never crash the bot
                    log.exception("Unexpected playback error")
                    await self._notify(state, f"Skipping **{next_track.title}** — unexpected playback error.")
                    continue

                def _after(error: Exception | None) -> None:
                    if error:
                        log.error("Playback error: %s", error)
                    self._play_next(guild)

                try:
                    state.voice_client.play(source, after=_after)
                except discord.ClientException:
                    # Something else started playback concurrently; don't double-start.
                    log.warning("play() rejected — playback already in progress for guild %s", guild.id)
                    return
                await self._notify(state, f"Now playing: **{next_track.title}** ({next_track.formatted_duration()})")
                return

    async def _notify(self, state: GuildMusicState, message: str) -> None:
        if state.text_channel is not None:
            try:
                await state.text_channel.send(message)
            except discord.HTTPException:
                log.warning("Failed to send notification message")

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
        state.clear()
        await state.voice_client.disconnect()
        state.voice_client = None
        self.manager.remove(interaction.guild_id)
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

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        """Handle unexpected disconnects (e.g. connection drops, kicked from
        channel) by cleaning up state instead of leaving it dangling."""
        if member.id != self.bot.user.id:
            return
        if before.channel is not None and after.channel is None:
            state = self.manager.get(member.guild.id) if member.guild else None
            if state:
                state.clear()
                state.voice_client = None


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
