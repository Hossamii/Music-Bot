"""
Text commands for the music bot: join/leave, play/pause/resume/skip/stop,
queue display, volume control, bass boost, and admin appearance/presence
controls. All music playback logic and error handling for a single guild
lives in this cog.

These are plain prefix-less text commands (e.g. typing "play believer" in
any channel), NOT Discord slash commands — see bot.py where
command_prefix="" is set.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp
import discord
from discord.ext import commands

from utils.queue_manager import GuildMusicState, MusicManager, Track
from utils.ytdl_source import TrackUnavailableError, YTDLSource

log = logging.getLogger("music.cog")

# FFmpeg `-af` equalizer filter graphs for the bassboost command. Boosts low
# frequencies (~60Hz and ~150Hz) at increasing gain per level — good for
# heavy/loud tracks.
BASS_PRESETS: dict[str, str] = {
    "off": "",
    "low": "equalizer=f=60:width_type=o:width=2:g=4,equalizer=f=150:width_type=o:width=2:g=2",
    "medium": "equalizer=f=60:width_type=o:width=2:g=8,equalizer=f=150:width_type=o:width=2:g=4",
    "high": "equalizer=f=60:width_type=o:width=2:g=12,equalizer=f=150:width_type=o:width=2:g=6",
    "extreme": "equalizer=f=60:width_type=o:width=2:g=18,equalizer=f=150:width_type=o:width=2:g=9",
}
BASS_LEVELS = tuple(BASS_PRESETS.keys())

ACTIVITY_TYPES: dict[str, discord.ActivityType] = {
    "playing": discord.ActivityType.playing,
    "listening": discord.ActivityType.listening,
    "watching": discord.ActivityType.watching,
    "competing": discord.ActivityType.competing,
}

# discord.py's Status enum — this is the colored dot next to the bot's name
# (separate from ACTIVITY_TYPES above, which is the "Listening to ..." text).
# Discord has no real settable "offline" state for a logged-in bot; the
# closest equivalent is `invisible`, which makes it appear offline to
# members while staying connected.
STATUS_PRESETS: dict[str, discord.Status] = {
    "online": discord.Status.online,
    "idle": discord.Status.idle,
    "dnd": discord.Status.dnd,
    "offline": discord.Status.invisible,
}


class Music(commands.Cog):
    """Music playback commands, invoked as plain text (no prefix, no slash).
    All commands are decorated with @commands.guild_only() since playback
    doesn't make sense in DMs and ctx.author.voice would be None there."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.manager = MusicManager()

    # ---------- helpers ----------

    def _state(self, guild_id: int) -> GuildMusicState:
        return self.manager.get(guild_id)

    async def _ensure_voice(self, ctx: commands.Context) -> discord.VoiceClient | None:
        """Ensure the bot is connected to the author's voice channel. Returns
        the voice client, or None (after sending an error) if it can't join."""
        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send("You need to be in a voice channel first.")
            return None

        channel = ctx.author.voice.channel
        state = self._state(ctx.guild.id)

        if state.voice_client and state.voice_client.is_connected():
            if state.voice_client.channel.id != channel.id:
                await state.voice_client.move_to(channel)
            return state.voice_client

        try:
            voice_client = await channel.connect()
        except discord.ClientException as exc:
            await ctx.send(f"Couldn't join the voice channel: {exc}")
            return None

        state.voice_client = voice_client
        state.text_channel = ctx.channel
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
        """Update the bot's global activity text (e.g. "Listening to ...").
        Note: Discord bots only have ONE presence shared across every server
        they're in — this isn't a per-guild setting (that's a platform
        limitation). Re-sends the current status (online/idle/dnd/offline)
        alongside it, so this never silently resets a status set via
        `setpresence` back to the online default."""
        activity = discord.Activity(type=activity_type, name=text or "play <song>")
        try:
            await self.bot.change_presence(status=self.bot.status, activity=activity)
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

    # ---------- commands (plain text, no prefix, no slash) ----------

    @commands.command(name="join")
    @commands.guild_only()
    async def join(self, ctx: commands.Context):
        voice_client = await self._ensure_voice(ctx)
        if voice_client is None:
            return
        await ctx.send(f"Joined **{voice_client.channel.name}**.")

    @commands.command(name="leave")
    @commands.guild_only()
    async def leave(self, ctx: commands.Context):
        state = self._state(ctx.guild.id)
        if state.voice_client is None or not state.voice_client.is_connected():
            await ctx.send("I'm not in a voice channel.")
            return
        # Mark this as an intentional disconnect so on_voice_state_update
        # doesn't try to auto-reconnect (the bot normally stays in the
        # room and only leaves when explicitly told to via "leave").
        state.expected_disconnect = True
        state.clear()
        await state.voice_client.disconnect()
        state.voice_client = None
        await ctx.send("Disconnected and cleared the queue.")

    @commands.command(name="play")
    @commands.guild_only()
    async def play(self, ctx: commands.Context, *, query: str = None):
        if not query:
            await ctx.send("Tell me what to play, e.g. `play believer imagine dragons`.")
            return

        source = "youtube"
        lowered = query.lower()
        if lowered.startswith("sc "):
            source, query = "soundcloud", query[3:].strip()
        elif lowered.startswith("soundcloud "):
            source, query = "soundcloud", query[len("soundcloud "):].strip()

        voice_client = await self._ensure_voice(ctx)
        if voice_client is None:
            return

        state = self._state(ctx.guild.id)
        state.text_channel = ctx.channel

        try:
            track = await YTDLSource.resolve(
                query,
                requested_by=str(ctx.author.display_name),
                source=source,
                loop=self.bot.loop,
            )
        except TrackUnavailableError as exc:
            await ctx.send(f"Couldn't play that: {exc}")
            return
        except Exception:  # noqa: BLE001
            log.exception("Unexpected error resolving track")
            await ctx.send("Something went wrong looking that up. Please try a different search or URL.")
            return

        already_playing = state.is_playing()
        position = state.add(track)

        if already_playing:
            await ctx.send(f"Queued **{track.title}** ({track.formatted_duration()}) — position {position}.")
        else:
            await ctx.send(f"Loading **{track.title}**...")
            await self._advance_queue(ctx.guild)

    @commands.command(name="pause")
    @commands.guild_only()
    async def pause(self, ctx: commands.Context):
        state = self._state(ctx.guild.id)
        if not state.voice_client or not state.voice_client.is_playing():
            await ctx.send("Nothing is playing.")
            return
        state.voice_client.pause()
        await ctx.send("Paused.")

    @commands.command(name="resume")
    @commands.guild_only()
    async def resume(self, ctx: commands.Context):
        state = self._state(ctx.guild.id)
        if not state.voice_client or not state.voice_client.is_paused():
            await ctx.send("Nothing is paused.")
            return
        state.voice_client.resume()
        await ctx.send("Resumed.")

    @commands.command(name="skip")
    @commands.guild_only()
    async def skip(self, ctx: commands.Context):
        state = self._state(ctx.guild.id)
        if not state.voice_client or not (state.voice_client.is_playing() or state.voice_client.is_paused()):
            await ctx.send("Nothing is playing.")
            return
        skipped = state.current.title if state.current else "current track"
        state.voice_client.stop()  # triggers the `after` callback -> plays next
        await ctx.send(f"Skipped **{skipped}**.")

    @commands.command(name="stop")
    @commands.guild_only()
    async def stop(self, ctx: commands.Context):
        state = self._state(ctx.guild.id)
        state.clear()
        if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
            state.voice_client.stop()
        await ctx.send("Stopped and cleared the queue.")

    @commands.command(name="queue")
    @commands.guild_only()
    async def queue(self, ctx: commands.Context):
        state = self._state(ctx.guild.id)
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

        await ctx.send("\n".join(lines))

    @commands.command(name="nowplaying", aliases=["np"])
    @commands.guild_only()
    async def nowplaying(self, ctx: commands.Context):
        state = self._state(ctx.guild.id)
        if not state.current:
            await ctx.send("Nothing is playing.")
            return
        t = state.current
        embed = discord.Embed(title=t.title, url=t.webpage_url, description=f"Requested by {t.requested_by}")
        embed.add_field(name="Duration", value=t.formatted_duration())
        embed.add_field(name="Source", value=t.source.title())
        if t.thumbnail:
            embed.set_thumbnail(url=t.thumbnail)
        await ctx.send(embed=embed)

    @commands.command(name="volume")
    @commands.guild_only()
    async def volume(self, ctx: commands.Context, level: int = None):
        if level is None or not (0 <= level <= 200):
            await ctx.send("Usage: `volume <0-200>`")
            return
        state = self._state(ctx.guild.id)
        state.volume = level / 100
        if state.voice_client and state.voice_client.source and isinstance(
            state.voice_client.source, discord.PCMVolumeTransformer
        ):
            state.voice_client.source.volume = state.volume
        await ctx.send(f"Volume set to {level}%.")

    @commands.command(name="bassboost")
    @commands.guild_only()
    async def bassboost(self, ctx: commands.Context, level: str = None):
        if level is None or level.lower() not in BASS_LEVELS:
            await ctx.send(f"Usage: `bassboost <{'/'.join(BASS_LEVELS)}>`")
            return
        level = level.lower()
        state = self._state(ctx.guild.id)
        state.bass_level = level

        currently_playing = state.voice_client and (
            state.voice_client.is_playing() or state.voice_client.is_paused()
        )
        if currently_playing and state.current:
            # Restart the current track with the new filter applied. This
            # restarts from the beginning — FFmpeg can't resume mid-stream
            # once the filter graph changes.
            state.queue.appendleft(state.current)
            state.voice_client.stop()  # triggers `after` -> plays the requeued track
            await ctx.send(f"Bass boost set to **{level}**. Restarting the current track from the top to apply it.")
        else:
            await ctx.send(f"Bass boost set to **{level}**. It'll apply to the next track played.")

    # ---------- manual bot appearance controls (admin only) ----------

    @commands.command(name="setnickname")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def setnickname(self, ctx: commands.Context, *, name: str = None):
        await self._update_nickname(ctx.guild, name)
        if name:
            await ctx.send(f"Nickname set to **{name}**.")
        else:
            await ctx.send("Nickname reset to default.")

    @commands.command(name="setstatus")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def setstatus(self, ctx: commands.Context, activity_type: str = None, *, text: str = None):
        if activity_type is None or activity_type.lower() not in ACTIVITY_TYPES or not text:
            await ctx.send(f"Usage: `setstatus <{'/'.join(ACTIVITY_TYPES)}> <text>`")
            return
        activity_type = activity_type.lower()
        await self._update_presence(text, ACTIVITY_TYPES[activity_type])
        await ctx.send(f"Status set to **{activity_type} {text}**. (Shared across every server the bot is in.)")

    @commands.command(name="setpresence")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def setpresence(self, ctx: commands.Context, state: str = None):
        """Controls the colored presence dot (online/idle/dnd/offline) —
        different from `setstatus`, which controls the "Listening to ..."
        activity text next to it."""
        if state is None or state.lower() not in STATUS_PRESETS:
            await ctx.send(f"Usage: `setpresence <{'/'.join(STATUS_PRESETS)}>`")
            return
        state = state.lower()
        try:
            await self.bot.change_presence(status=STATUS_PRESETS[state], activity=self.bot.activity)
        except discord.HTTPException:
            log.warning("Failed to update bot presence status")
            await ctx.send("Couldn't update the status right now. Try again in a bit.")
            return
        note = " (Discord has no true offline state for bots — this makes it appear offline.)" if state == "offline" else ""
        await ctx.send(f"Bot presence set to **{state}**.{note} (Shared across every server the bot is in.)")

    @commands.command(name="setavatar")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def setavatar(self, ctx: commands.Context, image_url: str = None):
        if not image_url:
            await ctx.send("Usage: `setavatar <direct image URL>`")
            return
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        await ctx.send(f"Couldn't download that image (HTTP {resp.status}).")
                        return
                    image_bytes = await resp.read()
        except aiohttp.ClientError as exc:
            await ctx.send(f"Couldn't download that image: {exc}")
            return

        try:
            await self.bot.user.edit(avatar=image_bytes)
        except discord.HTTPException as exc:
            # Discord allows avatar changes only ~2 times per hour — this is
            # the most common failure reason here.
            await ctx.send(
                f"Couldn't update the avatar: {exc}. Discord limits avatar changes to "
                "about twice per hour, so this may just need a bit more time."
            )
            return

        await ctx.send("Profile picture updated.")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        """Keep the bot 'planted' in its voice room. If it gets disconnected
        unexpectedly (network blip, accidental kick, server restart, etc.)
        it rejoins the same channel and resumes the interrupted track,
        instead of silently leaving. Intentional disconnects via "leave" set
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