# Discord MCP

A local MCP server that lets Claude **interact with**, **manage**, and **moderate** your
Discord server — including detecting and removing scammers. Built on `discord.py`.

Same idea as the TradingView MCP, but easier: Discord has a real official Bot API,
so no screen-scraping/CDP bridge is needed.

## Tools

**Interaction**
- `list_guilds` — servers the bot is in
- `list_channels` — channels (id, type, category, slowmode)
- `read_messages` — recent messages in a channel
- `send_message` — post to a channel
- `get_member` — account age, join date, roles, avatar status

**Channels**
- `create_channel` (text/voice), `create_category`, `rename_channel`, `delete_channel`
- `set_slowmode` — throttle a channel (great during raids)

**Moderation**
- `kick_member`, `ban_member`, `timeout_member`, `purge_messages`
- `scan_for_scammers` — **dry-run** heuristic scan, returns scored suspects, changes nothing
- `purge_scammers` — acts on suspects; **no-op unless `confirm=true`**

### How scam detection works
`scan_for_scammers` reads recent messages across channels, then scores authors with
young accounts on transparent signals (each adds points + a reason):
account age, recent join, default avatar, Discord invite links, external links,
phishing keyword + link combos, known scam phrasing (free nitro / airdrop / wallet
verify / crypto giveaway…), `@everyone` abuse, and the same message blasted across
multiple channels. Tune the weights in `scam_heuristics.py`. Default suspect
threshold is 45.

Recommended flow: `scan_for_scammers` → review the list → `purge_scammers(action="ban", confirm=true)`
(or `ban_member` one at a time).

## Setup

**Prerequisite:** Python **3.10+** (the `mcp` package requires it). This machine's
default `python3` is 3.9, so `run.sh` auto-selects `python3.12`/`python3.11` if
present (`brew install python@3.12` otherwise). The venv is built on the internal
disk, not on the exFAT drive, to avoid macOS `._*.pth` files crashing Python.

### 1. Create the bot
1. https://discord.com/developers/applications → **New Application**.
2. **Bot** tab → **Add Bot** → **Reset Token** → copy the token.
3. Under **Privileged Gateway Intents**, enable **SERVER MEMBERS INTENT** and
   **MESSAGE CONTENT INTENT** (both required — members for moderation, message
   content for scam scanning).

### 2. Invite the bot with the right permissions
**OAuth2 → URL Generator**: scope `bot`, then tick:
`Manage Channels`, `Kick Members`, `Ban Members`, `Moderate Members`,
`Manage Messages`, `Read Message History`, `View Channels`, `Send Messages`.
Open the generated URL and add the bot to your server.

> The bot's role must sit **above** the roles of members you want to moderate,
> or Discord will reject kick/ban/timeout with a permissions error.

### 3. Configure
```bash
cd "discord-mcp"
cp .env.example .env
# edit .env: paste DISCORD_BOT_TOKEN, optionally DISCORD_GUILD_ID
chmod +x run.sh
```

### 4. Register with Claude Code
```bash
claude mcp add discord -- "/Volumes/NO NAME/discord-mcp/run.sh"
```
(or add an entry pointing at `run.sh` in your Claude Desktop `mcpConfig`). First
launch creates a venv and installs deps automatically. Restart Claude, then try:
*"list my Discord channels"* or *"scan for scammers in the last hour."*

## Safety notes
- Read/scan tools never modify anything.
- Destructive single-target tools act when called; **bulk** `purge_scammers` is a
  no-op unless `confirm=true`.
- Heuristics catch the obvious wave-of-bots / crypto-scam patterns. Treat the
  score as a strong signal, not gospel — review before mass-banning, and tune
  weights/threshold to your community.
