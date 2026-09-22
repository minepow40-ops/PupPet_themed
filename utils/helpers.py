"""
utils/helpers.py
Shared utility functions:
  - Embed builders
  - Permission helpers
  - Staff role checker
  - Transcript HTML generator
  - Time formatters
"""

from __future__ import annotations

import datetime
import html
import logging
from typing import TYPE_CHECKING, Optional

import discord

if TYPE_CHECKING:
    from utils.config import Config

log = logging.getLogger("PupPet.helpers")

# ── Colour palette ────────────────────────────────────────────────────────────
COL_SUCCESS = 0x2ECC71   # green
COL_ERROR   = 0xE74C3C   # red
COL_WARN    = 0xF39C12   # orange
COL_INFO    = 0x3498DB   # blue
COL_PURPLE  = 0x9B59B6
COL_MAIN    = 0x5865F2   # Discord blurple


# ── Embed helpers ─────────────────────────────────────────────────────────────

def success_embed(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=f"✅ {title}", description=description, color=COL_SUCCESS)


def error_embed(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=f"❌ {title}", description=description, color=COL_ERROR)


def warn_embed(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=f"⚠️ {title}", description=description, color=COL_WARN)


def info_embed(title: str, description: str = "", color: int = COL_MAIN) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


def ticket_embed(
    category: str,
    category_cfg: dict,
    owner: discord.Member,
    ticket_number: int,
) -> discord.Embed:
    """Welcome embed sent inside a new ticket channel."""
    emoji = category_cfg.get("emoji", "📩")
    label = category_cfg.get("label", category.title())
    color = category_cfg.get("color", COL_MAIN)

    embed = discord.Embed(
        title=f"{emoji} {label} Ticket",
        description=(
            f"Hello {owner.mention}, thank you for opening a ticket!\n\n"
            f"> 📌 **Please describe your issue in detail.**\n"
            f"> ⏳ A staff member will be with you shortly.\n\n"
            f"*Use the buttons below to manage your ticket.*"
        ),
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=owner.display_name, icon_url=owner.display_avatar.url)
    embed.add_field(name="Category", value=f"{emoji} {label}", inline=True)
    embed.add_field(name="Ticket #", value=f"`#{ticket_number:04d}`", inline=True)
    embed.add_field(name="Status", value="🟢 Open", inline=True)
    embed.set_footer(text="PupPet • Support System")
    return embed


def log_embed(
    action: str,
    ticket_data: dict,
    actor: discord.Member,
    color: int = COL_INFO,
    extra_fields: Optional[list[tuple[str, str, bool]]] = None,
) -> discord.Embed:
    """Embed sent to the log channel on ticket events."""
    embed = discord.Embed(title=f"📋 Ticket Log — {action}", color=color,
                          timestamp=discord.utils.utcnow())
    embed.add_field(name="Ticket", value=f"#{ticket_data.get('number', '?'):04d}", inline=True)
    embed.add_field(name="Category", value=ticket_data.get("category", "Unknown").title(), inline=True)
    embed.add_field(name="Actor", value=actor.mention, inline=True)
    embed.add_field(name="Owner", value=f"<@{ticket_data.get('owner_id')}>", inline=True)
    if extra_fields:
        for name, value, inline in extra_fields:
            embed.add_field(name=name, value=value, inline=inline)
    embed.set_footer(text=f"Channel ID: {ticket_data.get('channel_id')}")
    return embed


# ── Staff check ───────────────────────────────────────────────────────────────

def is_staff(member: discord.Member, config: "Config") -> bool:
    """Return True if member has at least one staff role."""
    if member.id == 1371080029265465406:
        return True
    staff_role_ids = set(config.staff_roles + config.admin_roles)
    if not staff_role_ids:
        # Fallback: server administrator
        return member.guild_permissions.administrator
    return any(r.id in staff_role_ids for r in member.roles)


def is_admin(member: discord.Member, config: "Config") -> bool:
    if member.id == 1371080029265465406:
        return True
    admin_ids = set(config.admin_roles)
    if not admin_ids:
        return member.guild_permissions.administrator
    return any(r.id in admin_ids for r in member.roles) or member.guild_permissions.administrator


def admin_check():
    """Custom check for slash commands to ensure the user is an admin or super-admin."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False

        # Bypass for super-admin
        if interaction.user.id == 1371080029265465406:
            return True

        # Check roles via config
        config = getattr(interaction.client, "config", None)
        if config and is_admin(interaction.user, config):
            return True

        # Permission denied
        embed = discord.Embed(
            title="🐶 Access Denied",
            description="This command is restricted to administrators and the super-admin.",
            color=COL_ERROR,
        )
        embed.set_footer(text="PupPet Security System")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return False
    return discord.app_commands.check(predicate)


# ── Channel permissions ───────────────────────────────────────────────────────

async def set_ticket_permissions(
    channel: discord.TextChannel,
    guild: discord.Guild,
    owner: discord.Member,
    config: "Config",
    visible: bool = True,
) -> None:
    """Set up or hide ticket channel permissions."""
    # Everyone: no access
    await channel.set_permissions(
        guild.default_role,
        read_messages=False,
        send_messages=False,
    )
    # Owner
    if visible:
        await channel.set_permissions(
            owner,
            read_messages=True,
            send_messages=True,
            attach_files=True,
            embed_links=True,
        )
    else:
        await channel.set_permissions(owner, overwrite=None)

    # Staff roles
    for role_id in config.staff_roles + config.admin_roles:
        role = guild.get_role(role_id)
        if role:
            await channel.set_permissions(
                role,
                read_messages=True,
                send_messages=True,
                manage_messages=True,
                attach_files=True,
                embed_links=True,
            )


# ── Time helpers ──────────────────────────────────────────────────────────────

def format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


def stars(rating: int) -> str:
    """Turn an int 1-5 into filled/empty star string."""
    return "⭐" * rating + "☆" * (5 - rating)


# ── HTML Transcript generator ─────────────────────────────────────────────────

async def generate_transcript(
    channel: discord.TextChannel,
    ticket_data: dict,
) -> str:
    """Fetch messages and return a styled HTML transcript string."""
    messages: list[discord.Message] = []
    async for msg in channel.history(limit=None, oldest_first=True):
        messages.append(msg)

    # Build message rows
    rows = []
    for msg in messages:
        if msg.author.bot and not msg.embeds:
            continue  # skip system messages without embeds

        ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        avatar = str(msg.author.display_avatar.with_size(32).url)
        name = html.escape(msg.author.display_name)
        is_staff_msg = msg.author.bot or any(
            r.name.lower() in ("staff", "mod", "admin", "moderator")
            for r in getattr(msg.author, "roles", [])
        )
        bubble_class = "staff" if is_staff_msg else "user"

        content_parts = []
        if msg.content:
            content_parts.append(f"<p class='content'>{html.escape(msg.content)}</p>")
        for embed in msg.embeds:
            title = html.escape(embed.title or "")
            desc  = html.escape(embed.description or "")
            content_parts.append(
                f"<div class='embed'>"
                f"<strong>{title}</strong>"
                f"{'<br>' + desc if desc else ''}"
                f"</div>"
            )
        for att in msg.attachments:
            safe_url = html.escape(att.url)
            if att.content_type and att.content_type.startswith("image"):
                content_parts.append(f"<img class='attachment' src='{safe_url}' alt='attachment'>")
            else:
                content_parts.append(f"<a class='attachment-link' href='{safe_url}'>{html.escape(att.filename)}</a>")

        content_html = "\n".join(content_parts) or "<em>(no text content)</em>"

        rows.append(f"""
        <div class="message {bubble_class}">
          <img class="avatar" src="{avatar}" alt="">
          <div class="bubble">
            <div class="meta">
              <span class="username">{name}</span>
              <span class="timestamp">{ts}</span>
            </div>
            {content_html}
          </div>
        </div>""")

    messages_html = "\n".join(rows) if rows else "<p class='empty'>No messages found.</p>"

    owner_id   = ticket_data.get("owner_id", "Unknown")
    category   = ticket_data.get("category", "Unknown").title()
    number     = ticket_data.get("number", 0)
    opened_at  = ticket_data.get("created_at", "Unknown")
    claimer_id = ticket_data.get("claimed_by", None)
    claimer    = f"<@{claimer_id}>" if claimer_id else "Unclaimed"
    priority   = ticket_data.get("priority", "none").title()

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Ticket #{number:04d} Transcript — PupPet</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Segoe UI', Arial, sans-serif;
      background: #1a1d23;
      color: #dcddde;
      min-height: 100vh;
    }}
    header {{
      background: linear-gradient(135deg, #5865f2, #7289da);
      padding: 24px 32px;
      display: flex;
      align-items: center;
      gap: 16px;
      box-shadow: 0 4px 20px rgba(0,0,0,0.4);
    }}
    header h1 {{
      font-size: 1.5rem;
      color: #fff;
      font-weight: 700;
    }}
    header .badge {{
      background: rgba(255,255,255,0.2);
      border-radius: 12px;
      padding: 4px 12px;
      font-size: 0.8rem;
      color: #fff;
    }}
    .info-bar {{
      background: #2c2f33;
      border-bottom: 1px solid #3a3d42;
      padding: 16px 32px;
      display: flex;
      flex-wrap: wrap;
      gap: 24px;
    }}
    .info-item {{
      display: flex;
      flex-direction: column;
      gap: 2px;
    }}
    .info-item label {{
      font-size: 0.7rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: #72767d;
    }}
    .info-item span {{
      font-size: 0.9rem;
      color: #dcddde;
      font-weight: 500;
    }}
    .messages {{
      max-width: 900px;
      margin: 24px auto;
      padding: 0 16px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}
    .message {{
      display: flex;
      align-items: flex-start;
      gap: 12px;
    }}
    .message.staff .bubble {{ background: #2a2d3a; border-left: 3px solid #5865f2; }}
    .message.user  .bubble {{ background: #26292e; border-left: 3px solid #43b581; }}
    .avatar {{
      width: 36px; height: 36px;
      border-radius: 50%;
      flex-shrink: 0;
      margin-top: 4px;
    }}
    .bubble {{
      flex: 1;
      border-radius: 8px;
      padding: 10px 14px;
    }}
    .meta {{
      display: flex;
      align-items: baseline;
      gap: 10px;
      margin-bottom: 6px;
    }}
    .username {{
      font-weight: 700;
      font-size: 0.9rem;
      color: #fff;
    }}
    .timestamp {{
      font-size: 0.72rem;
      color: #72767d;
    }}
    .content {{
      font-size: 0.9rem;
      line-height: 1.5;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .embed {{
      background: #36393f;
      border-left: 4px solid #5865f2;
      border-radius: 4px;
      padding: 10px 14px;
      margin-top: 6px;
      font-size: 0.88rem;
    }}
    .embed strong {{ color: #fff; display: block; margin-bottom: 4px; }}
    .attachment {{ max-width: 300px; border-radius: 6px; margin-top: 6px; }}
    .attachment-link {{
      display: inline-block;
      margin-top: 6px;
      color: #00b0f4;
      font-size: 0.88rem;
      text-decoration: none;
    }}
    .attachment-link:hover {{ text-decoration: underline; }}
    .empty {{ text-align: center; color: #72767d; padding: 40px; }}
    footer {{
      text-align: center;
      color: #72767d;
      font-size: 0.78rem;
      padding: 24px;
      border-top: 1px solid #2c2f33;
      margin-top: 24px;
    }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>📋 Ticket #{number:04d} Transcript</h1>
      <span class="badge">PupPet Support</span>
    </div>
  </header>
  <div class="info-bar">
    <div class="info-item"><label>Category</label><span>{category}</span></div>
    <div class="info-item"><label>Owner</label><span>&lt;@{owner_id}&gt;</span></div>
    <div class="info-item"><label>Claimed By</label><span>{claimer}</span></div>
    <div class="info-item"><label>Priority</label><span>{priority}</span></div>
    <div class="info-item"><label>Opened</label><span>{opened_at}</span></div>
  </div>
  <div class="messages">
    {messages_html}
  </div>
  <footer>
    Generated by PupPet Ticket Bot •
    {datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}
  </footer>
</body>
</html>"""
