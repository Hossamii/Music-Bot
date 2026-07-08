"""
Entry point for the global Discord music bot.

Loads the bot token from the DISCORD_BOT_TOKEN secret, starts the keep-alive
Flask server, registers the music cog, syncs slash commands, and runs the
bot with UTF-8-safe logging so titles in any language/script display and log
correctly without crashing.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys

import discord
from discord.ext import commands

# Ensure stdout/stderr are UTF-8 so logging titles in any script (Arabic,
# Japanese, Hindi, etc.) never raises a UnicodeEncodeError on this console.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from keep_alive import keep_alive  # noqa: E402  (import after stdout patch)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("music.bot")

TOKEN = os.environ.get("DISCORD_BOT_TOKEN")

INTENTS = discord.Intents.default()
INTENTS.voice_states = True
INTENTS.guilds = True
INTENTS.message_content = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)


@bot.event
async def on_ready():
    log.info("Logged in as %s (id: %s)", bot.user, bot.user.id)
    try:
        synced = await bot.tree.sync()
        log.info("Synced %d slash command(s).", len(synced))
    except discord.HTTPException:
        log.exception("Failed to sync slash commands")
    activity = discord.Activity(type=discord.ActivityType.listening, name="/play")
    await bot.change_presence(activity=activity)


@bot.event
async def on_disconnect():
    log.warning("Bot disconnected from Discord gateway — will attempt to reconnect.")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
    log.error("Slash command error in /%s: %s", interaction.command.name if interaction.command else "?", error)
    message = "Something went wrong running that command. Please try again."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


async def main() -> None:
    if not TOKEN:
        log.error(
            "DISCORD_BOT_TOKEN is not set. Add it in Replit's Secrets tool "
            "(see README.md for step-by-step instructions) and restart the bot."
        )
        return

    keep_alive()

    async with bot:
        await bot.load_extension("cogs.music")
        try:
            await bot.start(TOKEN)
        except discord.LoginFailure:
            log.error("Login failed — DISCORD_BOT_TOKEN is invalid. Double-check the secret value.")
        except discord.HTTPException as exc:
            log.error("Discord connection error: %s", exc)


if __name__ == "__main__":
    # Make sure imports resolve relative to this file regardless of cwd.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    asyncio.run(main())
