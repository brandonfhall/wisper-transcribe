"""
Phase 0 spike — Pycord per-user voice receive under DAVE/E2EE.

This script is THROWAWAY. It is not imported by any production code and will
be deleted after the acceptance gate is recorded in plan.md.

Usage:
    DISCORD_BOT_TOKEN=<token> \
    DISCORD_GUILD_ID=<guild_id> \
    DISCORD_CHANNEL_ID=<voice_channel_id> \
    python scripts/spike_voice_receive.py

The script joins the given voice channel, records for RECORD_SECONDS seconds
(default 60), then writes per-user .ogg files to scripts/spike_output/ and exits.

Acceptance gates (record the outcome in plan.md):
  A — stable py-cord works: per-user .ogg files have audible audio.
  B — only master works: stable fails with 4017; git+master succeeds.
  C — nothing works: both fail with 4017 or produce silent/corrupt files.
"""

import asyncio
import os
import sys
from pathlib import Path

try:
    import discord
    from discord.sinks import OggOpusSink
except ImportError:
    print("ERROR: py-cord not installed. Run: .venv/bin/pip install -e '.[dev]'")
    sys.exit(1)

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0"))
CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", "0"))
RECORD_SECONDS = int(os.environ.get("RECORD_SECONDS", "60"))
OUTPUT_DIR = Path(__file__).parent / "spike_output"

if not TOKEN or not GUILD_ID or not CHANNEL_ID:
    print("ERROR: set DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, DISCORD_CHANNEL_ID")
    sys.exit(1)


async def finished_callback(sink: OggOpusSink, channel, *args):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if not sink.audio_data:
        print("WARNING: sink.audio_data is empty — no audio received.")
        print("  This may indicate DAVE/E2EE is blocking per-user receive (gate C).")
        await channel.send("Spike done — no audio data received (possible DAVE block).")
        return

    for user_id, audio in sink.audio_data.items():
        out_path = OUTPUT_DIR / f"{user_id}.ogg"
        with open(out_path, "wb") as f:
            f.write(audio.file.read())
        print(f"  Wrote {out_path} ({out_path.stat().st_size} bytes)")

    print(f"\nDone. {len(sink.audio_data)} speaker(s) captured.")
    print(f"Check files in {OUTPUT_DIR} — play with: ffplay {OUTPUT_DIR}/<user_id>.ogg")


intents = discord.Intents.default()  # guilds + voice_states — no privileged intents needed
bot = discord.Bot(intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id {bot.user.id})")
    print(f"Pycord version: {discord.__version__}")

    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        print(f"ERROR: guild {GUILD_ID} not found. Is the bot invited to this server?")
        await bot.close()
        return

    channel = guild.get_channel(CHANNEL_ID)
    if channel is None:
        print(f"ERROR: channel {CHANNEL_ID} not found in guild {guild.name}.")
        await bot.close()
        return

    if not isinstance(channel, discord.VoiceChannel):
        print(f"ERROR: channel '{channel.name}' (id {CHANNEL_ID}) is not a voice channel.")
        print("  Provide a VOICE channel ID, not a text channel.")
        await bot.close()
        return

    print(f"Joining voice channel: {channel.name} in {guild.name}")
    try:
        vc = await channel.connect()
    except discord.errors.ConnectionClosed as e:
        print(f"ERROR: connection closed on join — code {e.code}")
        if e.code == 4017:
            print("  Close code 4017 = DAVE/E2EE required. This is gate C.")
            print("  Try installing from master: pip install git+https://github.com/Pycord-Development/pycord@master#egg=py-cord[voice]")
        await bot.close()
        return

    print(f"Connected. Recording for {RECORD_SECONDS}s — speak into the channel now...")
    vc.start_recording(OggOpusSink(), finished_callback, channel)

    await asyncio.sleep(RECORD_SECONDS)

    print("Stopping recording...")
    vc.stop_recording()  # triggers finished_callback
    await asyncio.sleep(2)  # let callback flush files

    await vc.disconnect()
    await bot.close()


bot.run(TOKEN)
