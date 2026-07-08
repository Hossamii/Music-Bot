# Global Discord Music Bot

A Discord music bot built with `discord.py` that can search and play music from
YouTube and SoundCloud — including tracks in any language or script (Arabic,
Japanese, Hindi, Spanish, Korean, etc.) — with a full queue, volume control,
and 24/7 uptime support on Replit.

## Features

- `/join` — joins your current voice channel
- `/leave` — disconnects and clears the queue
- `/play <query or URL>` — searches YouTube (default) or SoundCloud, or plays
  a direct URL. Add to the queue if something is already playing.
- `/pause`, `/resume` — pause/resume playback
- `/skip` — skip the current track
- `/stop` — stop playback and clear the queue
- `/queue` — show the upcoming songs
- `/volume <0-200>` — adjust playback volume
- `/nowplaying` — show the currently playing track

## File structure

```
discord-music-bot/
├── bot.py              # Entry point — loads env, starts the bot + keep_alive
├── keep_alive.py        # Flask server to keep the Repl alive 24/7
├── requirements.txt      # Python dependencies (for reference; already installed)
├── cogs/
│   └── music.py          # All music commands (slash commands) and queue logic
├── utils/
│   ├── ytdl_source.py     # yt-dlp/FFmpeg audio source wrapper (YouTube + SoundCloud)
│   └── queue_manager.py   # Per-guild song queue and playback state
└── .env.example          # Documents which secret to set (do NOT put real tokens here)
```

## Step-by-step setup

1. **Create the Discord application & bot**
   - Go to https://discord.com/developers/applications and click **New Application**.
   - Open the **Bot** tab, click **Add Bot**.
   - Under **Privileged Gateway Intents**, enable **Message Content Intent** (not strictly
     required for slash commands, but harmless to enable).
   - Click **Reset Token** / **Copy** to get your bot token.

2. **Invite the bot to your server**
   - Go to **OAuth2 → URL Generator**.
   - Scopes: check `bot` and `applications.commands`.
   - Bot permissions: `Connect`, `Speak`, `Send Messages`, `Read Message History`, `Use Slash Commands`.
   - Open the generated URL and add the bot to your server.

3. **Add your bot token to Replit Secrets**
   - In this Repl, open the **Secrets** tool (lock icon in the left sidebar), or the
     Secrets tab in your workspace.
   - Add a new secret named exactly `DISCORD_BOT_TOKEN` and paste your bot token as the value.
   - Never paste the token directly into code or commit it to a file.

4. **Run the bot**
   - The workflow named `Discord Music Bot` runs `python discord-music-bot/bot.py`.
   - Start it from the Replit workflow panel (or ask the agent to restart it).
   - On first run, the bot registers slash commands with Discord — this can take up to a
     minute to propagate. Restart Discord (Ctrl+R) if commands don't show up immediately.

5. **Keep it alive 24/7**
   - `keep_alive.py` starts a tiny Flask web server that responds `Bot is alive!`.
   - If you deploy this Repl (Reserved VM / Autoscale deployment isn't required for a bot,
     but a simple "Always On"-style deployment keeps the process running continuously),
     use an uptime pinger (e.g. UptimeRobot) pointed at the Repl's web URL to prevent the
     free workspace from sleeping. On a paid always-on deployment this isn't necessary.

6. **Use it**
   - Join a voice channel in your server.
   - Run `/play believer imagine dragons` or `/play <YouTube/SoundCloud URL>`.
   - Use `/queue`, `/skip`, `/pause`, `/resume`, `/volume 80`, `/stop`, `/leave` as needed.

## Notes on global/international content

- `/play` defaults to searching YouTube (`ytsearch:`), which indexes music from every
  country and language. Add "soundcloud" as a source flag (`source: soundcloud` slash
  command option) to search SoundCloud instead.
- Non-Latin titles (Arabic, Japanese, Hindi, Korean, etc.) are handled safely — all file
  I/O and Discord messages use UTF-8, and yt-dlp is configured to preserve original title
  metadata instead of transliterating it.
- Age-restricted or geo-blocked videos are caught explicitly and reported back to the user
  as a friendly error (with a suggestion to try another source or query) instead of
  crashing the bot or hanging the voice connection.

## Troubleshooting

- **"Missing Permissions" errors**: re-invite the bot with the scopes/permissions in step 2.
- **No sound / bot joins but doesn't play**: check the workflow console logs — FFmpeg is
  pre-installed for you, but confirm `ffmpeg -version` works in the shell.
- **Slash commands don't appear**: wait ~1 minute after startup, or kick and re-invite the bot.
- **"Video unavailable in your region"**: the bot will report this in Discord instead of
  crashing; try a different search term or a mirror/upload of the same track.
