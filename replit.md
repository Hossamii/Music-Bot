# Global Discord Music Bot

A Discord bot (`discord-music-bot/`) that searches and plays music from YouTube and
SoundCloud — any language/region — with a queue, volume control, and slash commands.
This standalone Python bot lives alongside the pnpm workspace's web artifacts but is
not itself a workspace artifact.

## Run & Operate

- Workflow `Discord Music Bot` runs `cd discord-music-bot && python bot.py`
- Required secret: `DISCORD_BOT_TOKEN` — Discord bot token (Secrets tool)
- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)
- Discord bot: Python 3.12, discord.py 2.7 (+ `davey` for voice DAVE protocol support), yt-dlp, FFmpeg, Flask keep-alive

## Where things live

- `discord-music-bot/` — the Discord music bot (see its own README.md for setup and usage)
- See `discord-music-bot/README.md` for the bot's file structure, setup steps, and troubleshooting

## Architecture decisions

- The Discord bot is a standalone Python process (own workflow, own Python env under `.pythonlibs`), not a pnpm workspace artifact — it has no web preview.
- `discord.py` 2.7+ raises at voice-connect time unless the `davey` package (DAVE E2EE protocol) is installed, in addition to `PyNaCl` — always keep both installed for voice to work.
- Playback advance in the music bot is fully serialized per guild through a single `asyncio.Lock`, iterating (not recursing) over unavailable tracks to avoid deadlocks; concurrent `/play` calls are safe because `_advance_queue` no-ops if playback already started.

## Product

- Discord slash-command music bot: `/join`, `/leave`, `/play`, `/pause`, `/resume`, `/skip`, `/stop`, `/queue`, `/nowplaying`, `/volume`. Searches YouTube or SoundCloud for tracks in any language/region, or plays a direct URL.

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

- Discord bot: DAVE (`davey`) package must be installed alongside `PyNaCl` or voice connections fail with `RuntimeError`.
- Discord bot: the Flask keep-alive server binds to port 8000 (configurable via `KEEP_ALIVE_PORT`) — do not reuse that port for another workflow.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
