"""
Discord MCP server.

Exposes Discord server-ops as MCP tools across three areas:
  - Interaction     : list_guilds, list_channels, read_messages, send_message, get_member
  - Channels        : create_channel, create_category, rename_channel, delete_channel, set_slowmode
  - Moderation      : kick_member, ban_member, timeout_member, purge_messages,
                      scan_for_scammers (dry-run), purge_scammers (acts only with confirm=true)

Runs a discord.py gateway client in the background; tools call into it. Designed
for stdio transport (e.g. Claude Code / Claude Desktop).

Env:
  DISCORD_BOT_TOKEN   (required)  bot token from the Developer Portal
  DISCORD_GUILD_ID    (optional)  default server id, so tools don't need guild_id each call

Safety posture: read/scan tools never modify the server. Destructive tools act
immediately when called, EXCEPT bulk scammer removal (purge_scammers) which is a
no-op unless confirm=true — so an accidental call won't ban anyone.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import discord
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from scam_heuristics import (
    DEFAULT_THRESHOLD,
    MemberSignals,
    score_member,
)

load_dotenv()

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
DEFAULT_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "").strip()

# --- Discord client ----------------------------------------------------------

intents = discord.Intents.default()
intents.members = True          # needed to read/fetch members for moderation
intents.message_content = True  # needed to scan message text for scams
intents.guilds = True

client = discord.Client(intents=intents)
_ready = asyncio.Event()


@client.event
async def on_ready():
    _ready.set()


async def _ensure_ready():
    if not _ready.is_set():
        await asyncio.wait_for(_ready.wait(), timeout=30)


def _resolve_guild(guild_id: Optional[Union[str, int]]) -> discord.Guild:
    gid = guild_id or DEFAULT_GUILD_ID
    if not gid:
        guilds = ", ".join(f"{g.name}={g.id}" for g in client.guilds) or "(none)"
        raise ValueError(
            f"No guild_id given and DISCORD_GUILD_ID not set. Bot is in: {guilds}"
        )
    guild = client.get_guild(int(gid))
    if guild is None:
        raise ValueError(f"Bot is not in guild {gid} (or it isn't cached yet).")
    return guild


def _chan_type(ch) -> str:
    return type(ch).__name__.replace("Channel", "").lower() or "unknown"


# --- MCP server with lifespan that boots the gateway client ------------------


@asynccontextmanager
async def lifespan(_server):
    if not TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set. Copy .env.example to .env.")
    task = asyncio.create_task(client.start(TOKEN))
    try:
        await _ensure_ready()
        yield {}
    finally:
        await client.close()
        task.cancel()


mcp = FastMCP("discord", lifespan=lifespan)


# ============================================================================
# Interaction
# ============================================================================


@mcp.tool()
async def list_guilds() -> list[dict]:
    """List every Discord server (guild) the bot is a member of."""
    await _ensure_ready()
    return [
        {"id": str(g.id), "name": g.name, "member_count": g.member_count}
        for g in client.guilds
    ]


@mcp.tool()
async def list_channels(guild_id: Optional[str] = None) -> list[dict]:
    """List channels in a guild (id, name, type, category, slowmode)."""
    await _ensure_ready()
    guild = _resolve_guild(guild_id)
    out = []
    for ch in guild.channels:
        out.append(
            {
                "id": str(ch.id),
                "name": ch.name,
                "type": _chan_type(ch),
                "category": ch.category.name if getattr(ch, "category", None) else None,
                "slowmode": getattr(ch, "slowmode_delay", None),
            }
        )
    return out


@mcp.tool()
async def read_messages(channel_id: str, limit: int = 30) -> list[dict]:
    """Read the most recent messages from a channel (newest last). limit max 100."""
    await _ensure_ready()
    limit = max(1, min(int(limit), 100))
    ch = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
    msgs = [m async for m in ch.history(limit=limit)]
    msgs.reverse()
    return [
        {
            "id": str(m.id),
            "author": f"{m.author}",
            "author_id": str(m.author.id),
            "content": m.content,
            "created_at": m.created_at.isoformat(),
        }
        for m in msgs
    ]


@mcp.tool()
async def send_message(channel_id: str, content: str) -> str:
    """Send a text message to a channel."""
    await _ensure_ready()
    ch = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
    msg = await ch.send(content)
    return f"Sent message {msg.id} to #{ch.name}"


@mcp.tool()
async def get_member(user_id: str, guild_id: Optional[str] = None) -> dict:
    """Get details on a member: account age, join date, roles, avatar status."""
    await _ensure_ready()
    guild = _resolve_guild(guild_id)
    member = guild.get_member(int(user_id)) or await guild.fetch_member(int(user_id))
    now = datetime.now(timezone.utc)
    return {
        "id": str(member.id),
        "name": f"{member}",
        "display_name": member.display_name,
        "is_bot": member.bot,
        "account_created": member.created_at.isoformat(),
        "account_age_days": round((now - member.created_at).total_seconds() / 86400, 1),
        "joined_at": member.joined_at.isoformat() if member.joined_at else None,
        "roles": [r.name for r in member.roles if r.name != "@everyone"],
        "default_avatar": member.avatar is None,
    }


# ============================================================================
# Channel management
# ============================================================================


@mcp.tool()
async def create_channel(
    name: str,
    type: str = "text",
    category_id: Optional[str] = None,
    guild_id: Optional[str] = None,
) -> str:
    """Create a channel. type = 'text' or 'voice'. Optionally place under a category."""
    await _ensure_ready()
    guild = _resolve_guild(guild_id)
    category = guild.get_channel(int(category_id)) if category_id else None
    if type == "voice":
        ch = await guild.create_voice_channel(name, category=category)
    else:
        ch = await guild.create_text_channel(name, category=category)
    return f"Created {type} channel #{ch.name} ({ch.id})"


@mcp.tool()
async def create_category(name: str, guild_id: Optional[str] = None) -> str:
    """Create a category (channel group)."""
    await _ensure_ready()
    guild = _resolve_guild(guild_id)
    cat = await guild.create_category(name)
    return f"Created category {cat.name} ({cat.id})"


@mcp.tool()
async def rename_channel(channel_id: str, new_name: str) -> str:
    """Rename a channel."""
    await _ensure_ready()
    ch = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
    old = ch.name
    await ch.edit(name=new_name)
    return f"Renamed #{old} -> #{new_name}"


@mcp.tool()
async def delete_channel(channel_id: str, reason: str = "Deleted via MCP") -> str:
    """Delete a channel. Irreversible."""
    await _ensure_ready()
    ch = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
    name = ch.name
    await ch.delete(reason=reason)
    return f"Deleted channel #{name}"


@mcp.tool()
async def set_slowmode(channel_id: str, seconds: int) -> str:
    """Set per-user slowmode on a channel (0-21600 seconds). Great against raids."""
    await _ensure_ready()
    seconds = max(0, min(int(seconds), 21600))
    ch = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
    await ch.edit(slowmode_delay=seconds)
    return f"Set slowmode on #{ch.name} to {seconds}s"


# ============================================================================
# Moderation
# ============================================================================


@mcp.tool()
async def kick_member(user_id: str, reason: str = "Kicked via MCP", guild_id: Optional[str] = None) -> str:
    """Kick a member from the server."""
    await _ensure_ready()
    guild = _resolve_guild(guild_id)
    member = guild.get_member(int(user_id)) or await guild.fetch_member(int(user_id))
    await member.kick(reason=reason)
    return f"Kicked {member} ({member.id}) — {reason}"


@mcp.tool()
async def ban_member(
    user_id: str,
    reason: str = "Banned via MCP",
    delete_message_days: int = 1,
    guild_id: Optional[str] = None,
) -> str:
    """Ban a member and optionally delete their recent messages (0-7 days)."""
    await _ensure_ready()
    guild = _resolve_guild(guild_id)
    days = max(0, min(int(delete_message_days), 7))
    user = discord.Object(id=int(user_id))
    await guild.ban(user, reason=reason, delete_message_seconds=days * 86400)
    return f"Banned user {user_id} (deleted {days}d of messages) — {reason}"


@mcp.tool()
async def timeout_member(
    user_id: str,
    minutes: int,
    reason: str = "Timed out via MCP",
    guild_id: Optional[str] = None,
) -> str:
    """Time out (mute) a member for N minutes (max 40320 = 28 days)."""
    await _ensure_ready()
    guild = _resolve_guild(guild_id)
    minutes = max(1, min(int(minutes), 40320))
    member = guild.get_member(int(user_id)) or await guild.fetch_member(int(user_id))
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    await member.edit(timed_out_until=until, reason=reason)
    return f"Timed out {member} for {minutes} min — {reason}"


@mcp.tool()
async def purge_messages(
    channel_id: str,
    limit: int = 50,
    user_id: Optional[str] = None,
) -> str:
    """Bulk-delete recent messages in a channel (max 200). If user_id is given,
    only that user's messages are removed. Discord can only bulk-delete messages
    younger than 14 days."""
    await _ensure_ready()
    limit = max(1, min(int(limit), 200))
    ch = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
    uid = int(user_id) if user_id else None

    def check(m):
        return uid is None or m.author.id == uid

    deleted = await ch.purge(limit=limit, check=check)
    return f"Deleted {len(deleted)} messages in #{ch.name}"


async def _gather_member_signals(
    guild: discord.Guild,
    lookback_minutes: int,
    max_account_age_days: float,
    channel_scan_limit: int,
) -> dict[int, MemberSignals]:
    """Scan recent messages across text channels and build signals per author,
    limited to members whose accounts are younger than max_account_age_days (the
    population scammers come from)."""
    now = datetime.now(timezone.utc)
    after = now - timedelta(minutes=lookback_minutes)
    sigs: dict[int, MemberSignals] = {}

    for ch in guild.text_channels:
        perms = ch.permissions_for(guild.me)
        if not perms.read_message_history:
            continue
        try:
            async for m in ch.history(limit=channel_scan_limit, after=after):
                author = m.author
                if author.bot:
                    continue
                member = guild.get_member(author.id)
                # Only consider recently-created accounts to keep it cheap & precise.
                created = author.created_at
                age_days = (now - created).total_seconds() / 86400
                if age_days > max_account_age_days:
                    continue
                sig = sigs.get(author.id)
                if sig is None:
                    joined_min = None
                    role_count = 0
                    default_av = author.avatar is None
                    if member is not None and member.joined_at:
                        joined_min = (now - member.joined_at).total_seconds() / 60
                        role_count = len([r for r in member.roles if r.name != "@everyone"])
                        default_av = member.avatar is None
                    sig = MemberSignals(
                        user_id=author.id,
                        name=str(author),
                        account_age_days=age_days,
                        joined_age_minutes=joined_min,
                        has_default_avatar=default_av,
                        role_count=role_count,
                    )
                    sigs[author.id] = sig
                if m.content:
                    sig.messages.append(m.content)
                    sig.messages_by_channel[ch.id] = m.content
        except discord.Forbidden:
            continue
    return sigs


@mcp.tool()
async def scan_for_scammers(
    guild_id: Optional[str] = None,
    lookback_minutes: int = 60,
    min_score: int = DEFAULT_THRESHOLD,
    max_account_age_days: float = 30,
    channel_scan_limit: int = 80,
) -> dict:
    """DRY-RUN scam scan. Reads recent messages across channels, scores authors with
    young accounts using transparent heuristics (account age, links, invites, scam
    phrasing, cross-channel blasts, mass mentions), and returns suspects sorted by
    score. Takes NO action. Feed user_ids to purge_scammers / ban_member to act."""
    await _ensure_ready()
    guild = _resolve_guild(guild_id)
    sigs = await _gather_member_signals(
        guild, lookback_minutes, max_account_age_days, channel_scan_limit
    )
    scored = [score_member(s) for s in sigs.values()]
    suspects = sorted(
        (s for s in scored if s.score >= min_score),
        key=lambda s: s.score,
        reverse=True,
    )
    return {
        "scanned_members": len(sigs),
        "lookback_minutes": lookback_minutes,
        "min_score": min_score,
        "suspect_count": len(suspects),
        "suspects": [s.to_dict() for s in suspects],
        "note": "Dry run — nothing was changed. Use purge_scammers(confirm=true) or ban_member to act.",
    }


@mcp.tool()
async def purge_scammers(
    guild_id: Optional[str] = None,
    lookback_minutes: int = 60,
    min_score: int = DEFAULT_THRESHOLD,
    max_account_age_days: float = 30,
    action: str = "timeout",
    confirm: bool = False,
) -> dict:
    """Scan for scammers and ACT on them. action = 'timeout' | 'kick' | 'ban'.
    NO-OP unless confirm=true (so an accidental call changes nothing). Returns what
    it did / would do. Always run scan_for_scammers first to review the list."""
    await _ensure_ready()
    guild = _resolve_guild(guild_id)
    if action not in ("timeout", "kick", "ban"):
        raise ValueError("action must be 'timeout', 'kick', or 'ban'")

    sigs = await _gather_member_signals(
        guild, lookback_minutes, max_account_age_days, channel_scan_limit=80
    )
    suspects = sorted(
        (s for s in (score_member(x) for x in sigs.values()) if s.score >= min_score),
        key=lambda s: s.score,
        reverse=True,
    )

    if not confirm:
        return {
            "confirmed": False,
            "action": action,
            "would_affect": len(suspects),
            "suspects": [s.to_dict() for s in suspects],
            "note": "DRY RUN. Re-call with confirm=true to apply.",
        }

    results = []
    for s in suspects:
        reason = f"Auto-mod (score {s.score}): {'; '.join(s.reasons)[:400]}"
        try:
            if action == "ban":
                await guild.ban(
                    discord.Object(id=s.user_id), reason=reason, delete_message_seconds=86400
                )
            else:
                member = guild.get_member(s.user_id) or await guild.fetch_member(s.user_id)
                if action == "kick":
                    await member.kick(reason=reason)
                else:  # timeout
                    until = datetime.now(timezone.utc) + timedelta(hours=24)
                    await member.edit(timed_out_until=until, reason=reason)
            results.append({"user_id": str(s.user_id), "name": s.name, "score": s.score, "ok": True})
        except Exception as e:  # noqa: BLE001 - surface per-member failures
            results.append({"user_id": str(s.user_id), "name": s.name, "ok": False, "error": str(e)})

    return {
        "confirmed": True,
        "action": action,
        "affected": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok")),
        "results": results,
    }


if __name__ == "__main__":
    mcp.run()
