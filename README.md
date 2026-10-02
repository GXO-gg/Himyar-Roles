# Himyar Roles

Auto role on join, button role panels, and staff role tools — all set up with slash commands inside Discord.

Made with love by ♚GXO — https://himyar.org/

## Commands

| Command | What it does |
|---|---|
| `/autorole add` · `remove` · `list` | Roles every new member gets when they join (waits for the rules screen if the server uses one; bots are skipped) |
| `/panel create` | Posts a button panel in a channel |
| `/panel addrole` · `removerole` | Adds/removes a role button (label, emoji, grey/blue/green/red) — up to 25 per panel |
| `/panel edit` · `list` · `delete` | Change title/text/color, see all panels, delete one |
| `/role add` · `remove` | Give or take a role from one member |
| `/role bulk` · `cancel` | Give/remove a role for everyone, humans, or bots, with live progress |
| `/temprole add` · `remove` · `list` | Role that removes itself after e.g. `30m`, `12h`, `7d`, `1w2d` (max 365 days) |
| `/help` | Overview for server admins |

All staff commands need **Manage Roles**, and staff can only manage roles below their own top role.
Roles with staff permissions (Administrator, Manage Server, Ban, Kick, Timeout, Manage Roles/Channels/Messages, etc.) are blocked from auto roles, buttons and bulk-add.

## Discord Developer Portal setup

1. Create the application, add a bot, copy the token.
2. **Bot → Privileged Gateway Intents:** turn on **Server Members Intent** (nothing else).
3. Invite link (Manage Roles, View Channels, Send Messages, Embed Links, Read Message History):
   `https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&permissions=268520448&scope=bot+applications.commands`
4. In Server Settings → Roles, drag the bot's role **above** every role it should hand out.

## Run

**Coolify:** new resource from the GitHub repo, build pack **Dockerfile**, add env var `DISCORD_TOKEN`, and add a persistent storage volume mounted at `/app/data` (keeps panels/auto roles across redeploys).

**Plain server:**
```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env   # then put the token in .env
venv/bin/python bot.py
```
