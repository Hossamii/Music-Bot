---
name: Non-artifact background services
description: How to run a long-lived process (e.g. a Discord bot) that doesn't fit any artifact type in this workspace.
---

Not every user request maps to an artifact type (`react-vite`, `expo`,
`slides`, `video-js`, `openscad`, `data-visualization`). Standalone bots,
scripts, or workers (e.g. a Discord bot) have no web/mobile preview and
should not be forced into `createArtifact`.

**Why:** `createArtifact` scaffolds a workspace package with a preview path
and proxy routing, which a headless process doesn't need or benefit from.

**How to apply:** Create the project as a plain directory in the repo root
(not under `artifacts/`), install its language runtime/packages via the
package-management skill, and bind it to a `configureWorkflow` call with an
appropriate `outputType` (`console` for a bot/backend with no HTTP UI to
click through). If it needs to stay alive on Replit's free tier, add a small
Flask/HTTP keep-alive server on an unused port so an external uptime pinger
can hit it — pick a port not already claimed by other workflows (check
running workflow logs first).
