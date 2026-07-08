---
name: Discord.py voice requires davey
description: discord.py 2.7+ VoiceClient raises RuntimeError unless the davey package is installed, in addition to PyNaCl.
---

In discord.py 2.7+, `VoiceClient.__init__` unconditionally checks for both
`PyNaCl` and a package providing DAVE (Discord's E2EE voice protocol) support.
If the DAVE package is missing, connecting to a voice channel raises
`RuntimeError('davey library needed in order to use voice')` even though the
bot logs in and syncs commands fine otherwise — the failure only surfaces the
first time a voice command (`/join`, `/play`) is used.

**Why:** discord.py silently logs a warning at startup ("davey is not
installed, voice will NOT be supported") but does not fail fast, so this is
easy to miss until a user actually tries to play audio.

**How to apply:** When setting up any discord.py bot with voice/music
features, install both `PyNaCl` and `davey` (PyPI package name `davey`) up
front. Verify by restarting the bot workflow and confirming the "davey is not
installed" warning is absent from startup logs.
