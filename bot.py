"""
Himyar Roles — auto roles on join, button role panels, and staff role tools.
Made with love by ♚GXO — https://himyar.org/
"""

import asyncio
import logging
import os
import re
import time
from typing import Literal, Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATA_DIR = os.getenv("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "roles.db")

BRAND_COLOR = 0x1F8B4C
FOOTER = "Made with love by ♚GXO • himyar.org"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("himyar-roles")

# Roles with any of these permissions are never handed out automatically,
# by button, or in bulk — a stray click shouldn't make someone staff.
DANGEROUS_PERMS = (
    "administrator", "manage_guild", "manage_roles", "manage_channels",
    "manage_webhooks", "ban_members", "kick_members", "moderate_members",
    "manage_messages", "mention_everyone",
)

BUTTON_STYLES = {
    "grey": discord.ButtonStyle.secondary,
    "blue": discord.ButtonStyle.primary,
    "green": discord.ButtonStyle.success,
    "red": discord.ButtonStyle.danger,
}

MAX_BUTTONS = 25
MAX_TEMP_SECONDS = 365 * 86400

db: aiosqlite.Connection = None  # set in setup_hook
bulk_jobs: dict[int, asyncio.Event] = {}  # guild_id -> cancel flag


# ───────────────────────── helpers ─────────────────────────

def embed(text: str, *, title: Optional[str] = None, error: bool = False) -> discord.Embed:
    e = discord.Embed(description=text, color=0xE74C3C if error else BRAND_COLOR)
    if title:
        e.title = title
    return e


def role_problem(guild: discord.Guild, role: discord.Role, *, safe_only: bool = False) -> Optional[str]:
    """Why the bot can't hand out this role, or None if it can."""
    if role.is_default():
        return "That's the @everyone role."
    if role.managed:
        return f"{role.mention} is managed by a bot or integration and can't be assigned."
    if role >= guild.me.top_role:
        return (f"{role.mention} is above or equal to my highest role. "
                "Drag my role above it in **Server Settings → Roles**.")
    if safe_only:
        bad = [p.replace("_", " ") for p in DANGEROUS_PERMS if getattr(role.permissions, p)]
        if bad:
            return (f"{role.mention} has staff permissions ({', '.join(bad)}), "
                    "so it can't be given automatically, by button, or in bulk.")
    return None


def staff_problem(member: discord.Member, role: discord.Role) -> Optional[str]:
    """Stop staff from handing out roles at or above their own."""
    if member.id == member.guild.owner_id:
        return None
    if role >= member.top_role:
        return f"{role.mention} is at or above your highest role, so you can't manage it."
    return None


_DUR_RE = re.compile(r"(\d+)(w|d|h|m|s)")
_UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(text: str) -> Optional[int]:
    """'1h30m', '7d', '2w 3d' -> seconds. None if invalid."""
    text = text.strip().lower().replace(" ", "")
    if not text:
        return None
    pos, total = 0, 0
    for m in _DUR_RE.finditer(text):
        if m.start() != pos:
            return None
        total += int(m.group(1)) * _UNITS[m.group(2)]
        pos = m.end()
    if pos != len(text) or total <= 0:
        return None
    return total


def parse_color(text: Optional[str]) -> Optional[int]:
    if not text:
        return BRAND_COLOR
    text = text.strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{6}", text):
        return int(text, 16)
    return None


# ───────────────────────── role buttons ─────────────────────────

class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"himyar-roles:(?P<role_id>\d+)"):
    """Persistent toggle button. Survives restarts because the role id lives in the custom_id."""

    def __init__(self, role_id: int, label: Optional[str] = None,
                 emoji: Optional[str] = None, style: discord.ButtonStyle = discord.ButtonStyle.secondary):
        super().__init__(discord.ui.Button(
            label=label, emoji=emoji, style=style, custom_id=f"himyar-roles:{role_id}",
        ))
        self.role_id = role_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["role_id"]))

    async def callback(self, interaction: discord.Interaction):
        guild, member = interaction.guild, interaction.user
        role = guild.get_role(self.role_id) if guild else None
        if role is None:
            return await interaction.response.send_message(
                embed=embed("This role no longer exists. Ask staff to update this panel.", error=True),
                ephemeral=True)
        if role_problem(guild, role, safe_only=True):
            return await interaction.response.send_message(
                embed=embed("I can't give this role right now. Ask staff to check the panel.", error=True),
                ephemeral=True)
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Himyar Roles: button panel")
                text = f"Removed {role.mention}."
            else:
                await member.add_roles(role, reason="Himyar Roles: button panel")
                text = f"Gave you {role.mention}."
        except discord.Forbidden:
            text = "I don't have permission to change your roles. Ask staff to check my role position."
            return await interaction.response.send_message(embed=embed(text, error=True), ephemeral=True)
        await interaction.response.send_message(embed=embed(text), ephemeral=True)


async def panel_buttons(panel_id: int) -> list[tuple]:
    async with db.execute(
        "SELECT role_id, label, emoji, style FROM panel_buttons WHERE panel_id=? ORDER BY position",
        (panel_id,),
    ) as cur:
        return await cur.fetchall()


def build_panel(title: str, description: str, color: int, buttons: list[tuple]):
    e = discord.Embed(title=title, description=description, color=color)
    view = discord.ui.View(timeout=None)
    for role_id, label, emoji, style in buttons:
        view.add_item(RoleButton(role_id, label=label, emoji=emoji or None,
                                 style=BUTTON_STYLES.get(style, discord.ButtonStyle.secondary)))
    return e, view


async def get_panel(guild_id: int, panel_id: int):
    async with db.execute(
        "SELECT id, channel_id, message_id, title, description, color FROM panels WHERE id=? AND guild_id=?",
        (panel_id, guild_id),
    ) as cur:
        return await cur.fetchone()


async def push_panel(panel, buttons: list[tuple]) -> Optional[str]:
    """Edit the panel message to match `buttons`. Returns an error string or None."""
    _, channel_id, message_id, title, description, color = panel
    channel = bot.get_channel(channel_id)
    if channel is None:
        return "I can't find this panel's channel anymore. Use `/panel delete` and make a new one."
    e, view = build_panel(title, description, color, buttons)
    try:
        await channel.get_partial_message(message_id).edit(embed=e, view=view)
    except discord.NotFound:
        return "This panel's message was deleted. Use `/panel delete` and make a new one."
    except discord.Forbidden:
        return f"I don't have permission to edit messages in {channel.mention}."
    except discord.HTTPException as exc:
        return f"Discord rejected the update — usually a bad emoji. ({exc.text})"
    return None


async def panel_autocomplete(interaction: discord.Interaction, current: str):
    async with db.execute("SELECT id, title FROM panels WHERE guild_id=? ORDER BY id", (interaction.guild_id,)) as cur:
        rows = await cur.fetchall()
    current = current.lower()
    return [
        app_commands.Choice(name=f"#{pid} — {title}"[:100], value=pid)
        for pid, title in rows if current in f"#{pid} {title}".lower()
    ][:25]


# ───────────────────────── bot ─────────────────────────

class HimyarRoles(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True  # needed for join events and bulk roles
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

    async def setup_hook(self):
        global db
        db = await aiosqlite.connect(DB_PATH)
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS autoroles (
                guild_id INTEGER, role_id INTEGER, PRIMARY KEY (guild_id, role_id));
            CREATE TABLE IF NOT EXISTS panels (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, channel_id INTEGER,
                message_id INTEGER, title TEXT, description TEXT, color INTEGER);
            CREATE TABLE IF NOT EXISTS panel_buttons (
                panel_id INTEGER, role_id INTEGER, label TEXT, emoji TEXT, style TEXT, position INTEGER,
                PRIMARY KEY (panel_id, role_id));
            CREATE TABLE IF NOT EXISTS temproles (
                guild_id INTEGER, user_id INTEGER, role_id INTEGER, expires_at INTEGER,
                PRIMARY KEY (guild_id, user_id, role_id));
        """)
        await db.commit()
        self.add_dynamic_items(RoleButton)
        self.tree.add_command(AutoRoleGroup())
        self.tree.add_command(PanelGroup())
        self.tree.add_command(RoleGroup())
        self.tree.add_command(TempRoleGroup())
        await self.tree.sync()
        temp_role_loop.start()

    async def close(self):
        if db:
            await db.close()
        await super().close()


bot = HimyarRoles()


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s) in %d servers", bot.user, bot.user.id, len(bot.guilds))
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="/help • himyar.org"))


# ───────────────────────── auto role ─────────────────────────

async def give_autoroles(member: discord.Member):
    async with db.execute("SELECT role_id FROM autoroles WHERE guild_id=?", (member.guild.id,)) as cur:
        ids = [r[0] for r in await cur.fetchall()]
    roles = [r for r in (member.guild.get_role(i) for i in ids)
             if r and not role_problem(member.guild, r, safe_only=True) and r not in member.roles]
    if roles:
        try:
            await member.add_roles(*roles, reason="Himyar Roles: auto role")
        except discord.HTTPException as exc:
            log.warning("Auto role failed in %s: %s", member.guild.id, exc)


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return
    # If the server has a rules screen, wait until they accept it (see on_member_update).
    if member.pending:
        return
    await give_autoroles(member)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if before.pending and not after.pending and not after.bot:
        await give_autoroles(after)


@bot.event
async def on_member_remove(member: discord.Member):
    await db.execute("DELETE FROM temproles WHERE guild_id=? AND user_id=?", (member.guild.id, member.id))
    await db.commit()


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    async with db.execute("SELECT id FROM panels WHERE message_id=?", (payload.message_id,)) as cur:
        row = await cur.fetchone()
    if row:
        await db.execute("DELETE FROM panel_buttons WHERE panel_id=?", (row[0],))
        await db.execute("DELETE FROM panels WHERE id=?", (row[0],))
        await db.commit()


class AutoRoleGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="autorole", description="Roles given automatically when someone joins",
                         guild_only=True, default_permissions=discord.Permissions(manage_roles=True))

    @app_commands.command(name="add", description="Give this role to every new member")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def add(self, interaction: discord.Interaction, role: discord.Role):
        problem = role_problem(interaction.guild, role, safe_only=True) or staff_problem(interaction.user, role)
        if problem:
            return await interaction.response.send_message(embed=embed(problem, error=True), ephemeral=True)
        await db.execute("INSERT OR IGNORE INTO autoroles VALUES (?, ?)", (interaction.guild_id, role.id))
        await db.commit()
        await interaction.response.send_message(
            embed=embed(f"New members will now get {role.mention} when they join."), ephemeral=True)

    @app_commands.command(name="remove", description="Stop giving this role to new members")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def remove(self, interaction: discord.Interaction, role: discord.Role):
        cur = await db.execute("DELETE FROM autoroles WHERE guild_id=? AND role_id=?", (interaction.guild_id, role.id))
        await db.commit()
        text = (f"{role.mention} is no longer an auto role." if cur.rowcount
                else f"{role.mention} wasn't an auto role.")
        await interaction.response.send_message(embed=embed(text), ephemeral=True)

    @app_commands.command(name="list", description="Show the auto roles for this server")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def list_(self, interaction: discord.Interaction):
        async with db.execute("SELECT role_id FROM autoroles WHERE guild_id=?", (interaction.guild_id,)) as cur:
            ids = [r[0] for r in await cur.fetchall()]
        if not ids:
            text = "No auto roles yet. Add one with `/autorole add`."
        else:
            lines = []
            for i in ids:
                role = interaction.guild.get_role(i)
                if role is None:
                    lines.append(f"• deleted role `{i}` — remove with `/autorole remove`")
                else:
                    p = role_problem(interaction.guild, role, safe_only=True)
                    lines.append(f"• {role.mention}" + (" ⚠️ can't be given right now" if p else ""))
            text = "\n".join(lines)
        await interaction.response.send_message(embed=embed(text, title="Auto roles"), ephemeral=True)


# ───────────────────────── panels ─────────────────────────

class PanelGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="panel", description="Button role panels members can click",
                         guild_only=True, default_permissions=discord.Permissions(manage_roles=True))

    @app_commands.command(name="create", description="Post a new button role panel")
    @app_commands.describe(channel="Where to post it", title="Panel title",
                           description="Text under the title", color="Hex color, e.g. #1F8B4C")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def create(self, interaction: discord.Interaction, channel: discord.TextChannel, title: str,
                     description: Optional[str] = None, color: Optional[str] = None):
        col = parse_color(color)
        if col is None:
            return await interaction.response.send_message(
                embed=embed("Color must be a hex code like `#1F8B4C`.", error=True), ephemeral=True)
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                embed=embed(f"I need View Channel, Send Messages and Embed Links in {channel.mention}.", error=True),
                ephemeral=True)
        title = title[:256]
        description = (description or "Click a button below to get or remove a role.")[:4000]
        e, view = build_panel(title, description, col, [])
        msg = await channel.send(embed=e, view=view)
        cur = await db.execute(
            "INSERT INTO panels (guild_id, channel_id, message_id, title, description, color) VALUES (?,?,?,?,?,?)",
            (interaction.guild_id, channel.id, msg.id, title, description, col))
        await db.commit()
        await interaction.response.send_message(
            embed=embed(f"Panel **#{cur.lastrowid}** posted in {channel.mention}.\n"
                        f"Add buttons with `/panel addrole`."), ephemeral=True)

    @app_commands.command(name="addrole", description="Add a role button to a panel")
    @app_commands.describe(panel="Which panel", role="Role the button gives",
                           label="Button text (defaults to the role name)", emoji="Optional emoji",
                           style="Button color")
    @app_commands.autocomplete(panel=panel_autocomplete)
    @app_commands.checks.has_permissions(manage_roles=True)
    async def addrole(self, interaction: discord.Interaction, panel: int, role: discord.Role,
                      label: Optional[str] = None, emoji: Optional[str] = None,
                      style: Literal["grey", "blue", "green", "red"] = "grey"):
        row = await get_panel(interaction.guild_id, panel)
        if row is None:
            return await interaction.response.send_message(embed=embed("Panel not found.", error=True), ephemeral=True)
        problem = role_problem(interaction.guild, role, safe_only=True) or staff_problem(interaction.user, role)
        if problem:
            return await interaction.response.send_message(embed=embed(problem, error=True), ephemeral=True)
        buttons = await panel_buttons(panel)
        if any(b[0] == role.id for b in buttons):
            return await interaction.response.send_message(
                embed=embed(f"{role.mention} is already on this panel.", error=True), ephemeral=True)
        if len(buttons) >= MAX_BUTTONS:
            return await interaction.response.send_message(
                embed=embed("A panel can hold 25 buttons. Make another panel.", error=True), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        new = (role.id, (label or role.name)[:80], (emoji or "").strip() or None, style)
        error = await push_panel(row, buttons + [new])
        if error:
            return await interaction.followup.send(embed=embed(error, error=True), ephemeral=True)
        await db.execute("INSERT INTO panel_buttons VALUES (?,?,?,?,?,?)", (panel, *new, len(buttons)))
        await db.commit()
        await interaction.followup.send(embed=embed(f"Added a button for {role.mention} to panel #{panel}."),
                                        ephemeral=True)

    @app_commands.command(name="removerole", description="Remove a role button from a panel")
    @app_commands.autocomplete(panel=panel_autocomplete)
    @app_commands.checks.has_permissions(manage_roles=True)
    async def removerole(self, interaction: discord.Interaction, panel: int, role: discord.Role):
        row = await get_panel(interaction.guild_id, panel)
        if row is None:
            return await interaction.response.send_message(embed=embed("Panel not found.", error=True), ephemeral=True)
        buttons = await panel_buttons(panel)
        remaining = [b for b in buttons if b[0] != role.id]
        if len(remaining) == len(buttons):
            return await interaction.response.send_message(
                embed=embed(f"{role.mention} isn't on this panel.", error=True), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        error = await push_panel(row, remaining)
        if error:
            return await interaction.followup.send(embed=embed(error, error=True), ephemeral=True)
        await db.execute("DELETE FROM panel_buttons WHERE panel_id=? AND role_id=?", (panel, role.id))
        await db.commit()
        await interaction.followup.send(embed=embed(f"Removed {role.mention} from panel #{panel}."), ephemeral=True)

    @app_commands.command(name="edit", description="Change a panel's title, text or color")
    @app_commands.autocomplete(panel=panel_autocomplete)
    @app_commands.checks.has_permissions(manage_roles=True)
    async def edit(self, interaction: discord.Interaction, panel: int, title: Optional[str] = None,
                   description: Optional[str] = None, color: Optional[str] = None):
        row = await get_panel(interaction.guild_id, panel)
        if row is None:
            return await interaction.response.send_message(embed=embed("Panel not found.", error=True), ephemeral=True)
        col = row[5] if color is None else parse_color(color)
        if col is None:
            return await interaction.response.send_message(
                embed=embed("Color must be a hex code like `#1F8B4C`.", error=True), ephemeral=True)
        new_row = (row[0], row[1], row[2], (title or row[3])[:256], (description or row[4])[:4000], col)
        await interaction.response.defer(ephemeral=True)
        error = await push_panel(new_row, await panel_buttons(panel))
        if error:
            return await interaction.followup.send(embed=embed(error, error=True), ephemeral=True)
        await db.execute("UPDATE panels SET title=?, description=?, color=? WHERE id=?",
                         (new_row[3], new_row[4], col, panel))
        await db.commit()
        await interaction.followup.send(embed=embed(f"Panel #{panel} updated."), ephemeral=True)

    @app_commands.command(name="list", description="Show this server's panels")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def list_(self, interaction: discord.Interaction):
        async with db.execute(
            "SELECT p.id, p.title, p.channel_id, p.message_id, COUNT(b.role_id) FROM panels p "
            "LEFT JOIN panel_buttons b ON b.panel_id=p.id WHERE p.guild_id=? GROUP BY p.id ORDER BY p.id",
            (interaction.guild_id,),
        ) as cur:
            rows = await cur.fetchall()
        if not rows:
            text = "No panels yet. Make one with `/panel create`."
        else:
            text = "\n".join(
                f"**#{pid}** {title} — <#{ch}> · {n} button{'s' if n != 1 else ''} · "
                f"[jump](https://discord.com/channels/{interaction.guild_id}/{ch}/{mid})"
                for pid, title, ch, mid, n in rows)
        await interaction.response.send_message(embed=embed(text, title="Role panels"), ephemeral=True)

    @app_commands.command(name="delete", description="Delete a panel and its message")
    @app_commands.autocomplete(panel=panel_autocomplete)
    @app_commands.checks.has_permissions(manage_roles=True)
    async def delete(self, interaction: discord.Interaction, panel: int):
        row = await get_panel(interaction.guild_id, panel)
        if row is None:
            return await interaction.response.send_message(embed=embed("Panel not found.", error=True), ephemeral=True)
        channel = bot.get_channel(row[1])
        if channel:
            try:
                await channel.get_partial_message(row[2]).delete()
            except discord.HTTPException:
                pass
        await db.execute("DELETE FROM panel_buttons WHERE panel_id=?", (panel,))
        await db.execute("DELETE FROM panels WHERE id=?", (panel,))
        await db.commit()
        await interaction.response.send_message(embed=embed(f"Panel #{panel} deleted."), ephemeral=True)


# ───────────────────────── staff role tools ─────────────────────────

async def run_bulk(guild: discord.Guild, channel, role: discord.Role, members: list[discord.Member],
                   action: str, by: discord.Member, cancel: asyncio.Event):
    verb = "Giving" if action == "add" else "Removing"
    done = failed = 0
    progress = None
    try:
        progress = await channel.send(embed=embed(f"{verb} {role.mention}: 0 / {len(members)}"))
    except discord.HTTPException:
        pass
    last_edit = time.monotonic()
    try:
        for m in members:
            if cancel.is_set():
                break
            try:
                if action == "add":
                    await m.add_roles(role, reason=f"Himyar Roles bulk by {by}")
                else:
                    await m.remove_roles(role, reason=f"Himyar Roles bulk by {by}")
                done += 1
            except discord.HTTPException:
                failed += 1
            await asyncio.sleep(0.4)
            if progress and time.monotonic() - last_edit > 5:
                last_edit = time.monotonic()
                try:
                    await progress.edit(embed=embed(f"{verb} {role.mention}: {done + failed} / {len(members)}"))
                except discord.HTTPException:
                    progress = None
    finally:
        bulk_jobs.pop(guild.id, None)
    status = "Cancelled" if cancel.is_set() else "Finished"
    text = f"{status}. {done} member{'s' if done != 1 else ''} updated with {role.mention}"
    text += f", {failed} failed." if failed else "."
    if progress:
        try:
            await progress.edit(embed=embed(text))
        except discord.HTTPException:
            pass


class RoleGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="role", description="Give or remove roles",
                         guild_only=True, default_permissions=discord.Permissions(manage_roles=True))

    @app_commands.command(name="add", description="Give a role to a member")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def add(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        problem = role_problem(interaction.guild, role) or staff_problem(interaction.user, role)
        if problem:
            return await interaction.response.send_message(embed=embed(problem, error=True), ephemeral=True)
        if role in member.roles:
            return await interaction.response.send_message(
                embed=embed(f"{member.mention} already has {role.mention}."), ephemeral=True)
        await member.add_roles(role, reason=f"Himyar Roles: /role add by {interaction.user}")
        await interaction.response.send_message(embed=embed(f"Gave {role.mention} to {member.mention}."),
                                                ephemeral=True)

    @app_commands.command(name="remove", description="Remove a role from a member")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def remove(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        problem = role_problem(interaction.guild, role) or staff_problem(interaction.user, role)
        if problem:
            return await interaction.response.send_message(embed=embed(problem, error=True), ephemeral=True)
        if role not in member.roles:
            return await interaction.response.send_message(
                embed=embed(f"{member.mention} doesn't have {role.mention}."), ephemeral=True)
        await member.remove_roles(role, reason=f"Himyar Roles: /role remove by {interaction.user}")
        await db.execute("DELETE FROM temproles WHERE guild_id=? AND user_id=? AND role_id=?",
                         (interaction.guild_id, member.id, role.id))
        await db.commit()
        await interaction.response.send_message(embed=embed(f"Removed {role.mention} from {member.mention}."),
                                                ephemeral=True)

    @app_commands.command(name="bulk", description="Give or remove a role for many members at once")
    @app_commands.describe(role="The role", action="Give or remove it", target="Who to change")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def bulk(self, interaction: discord.Interaction, role: discord.Role,
                   action: Literal["add", "remove"], target: Literal["everyone", "humans", "bots"]):
        guild = interaction.guild
        problem = (role_problem(guild, role, safe_only=(action == "add"))
                   or staff_problem(interaction.user, role))
        if problem:
            return await interaction.response.send_message(embed=embed(problem, error=True), ephemeral=True)
        if guild.id in bulk_jobs:
            return await interaction.response.send_message(
                embed=embed("A bulk job is already running here. Stop it with `/role cancel`.", error=True),
                ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        if not guild.chunked:
            await guild.chunk()
        members = [
            m for m in guild.members
            if (target == "everyone" or (target == "bots") == m.bot)
            and ((role not in m.roles) if action == "add" else (role in m.roles))
        ]
        if not members:
            return await interaction.followup.send(embed=embed("Nobody to change — they're all set already."),
                                                   ephemeral=True)
        cancel = asyncio.Event()
        bulk_jobs[guild.id] = cancel
        minutes = max(1, round(len(members) * 0.5 / 60))
        await interaction.followup.send(
            embed=embed(f"Started: {'giving' if action == 'add' else 'removing'} {role.mention} "
                        f"for **{len(members)}** members (about {minutes} min). "
                        f"Progress is posted in this channel. Stop anytime with `/role cancel`."),
            ephemeral=True)
        asyncio.create_task(run_bulk(guild, interaction.channel, role, members, action, interaction.user, cancel))

    @app_commands.command(name="cancel", description="Stop a running bulk role job")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def cancel(self, interaction: discord.Interaction):
        job = bulk_jobs.get(interaction.guild_id)
        if job is None:
            return await interaction.response.send_message(embed=embed("No bulk job is running."), ephemeral=True)
        job.set()
        await interaction.response.send_message(embed=embed("Stopping the bulk job…"), ephemeral=True)


class TempRoleGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="temprole", description="Roles that remove themselves after a while",
                         guild_only=True, default_permissions=discord.Permissions(manage_roles=True))

    @app_commands.command(name="add", description="Give a role that's removed automatically later")
    @app_commands.describe(duration="How long, e.g. 30m, 12h, 7d, 1w2d")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def add(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role,
                  duration: str):
        seconds = parse_duration(duration)
        if seconds is None:
            return await interaction.response.send_message(
                embed=embed("Use a duration like `30m`, `12h`, `7d` or `1w2d`.", error=True), ephemeral=True)
        if seconds > MAX_TEMP_SECONDS:
            return await interaction.response.send_message(
                embed=embed("Temporary roles can last up to 365 days.", error=True), ephemeral=True)
        problem = role_problem(interaction.guild, role) or staff_problem(interaction.user, role)
        if problem:
            return await interaction.response.send_message(embed=embed(problem, error=True), ephemeral=True)
        expires = int(time.time()) + seconds
        if role not in member.roles:
            await member.add_roles(role, reason=f"Himyar Roles: temp role by {interaction.user}")
        await db.execute("INSERT OR REPLACE INTO temproles VALUES (?,?,?,?)",
                         (interaction.guild_id, member.id, role.id, expires))
        await db.commit()
        await interaction.response.send_message(
            embed=embed(f"Gave {role.mention} to {member.mention}. It'll be removed <t:{expires}:R>."),
            ephemeral=True)

    @app_commands.command(name="remove", description="Remove a temporary role now")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def remove(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        problem = staff_problem(interaction.user, role)
        if problem:
            return await interaction.response.send_message(embed=embed(problem, error=True), ephemeral=True)
        cur = await db.execute("DELETE FROM temproles WHERE guild_id=? AND user_id=? AND role_id=?",
                               (interaction.guild_id, member.id, role.id))
        await db.commit()
        if not cur.rowcount:
            return await interaction.response.send_message(
                embed=embed(f"{member.mention} doesn't have {role.mention} as a temporary role."), ephemeral=True)
        if role in member.roles and not role_problem(interaction.guild, role):
            await member.remove_roles(role, reason=f"Himyar Roles: temp role removed by {interaction.user}")
        await interaction.response.send_message(
            embed=embed(f"Removed temporary {role.mention} from {member.mention}."), ephemeral=True)

    @app_commands.command(name="list", description="Show active temporary roles")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def list_(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        sql = "SELECT user_id, role_id, expires_at FROM temproles WHERE guild_id=?"
        args = [interaction.guild_id]
        if member:
            sql += " AND user_id=?"
            args.append(member.id)
        async with db.execute(sql + " ORDER BY expires_at LIMIT 26", args) as cur:
            rows = await cur.fetchall()
        if not rows:
            text = "No active temporary roles."
        else:
            text = "\n".join(f"<@{u}> — <@&{r}> · ends <t:{e}:R>" for u, r, e in rows[:25])
            if len(rows) > 25:
                text += "\n…and more."
        await interaction.response.send_message(embed=embed(text, title="Temporary roles"), ephemeral=True)


@tasks.loop(seconds=30)
async def temp_role_loop():
    async with db.execute("SELECT guild_id, user_id, role_id FROM temproles WHERE expires_at<=?",
                          (int(time.time()),)) as cur:
        rows = await cur.fetchall()
    for guild_id, user_id, role_id in rows:
        guild = bot.get_guild(guild_id)
        role = guild.get_role(role_id) if guild else None
        if guild and role:
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.NotFound:
                    member = None
                except discord.HTTPException:
                    continue  # try again next loop
            if member and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Himyar Roles: temporary role expired")
                except discord.Forbidden:
                    pass  # can't remove it; drop the record so we don't retry forever
                except discord.HTTPException:
                    continue
        await db.execute("DELETE FROM temproles WHERE guild_id=? AND user_id=? AND role_id=?",
                         (guild_id, user_id, role_id))
    if rows:
        await db.commit()


@temp_role_loop.before_loop
async def _wait_ready():
    await bot.wait_until_ready()


# ───────────────────────── help & errors ─────────────────────────

@bot.tree.command(name="help", description="How to use Himyar Roles")
async def help_cmd(interaction: discord.Interaction):
    e = discord.Embed(title="Himyar Roles", color=BRAND_COLOR, description=(
        "Auto roles, button role panels and staff role tools.\n"
        "Make sure my role sits **above** every role you want me to manage."))
    e.add_field(name="Auto role", inline=False, value=(
        "`/autorole add` · `/autorole remove` · `/autorole list`\n"
        "Given when someone joins (or after they accept the rules screen, if you use one)."))
    e.add_field(name="Button panels", inline=False, value=(
        "`/panel create` · `/panel addrole` · `/panel removerole`\n"
        "`/panel edit` · `/panel list` · `/panel delete`\n"
        "Members click a button to get or remove a role."))
    e.add_field(name="Staff tools", inline=False, value=(
        "`/role add` · `/role remove` · `/role bulk` · `/role cancel`\n"
        "`/temprole add` · `/temprole remove` · `/temprole list`"))
    e.add_field(name="Safety", inline=False, value=(
        "Roles with staff permissions (Admin, Manage Server, Ban, etc.) can't be auto roles, "
        "panel buttons or bulk-given. Staff can only manage roles below their own."))
    e.set_footer(text=FOOTER)
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        text = "You need the **Manage Roles** permission for this."
    elif isinstance(error, app_commands.CommandInvokeError) and isinstance(error.original, discord.Forbidden):
        text = "Discord blocked that — check my role is above the role and I have Manage Roles."
    else:
        log.exception("Command error", exc_info=error)
        text = "Something went wrong. Try again in a moment."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed(text, error=True), ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed(text, error=True), ephemeral=True)
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing — put it in .env")
    bot.run(TOKEN, log_handler=None)
