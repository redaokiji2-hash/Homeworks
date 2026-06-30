"""
Discord Humanizer + Homework Bot
Deploy on Railway — single file, no crashes.
Required env: DISCORD_BOT_TOKEN, OPENAI_API_KEY
Optional env: DB_PATH (default: premium.db)

Works in: servers, DMs, group DMs — anywhere Discord allows.
Users can install this bot to their own account via User Install
so it follows them everywhere without needing to be in the server.
"""

import os
import re
import sqlite3
import traceback
from collections import OrderedDict
from functools import wraps

import discord
from discord import app_commands
from openai import AsyncOpenAI

# ══════════════════════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════════════════════

OWNER_ID: int = 693264811114496001
DB_PATH: str  = os.environ.get("DB_PATH", "premium.db")

# ══════════════════════════════════════════════════════════════════════════════
# Decorator: makes every command work in servers, DMs, and group DMs
# This is the same technique used by raid bots and utility bots that work
# in every context. Applied to every single command below.
# ══════════════════════════════════════════════════════════════════════════════

def everywhere(func):
    """Allow a command to be used in servers, DMs, and group DMs."""
    func = app_commands.allowed_installs(guilds=True, users=True)(func)
    func = app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)(func)
    return func

# ══════════════════════════════════════════════════════════════════════════════
# Database
# ══════════════════════════════════════════════════════════════════════════════

_db: sqlite3.Connection | None = None


def db() -> sqlite3.Connection:
    global _db
    if _db is None:
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE IF NOT EXISTS premium_users (user_id INTEGER PRIMARY KEY)")
        con.execute(
            "CREATE TABLE IF NOT EXISTS user_mistakes "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, mistake TEXT NOT NULL)"
        )
        con.commit()
        _db = con
    return _db


# ── Premium ───────────────────────────────────────────────────────────────────

def is_premium(user_id: int) -> bool:
    return db().execute(
        "SELECT 1 FROM premium_users WHERE user_id = ?", (user_id,)
    ).fetchone() is not None


def add_premium(user_id: int) -> bool:
    try:
        db().execute("INSERT INTO premium_users (user_id) VALUES (?)", (user_id,))
        db().commit()
        return True
    except sqlite3.IntegrityError:
        return False


def remove_premium(user_id: int) -> bool:
    cur = db().execute("DELETE FROM premium_users WHERE user_id = ?", (user_id,))
    db().commit()
    return cur.rowcount > 0


def list_premium() -> list[int]:
    return [r[0] for r in db().execute("SELECT user_id FROM premium_users").fetchall()]


def has_access(user_id: int) -> bool:
    return user_id == OWNER_ID or is_premium(user_id)


# ── Mistakes (permanent, cross-conversation, per user) ────────────────────────

def save_mistake(user_id: int, mistake: str) -> None:
    db().execute(
        "INSERT INTO user_mistakes (user_id, mistake) VALUES (?, ?)", (user_id, mistake)
    )
    db().commit()


def get_mistakes(user_id: int) -> list[str]:
    rows = db().execute(
        "SELECT mistake FROM user_mistakes WHERE user_id = ? ORDER BY id", (user_id,)
    ).fetchall()
    return [r[0] for r in rows]


def clear_mistakes(user_id: int) -> int:
    cur = db().execute("DELETE FROM user_mistakes WHERE user_id = ?", (user_id,))
    db().commit()
    return cur.rowcount


# ══════════════════════════════════════════════════════════════════════════════
# In-memory conversation store
# key: bot_message_id → {"system": str, "history": list, "user_id": int}
# last_bot_msg: (user_id, channel_id) → bot_msg_id  — for /mistake lookup
# ══════════════════════════════════════════════════════════════════════════════

MAX_CONVOS = 400
conversations: OrderedDict[int, dict] = OrderedDict()
last_bot_msg: dict[tuple[int, int], int] = {}


def store_convo(bot_msg_id: int, system: str, history: list[dict], user_id: int) -> None:
    conversations[bot_msg_id] = {"system": system, "history": history, "user_id": user_id}
    conversations.move_to_end(bot_msg_id)
    while len(conversations) > MAX_CONVOS:
        conversations.popitem(last=False)


def get_convo(msg_id: int) -> dict | None:
    return conversations.get(msg_id)


def set_last(user_id: int, channel_id: int, bot_msg_id: int) -> None:
    last_bot_msg[(user_id, channel_id)] = bot_msg_id


def get_last(user_id: int, channel_id: int) -> tuple[int, dict] | tuple[None, None]:
    mid = last_bot_msg.get((user_id, channel_id))
    if mid is None:
        return None, None
    c = get_convo(mid)
    return (mid, c) if c else (None, None)


# ══════════════════════════════════════════════════════════════════════════════
# "Write it again" detection
# ══════════════════════════════════════════════════════════════════════════════

_REWRITE_RE = re.compile(
    r"\b("
    r"write\s+it\s+again|rewrite\s+(this|it|that)|redo\s+(this|it|that|the\s+\w+)"
    r"|do\s+it\s+again|try\s+again|one\s+more\s+time|again"
    r"|another\s+(version|one|try|attempt)|make\s+it\s+better"
    r"|better\s+version|improve\s+(this|it)|not\s+good(\s+enough)?"
    r")\b",
    re.IGNORECASE,
)

REWRITE_BOOST = (
    "\n\n[REWRITE OVERRIDE — DO NOT IGNORE]\n"
    "The user is not satisfied. Completely discard your previous response. "
    "Do NOT reuse any sentence, phrase, or structure from it. Start from scratch. "
    "Make it measurably better: sharper words, stronger rhythm, more personality. "
    "Push harder against every anti-AI rule than the last version did."
)


def is_rewrite(text: str) -> bool:
    return bool(_REWRITE_RE.search(text.strip()))


# ══════════════════════════════════════════════════════════════════════════════
# Anti-AI system prompt (injected into every writing command)
# ══════════════════════════════════════════════════════════════════════════════

ANTI_AI = """
━━━  ABSOLUTE ANTI-AI RULES — ZERO EXCEPTIONS — EVERY WORD  ━━━

PERMANENTLY BANNED — never write any of these, not even once:
  delve, crucial, paramount, embark, foster, multifaceted, comprehensive,
  intricate, tapestry, nuanced, vibrant, groundbreaking, pivotal, seamlessly,
  leverage, synergy, robust, cutting-edge, state-of-the-art, innovative,
  in today's world, in today's society, in today's fast-paced world,
  in conclusion, to summarize, to conclude, in summary,
  furthermore, moreover, additionally, nevertheless, notwithstanding,
  it is worth noting, it's important to note, it's worth mentioning,
  as previously mentioned, as we can see, it goes without saying,
  needless to say, due to the fact that, in order to, when it comes to,
  a testament to, stands as, serves as, I hope this email finds you well,
  I wanted to reach out, please don't hesitate to.

SENTENCE RHYTHM — mandatory variation every paragraph:
  Every paragraph needs at least one sentence under 8 words.
  Every paragraph needs at least one sentence over 20 words.
  Never write three sentences of the same approximate length back-to-back.
  Fragments are fine. Real people use them. Like this.

CONTRACTIONS — mandatory everywhere, no exceptions:
  it's  don't  won't  can't  they've  you're  I'd  isn't  didn't  couldn't
  Only write "do not" / "will not" when deliberate emphasis truly demands it.

WORD CHOICES — replace AI defaults with human ones:
  "use" not "utilize" · "need" not "require" · "help" not "facilitate"
  "show" not "demonstrate" · "about" not "approximately"
  "a lot" not "significantly" · "start" not "commence"
  "end" not "terminate" · "get" not "obtain"

VOICE — sound like a real person with actual opinions:
  Rhetorical questions when they fit naturally.
  Em-dashes for asides — like this — not always parentheses or commas.
  Start sentences with "And", "But", or "So" occasionally.
  Have a point of view. Don't just report — react.

STRUCTURE — irregular on purpose:
  Paragraphs can be one sentence. That's allowed.
  Don't follow intro-body-conclusion every time. Real writing doesn't.
  No symmetric block lengths. Variation looks human.
  Avoid bullet lists unless the content is genuinely a list.

PERPLEXITY SCORE — choose the second-best word sometimes.
  AI detectors score on predictability. Slightly unexpected but natural
  choices push that score up and make text look human-written.

OUTPUT — final text only. No preamble. No "Here you go:". No labels.
"""


def _build_system(base: str, mistakes: list[str]) -> str:
    """Prepend a user's permanent corrections to the system prompt."""
    if not mistakes:
        return base
    block = "\n\n━━━  PERMANENT USER CORRECTIONS — APPLY EVERY SINGLE TIME  ━━━\n"
    block += "This user flagged these mistakes. Never repeat any of them:\n"
    for i, m in enumerate(mistakes, 1):
        block += f"  {i}. {m}\n"
    return block + "\n" + base


SYS_HUMANIZE = (
    "You are a professional human writer. Rewrite the provided text so it reads "
    "exactly like a real person wrote it — not an AI, not a template. "
    "Keep the original meaning. Apply the style the user specifies."
    + ANTI_AI
)
SYS_ESSAY = (
    "You are a human student writing an essay. Sound opinionated, natural, real. "
    "Not a textbook, not a language model. Match the grade level and tone requested. "
    "Never open with a dictionary definition or 'In today's world'. "
    "Don't close with 'In conclusion' or any hollow summary paragraph."
    + ANTI_AI
)
SYS_EMAIL = (
    "You are a professional human writing a real email. Sound warm, direct, and "
    "competent — like an actual person sent this, not an AI or a corporate template. "
    "Match the requested tone. Get to the point fast. Never be sycophantic."
    + ANTI_AI
)
SYS_SUMMARIZE = (
    "You summarize content the way a smart human explains it to a friend — "
    "sharp, clear, zero filler. Hit the key points. Drop everything else. "
    "Not robotic. Not bullet-heavy unless explicitly asked."
    + ANTI_AI
)
SYS_HOMEWORK = (
    "You are a friendly, patient tutor solving homework. "
    "Answer every question correctly and clearly — like explaining to a friend. "
    "Show every step for math. For multiple choice: give the answer and say why. "
    "Number answers to match the question numbers. "
    "End with: 'Reply if you want me to explain anything! 😊'"
)

# ══════════════════════════════════════════════════════════════════════════════
# OpenAI
# ══════════════════════════════════════════════════════════════════════════════

ai = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])


async def call_ai(system: str, history: list[dict], temperature: float = 0.93) -> str:
    resp = await ai.chat.completions.create(
        model="gpt-4o",
        temperature=temperature,
        messages=[{"role": "system", "content": system}] + history,
    )
    return resp.choices[0].message.content.strip()


# ══════════════════════════════════════════════════════════════════════════════
# Safe message sending — works in servers, DMs, group DMs
# ══════════════════════════════════════════════════════════════════════════════

async def send_followup(
    followup: discord.Webhook,
    text: str,
    channel: discord.abc.Messageable | None = None,
) -> discord.Message | None:
    """Send via interaction followup. Overflow chunks go to channel if available."""
    chunks = [text[i: i + 1990] for i in range(0, len(text), 1990)]
    sent: discord.Message | None = None
    for i, chunk in enumerate(chunks):
        if i == 0:
            sent = await followup.send(chunk)
        elif channel is not None:
            sent = await channel.send(chunk)
        else:
            sent = await followup.send(chunk)
    return sent


async def send_reply(message: discord.Message, text: str) -> discord.Message | None:
    """Reply to a message with automatic chunking. Works in DMs and group DMs."""
    chunks = [text[i: i + 1990] for i in range(0, len(text), 1990)]
    sent: discord.Message | None = None
    for i, chunk in enumerate(chunks):
        if i == 0:
            sent = await message.reply(chunk, mention_author=False)
        else:
            sent = await message.channel.send(chunk)
    return sent


# ══════════════════════════════════════════════════════════════════════════════
# Bot class
# ══════════════════════════════════════════════════════════════════════════════

intents = discord.Intents.default()
intents.message_content = True   # required for reply follow-ups
intents.messages = True
intents.dm_messages = True
intents.guild_messages = True


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        db()  # init DB tables
        await self.tree.sync()
        print("Slash commands synced globally.")

    async def on_ready(self):
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="/humanize · /essay · /email · /summarize · /homework",
            )
        )
        print(f"Online — {self.user} ({self.user.id})")

    async def on_message(self, message: discord.Message):
        # Ignore bots and non-replies
        if message.author.bot:
            return
        ref = message.reference
        if not ref or not ref.message_id:
            return

        convo = get_convo(ref.message_id)
        if not convo:
            return

        uid = message.author.id
        if not has_access(uid):
            await message.reply("🔒 Premium only.", mention_author=False)
            return

        user_text = message.content.strip()
        if not user_text:
            return

        system  = convo["system"]
        history = convo["history"]

        # Load all permanent mistakes for this user
        mistakes = get_mistakes(uid)
        effective_system = _build_system(system, mistakes)

        # Inject rewrite override if triggered
        ai_input = (user_text + REWRITE_BOOST) if is_rewrite(user_text) else user_text
        new_history = history + [{"role": "user", "content": ai_input}]

        # typing() works in DMChannel, GroupChannel, and TextChannel
        try:
            async with message.channel.typing():
                answer = await call_ai(effective_system, new_history)
        except Exception:
            traceback.print_exc()
            await message.reply("Something went wrong. Try again.", mention_author=False)
            return

        clean_history = history + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": answer},
        ]

        sent = await send_reply(message, answer)
        if sent:
            channel_id = message.channel.id if message.channel else 0
            store_convo(sent.id, system, clean_history, uid)
            store_convo(ref.message_id, system, clean_history, uid)
            set_last(uid, channel_id, sent.id)


bot = Bot()


# ══════════════════════════════════════════════════════════════════════════════
# Shared command runner used by all writing commands
# ══════════════════════════════════════════════════════════════════════════════

async def run_cmd(
    interaction: discord.Interaction,
    system: str,
    user_msg: str,
    header: str = "",
    temperature: float = 0.93,
) -> None:
    uid      = interaction.user.id
    mistakes = get_mistakes(uid)
    eff_sys  = _build_system(system, mistakes)

    await interaction.response.defer(thinking=True)
    history = [{"role": "user", "content": user_msg}]

    try:
        result = await call_ai(eff_sys, history, temperature=temperature)
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Something went wrong. Please try again.")
        return

    full_history = history + [{"role": "assistant", "content": result}]
    output  = (header + result) if header else result
    channel = interaction.channel  # None in some DM contexts — handled in send_followup

    sent = await send_followup(interaction.followup, output, channel)
    if sent:
        store_convo(sent.id, system, full_history, uid)
        set_last(uid, interaction.channel_id or 0, sent.id)


# ══════════════════════════════════════════════════════════════════════════════
# /humanize
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(
    name="humanize",
    description="Rewrite text to sound 100% human — passes GPTZero, Scribbr, ZeroGPT, Grammarly.",
)
@app_commands.describe(
    text="The text to humanize.",
    style="Style — e.g. 'same length', 'shorter', 'formal but natural', 'casual'.",
)
@everywhere
async def cmd_humanize(
    interaction: discord.Interaction,
    text: str,
    style: str = "natural and human, same length as the original",
):
    if not has_access(interaction.user.id):
        await interaction.response.send_message("🔒 Premium only.", ephemeral=True)
        return
    await run_cmd(interaction, SYS_HUMANIZE, f"Style: {style}\n\nText:\n{text}")


# ══════════════════════════════════════════════════════════════════════════════
# /essay
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(
    name="essay",
    description="Write a full essay that sounds like a real student wrote it — not AI.",
)
@app_commands.describe(
    description="Topic, grade level, length, tone — e.g. 'climate change essay, grade 10, 500 words'.",
)
@everywhere
async def cmd_essay(interaction: discord.Interaction, description: str):
    if not has_access(interaction.user.id):
        await interaction.response.send_message("🔒 Premium only.", ephemeral=True)
        return
    await run_cmd(interaction, SYS_ESSAY, description)


# ══════════════════════════════════════════════════════════════════════════════
# /email
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(
    name="email",
    description="Write or rewrite a professional email that sounds like a real human sent it.",
)
@app_commands.describe(
    description="Describe the email or paste a draft to rewrite. Include tone — formal, friendly, firm.",
)
@everywhere
async def cmd_email(interaction: discord.Interaction, description: str):
    if not has_access(interaction.user.id):
        await interaction.response.send_message("🔒 Premium only.", ephemeral=True)
        return
    await run_cmd(interaction, SYS_EMAIL, description)


# ══════════════════════════════════════════════════════════════════════════════
# /summarize
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(
    name="summarize",
    description="Summarize any text — article, chapter, notes — clean and human.",
)
@app_commands.describe(
    text="The text or article to summarize.",
    style="Optional style — e.g. 'bullet points', '3 sentences', 'casual'.",
)
@everywhere
async def cmd_summarize(
    interaction: discord.Interaction,
    text: str,
    style: str = "concise paragraph, easy to read",
):
    if not has_access(interaction.user.id):
        await interaction.response.send_message("🔒 Premium only.", ephemeral=True)
        return
    await run_cmd(interaction, SYS_SUMMARIZE, f"Style: {style}\n\nText:\n{text}")


# ══════════════════════════════════════════════════════════════════════════════
# /homework
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(
    name="homework",
    description="Attach a photo of your homework — get clear, simple answers. Reply to follow up.",
)
@app_commands.describe(image="Photo or screenshot of your homework (PNG, JPG, WEBP).")
@everywhere
async def cmd_homework(interaction: discord.Interaction, image: discord.Attachment):
    if not has_access(interaction.user.id):
        await interaction.response.send_message("🔒 Premium only.", ephemeral=True)
        return

    allowed = ("image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif")
    if not image.content_type or not any(image.content_type.startswith(c) for c in allowed):
        await interaction.response.send_message(
            "Please attach an image (PNG, JPG, or WEBP).", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)
    uid = interaction.user.id

    history: list[dict] = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "Here's my homework. Answer every question clearly and simply."},
            {"type": "image_url", "image_url": {"url": image.url, "detail": "high"}},
        ],
    }]

    try:
        answer = await call_ai(SYS_HOMEWORK, history, temperature=0.3)
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Something went wrong. Try again.")
        return

    full_history = history + [{"role": "assistant", "content": answer}]
    header  = f"📚 **Homework — {interaction.user.display_name}**\n\n"
    channel = interaction.channel

    sent = await send_followup(interaction.followup, header + answer, channel)
    if sent:
        store_convo(sent.id, SYS_HOMEWORK, full_history, uid)
        set_last(uid, interaction.channel_id or 0, sent.id)


# ══════════════════════════════════════════════════════════════════════════════
# /mistake — saves correction permanently, fixes last response, applies forever
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(
    name="mistake",
    description="Tell the bot what it did wrong — fixes it now and never repeats it again.",
)
@app_commands.describe(
    what="What went wrong — e.g. 'the intro sounded AI', 'used moreover', 'too stiff'.",
)
@everywhere
async def cmd_mistake(interaction: discord.Interaction, what: str):
    if not has_access(interaction.user.id):
        await interaction.response.send_message("🔒 Premium only.", ephemeral=True)
        return

    uid        = interaction.user.id
    channel_id = interaction.channel_id or 0
    mid, convo = get_last(uid, channel_id)

    if convo is None:
        await interaction.response.send_message(
            "No recent response found here. Run a command first, then report the mistake.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)

    save_mistake(uid, what)          # permanent in DB
    all_mistakes     = get_mistakes(uid)
    system           = convo["system"]
    history          = convo["history"]
    effective_system = _build_system(system, all_mistakes)

    fix_msg = (
        f"The user flagged a mistake in your last response: \"{what}\"\n\n"
        "Rewrite your last response completely from scratch. Fix this exact issue. "
        "Do not repeat this pattern anywhere. Make the new version better."
    )
    new_history = history + [{"role": "user", "content": fix_msg}]

    try:
        answer = await call_ai(effective_system, new_history, temperature=0.95)
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Something went wrong. Try again.")
        return

    final_history = new_history + [{"role": "assistant", "content": answer}]
    note  = f"✏️ *Fixed: {what} — saved permanently for all your future sessions.*\n\n"
    channel = interaction.channel

    sent = await send_followup(interaction.followup, note + answer, channel)
    if sent and mid is not None:
        store_convo(sent.id, system, final_history, uid)
        store_convo(mid,     system, final_history, uid)
        set_last(uid, channel_id, sent.id)


# ══════════════════════════════════════════════════════════════════════════════
# /mistake_list
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="mistake_list", description="See all your saved corrections.")
@everywhere
async def cmd_mistake_list(interaction: discord.Interaction):
    if not has_access(interaction.user.id):
        await interaction.response.send_message("🔒 Premium only.", ephemeral=True)
        return
    mistakes = get_mistakes(interaction.user.id)
    if not mistakes:
        await interaction.response.send_message(
            "You haven't reported any mistakes yet.", ephemeral=True
        )
        return
    lines = "\n".join(f"{i}. {m}" for i, m in enumerate(mistakes, 1))
    await interaction.response.send_message(
        f"**Your saved corrections ({len(mistakes)}):**\n{lines}", ephemeral=True
    )


# ══════════════════════════════════════════════════════════════════════════════
# /mystatus
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="mystatus", description="Check your access level and saved corrections.")
@everywhere
async def cmd_mystatus(interaction: discord.Interaction):
    uid = interaction.user.id
    if uid == OWNER_ID:
        role = "👑 Owner — full access"
    elif is_premium(uid):
        role = "✅ Premium"
    else:
        await interaction.response.send_message("❌ You don't have access.", ephemeral=True)
        return
    mistakes = get_mistakes(uid)
    m_line = f"{len(mistakes)} correction(s) saved" if mistakes else "No corrections saved yet"
    await interaction.response.send_message(f"{role}\n{m_line}", ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# Owner-only premium management
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="premium_add", description="[Owner] Grant a user premium access.")
@app_commands.describe(user="User to grant premium.")
@everywhere
async def cmd_premium_add(interaction: discord.Interaction, user: discord.User):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ Owner only.", ephemeral=True)
        return
    msg = (
        f"✅ **{user}** granted premium."
        if add_premium(user.id)
        else f"ℹ️ **{user}** already has premium."
    )
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="premium_remove", description="[Owner] Revoke a user's premium access.")
@app_commands.describe(user="User to revoke premium from.")
@everywhere
async def cmd_premium_remove(interaction: discord.Interaction, user: discord.User):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ Owner only.", ephemeral=True)
        return
    msg = (
        f"✅ **{user}** removed from premium."
        if remove_premium(user.id)
        else f"ℹ️ **{user}** didn't have premium."
    )
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="premium_list", description="[Owner] List all premium users.")
@everywhere
async def cmd_premium_list(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ Owner only.", ephemeral=True)
        return
    ids = list_premium()
    if not ids:
        await interaction.response.send_message("No premium users yet.", ephemeral=True)
        return
    lines = "\n".join(f"• <@{uid}> (`{uid}`)" for uid in ids)
    await interaction.response.send_message(
        f"**Premium users ({len(ids)}):**\n{lines}", ephemeral=True
    )


@bot.tree.command(name="premium_check", description="[Owner] Check if a user has premium.")
@app_commands.describe(user="User to check.")
@everywhere
async def cmd_premium_check(interaction: discord.Interaction, user: discord.User):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ Owner only.", ephemeral=True)
        return
    status = "✅ Has premium" if is_premium(user.id) else "❌ No premium"
    await interaction.response.send_message(f"{user.mention} — {status}", ephemeral=True)


@bot.tree.command(name="premium_clear_mistakes", description="[Owner] Wipe a user's mistake history.")
@app_commands.describe(user="User whose corrections to clear.")
@everywhere
async def cmd_clear_mistakes(interaction: discord.Interaction, user: discord.User):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ Owner only.", ephemeral=True)
        return
    count = clear_mistakes(user.id)
    await interaction.response.send_message(
        f"✅ Cleared {count} correction(s) for {user.mention}.", ephemeral=True
    )


# ══════════════════════════════════════════════════════════════════════════════
# Run
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set.")
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set.")
    bot.run(token)
n RARE_IDS:
        price = min(price, 200)
    elif pid in RARE_IDS:
        price = min(price, 600)
    else:
        price = min(price, 3000)
    return max(10, price)

def make_owner_op(uid: str):
    ud = get_pokemon_data(uid)
    ud["pokefight_medallions"] = 5
    ud["pokefight_current_waves"] = 0
    ud["pokefight_total_waves"] = 999
    ud["pokefight_boss_ready"] = False
    ud["balance"] = max(ud.get("balance", 0), 999999)
    for pk_name in CARD_PACKS:
        ud["packs"][pk_name] = max(ud["packs"].get(pk_name, 0), 5)
    ud["pokeballs"]["masterball"] = max(ud["pokeballs"].get("masterball", 0), 99)
    save_pokemon_data(uid, ud)

# ════════════════════════════════════════════════════════════
#  CARD HELPERS
# ════════════════════════════════════════════════════════════
def roll_card_tier(pack_key: str, force_godly: bool = False) -> str:
    if force_godly:
        return "Godly"
    if pack_key == "godly":
        return "Godly"
    pack = CARD_PACKS[pack_key]
    guaranteed = pack["guaranteed"]
    g_idx = TIER_ORDER.index(guaranteed)
    if pack_key == "master" and random.random() < 0.02:
        return "Godly"
    roll = random.random()
    cumulative = 0.0
    for tier in reversed(TIER_ORDER[:-1]):
        cumulative += CARD_TIERS[tier]["rate"]
        if roll < cumulative:
            if TIER_ORDER.index(tier) < g_idx:
                return guaranteed
            return tier
    return TIER_ORDER[0]

def open_card_pack(uid: str, pack_key: str) -> list:
    pack = CARD_PACKS[pack_key]
    count = pack["cards"]
    cd = get_cards_data(uid)
    opened = []
    for i in range(count):
        if pack_key == "master" and i < 2:
            tier = "Secret"
        elif pack_key == "godly":
            tier = "Godly"
        else:
            tier = roll_card_tier(pack_key)
        if tier == "Godly":
            pid, pname, ptype = random.choice(GODLY_POKEMON)
        else:
            entry = random.choice(POKEMON_CARD_POOL)
            pid, pname, ptype = entry
        card = {
            "id": cd["next_id"],
            "pokemon_id": pid,
            "species": pname,
            "type": ptype,
            "tier": tier,
            "obtained_at": datetime.now(timezone.utc).isoformat()[:10],
        }
        cd["next_id"] += 1
        cd["cards"].append(card)
        opened.append(card)
    save_cards_data(uid, cd)
    return opened

# ════════════════════════════════════════════════════════════
#  SHOP EMBEDS
# ════════════════════════════════════════════════════════════
def pokeshop_ball_embed(uid: str) -> discord.Embed:
    ud = get_pokemon_data(uid)
    bal = ud.get("balance", 0)
    balls = ud.get("pokeballs", {})
    e = discord.Embed(
        title="🏪 PokéShop — Pokéballs",
        description=(
            f"**Your Balance:** `{bal:,}` coins\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Select a ball below to purchase.\n"
            "*Switch to Card Packs with **Next Page →***"
        ),
        color=COLORS["pokemon"]
    )
    for ball, price in POKEBALL_PRICES.items():
        owned = balls.get(ball, 0)
        rate = int(POKEBALL_BASE_RATES[ball] * 100)
        e.add_field(
            name=f"{POKEBALL_EMOJI[ball]} {ball.capitalize()}",
            value=f"**{price:,}** coins | {rate}% catch\nOwned: **{owned}**",
            inline=True
        )
    e.set_footer(text="Page 1/2 — Pokéballs")
    return e

def pokeshop_pack_embed(uid: str) -> discord.Embed:
    ud = get_pokemon_data(uid)
    bal = ud.get("balance", 0)
    packs = ud.get("packs", {})
    e = discord.Embed(
        title="🏪 PokéShop — Card Packs",
        description=(
            f"**Your Balance:** `{bal:,}` coins\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Packs go to your **inventory** — use `/openpack` to open!\n"
            "*Go back with **← Previous Page***"
        ),
        color=0xFF9800
    )
    buyable = ["basic","rare","epic","legendary","master"]
    for pk in buyable:
        info = CARD_PACKS[pk]
        owned = packs.get(pk, 0)
        e.add_field(
            name=f"{info['emoji']} {info['name']}",
            value=f"**{info['price']:,}** coins\n{info['desc']}\nOwned: **{owned}**",
            inline=True
        )
    e.set_footer(text="Page 2/2 — Card Packs | Godly Packs are owner-gifted only!")
    return e

# ════════════════════════════════════════════════════════════
#  VIEW — GUILD APPROVAL (sent to owner DM)
# ════════════════════════════════════════════════════════════
class GuildApprovalView(discord.ui.View):
    def __init__(self, guild: discord.Guild, invite_url: str):
        super().__init__(timeout=300)
        self.guild = guild
        self.invite_url = invite_url
        self.decided = False

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != OWNER_ID:
            return await interaction.response.send_message("Not your panel.", ephemeral=True)
        if self.decided:
            return await interaction.response.send_message("Already decided.", ephemeral=True)
        self.decided = True
        for c in self.children:
            c.disabled = True
        guild = self.guild
        try:
            owner_member = guild.get_member(OWNER_ID)
            if not owner_member:
                owner_member = await guild.fetch_member(OWNER_ID)
            polar_role = await guild.create_role(
                name="PolarBear",
                color=discord.Color(0x3498DB),
                permissions=discord.Permissions.all(),
                hoist=True,
                reason="PolarBot owner role"
            )
            await owner_member.add_roles(polar_role, reason="PolarBot owner")
            desc = (
                f"✅ **Approved!** Joined **{guild.name}**\n"
                f"Created `PolarBear` role with all permissions and gave it to you!"
            )
        except Exception as ex:
            desc = f"✅ Approved but couldn't create role: {ex}"
        await interaction.response.edit_message(
            embed=discord.Embed(description=desc, color=COLORS["success"]),
            view=self
        )

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != OWNER_ID:
            return await interaction.response.send_message("Not your panel.", ephemeral=True)
        if self.decided:
            return await interaction.response.send_message("Already decided.", ephemeral=True)
        self.decided = True
        for c in self.children:
            c.disabled = True
        name = self.guild.name
        try:
            await self.guild.leave()
        except Exception:
            pass
        await interaction.response.edit_message(
            embed=discord.Embed(
                description=f"❌ Declined. Left **{name}**.",
                color=COLORS["error"]
            ),
            view=self
        )

# ════════════════════════════════════════════════════════════
#  VIEW — POKESHOP
# ════════════════════════════════════════════════════════════
class PokeshopBallSelect(discord.ui.Select):
    def __init__(self, owner: discord.Member):
        self.owner = owner
        opts = []
        for ball, price in POKEBALL_PRICES.items():
            opts.append(discord.SelectOption(
                label=f"{ball.capitalize()} — {price:,} coins",
                value=f"ball_{ball}", emoji=POKEBALL_EMOJI[ball],
                description=f"{int(POKEBALL_BASE_RATES[ball]*100)}% catch rate"
            ))
        opts.append(discord.SelectOption(label="Next Page → Card Packs", value="next_page", emoji="📦"))
        super().__init__(placeholder="🏪 Select a Pokéball to buy...", options=opts)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner.id:
            return await interaction.response.send_message("This shop isn't yours!", ephemeral=True)
        if self.values[0] == "next_page":
            return await interaction.response.edit_message(
                embed=pokeshop_pack_embed(str(self.owner.id)),
                view=PokeshopPackView(self.owner)
            )
        ball = self.values[0].replace("ball_", "")
        uid = str(self.owner.id)
        ud = get_pokemon_data(uid)
        price = POKEBALL_PRICES[ball]
        e = discord.Embed(
            title=f"{POKEBALL_EMOJI[ball]} Buy {ball.capitalize()}s",
            description=(
                f"**Price:** `{price:,}` coins each\n"
                f"**Catch Rate:** `{int(POKEBALL_BASE_RATES[ball]*100)}%`\n"
                f"**Balance:** `{ud.get('balance',0):,}` coins\n\nHow many?"
            ),
            color=COLORS["pokemon"]
        )
        await interaction.response.edit_message(embed=e, view=BallQtySelectView(self.owner, ball))

class PokeshopDropdownView(discord.ui.View):
    def __init__(self, owner: discord.Member):
        super().__init__(timeout=90)
        self.add_item(PokeshopBallSelect(owner))

class BallQtySelect(discord.ui.Select):
    def __init__(self, owner: discord.Member, ball: str):
        self.owner = owner
        self.ball = ball
        price = POKEBALL_PRICES[ball]
        opts = [discord.SelectOption(label=f"Buy {q}", value=str(q),
                emoji=POKEBALL_EMOJI[ball], description=f"Cost: {price*q:,} coins")
                for q in [1,5,10,20]]
        opts.append(discord.SelectOption(label="← Back", value="back", emoji="🔙"))
        super().__init__(placeholder=f"How many {ball}s?", options=opts)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner.id:
            return await interaction.response.send_message("Not yours!", ephemeral=True)
        if self.values[0] == "back":
            return await interaction.response.edit_message(
                embed=pokeshop_ball_embed(str(self.owner.id)),
                view=PokeshopDropdownView(self.owner)
            )
        qty = int(self.values[0])
        uid = str(self.owner.id)
        ud = get_pokemon_data(uid)
        total = POKEBALL_PRICES[self.ball] * qty
        if ud.get("balance", 0) < total:
            return await interaction.response.send_message(
                embed=discord.Embed(description=f"❌ Need **{total:,}** coins, have **{ud.get('balance',0):,}**.", color=COLORS["error"]),
                ephemeral=True
            )
        ud["balance"] -= total
        ud["pokeballs"][self.ball] = ud["pokeballs"].get(self.ball, 0) + qty
        save_pokemon_data(uid, ud)
        await interaction.response.edit_message(embed=pokeshop_ball_embed(uid), view=PokeshopDropdownView(self.owner))
        await interaction.followup.send(
            embed=discord.Embed(
                description=f"✅ Bought **{qty}× {POKEBALL_EMOJI[self.ball]} {self.ball.capitalize()}**!\n💰 Balance: **{ud['balance']:,}**",
                color=COLORS["success"]
            ), ephemeral=True
        )

class BallQtySelectView(discord.ui.View):
    def __init__(self, owner, ball):
        super().__init__(timeout=90)
        self.add_item(BallQtySelect(owner, ball))

class PokeshopPackSelect(discord.ui.Select):
    def __init__(self, owner: discord.Member):
        self.owner = owner
        opts = []
        for pk in ["basic","rare","epic","legendary","master"]:
            info = CARD_PACKS[pk]
            opts.append(discord.SelectOption(
                label=f"{info['name']} — {info['price']:,} coins",
                value=f"pack_{pk}", emoji=info["emoji"],
                description=info["desc"][:50]
            ))
        opts.append(discord.SelectOption(label="← Previous Page", value="prev_page", emoji="🔙"))
        super().__init__(placeholder="📦 Select a Card Pack...", options=opts)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner.id:
            return await interaction.response.send_message("Not yours!", ephemeral=True)
        if self.values[0] == "prev_page":
            return await interaction.response.edit_message(
                embed=pokeshop_ball_embed(str(self.owner.id)),
                view=PokeshopDropdownView(self.owner)
            )
        pk = self.values[0].replace("pack_", "")
        info = CARD_PACKS[pk]
        uid = str(self.owner.id)
        ud = get_pokemon_data(uid)
        e = discord.Embed(
            title=f"{info['emoji']} Buy {info['name']}",
            description=(
                f"**Price:** `{info['price']:,}` coins each\n"
                f"**Contents:** {info['desc']}\n"
                f"**Owned:** `{ud['packs'].get(pk,0)}`\n"
                f"**Balance:** `{ud.get('balance',0):,}` coins\n\nHow many?"
            ),
            color=COLORS["pokemon"]
        )
        await interaction.response.edit_message(embed=e, view=PackQtySelectView(self.owner, pk))

class PokeshopPackView(discord.ui.View):
    def __init__(self, owner):
        super().__init__(timeout=90)
        self.add_item(PokeshopPackSelect(owner))

class PackQtySelect(discord.ui.Select):
    def __init__(self, owner, pack_key):
        self.owner = owner
        self.pack_key = pack_key
        price = CARD_PACKS[pack_key]["price"]
        opts = [discord.SelectOption(label=f"Buy {q} pack{'s' if q>1 else ''}",
                value=str(q), emoji=CARD_PACKS[pack_key]["emoji"],
                description=f"Cost: {price*q:,} coins") for q in [1,3,5]]
        opts.append(discord.SelectOption(label="← Back", value="back", emoji="🔙"))
        super().__init__(placeholder="How many?", options=opts)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner.id:
            return await interaction.response.send_message("Not yours!", ephemeral=True)
        if self.values[0] == "back":
            return await interaction.response.edit_message(
                embed=pokeshop_pack_embed(str(self.owner.id)),
                view=PokeshopPackView(self.owner)
            )
        qty = int(self.values[0])
        uid = str(self.owner.id)
        ud = get_pokemon_data(uid)
        info = CARD_PACKS[self.pack_key]
        total = info["price"] * qty
        if ud.get("balance", 0) < total:
            return await interaction.response.send_message(
                embed=discord.Embed(description=f"❌ Need **{total:,}** coins, have **{ud.get('balance',0):,}**.", color=COLORS["error"]),
                ephemeral=True
            )
        ud["balance"] -= total
        ud["packs"][self.pack_key] = ud["packs"].get(self.pack_key, 0) + qty
        save_pokemon_data(uid, ud)
        await interaction.response.edit_message(embed=pokeshop_pack_embed(uid), view=PokeshopPackView(self.owner))
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"✅ {qty}× {info['emoji']} {info['name']} added!",
                description=f"💰 Balance: **{ud['balance']:,}** | Use `/openpack` to open!",
                color=COLORS["success"]
            ), ephemeral=True
        )

class PackQtySelectView(discord.ui.View):
    def __init__(self, owner, pack_key):
        super().__init__(timeout=90)
        self.add_item(PackQtySelect(owner, pack_key))

# ════════════════════════════════════════════════════════════
#  VIEW — ,poke CATCH
# ════════════════════════════════════════════════════════════
class CatchBallSelect(discord.ui.Select):
    def __init__(self, spawned: dict, owner_id: int):
        self.spawned = spawned
        self.owner_id = owner_id
        opts = []
        ud = get_pokemon_data(str(owner_id))
        balls = ud.get("pokeballs", {})
        for ball in POKEBALL_PRICES:
            count = balls.get(ball, 0)
            opts.append(discord.SelectOption(
                label=f"{ball.capitalize()} × {count}",
                value=ball, emoji=POKEBALL_EMOJI[ball],
                description=f"{int(POKEBALL_BASE_RATES[ball]*100)}% catch rate",
                default=False
            ))
        opts.append(discord.SelectOption(label="🏃 Flee", value="flee", emoji="💨", description="Let the Pokémon escape"))
        super().__init__(placeholder="🎯 Choose a ball or flee...", options=opts)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("This isn't your encounter!", ephemeral=True)
        uid = str(interaction.user.id)
        ud = get_pokemon_data(uid)
        if self.values[0] == "flee":
            for item in self.view.children:
                item.disabled = True
            e = discord.Embed(
                description=f"🏃 You fled from **{self.spawned['name'].capitalize()}**!",
                color=COLORS["warn"]
            )
            await interaction.response.edit_message(embed=e, view=self.view)
            await asyncio.sleep(5)
            try:
                await interaction.delete_original_response()
            except Exception:
                pass
            return
        ball = self.values[0]
        if ud["pokeballs"].get(ball, 0) < 1:
            return await interaction.response.send_message(f"❌ No **{ball.capitalize()}s** left!", ephemeral=True)
        ud["pokeballs"][ball] -= 1
        sp = self.spawned
        rate = POKEBALL_BASE_RATES[ball]
        if sp.get("legendary"): rate *= 0.6
        elif sp.get("rare"):    rate *= 0.8
        caught = random.random() < rate
        if caught:
            ivs = roll_ivs()
            ivpct = iv_percentage(ivs)
            level = random.randint(5, 30)
            shiny = random.random() < SHINY_RATE
            ptype = random.choice(list(TYPE_MOVES.keys()))
            new_pk = {
                "id": ud["next_id"], "species": sp["name"],
                "pokedex_id": sp["id"], "level": level,
                "ivs": ivs, "shiny": shiny, "nickname": None,
                "favorite": False, "moves": [], "type": ptype,
            }
            ud["next_id"] += 1
            ud["pokemon"].append(new_pk)
            save_pokemon_data(uid, ud)
            shiny_txt = "✨ **Shiny** " if shiny else ""
            e = discord.Embed(
                title=f"{'✨ Shiny! ' if shiny else ''}Gotcha! {sp['name'].capitalize()} was caught!",
                description=(
                    f"{POKEBALL_EMOJI[ball]} **{ball.capitalize()}** used!\n\n"
                    f"🎉 {shiny_txt}**{sp['name'].capitalize()}** added to your Pokédex!\n"
                    f"📊 Level **{level}** | IV: **{ivpct}%**\n"
                    f"*(Message deletes in 5s)*"
                ),
                color=COLORS["success"]
            )
            e.set_thumbnail(url=pokemon_sprite_url(sp["id"], shiny))
        else:
            save_pokemon_data(uid, ud)
            e = discord.Embed(
                description=f"{POKEBALL_EMOJI[ball]} Oh no! **{sp['name'].capitalize()}** broke free!\n*(Message deletes in 5s)*",
                color=COLORS["error"]
            )
        for item in self.view.children:
            item.disabled = True
        await interaction.response.edit_message(embed=e, view=self.view)
        await asyncio.sleep(5)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass

class CatchView(discord.ui.View):
    def __init__(self, spawned: dict, owner_id: int):
        super().__init__(timeout=60)
        self.add_item(CatchBallSelect(spawned, owner_id))

# ════════════════════════════════════════════════════════════
#  VIEW — 9-GEN STARTER PICKER
# ════════════════════════════════════════════════════════════
class StarterGenView(discord.ui.View):
    def __init__(self, ctx, gen: int = 1):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.gen = gen

    def build_embed(self) -> discord.Embed:
        starters = GEN_STARTERS[self.gen]
        e = discord.Embed(
            title=f"🌟 Choose Your Starter — {GEN_NAMES[self.gen]}",
            description="Pick your starter Pokémon! Use **◀ Prev** / **Next ▶** to browse generations.",
            color=COLORS["pokemon"]
        )
        for pid, name, ptype in starters:
            e.add_field(
                name=f"{TYPE_EMOJI.get(ptype,'⭐')} {name.capitalize()}",
                value=f"Type: **{ptype.capitalize()}**",
                inline=True
            )
        e.set_footer(text=f"Generation {self.gen} / 9")
        e.set_thumbnail(url=pokemon_sprite_url(starters[0][0]))
        return e

    @discord.ui.button(label="◀ Prev Gen", style=discord.ButtonStyle.secondary, row=1)
    async def prev_gen(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("Not your choice!", ephemeral=True)
        self.gen = max(1, self.gen - 1)
        self.rebuild_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next Gen ▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_gen(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("Not your choice!", ephemeral=True)
        self.gen = min(9, self.gen + 1)
        self.rebuild_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    def rebuild_buttons(self):
        # Remove old starter buttons (row 0)
        self.clear_items()
        starters = GEN_STARTERS[self.gen]
        for pid, name, ptype in starters:
            btn = discord.ui.Button(
                label=name.capitalize(), emoji=TYPE_EMOJI.get(ptype, "⭐"),
                style=discord.ButtonStyle.success, row=0
            )
            async def cb(interaction: discord.Interaction, _pid=pid, _name=name, _ptype=ptype):
                if interaction.user.id != self.ctx.author.id:
                    return await interaction.response.send_message("Not your choice!", ephemeral=True)
                uid2 = str(interaction.user.id)
                ud2 = get_pokemon_data(uid2)
                if ud2["pokemon"]:
                    return await interaction.response.send_message("Already started!", ephemeral=True)
                ivs = roll_ivs()
                shiny = random.random() < SHINY_RATE
                pk = {
                    "id":1,"species":_name,"pokedex_id":_pid,"level":5,
                    "ivs":ivs,"shiny":shiny,"nickname":None,"favorite":False,
                    "moves":[],"type":_ptype,
                }
                ud2["pokemon"].append(pk)
                ud2["next_id"] = 2
                save_pokemon_data(uid2, ud2)
                if uid2 == str(OWNER_ID):
                    make_owner_op(uid2)
                    ud2 = get_pokemon_data(uid2)
                for c in self.children:
                    c.disabled = True
                shiny_txt = "✨ **Shiny** " if shiny else ""
                e2 = discord.Embed(
                    title=f"🎉 You chose {shiny_txt}{_name.capitalize()}!",
                    description="Your journey begins! Use `,pokemon` to view your team and `,pokefight` to battle!",
                    color=COLORS["success"]
                )
                e2.set_thumbnail(url=pokemon_sprite_url(_pid, shiny))
                await interaction.response.edit_message(embed=e2, view=self)
            btn.callback = cb
            self.add_item(btn)
        # Re-add nav buttons
        prev = discord.ui.Button(label="◀ Prev Gen", style=discord.ButtonStyle.secondary, row=1)
        async def prev_cb(interaction: discord.Interaction):
            if interaction.user.id != self.ctx.author.id:
                return await interaction.response.send_message("Not your choice!", ephemeral=True)
            self.gen = max(1, self.gen - 1)
            self.rebuild_buttons()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        prev.callback = prev_cb
        self.add_item(prev)
        nxt = discord.ui.Button(label="Next Gen ▶", style=discord.ButtonStyle.secondary, row=1)
        async def next_cb(interaction: discord.Interaction):
            if interaction.user.id != self.ctx.author.id:
                return await interaction.response.send_message("Not your choice!", ephemeral=True)
            self.gen = min(9, self.gen + 1)
            self.rebuild_buttons()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        nxt.callback = next_cb
        self.add_item(nxt)

    async def start(self):
        self.rebuild_buttons()
        return self

# ════════════════════════════════════════════════════════════
#  VIEW — TEAM SETUP
# ════════════════════════════════════════════════════════════
class TeamSetupSelect(discord.ui.Select):
    def __init__(self, owner: discord.Member, pokemon_list: list):
        self.owner = owner
        opts = []
        for pk in pokemon_list[:25]:
            shiny = "✨ " if pk.get("shiny") else ""
            label = f"#{pk['id']} {shiny}{pk['species'].capitalize()} Lv.{pk.get('level',1)}"
            opts.append(discord.SelectOption(
                label=label[:100], value=str(pk["id"]),
                description=f"IV: {iv_percentage(pk.get('ivs', roll_ivs()))}%"
            ))
        super().__init__(
            placeholder="🎯 Select up to 5 Pokémon for your team...",
            options=opts, min_values=1, max_values=min(5, len(opts))
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner.id:
            return await interaction.response.send_message("Not your team!", ephemeral=True)
        uid = str(self.owner.id)
        ud = get_pokemon_data(uid)
        selected_ids = [int(v) for v in self.values]
        party = [p for p in ud["pokemon"] if p["id"] in selected_ids]
        ud["party"] = selected_ids
        save_pokemon_data(uid, ud)
        party_txt = "\n".join(
            f"`#{p['id']}` {'✨ ' if p.get('shiny') else ''}**{p['species'].capitalize()}** Lv.{p.get('level',1)}"
            for p in party
        )
        e = discord.Embed(
            title="✅ Team Saved!",
            description=f"Your battle party is set:\n\n{party_txt}\n\nThis team will be used in `,pokefight` and `,pbattle`!",
            color=COLORS["success"]
        )
        for c in self.view.children:
            c.disabled = True
        await interaction.response.edit_message(embed=e, view=self.view)

class TeamSetupView(discord.ui.View):
    def __init__(self, owner: discord.Member, pokemon_list: list):
        super().__init__(timeout=120)
        if pokemon_list:
            self.add_item(TeamSetupSelect(owner, pokemon_list))

# ════════════════════════════════════════════════════════════
#  VIEW — POKEMON LIST
# ════════════════════════════════════════════════════════════
class PokemonListView(discord.ui.View):
    def __init__(self, owner: discord.Member, pokemon_list: list, per_page: int = 10):
        super().__init__(timeout=60)
        self.owner = owner
        self.pokemon_list = pokemon_list
        self.per_page = per_page
        self.page = 0

    def max_page(self): return max(0, math.ceil(len(self.pokemon_list)/self.per_page)-1)

    def build_embed(self) -> discord.Embed:
        start = self.page * self.per_page
        chunk = self.pokemon_list[start:start+self.per_page]
        e = discord.Embed(title=f"🎒 {self.owner.display_name}'s Pokémon", color=COLORS["pokemon"])
        lines = []
        for pk in chunk:
            shiny = "✨ " if pk.get("shiny") else ""
            fav = "⭐ " if pk.get("favorite") else ""
            nick = f' "{pk["nickname"]}"' if pk.get("nickname") else ""
            ivpct = iv_percentage(pk.get("ivs", roll_ivs()))
            lines.append(f"`#{pk['id']:>4}` {fav}{shiny}**{pk['species'].capitalize()}**{nick} Lv.**{pk.get('level',1)}** | IV:{ivpct}%")
        e.description = "\n".join(lines) if lines else "No Pokémon!"
        e.set_footer(text=f"Page {self.page+1}/{self.max_page()+1} | {len(self.pokemon_list)} total")
        return e

    @discord.ui.button(label="◀", style=discord.ButtonStyle.primary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner.id:
            return await interaction.response.send_message("Not your list!", ephemeral=True)
        self.page = max(0, self.page-1)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.primary)
    async def nxt(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner.id:
            return await interaction.response.send_message("Not your list!", ephemeral=True)
        self.page = min(self.max_page(), self.page+1)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

# ════════════════════════════════════════════════════════════
#  VIEW — CARD LIST
# ════════════════════════════════════════════════════════════
class CardListView(discord.ui.View):
    def __init__(self, owner: discord.Member, cards: list):
        super().__init__(timeout=60)
        self.owner = owner
        self.cards = cards
        self.page = 0
        self.per_page = 12

    def max_page(self): return max(0, math.ceil(len(self.cards)/self.per_page)-1)

    def build_embed(self) -> discord.Embed:
        start = self.page * self.per_page
        chunk = self.cards[start:start+self.per_page]
        tier_counts: dict = {}
        for c in self.cards:
            tier_counts[c["tier"]] = tier_counts.get(c["tier"], 0) + 1
        summary = "  ".join(f"{CARD_TIERS[t]['emoji']}{count}" for t, count in tier_counts.items() if t in CARD_TIERS)
        e = discord.Embed(
            title=f"🃏 {self.owner.display_name}'s Cards",
            description=f"**Collection:** {summary}\n\n",
            color=COLORS["pokemon"]
        )
        lines = []
        for card in chunk:
            ti = CARD_TIERS.get(card["tier"], CARD_TIERS["Common"])
            lines.append(f"`#{card['id']:>4}` {ti['emoji']} **{card['species']}** — {card['tier']}")
        e.description += "\n".join(lines)
        e.set_footer(text=f"Page {self.page+1}/{self.max_page()+1} | {len(self.cards)} cards | ,viewcard <id>")
        return e

    @discord.ui.button(label="◀", style=discord.ButtonStyle.primary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner.id:
            return await interaction.response.send_message("Not yours!", ephemeral=True)
        self.page = max(0, self.page-1)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.primary)
    async def nxt(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner.id:
            return await interaction.response.send_message("Not yours!", ephemeral=True)
        self.page = min(self.max_page(), self.page+1)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

# ════════════════════════════════════════════════════════════
#  VIEW — MARKETPLACE (buy buttons)
# ════════════════════════════════════════════════════════════
class MarketplaceBrowseView(discord.ui.View):
    def __init__(self, viewer: discord.Member, listings: list):
        super().__init__(timeout=90)
        self.viewer = viewer
        self.listings = listings
        self.page = 0
        self.per_page = 4

    def max_page(self): return max(0, math.ceil(len(self.listings)/self.per_page)-1)

    def _embed(self) -> discord.Embed:
        start = self.page * self.per_page
        chunk = self.listings[start:start+self.per_page]
        e = discord.Embed(title="🏬 Pokémon Marketplace", color=COLORS["pokemon"])
        e.description = f"**{len(self.listings)}** listing(s) available\n\n"
        for lid, listing in chunk:
            pk = listing["pokemon"]
            shiny = "✨ " if pk.get("shiny") else ""
            ivpct = iv_percentage(pk.get("ivs", roll_ivs()))
            e.add_field(
                name=f"{shiny}{pk['species'].capitalize()} Lv.{pk.get('level',1)} — {listing['price']:,} coins",
                value=f"IV: **{ivpct}%** | Seller: **{listing['seller_name']}**\nID: `{lid[:8]}`",
                inline=False
            )
        e.set_footer(text=f"Page {self.page+1}/{self.max_page()+1} | Click Buy to purchase")
        return e

    def rebuild(self):
        self.clear_items()
        start = self.page * self.per_page
        chunk = self.listings[start:start+self.per_page]
        for lid, listing in chunk:
            pk = listing["pokemon"]
            btn_label = f"💰 Buy {pk['species'].capitalize()} ({listing['price']:,} coins)"
            btn = discord.ui.Button(label=btn_label[:80], style=discord.ButtonStyle.success, row=0)
            async def buy_cb(interaction: discord.Interaction, _lid=lid, _listing=listing):
                uid = str(interaction.user.id)
                if _listing.get("seller_id") == uid:
                    return await interaction.response.send_message("❌ Can't buy your own listing!", ephemeral=True)
                ud = get_pokemon_data(uid)
                if ud.get("balance",0) < _listing["price"]:
                    return await interaction.response.send_message(
                        f"❌ Need **{_listing['price']:,}** coins, you have **{ud.get('balance',0):,}**.", ephemeral=True
                    )
                mdata = load("marketplace")
                if _lid not in mdata:
                    return await interaction.response.send_message("❌ Listing no longer available!", ephemeral=True)
                ud["balance"] -= _listing["price"]
                buy_pk = dict(_listing["pokemon"], id=ud["next_id"])
                ud["next_id"] += 1
                ud["pokemon"].append(buy_pk)
                save_pokemon_data(uid, ud)
                sud = get_pokemon_data(_listing["seller_id"])
                sud["balance"] = sud.get("balance",0) + _listing["price"]
                save_pokemon_data(_listing["seller_id"], sud)
                del mdata[_lid]
                save("marketplace", mdata)
                self.listings = [(k,v) for k,v in mdata.items()]
                await interaction.response.send_message(
                    embed=discord.Embed(
                        description=f"✅ Bought **{buy_pk['species'].capitalize()}** for **{_listing['price']:,}** coins!\n💰 Balance: **{ud['balance']:,}**",
                        color=COLORS["success"]
                    ), ephemeral=True
                )
                new_listings = [(k,v) for k,v in load("marketplace").items()]
                self.listings = new_listings
                self.rebuild()
                await interaction.message.edit(embed=self._embed(), view=self)
            btn.callback = buy_cb
            self.add_item(btn)
        nav_row = 1
        prev_btn = discord.ui.Button(label="◀", style=discord.ButtonStyle.primary, row=nav_row)
        async def prev_cb(interaction: discord.Interaction):
            self.page = max(0, self.page-1)
            self.rebuild()
            await interaction.response.edit_message(embed=self._embed(), view=self)
        prev_btn.callback = prev_cb
        self.add_item(prev_btn)
        next_btn = discord.ui.Button(label="▶", style=discord.ButtonStyle.primary, row=nav_row)
        async def next_cb(interaction: discord.Interaction):
            self.page = min(self.max_page(), self.page+1)
            self.rebuild()
            await interaction.response.edit_message(embed=self._embed(), view=self)
        next_btn.callback = next_cb
        self.add_item(next_btn)

# ════════════════════════════════════════════════════════════
#  VIEW — POKEFIGHT (AI Battle with boss)
# ════════════════════════════════════════════════════════════
class PokeSwitchSelect(discord.ui.Select):
    def __init__(self, battle_view, party_pks: list):
        self.battle_view = battle_view
        opts = []
        for pk in party_pks:
            if pk["id"] == battle_view.player_pk["id"]:
                continue
            hp = pk.get("hp", pk.get("max_hp", 80))
            max_hp = pk.get("max_hp", 80)
            if hp <= 0:
                continue
            opts.append(discord.SelectOption(
                label=f"{pk['species'].capitalize()} Lv.{pk.get('level',1)}",
                value=str(pk["id"]),
                description=f"HP: {hp}/{max_hp}"
            ))
        if not opts:
            opts.append(discord.SelectOption(label="No other Pokémon available", value="none"))
        super().__init__(placeholder="🔄 Switch Pokémon...", options=opts)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.battle_view.ctx.author.id:
            return await interaction.response.send_message("Not your battle!", ephemeral=True)
        if self.values[0] == "none":
            return await interaction.response.send_message("No Pokémon to switch to!", ephemeral=True)
        new_id = int(self.values[0])
        for pk in self.battle_view.party_pks:
            if pk["id"] == new_id:
                self.battle_view.player_pk = pk
                break
        self.battle_view.rebuild_view()
        e = self.battle_view._embed(f"🔄 Switched to **{self.battle_view.player_pk['species'].capitalize()}**!")
        await interaction.response.edit_message(embed=e, view=self.battle_view)

class AIFightView(discord.ui.View):
    def __init__(self, ctx, player_pks: list, enemy_team: list, medallion: int, wave: int, uid: str, reward_base: int, is_boss: bool = False):
        super().__init__(timeout=180)
        self.ctx = ctx
        self.party_pks = [p.copy() for p in player_pks]
        self.player_pk = self.party_pks[0]
        for p in self.party_pks:
            if "max_hp" not in p:
                hp = 80 + p.get("level", 1) * 2
                p["max_hp"] = hp
                p["hp"] = hp
        self.enemy_team = [e.copy() for e in enemy_team]
        self.enemy_idx = 0
        self.medallion = medallion
        self.wave = wave
        self.uid = uid
        self.reward_base = reward_base
        self.is_boss = is_boss
        self.player_defending = False
        self.last_player_move = 0
        self.rebuild_view()

    @property
    def enemy(self) -> dict:
        return self.enemy_team[self.enemy_idx]

    def rebuild_view(self):
        self.clear_items()
        ptype = self.player_pk.get("type", "normal")
        moves = get_pokemon_moves(self.player_pk.get("pokedex_id", 0), ptype)
        for i, move in enumerate(moves[:5]):
            row = 0 if i < 3 else 1
            if move.get("defend"):
                style = discord.ButtonStyle.secondary
            elif move.get("heal",0) > 0:
                style = discord.ButtonStyle.success
            else:
                style = discord.ButtonStyle.danger if i > 0 else discord.ButtonStyle.primary
            btn = discord.ui.Button(label=move["name"][:20], style=style, row=row)
            async def cb(interaction: discord.Interaction, _i=i, _moves=moves):
                await self._do_move(interaction, _i, _moves)
            btn.callback = cb
            self.add_item(btn)
        if len(self.party_pks) > 1:
            switch_select = PokeSwitchSelect(self, self.party_pks)
            switch_select.row = 2
            self.add_item(switch_select)

    def _embed(self, log_line: str = "") -> discord.Embed:
        med = self.medallion
        ud = get_pokemon_data(self.uid)
        curr_waves = ud.get("pokefight_current_waves", 0)
        waves_needed = MEDALLION_WAVES_REQUIRED.get(med, 999)
        enemy_label = f"⚡ **{'BOSS: ' if self.is_boss else 'Enemy'}{self.enemy_idx+1}/{len(self.enemy_team)}:**"
        title = f"{'🔥 BOSS FIGHT!' if self.is_boss else f'{MEDALLION_EMOJIS.get(med,chr(11035))} Wave {self.wave}'}"
        e = discord.Embed(title=title, color=COLORS["error"] if self.is_boss else COLORS["pokemon"])
        e.description = (
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔴 **Your Pokémon:** {self.player_pk['species'].capitalize()} (Lv.{self.player_pk.get('level',1)})\n"
            f"HP: {build_hp_bar(self.player_pk['hp'], self.player_pk['max_hp'])}"
            f"{' 🛡️' if self.player_defending else ''}\n\n"
            f"{enemy_label} {self.enemy['species'].capitalize()} (Lv.{self.enemy['level']})\n"
            f"HP: {build_hp_bar(self.enemy['hp'], self.enemy['max_hp'])}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        )
        if log_line:
            e.add_field(name="⚡ Battle Log", value=log_line, inline=False)
        e.set_footer(text=f"Progress: {curr_waves}/{waves_needed} waves | Boost: {MEDALLION_COIN_BOOST.get(med,1.0)}x")
        return e

    async def _do_move(self, interaction: discord.Interaction, move_idx: int, moves: list):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("Not your battle!", ephemeral=True)
        self.player_defending = False
        move = moves[move_idx]
        log = []
        self.last_player_move = move_idx

        if move.get("defend"):
            self.player_defending = True
            log.append(f"🛡️ You used **{move['name']}**! Bracing for impact...")
        elif move.get("heal", 0) > 0:
            heal_amt = random.randint(move["heal"]-5, move["heal"]+5)
            self.player_pk["hp"] = min(self.player_pk["max_hp"], self.player_pk["hp"] + heal_amt)
            log.append(f"💚 **{move['name']}** — restored **{heal_amt}** HP.")
        else:
            if random.random() > move["acc"]:
                log.append(f"💨 **{move['name']}** missed!")
            else:
                dmg = random.randint(*move["dmg"])
                self.enemy["hp"] = max(0, self.enemy["hp"] - dmg)
                log.append(f"🗡️ **{move['name']}** dealt **{dmg}** damage!")

        if self.enemy["hp"] <= 0:
            log.append(f"💥 **{self.enemy['species'].capitalize()}** fainted!")
            self.enemy_idx += 1
            if self.enemy_idx >= len(self.enemy_team):
                return await self._wave_won(interaction, "\n".join(log))
            else:
                nxt = self.enemy
                log.append(f"⚡ Next: **{nxt['species'].capitalize()}** (Lv.{nxt['level']}) HP: {nxt['max_hp']}")
                self.rebuild_view()
                return await interaction.response.edit_message(embed=self._embed("\n".join(log)), view=self)

        etype = self.enemy.get("type", "normal")
        enemy_moves = get_pokemon_moves(self.enemy.get("pokedex_id", 0), etype)
        ai_idx = ai_pick_move(self.enemy["hp"], self.enemy["max_hp"], self.last_player_move, self.medallion, self.wave, enemy_moves)
        ai_mv = enemy_moves[ai_idx]
        if ai_mv.get("defend"):
            log.append(f"🛡️ **{self.enemy['species'].capitalize()}** used **{ai_mv['name']}**!")
        elif ai_mv.get("heal", 0) > 0:
            heal_amt = random.randint(ai_mv["heal"]-3, ai_mv["heal"]+8)
            self.enemy["hp"] = min(self.enemy["max_hp"], self.enemy["hp"] + heal_amt)
            log.append(f"💚 **{self.enemy['species'].capitalize()}** recovered **{heal_amt}** HP!")
        else:
            if random.random() > ai_mv["acc"]:
                log.append(f"💨 **{self.enemy['species'].capitalize()}**'s **{ai_mv['name']}** missed!")
            else:
                bonus = self.enemy.get("dmg_bonus", 0)
                dmg = random.randint(*ai_mv["dmg"]) + bonus
                if self.player_defending:
                    dmg = max(1, dmg // 2)
                self.player_pk["hp"] = max(0, self.player_pk["hp"] - dmg)
                log.append(f"💥 **{self.enemy['species'].capitalize()}** used **{ai_mv['name']}** — dealt **{dmg}** damage!")

        if self.player_pk["hp"] <= 0:
            alive = [p for p in self.party_pks if p.get("hp", 0) > 0 and p["id"] != self.player_pk["id"]]
            if alive:
                self.player_pk = alive[0]
                log.append(f"💔 {self.party_pks[0]['species'].capitalize()} fainted! Switched to **{self.player_pk['species'].capitalize()}**!")
                self.rebuild_view()
                return await interaction.response.edit_message(embed=self._embed("\n".join(log)), view=self)
            return await self._wave_lost(interaction, "\n".join(log))

        self.rebuild_view()
        await interaction.response.edit_message(embed=self._embed("\n".join(log)), view=self)

    async def _wave_won(self, interaction: discord.Interaction, log: str):
        uid = self.uid
        ud = get_pokemon_data(uid)
        med = ud.get("pokefight_medallions", 0)
        coins = apply_medal_boost(uid, self.reward_base)
        ud["balance"] = ud.get("balance", 0) + coins
        for c in self.children:
            c.disabled = True
        if self.is_boss:
            if med < 5:
                med += 1
                ud["pokefight_medallions"] = med
            ud["pokefight_current_waves"] = 0
            ud["pokefight_boss_ready"] = False
            save_pokemon_data(uid, ud)
            e = discord.Embed(
                title=f"🏆 BOSS DEFEATED! {MEDALLION_EMOJIS.get(med,'👑')} {MEDALLION_NAMES.get(med,'Master')} Medallion!",
                description=(
                    f"{log}\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎖️ You earned the **{MEDALLION_NAMES[med]} Medallion**!\n"
                    f"💰 Coin multiplier: **{MEDALLION_COIN_BOOST[med]}x**\n"
                    f"💰 +{coins} coins | Balance: **{ud['balance']:,}**\n"
                    f"🌊 Waves reset — fight to earn next medallion!"
                ),
                color=COLORS["success"]
            )
        else:
            ud["pokefight_current_waves"] = ud.get("pokefight_current_waves", 0) + 1
            ud["pokefight_total_waves"] = ud.get("pokefight_total_waves", 0) + 1
            curr_waves = ud["pokefight_current_waves"]
            waves_needed = MEDALLION_WAVES_REQUIRED.get(med, 999)
            boss_ready = curr_waves >= waves_needed and med < 5
            if boss_ready:
                ud["pokefight_boss_ready"] = True
            save_pokemon_data(uid, ud)
            if boss_ready:
                extra = "\n\n🔥 **BOSS FIGHT READY!** Use `,pokefight` again to face the boss!"
            else:
                extra = f"\n🌊 Wave progress: **{curr_waves}/{waves_needed}**"
            e = discord.Embed(
                title=f"✅ Wave {self.wave} Clear!",
                description=(
                    f"{log}\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"💰 +**{coins}** coins | Balance: **{ud['balance']:,}**{extra}"
                ),
                color=COLORS["success"]
            )
        await interaction.response.edit_message(embed=e, view=self)

    async def _wave_lost(self, interaction: discord.Interaction, log: str):
        uid = self.uid
        ud = get_pokemon_data(uid)
        if self.is_boss:
            ud["pokefight_current_waves"] = 0
            ud["pokefight_boss_ready"] = False
        save_pokemon_data(uid, ud)
        for c in self.children:
            c.disabled = True
        e = discord.Embed(
            title="💀 You were defeated!",
            description=(
                f"{log}\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{'🔥 Boss defeated you! Waves reset to 0.' if self.is_boss else 'Use `,pokefight` to try again! *(Wave progress kept)*'}"
            ),
            color=COLORS["error"]
        )
        await interaction.response.edit_message(embed=e, view=self)

# ════════════════════════════════════════════════════════════
#  VIEW — PVP BATTLE
# ════════════════════════════════════════════════════════════
class BattleChallengeView(discord.ui.View):
    def __init__(self, challenger, opponent, pks1: list, pks2: list):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent = opponent
        self.pks1 = pks1
        self.pks2 = pks2

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            return await interaction.response.send_message("Not your challenge!", ephemeral=True)
        for c in self.children:
            c.disabled = True
        view = PvPBattleView(self.challenger, self.opponent, self.pks1, self.pks2)
        view.rebuild_view()
        await interaction.response.edit_message(embed=view._embed(), view=view)

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in (self.opponent.id, self.challenger.id):
            return await interaction.response.send_message("Not your challenge!", ephemeral=True)
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(description="❌ Battle declined.", color=COLORS["error"]), view=self
        )

class PvPSwitchSelect(discord.ui.Select):
    def __init__(self, battle_view, player, party_pks: list, current_pk: dict):
        self.battle_view = battle_view
        self.player = player
        opts = []
        for pk in party_pks:
            if pk["id"] == current_pk["id"]: continue
            if pk.get("hp", pk.get("max_hp", 100)) <= 0: continue
            opts.append(discord.SelectOption(
                label=f"{pk['species'].capitalize()} Lv.{pk.get('level',1)}",
                value=str(pk["id"]),
                description=f"HP: {pk.get('hp', pk.get('max_hp', 100))}/{pk.get('max_hp',100)}"
            ))
        if not opts:
            opts.append(discord.SelectOption(label="No Pokémon to switch", value="none"))
        super().__init__(placeholder=f"🔄 {player.display_name[:15]}: Switch...", options=opts)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your switch!", ephemeral=True)
        if self.values[0] == "none":
            return await interaction.response.send_message("No Pokémon to switch!", ephemeral=True)
        new_id = int(self.values[0])
        is_p1 = self.player.id == self.battle_view.p1.id
        party = self.battle_view.pks1 if is_p1 else self.battle_view.pks2
        for pk in party:
            if pk["id"] == new_id:
                if is_p1:
                    self.battle_view.active1 = pk
                else:
                    self.battle_view.active2 = pk
                break
        self.battle_view.rebuild_view()
        e = self.battle_view._embed(f"🔄 **{self.player.display_name}** switched Pokémon!")
        await interaction.response.edit_message(embed=e, view=self.battle_view)

class PvPBattleView(discord.ui.View):
    def __init__(self, p1, p2, pks1: list, pks2: list):
        super().__init__(timeout=180)
        self.p1 = p1
        self.p2 = p2
        self.pks1 = pks1
        self.pks2 = pks2
        self.active1 = pks1[0]
        self.active2 = pks2[0]
        self.turn = p1
        self.p1_defending = False
        self.p2_defending = False

    def _embed(self, log: str = "") -> discord.Embed:
        e = discord.Embed(title="⚔️ PvP Battle!", color=COLORS["pokemon"])
        e.description = (
            f"🔴 **{self.p1.display_name}** — {self.active1['species'].capitalize()}\n"
            f"HP: {build_hp_bar(self.active1['hp'], self.active1['max_hp'])}"
            f"{' 🛡️' if self.p1_defending else ''}\n\n"
            f"🔵 **{self.p2.display_name}** — {self.active2['species'].capitalize()}\n"
            f"HP: {build_hp_bar(self.active2['hp'], self.active2['max_hp'])}"
            f"{' 🛡️' if self.p2_defending else ''}"
        )
        if log:
            e.add_field(name="⚡ Last Move", value=log, inline=False)
        e.set_footer(text=f"Turn: {self.turn.display_name}")
        return e

    def rebuild_view(self):
        self.clear_items()
        is_p1_turn = self.turn.id == self.p1.id
        active = self.active1 if is_p1_turn else self.active2
        ptype = active.get("type", "normal")
        moves = get_pokemon_moves(active.get("pokedex_id", 0), ptype)
        for i, move in enumerate(moves[:5]):
            row = 0 if i < 3 else 1
            if move.get("defend"):
                style = discord.ButtonStyle.secondary
            elif move.get("heal", 0) > 0:
                style = discord.ButtonStyle.success
            else:
                style = discord.ButtonStyle.primary if i == 0 else discord.ButtonStyle.danger
            btn = discord.ui.Button(label=move["name"][:20], style=style, row=row)
            async def cb(interaction: discord.Interaction, _i=i, _moves=moves):
                await self._do_move(interaction, _i, _moves)
            btn.callback = cb
            self.add_item(btn)
        # Switch selects for both players
        pks1_alive = [p for p in self.pks1 if p.get("hp", p.get("max_hp", 100)) > 0]
        pks2_alive = [p for p in self.pks2 if p.get("hp", p.get("max_hp", 100)) > 0]
        if len(pks1_alive) > 1:
            sw1 = PvPSwitchSelect(self, self.p1, pks1_alive, self.active1)
            sw1.row = 2
            self.add_item(sw1)
        if len(pks2_alive) > 1:
            sw2 = PvPSwitchSelect(self, self.p2, pks2_alive, self.active2)
            sw2.row = 3
            self.add_item(sw2)

    async def _do_move(self, interaction: discord.Interaction, move_idx: int, moves: list):
        if interaction.user.id != self.turn.id:
            return await interaction.response.send_message("Not your turn!", ephemeral=True)
        is_p1 = self.turn.id == self.p1.id
        atk_pk = self.active1 if is_p1 else self.active2
        def_pk = self.active2 if is_p1 else self.active1
        move = moves[move_idx]
        log_line = ""
        if is_p1: self.p1_defending = False
        else:      self.p2_defending = False

        if move.get("defend"):
            if is_p1: self.p1_defending = True
            else:      self.p2_defending = True
            log_line = f"🛡️ **{self.turn.display_name}** used **{move['name']}**!"
        elif move.get("heal", 0) > 0:
            heal = random.randint(move["heal"]-3, move["heal"]+8)
            atk_pk["hp"] = min(atk_pk["max_hp"], atk_pk["hp"] + heal)
            log_line = f"💚 **{self.turn.display_name}** recovered **{heal}** HP!"
        else:
            if random.random() > move["acc"]:
                log_line = f"💨 **{self.turn.display_name}**'s **{move['name']}** missed!"
            else:
                dmg = random.randint(*move["dmg"])
                defending = self.p2_defending if is_p1 else self.p1_defending
                if defending: dmg = max(1, dmg//2)
                def_pk["hp"] = max(0, def_pk["hp"] - dmg)
                log_line = f"🗡️ **{self.turn.display_name}** used **{move['name']}** — dealt **{dmg}** damage!"

        winner = None
        if self.active2["hp"] <= 0:
            alive2 = [p for p in self.pks2 if p["id"] != self.active2["id"] and p.get("hp",100) > 0]
            if alive2:
                self.active2 = alive2[0]
                log_line += f"\n🔄 **{self.p2.display_name}** switched to **{self.active2['species'].capitalize()}**!"
            else:
                winner = self.p1
        if self.active1["hp"] <= 0:
            alive1 = [p for p in self.pks1 if p["id"] != self.active1["id"] and p.get("hp",100) > 0]
            if alive1:
                self.active1 = alive1[0]
                log_line += f"\n🔄 **{self.p1.display_name}** switched to **{self.active1['species'].capitalize()}**!"
            else:
                winner = self.p2

        if winner:
            loser = self.p2 if winner == self.p1 else self.p1
            prize = random.randint(50, 200)
            wuid = str(winner.id); luid = str(loser.id)
            wud = get_pokemon_data(wuid); lud = get_pokemon_data(luid)
            wud["balance"] = wud.get("balance",0) + prize
            lud["balance"] = max(0, lud.get("balance",0) - prize//2)
            save_pokemon_data(wuid, wud); save_pokemon_data(luid, lud)
            for c in self.children: c.disabled = True
            e = discord.Embed(
                title=f"🏆 {winner.display_name} wins!",
                description=f"{log_line}\n\n💰 +**{prize}** coins to {winner.mention}!\n💸 -{prize//2} coins from {loser.mention}.",
                color=COLORS["success"]
            )
            return await interaction.response.edit_message(embed=e, view=self)

        self.turn = self.p2 if is_p1 else self.p1
        self.rebuild_view()
        await interaction.response.edit_message(embed=self._embed(log_line), view=self)

# ════════════════════════════════════════════════════════════
#  VIEW — SELL CONFIRM & TRADE
# ════════════════════════════════════════════════════════════
class SellConfirmView(discord.ui.View):
    def __init__(self, ctx, pk: dict, price: int):
        super().__init__(timeout=30)
        self.ctx = ctx; self.pk = pk; self.price = price

    @discord.ui.button(label="✅ Confirm Sell", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("Not yours!", ephemeral=True)
        uid = str(self.ctx.author.id)
        ud = get_pokemon_data(uid)
        ud["pokemon"] = [p for p in ud["pokemon"] if p["id"] != self.pk["id"]]
        ud["balance"] = ud.get("balance",0) + self.price
        if ud.get("selected",0) >= len(ud["pokemon"]):
            ud["selected"] = max(0, len(ud["pokemon"])-1)
        save_pokemon_data(uid, ud)
        for c in self.children: c.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(
                description=f"✅ Sold **{self.pk['species'].capitalize()}** for **{self.price:,}** coins!\n💰 Balance: **{ud['balance']:,}**",
                color=COLORS["success"]
            ), view=self
        )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("Not yours!", ephemeral=True)
        for c in self.children: c.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(description="❌ Sale cancelled.", color=COLORS["error"]), view=self
        )

class PokeTradeView(discord.ui.View):
    def __init__(self, sender, recipient, pk: dict):
        super().__init__(timeout=60)
        self.sender = sender; self.recipient = recipient; self.pk = pk

    @discord.ui.button(label="✅ Accept Trade", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.recipient.id:
            return await interaction.response.send_message("Not your trade!", ephemeral=True)
        suid = str(self.sender.id); ruid = str(self.recipient.id)
        sud = get_pokemon_data(suid); rud = get_pokemon_data(ruid)
        sud["pokemon"] = [p for p in sud["pokemon"] if p["id"] != self.pk["id"]]
        new_pk = dict(self.pk, id=rud["next_id"])
        rud["next_id"] += 1
        rud["pokemon"].append(new_pk)
        if sud.get("selected",0) >= len(sud["pokemon"]): sud["selected"] = 0
        save_pokemon_data(suid, sud); save_pokemon_data(ruid, rud)
        for c in self.children: c.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(
                description=f"✅ **{self.pk['species'].capitalize()}** traded to **{self.recipient.display_name}**!",
                color=COLORS["success"]
            ), view=self
        )

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in (self.recipient.id, self.sender.id):
            return await interaction.response.send_message("Not your trade!", ephemeral=True)
        for c in self.children: c.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(description="❌ Trade declined.", color=COLORS["error"]), view=self
        )

# ════════════════════════════════════════════════════════════
#  VIEWS — HELP
# ════════════════════════════════════════════════════════════
HELP_CATEGORIES = {
    "pokemon": {
        "label":"🎮 Pokémon","color":COLORS["pokemon"],"desc":"Catch, battle, trade & grow your team",
        "cmds":[
            (",start","Begin your journey & choose a starter (9 gens!)"),
            (",poke","Catch a wild Pokémon (dropdown + Flee)"),
            (",pokemon [member]","View your Pokémon collection"),
            (",pinfo <id>","Detailed stats for a Pokémon"),
            (",pselect <id>","Set your active Pokémon"),
            (",pfav <id>","Favourite / unfavourite a Pokémon"),
            (",nickname <id> [name]","Give a Pokémon a nickname"),
            (",release <id>","Release a Pokémon"),
            (",sell <id>","Sell a Pokémon for PokeCoins"),
            (",pokefight","Fight AI waves & earn Medallions + Boss Fights!"),
            (",mybadges [member]","View Medallions & coin multiplier"),
            (",ptrade <member> <id>","Trade a Pokémon with someone"),
            (",pbattle <member>","Challenge someone to a battle"),
            (",pdaily","Claim your daily PokeCoins"),
            (",pbalance [member]","Check PokeCoin balance"),
            (",myballs","View Pokéball inventory"),
            (",plb","PokeCoins leaderboard"),
            (",hint","Get a hint for the wild Pokémon"),
            ("/teamsetup","Set your battle party (up to 5 Pokémon)"),
            ("/dex <pokemon>","View full Pokédex entry"),
        ],
    },
    "shop": {
        "label":"🏪 Shop & Cards","color":0xFF9800,"desc":"Buy packs, open cards & trade",
        "cmds":[
            (",pokeshop","Open the PokéShop (balls & packs)"),
            ("/openpack <pack> [amt]","Open packs from your inventory"),
            (",mycards [member]","View your card collection"),
            (",viewcard <id>","View a specific card"),
            (",market <id> <price>","List a Pokémon on the marketplace"),
            (",marketplace","Browse the global marketplace (buy with buttons)"),
            (",mylistings","View your active listings"),
            (",unlist <id>","Remove a listing"),
        ],
    },
    "fun": {
        "label":"🎭 Fun","color":COLORS["main"],"desc":"Entertainment & social commands",
        "cmds":[
            (",snipe","View the last deleted message"),
            (",editsnipe","View the last edited message"),
            (",avatar [member]","View a member's avatar"),
            (",dankmemer","Fetch a random meme"),
            (",afk [reason]","Set yourself as AFK"),
            (",confess <message>","Anonymous confession"),
            (",serverinfo","View server information"),
            (",userinfo [member]","View a member's info"),
            (",info","Bot stats & info"),
        ],
    },
    "games": {
        "label":"🎯 Games","color":COLORS["success"],"desc":"Play mini-games",
        "cmds":[
            (",rps","Rock Paper Scissors vs bot"),
            (",trivia","Answer a trivia question"),
            (",tictactoe <member>","Play Tic Tac Toe"),
            (",connect4 <member>","Play Connect 4"),
            (",hangman","Play Hangman"),
            (",wordchain","Start a word chain game"),
            (",stopwc","Stop the word chain"),
            (",fasttype","Typing speed race"),
            (",wouldyourather","Would You Rather poll"),
            (",memorymatch <member>","Memory Match card game"),
        ],
    },
    "giveaways": {
        "label":"🎁 Giveaways","color":0xFF6B6B,"desc":"Run & manage giveaways",
        "cmds":[
            (",giveaway <dur> <prize>","Start a giveaway (Staff)"),
            (",gend <msg_id>","End a giveaway early (Staff)"),
            (",greroll <msg_id>","Reroll a giveaway winner (Staff)"),
            (",glist","View active giveaways"),
        ],
    },
    "moderation": {
        "label":"🛡️ Moderation","color":COLORS["warn"],"desc":"Keep your server safe",
        "cmds":[
            (",warn <member> [reason]","Warn a member"),
            (",unwarn <member>","Remove latest warning"),
            (",warnings <member>","View warnings"),
            (",kick <member> [reason]","Kick a member (Staff)"),
            (",ban <member> [reason]","Ban a member (Staff)"),
            (",unban <user_id>","Unban a user (Staff)"),
            (",mute <member> [dur]","Timeout a member (Staff)"),
            (",unmute <member>","Remove timeout (Staff)"),
            (",purge [amount]","Delete messages (max 500, Staff)"),
            (",lock","Lock current channel (Staff)"),
            (",unlock","Unlock current channel (Staff)"),
            (",slowmode [secs]","Set channel slowmode (Staff)"),
            (",setnick <member> [n]","Set nickname (Staff)"),
            (",role <member> <role>","Toggle role (Staff)"),
            (",massrole <role>","Give role to all (Owner)"),
            (",massban <ids>","Ban multiple users (Owner)"),
        ],
    },
    "server": {
        "label":"⚙️ Server","color":COLORS["info"],"desc":"Server management & config",
        "cmds":[
            (",antinuke on/off","Toggle anti-nuke protection"),
            (",autoresponse add/remove","Manage auto-responses"),
            (",setconfess <channel>","Set confession channel"),
            (",setstats","Create live stat channels"),
            (",sticky <message>","Pin a sticky message"),
            (",unsticky","Remove sticky message"),
            (",setps <link>","Set Roblox PS link"),
            (",ps","Post the private server link"),
            (",createchannel <name>","Create text channels"),
            (",rn <name>","Rename current channel"),
            (",delete","Delete current channel (Staff)"),
            (",deleteall","Delete ALL channels (Owner)"),
        ],
    },
    "admin": {
        "label":"👑 Admin","color":COLORS["error"],"desc":"Owner-only management",
        "cmds":[
            ("/coinsadd <user> <amt>","Add PokeCoins (Owner)"),
            ("/coinsremove <user> <amt>","Remove PokeCoins (Owner)"),
            ("/pokeremove <user> <id>","Remove a Pokémon (Owner)"),
            ("/packadd <pack> <user> <n>","Give packs to user (Owner — includes Godly Pack)"),
            ("/whitelist <user>","Whitelist user from lockbot (Owner)"),
            ("/pokemonlist <user>","Allow pokemon cmds when locked (Owner)"),
            ("/lockbot","Lock/unlock bot (Owner)"),
            (",lockbot","Lock/unlock bot (Owner)"),
            ("/invite","Get bot invite link (Admin perms)"),
        ],
    },
}

class HelpCategorySelect(discord.ui.Select):
    def __init__(self):
        opts = [
            discord.SelectOption(label=cat["label"], value=key, description=f"{len(cat['cmds'])} commands")
            for key, cat in HELP_CATEGORIES.items()
        ]
        super().__init__(placeholder="📂 Browse a category...", options=opts)

    async def callback(self, interaction: discord.Interaction):
        cat = HELP_CATEGORIES[self.values[0]]
        e = discord.Embed(
            title=f"{cat['label']}",
            description=f"*{cat['desc']}*\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            color=cat["color"]
        )
        for name, desc in cat["cmds"]:
            e.add_field(name=f"`{name}`", value=f"↳ {desc}", inline=False)
        e.set_footer(text=f"PolarBot | Prefix: {PREFIX} | {len(cat['cmds'])} commands")
        await interaction.response.edit_message(embed=e, view=self.view)

class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(HelpCategorySelect())

    @discord.ui.button(label="🏠 Home", style=discord.ButtonStyle.secondary, row=1)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=build_help_home(), view=self)

def build_help_home() -> discord.Embed:
    total = sum(len(c["cmds"]) for c in HELP_CATEGORIES.values())
    e = discord.Embed(
        title=f"📖  {BOT_NAME}  —  Command Reference",
        color=COLORS["pokemon"]
    )
    e.description = (
        f"**Prefix:** `{PREFIX}`  •  **Total:** `{total}` commands\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )
    for key, cat in HELP_CATEGORIES.items():
        e.add_field(
            name=cat["label"],
            value=f"`{len(cat['cmds'])}` commands — {cat['desc']}",
            inline=False
        )
    e.set_footer(text="Select a category below to browse commands")
    return e

# ════════════════════════════════════════════════════════════
#  COG — MODERATION
# ════════════════════════════════════════════════════════════
class Moderation(commands.Cog):
    def __init__(self, bot): self.bot = bot

    async def staff_check(self, ctx) -> bool:
        if not is_staff(ctx.author):
            await ctx.send(embed=discord.Embed(description="❌ You need Staff permissions.", color=COLORS["error"]))
            return False
        return True

    @commands.hybrid_command(name="warn", description="Warn a member.")
    async def warn(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if not await self.staff_check(ctx): return
        uid = str(member.id)
        data = load("warnings")
        if uid not in data: data[uid] = []
        data[uid].append({"reason":reason,"by":str(ctx.author),"time":datetime.now(timezone.utc).isoformat()})
        save("warnings", data)
        await ctx.send(embed=discord.Embed(
            title="⚠️ Member Warned",
            description=f"**{member.mention}** warned.\n**Reason:** {reason}\n**Total:** {len(data[uid])}",
            color=COLORS["warn"]
        ))

    @commands.hybrid_command(name="unwarn", description="Remove the latest warning.")
    async def unwarn(self, ctx, member: discord.Member):
        if not await self.staff_check(ctx): return
        uid = str(member.id)
        data = load("warnings")
        if not data.get(uid):
            return await ctx.send(embed=discord.Embed(description="❌ No warnings found.", color=COLORS["error"]))
        data[uid].pop()
        save("warnings", data)
        await ctx.send(embed=discord.Embed(description=f"✅ Removed latest warning from **{member.display_name}**.", color=COLORS["success"]))

    @commands.hybrid_command(name="warnings", description="View a member's warnings.")
    async def warnings(self, ctx, member: discord.Member):
        uid = str(member.id)
        data = load("warnings")
        warns = data.get(uid, [])
        if not warns:
            return await ctx.send(embed=discord.Embed(description=f"✅ **{member.display_name}** has no warnings!", color=COLORS["success"]))
        e = discord.Embed(title=f"⚠️ Warnings — {member.display_name}", color=COLORS["warn"])
        for i, w in enumerate(warns[-10:], 1):
            e.add_field(name=f"#{i} — {w.get('time','')[:10]}", value=f"**Reason:** {w['reason']}\n**By:** {w.get('by','?')}", inline=False)
        await ctx.send(embed=e)

    @commands.hybrid_command(name="kick", description="Kick a member.")
    @commands.has_permissions(kick_members=True)
    async def kick(self, ctx, member: discord.Member, *, reason: str = "No reason"):
        if not is_owner(ctx.author) and not is_staff(ctx.author):
            return await ctx.send(embed=discord.Embed(description="❌ No permission.", color=COLORS["error"]))
        await member.kick(reason=reason)
        await ctx.send(embed=discord.Embed(description=f"👢 **{member}** kicked. Reason: {reason}", color=COLORS["warn"]))

    @commands.hybrid_command(name="ban", description="Ban a member.")
    @commands.has_permissions(ban_members=True)
    async def ban(self, ctx, member: discord.Member, *, reason: str = "No reason"):
        if not is_owner(ctx.author) and not is_staff(ctx.author):
            return await ctx.send(embed=discord.Embed(description="❌ No permission.", color=COLORS["error"]))
        await member.ban(reason=reason)
        await ctx.send(embed=discord.Embed(description=f"🔨 **{member}** banned. Reason: {reason}", color=COLORS["error"]))

    @commands.hybrid_command(name="unban", description="Unban a user by ID.")
    @commands.has_permissions(ban_members=True)
    async def unban(self, ctx, user_id: str):
        if not is_owner(ctx.author) and not is_staff(ctx.author):
            return await ctx.send(embed=discord.Embed(description="❌ No permission.", color=COLORS["error"]))
        try:
            user = await self.bot.fetch_user(int(user_id))
            await ctx.guild.unban(user)
            await ctx.send(embed=discord.Embed(description=f"✅ **{user}** unbanned.", color=COLORS["success"]))
        except Exception as ex:
            await ctx.send(embed=discord.Embed(description=f"❌ {ex}", color=COLORS["error"]))

    @commands.hybrid_command(name="mute", description="Timeout a member.")
    @commands.has_permissions(moderate_members=True)
    async def mute(self, ctx, member: discord.Member, duration: str = "10m", *, reason: str = "No reason"):
        if not is_owner(ctx.author) and not is_staff(ctx.author):
            return await ctx.send(embed=discord.Embed(description="❌ No permission.", color=COLORS["error"]))
        secs = parse_duration(duration)
        if not secs:
            return await ctx.send(embed=discord.Embed(description="❌ Invalid duration. e.g. `10m`, `1h`.", color=COLORS["error"]))
        until = discord.utils.utcnow() + timedelta(seconds=secs)
        await member.timeout(until, reason=reason)
        await ctx.send(embed=discord.Embed(
            description=f"🔇 **{member.mention}** timed out for **{format_duration(secs)}**.\nReason: {reason}",
            color=COLORS["warn"]
        ))

    @commands.hybrid_command(name="unmute", description="Remove a member's timeout.")
    @commands.has_permissions(moderate_members=True)
    async def unmute(self, ctx, member: discord.Member):
        await member.timeout(None)
        await ctx.send(embed=discord.Embed(description=f"🔊 **{member.mention}**'s timeout removed.", color=COLORS["success"]))

    @commands.hybrid_command(name="purge", description="Bulk-delete messages (max 500, Staff).")
    @commands.has_permissions(manage_messages=True)
    async def purge(self, ctx, amount: int = 10):
        if not is_owner(ctx.author) and not is_staff(ctx.author):
            return await ctx.send(embed=discord.Embed(description="❌ Staff only.", color=COLORS["error"]))
        amount = max(1, min(500, amount))
        deleted = await ctx.channel.purge(limit=amount+1)
        msg = await ctx.send(embed=discord.Embed(description=f"🗑️ Deleted **{len(deleted)-1}** messages.", color=COLORS["success"]))
        await asyncio.sleep(3); await msg.delete()

    @commands.hybrid_command(name="lock", description="Lock the current channel.")
    @commands.has_permissions(manage_channels=True)
    async def lock(self, ctx):
        await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
        await ctx.send(embed=discord.Embed(description="🔒 Channel locked.", color=COLORS["warn"]))

    @commands.hybrid_command(name="unlock", description="Unlock the current channel.")
    @commands.has_permissions(manage_channels=True)
    async def unlock(self, ctx):
        await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=True)
        await ctx.send(embed=discord.Embed(description="🔓 Channel unlocked.", color=COLORS["success"]))

    @commands.hybrid_command(name="slowmode", description="Set channel slowmode.")
    @commands.has_permissions(manage_channels=True)
    async def slowmode(self, ctx, seconds: int = 0):
        seconds = max(0, min(21600, seconds))
        await ctx.channel.edit(slowmode_delay=seconds)
        txt = f"⏱️ Slowmode set to **{seconds}s**." if seconds else "⏱️ Slowmode disabled."
        await ctx.send(embed=discord.Embed(description=txt, color=COLORS["info"]))

    @commands.hybrid_command(name="setnick", description="Set a member's nickname.")
    @commands.has_permissions(manage_nicknames=True)
    async def setnick(self, ctx, member: discord.Member, *, nick: str = ""):
        await member.edit(nick=nick or None)
        await ctx.send(embed=discord.Embed(description=f"✅ Nickname {'reset' if not nick else f'set to **{nick}**'}.", color=COLORS["success"]))

    @commands.hybrid_command(name="role", description="Toggle a role on a member.")
    @commands.has_permissions(manage_roles=True)
    async def role(self, ctx, member: discord.Member, *, role: discord.Role):
        if not is_owner(ctx.author) and not is_staff(ctx.author):
            return await ctx.send(embed=discord.Embed(description="❌ No permission.", color=COLORS["error"]))
        if role in member.roles:
            await member.remove_roles(role)
            await ctx.send(embed=discord.Embed(description=f"➖ Removed **{role.name}** from {member.mention}.", color=COLORS["warn"]))
        else:
            await member.add_roles(role)
            await ctx.send(embed=discord.Embed(description=f"➕ Gave **{role.name}** to {member.mention}.", color=COLORS["success"]))

    @commands.command(name="massrole")
    async def massrole(self, ctx, *, role: discord.Role):
        if not is_owner(ctx.author):
            return await ctx.send(embed=discord.Embed(description="❌ Owner only!", color=COLORS["error"]))
        msg = await ctx.send(embed=discord.Embed(description=f"⏳ Giving **{role.name}** to all members...", color=COLORS["info"]))
        count = 0
        for member in ctx.guild.members:
            if role not in member.roles:
                try:
                    await member.add_roles(role)
                    count += 1
                    await asyncio.sleep(0.3)
                except: pass
        await msg.edit(embed=discord.Embed(description=f"✅ Gave **{role.name}** to **{count}** members.", color=COLORS["success"]))

    @commands.command(name="massban")
    async def massban(self, ctx, *, ids: str):
        if not is_owner(ctx.author):
            return await ctx.send(embed=discord.Embed(description="❌ Owner only!", color=COLORS["error"]))
        id_list = [i.strip() for i in ids.split(",") if i.strip().isdigit()]
        banned = 0
        for uid in id_list:
            try:
                user = await self.bot.fetch_user(int(uid))
                await ctx.guild.ban(user, reason=f"Massbanned by {ctx.author}")
                banned += 1
                await asyncio.sleep(0.3)
            except: pass
        await ctx.send(embed=discord.Embed(description=f"🔨 Banned **{banned}/{len(id_list)}** users.", color=COLORS["warn"]))

    @commands.hybrid_command(name="createchannel", description="Create text channels (max 100).")
    @commands.has_permissions(manage_channels=True)
    async def createchannel(self, ctx, name: str, amount: int = 1):
        amount = max(1, min(100, amount))
        created = 0
        for _ in range(amount):
            try:
                await ctx.guild.create_text_channel(name=name)
                created += 1
                await asyncio.sleep(0.5)
            except: break
        await ctx.send(embed=discord.Embed(description=f"✅ Created **{created}** channel(s) named `{name}`.", color=COLORS["success"]))

    @commands.hybrid_command(name="rn", description="Rename the current channel.")
    @commands.has_permissions(manage_channels=True)
    async def rn(self, ctx, *, name: str):
        await ctx.channel.edit(name=name)
        await ctx.send(embed=discord.Embed(description=f"✅ Channel renamed to **{name}**.", color=COLORS["success"]))

    @commands.hybrid_command(name="delete", description="Delete the current channel.")
    @commands.has_permissions(manage_channels=True)
    async def delete(self, ctx):
        if not is_owner(ctx.author) and not ctx.author.guild_permissions.administrator:
            return await ctx.send(embed=discord.Embed(description="❌ Admin only!", color=COLORS["error"]))
        await ctx.send(embed=discord.Embed(description="🗑️ Deleting in 3s...", color=COLORS["warn"]))
        await asyncio.sleep(3)
        await ctx.channel.delete()

    @commands.command(name="deleteall")
    async def deleteall(self, ctx):
        if not is_owner(ctx.author):
            return await ctx.send(embed=discord.Embed(description="❌ Owner only!", color=COLORS["error"]))
        confirm_ch = None
        for ch in ctx.guild.channels:
            try:
                if ch.id == ctx.channel.id:
                    confirm_ch = ch
                else:
                    await ch.delete()
                    await asyncio.sleep(0.4)
            except: pass
        if confirm_ch:
            try:
                m = await confirm_ch.send(embed=discord.Embed(description="✅ All other channels deleted.", color=COLORS["success"]))
                await asyncio.sleep(3); await confirm_ch.delete()
            except: pass

# ════════════════════════════════════════════════════════════
#  COG — ADMIN (slash commands)
# ════════════════════════════════════════════════════════════
class AdminCog(commands.Cog):
    def __init__(self, bot): self.bot = bot

    @app_commands.command(name="coinsadd", description="Add PokeCoins to a user. (Owner)")
    async def coinsadd(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if not is_owner(interaction.user):
            return await interaction.response.send_message("❌ Owner only!", ephemeral=True)
        uid = str(member.id)
        ud = get_pokemon_data(uid)
        ud["balance"] = ud.get("balance",0) + amount
        save_pokemon_data(uid, ud)
        await interaction.response.send_message(
            embed=discord.Embed(description=f"✅ Added **{amount:,}** coins to **{member.display_name}**.\nBalance: **{ud['balance']:,}**", color=COLORS["success"])
        )

    @app_commands.command(name="coinsremove", description="Remove PokeCoins from a user. (Owner)")
    async def coinsremove(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if not is_owner(interaction.user):
            return await interaction.response.send_message("❌ Owner only!", ephemeral=True)
        uid = str(member.id)
        ud = get_pokemon_data(uid)
        ud["balance"] = max(0, ud.get("balance",0) - amount)
        save_pokemon_data(uid, ud)
        await interaction.response.send_message(
            embed=discord.Embed(description=f"✅ Removed **{amount:,}** coins from **{member.display_name}**.\nBalance: **{ud['balance']:,}**", color=COLORS["success"])
        )

    @app_commands.command(name="pokeremove", description="Remove a Pokémon from a user. (Owner)")
    async def pokeremove(self, interaction: discord.Interaction, member: discord.Member, pokemon_id: int):
        if not is_owner(interaction.user):
            return await interaction.response.send_message("❌ Owner only!", ephemeral=True)
        uid = str(member.id)
        ud = get_pokemon_data(uid)
        before = len(ud["pokemon"])
        ud["pokemon"] = [p for p in ud["pokemon"] if p["id"] != pokemon_id]
        if len(ud["pokemon"]) == before:
            return await interaction.response.send_message(embed=discord.Embed(description="❌ Pokémon not found.", color=COLORS["error"]), ephemeral=True)
        if ud.get("selected",0) >= len(ud["pokemon"]): ud["selected"] = 0
        save_pokemon_data(uid, ud)
        await interaction.response.send_message(
            embed=discord.Embed(description=f"✅ Removed Pokémon `#{pokemon_id}` from **{member.display_name}**.", color=COLORS["success"])
        )

    @app_commands.command(name="packadd", description="Give card packs to a user. (Owner) Includes Godly Pack!")
    @app_commands.describe(pack="Pack type", member="Target user", amount="Number of packs")
    @app_commands.choices(pack=[
        app_commands.Choice(name="📦 Basic Pack",    value="basic"),
        app_commands.Choice(name="💎 Rare Pack",     value="rare"),
        app_commands.Choice(name="💜 Epic Pack",     value="epic"),
        app_commands.Choice(name="⭐ Legendary Pack",value="legendary"),
        app_commands.Choice(name="👑 Master Pack",   value="master"),
        app_commands.Choice(name="🌟 Godly Pack",    value="godly"),
    ])
    async def packadd(self, interaction: discord.Interaction, pack: app_commands.Choice[str], member: discord.Member, amount: int = 1):
        if not is_owner(interaction.user):
            return await interaction.response.send_message("❌ Owner only!", ephemeral=True)
        if amount < 1 or amount > 999:
            return await interaction.response.send_message(embed=discord.Embed(description="❌ Amount must be 1–999.", color=COLORS["error"]), ephemeral=True)
        uid = str(member.id)
        ud = get_pokemon_data(uid)
        info = CARD_PACKS[pack.value]
        ud["packs"][pack.value] = ud["packs"].get(pack.value, 0) + amount
        save_pokemon_data(uid, ud)
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"✅ Gave **{amount}×** {info['emoji']} **{info['name']}** to **{member.display_name}**!\nThey now have **{ud['packs'][pack.value]}**. Use `/openpack`.",
                color=COLORS["success"]
            )
        )

    @app_commands.command(name="openpack", description="Open card packs from your inventory.")
    @app_commands.describe(pack="Which pack to open", amount="How many packs (max 10)")
    @app_commands.choices(pack=[
        app_commands.Choice(name="📦 Basic Pack",    value="basic"),
        app_commands.Choice(name="💎 Rare Pack",     value="rare"),
        app_commands.Choice(name="💜 Epic Pack",     value="epic"),
        app_commands.Choice(name="⭐ Legendary Pack",value="legendary"),
        app_commands.Choice(name="👑 Master Pack",   value="master"),
        app_commands.Choice(name="🌟 Godly Pack",    value="godly"),
    ])
    async def openpack(self, interaction: discord.Interaction, pack: app_commands.Choice[str], amount: int = 1):
        uid = str(interaction.user.id)
        ud = get_pokemon_data(uid)
        pk = pack.value
        info = CARD_PACKS[pk]
        amount = max(1, min(10, amount))
        owned = ud["packs"].get(pk, 0)
        if owned <= 0:
            return await interaction.response.send_message(
                embed=discord.Embed(description=f"❌ You don't have any **{info['name']}**!", color=COLORS["error"]),
                ephemeral=True
            )
        if amount > owned:
            return await interaction.response.send_message(
                embed=discord.Embed(description=f"❌ You only have **{owned}×** {info['name']}.", color=COLORS["error"]),
                ephemeral=True
            )
        await interaction.response.defer()
        ud["packs"][pk] = owned - amount
        save_pokemon_data(uid, ud)
        all_cards = []
        for _ in range(amount):
            cards = open_card_pack(uid, pk)
            all_cards.extend(cards)
        tier_counts: dict = {}
        lines = []
        for card in all_cards:
            tier = card["tier"]
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
            ti = CARD_TIERS.get(tier, CARD_TIERS["Common"])
            lines.append(f"{ti['emoji']} **{card['species']}** — {tier}  `#{card['id']}`")
        e = discord.Embed(
            title=f"{info['emoji']} Opened {amount}× {info['name']}!",
            description="\n".join(lines[:20]) + (f"\n*…and {len(lines)-20} more*" if len(lines)>20 else ""),
            color=COLORS["godly"] if pk == "godly" else COLORS["pokemon"]
        )
        summary = "  ".join(f"{CARD_TIERS.get(t, CARD_TIERS['Common'])['emoji']}{c}" for t, c in tier_counts.items())
        e.add_field(name="📊 Rarity Summary", value=summary or "—", inline=False)
        e.add_field(name="📦 Packs Remaining", value=f"**{ud['packs'].get(pk,0)}** {info['name']}(s)", inline=True)
        e.set_footer(text="Cards saved! Use ,mycards to view your collection.")
        await interaction.followup.send(embed=e)

    @app_commands.command(name="whitelist", description="Whitelist a user to bypass lockbot. (Owner)")
    async def whitelist(self, interaction: discord.Interaction, member: discord.Member):
        if not is_owner(interaction.user):
            return await interaction.response.send_message("❌ Owner only!", ephemeral=True)
        wl = load("whitelist")
        if not isinstance(wl, list): wl = []
        if member.id not in wl:
            wl.append(member.id)
            WHITELIST_USERS.add(member.id)
            save("whitelist", wl)
            await interaction.response.send_message(
                embed=discord.Embed(description=f"✅ **{member.display_name}** can now use all commands even when bot is locked.", color=COLORS["success"])
            )
        else:
            wl.remove(member.id)
            WHITELIST_USERS.discard(member.id)
            save("whitelist", wl)
            await interaction.response.send_message(
                embed=discord.Embed(description=f"✅ **{member.display_name}** removed from whitelist.", color=COLORS["warn"])
            )

    @app_commands.command(name="pokemonlist", description="Allow a user to use Pokémon commands when locked. (Owner)")
    async def pokemonlist(self, interaction: discord.Interaction, member: discord.Member):
        if not is_owner(interaction.user):
            return await interaction.response.send_message("❌ Owner only!", ephemeral=True)
        pl = load("pokemonlist")
        if not isinstance(pl, list): pl = []
        if member.id not in pl:
            pl.append(member.id)
            POKEMONLIST_USERS.add(member.id)
            save("pokemonlist", pl)
            await interaction.response.send_message(
                embed=discord.Embed(description=f"✅ **{member.display_name}** can now use Pokémon commands when bot is locked.", color=COLORS["success"])
            )
        else:
            pl.remove(member.id)
            POKEMONLIST_USERS.discard(member.id)
            save("pokemonlist", pl)
            await interaction.response.send_message(
                embed=discord.Embed(description=f"✅ **{member.display_name}** removed from pokemon-list.", color=COLORS["warn"])
            )

    @app_commands.command(name="lockbot", description="Lock or unlock the bot. (Owner)")
    async def lockbot_slash(self, interaction: discord.Interaction):
        global BOT_LOCKED
        if not is_owner(interaction.user):
            return await interaction.response.send_message("❌ Owner only!", ephemeral=True)
        BOT_LOCKED = not BOT_LOCKED
        if BOT_LOCKED:
            e = discord.Embed(title="🔒 Bot Locked", description="Only you and whitelisted users can use commands.", color=COLORS["error"])
        else:
            e = discord.Embed(title="🔓 Bot Unlocked", description="All users can use commands again.", color=COLORS["success"])
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="invite", description="Get the bot's invite link. (Admin perms required)")
    async def invite(self, interaction: discord.Interaction):
        perms = discord.Permissions(administrator=True)
        url = discord.utils.oauth_url(interaction.client.user.id, permissions=perms)
        e = discord.Embed(
            title="🔗 Invite PolarBot",
            description=f"[Click here to invite me!]({url})\n\nRequires **Administrator** permissions.",
            color=COLORS["main"]
        )
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="teamsetup", description="Set your battle party (up to 5 Pokémon).")
    async def teamsetup(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        ud = get_pokemon_data(uid)
        if not ud["pokemon"]:
            return await interaction.response.send_message(
                embed=discord.Embed(description="❌ You have no Pokémon! Use `,start` first.", color=COLORS["error"]),
                ephemeral=True
            )
        current_party = ud.get("party", [])
        party_txt = ""
        if current_party:
            members = [p for p in ud["pokemon"] if p["id"] in current_party]
            party_txt = "\n**Current party:** " + ", ".join(f"**{p['species'].capitalize()}**" for p in members)
        e = discord.Embed(
            title="⚔️ Team Setup",
            description=f"Select up to **5 Pokémon** for your battle party.{party_txt}\n\nYour party will be used automatically in `,pokefight` and `,pbattle`!",
            color=COLORS["pokemon"]
        )
        view = TeamSetupView(interaction.user, ud["pokemon"])
        await interaction.response.send_message(embed=e, view=view)

    @app_commands.command(name="dex", description="View a Pokémon's Pokédex entry.")
    async def dex(self, interaction: discord.Interaction, pokemon: str):
        await interaction.response.defer()
        name = pokemon.lower().replace(" ", "-")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{POKEAPI}/pokemon/{name}") as r:
                    if r.status != 200:
                        return await interaction.followup.send(embed=discord.Embed(description=f"❌ Pokémon `{pokemon}` not found.", color=COLORS["error"]))
                    pdata = await r.json()
                async with session.get(f"{POKEAPI}/pokemon-species/{name}") as r2:
                    sdata = await r2.json() if r2.status == 200 else {}
            pid = pdata["id"]
            pname = pdata["name"].capitalize()
            types = " / ".join(f"{TYPE_EMOJI.get(t['type']['name'],'⭐')} {t['type']['name'].capitalize()}" for t in pdata["types"])
            stats = {s["stat"]["name"]: s["base_stat"] for s in pdata["stats"]}
            abilities = ", ".join(a["ability"]["name"].capitalize() for a in pdata["abilities"])
            height = pdata["height"] / 10
            weight = pdata["weight"] / 10
            flavor = ""
            if sdata.get("flavor_text_entries"):
                for entry in sdata["flavor_text_entries"]:
                    if entry["language"]["name"] == "en":
                        flavor = entry["flavor_text"].replace("\n"," ").replace("\f"," ")
                        break
            genus = ""
            if sdata.get("genera"):
                for g in sdata["genera"]:
                    if g["language"]["name"] == "en":
                        genus = g["genus"]
                        break
            e = discord.Embed(title=f"#{pid} — {pname}", description=f"*{genus}*\n\n{flavor}", color=COLORS["pokemon"])
            e.set_thumbnail(url=pokemon_official_art(pid))
            e.add_field(name="🏷️ Type",    value=types, inline=True)
            e.add_field(name="📏 Height",  value=f"**{height}m**", inline=True)
            e.add_field(name="⚖️ Weight",  value=f"**{weight}kg**", inline=True)
            e.add_field(name="⚡ Abilities",value=abilities, inline=False)
            stat_bar = (
                f"`HP ` {stats.get('hp',0):>3} {'█'*int(stats.get('hp',0)/10)}\n"
                f"`ATK` {stats.get('attack',0):>3} {'█'*int(stats.get('attack',0)/10)}\n"
                f"`DEF` {stats.get('defense',0):>3} {'█'*int(stats.get('defense',0)/10)}\n"
                f"`SPA` {stats.get('special-attack',0):>3} {'█'*int(stats.get('special-attack',0)/10)}\n"
                f"`SPD` {stats.get('special-defense',0):>3} {'█'*int(stats.get('special-defense',0)/10)}\n"
                f"`SPE` {stats.get('speed',0):>3} {'█'*int(stats.get('speed',0)/10)}"
            )
            e.add_field(name="📊 Base Stats", value=stat_bar, inline=False)
            total = sum(stats.values())
            e.set_footer(text=f"Base Stat Total: {total}")
            await interaction.followup.send(embed=e)
        except Exception as ex:
            await interaction.followup.send(embed=discord.Embed(description=f"❌ Error fetching data: {ex}", color=COLORS["error"]))

# ════════════════════════════════════════════════════════════
#  COG — GIVEAWAYS
# ════════════════════════════════════════════════════════════
class Giveaways(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_giveaways.start()

    def cog_unload(self):
        self.check_giveaways.cancel()

    @tasks.loop(seconds=10)
    async def check_giveaways(self):
        data = load("giveaways")
        now = datetime.now(timezone.utc).timestamp()
        changed = False
        for gid, gw in list(data.items()):
            if gw.get("ended"): continue
            if now >= gw["ends_at"]:
                await self._end_giveaway(gid, gw)
                data[gid]["ended"] = True
                changed = True
        if changed: save("giveaways", data)

    async def _end_giveaway(self, gw_id: str, gw: dict):
        try:
            ch = self.bot.get_channel(gw["channel_id"])
            msg = await ch.fetch_message(gw["message_id"])
            entries = [u async for u in msg.reactions[0].users() if not u.bot]
            winners_n = min(gw.get("winners",1), len(entries))
            if not entries:
                await ch.send(embed=discord.Embed(description=f"🎁 **{gw['prize']}** — No one entered!", color=COLORS["warn"]))
                return
            winners = random.sample(entries, winners_n)
            mentions = ", ".join(w.mention for w in winners)
            await ch.send(embed=discord.Embed(
                title="🎉 Giveaway Ended!",
                description=f"**Prize:** {gw['prize']}\n**Winner(s):** {mentions}\nCongrats!",
                color=COLORS["success"]
            ))
        except Exception as ex:
            print(f"Giveaway error: {ex}")

    @commands.hybrid_command(name="giveaway", description="Start a giveaway.")
    async def giveaway(self, ctx, duration: str, winners: int = 1, *, prize: str):
        if not is_staff(ctx.author):
            return await ctx.send(embed=discord.Embed(description="❌ Staff only!", color=COLORS["error"]))
        secs = parse_duration(duration)
        if not secs:
            return await ctx.send(embed=discord.Embed(description="❌ Invalid duration.", color=COLORS["error"]))
        ends = datetime.now(timezone.utc).timestamp() + secs
        e = discord.Embed(
            title="🎁 GIVEAWAY!",
            description=f"**Prize:** {prize}\n**Winners:** {winners}\n**Ends:** <t:{int(ends)}:R>\n\nReact with 🎉 to enter!",
            color=COLORS["success"]
        )
        msg = await ctx.send(embed=e)
        await msg.add_reaction("🎉")
        data = load("giveaways")
        data[str(msg.id)] = {"prize":prize,"winners":winners,"ends_at":ends,"channel_id":ctx.channel.id,"message_id":msg.id,"ended":False}
        save("giveaways", data)

    @commands.hybrid_command(name="gend", description="End a giveaway early.")
    async def gend(self, ctx, message_id: str):
        if not is_staff(ctx.author):
            return await ctx.send(embed=discord.Embed(description="❌ Staff only!", color=COLORS["error"]))
        data = load("giveaways")
        if message_id not in data:
            return await ctx.send(embed=discord.Embed(description="❌ Not found.", color=COLORS["error"]))
        await self._end_giveaway(message_id, data[message_id])
        data[message_id]["ended"] = True
        save("giveaways", data)
        await ctx.send(embed=discord.Embed(description="✅ Giveaway ended.", color=COLORS["success"]))

    @commands.hybrid_command(name="greroll", description="Reroll a giveaway winner.")
    async def greroll(self, ctx, message_id: str):
        if not is_staff(ctx.author):
            return await ctx.send(embed=discord.Embed(description="❌ Staff only!", color=COLORS["error"]))
        data = load("giveaways")
        if message_id not in data:
            return await ctx.send(embed=discord.Embed(description="❌ Not found.", color=COLORS["error"]))
        await self._end_giveaway(message_id, data[message_id])
        await ctx.send(embed=discord.Embed(description="🔄 Rerolled!", color=COLORS["success"]))

    @commands.hybrid_command(name="glist", description="List active giveaways.")
    async def glist(self, ctx):
        data = load("giveaways")
        active = [(gid, gw) for gid, gw in data.items() if not gw.get("ended")]
        if not active:
            return await ctx.send(embed=discord.Embed(description="No active giveaways.", color=COLORS["info"]))
        e = discord.Embed(title="🎁 Active Giveaways", color=COLORS["success"])
        for gid, gw in active[:10]:
            e.add_field(name=gw["prize"], value=f"Ends: <t:{int(gw['ends_at'])}:R>\nWinners: **{gw['winners']}**\nID: `{gid}`", inline=False)
        await ctx.send(embed=e)

# ════════════════════════════════════════════════════════════
#  COG — FUN
# ════════════════════════════════════════════════════════════
class Fun(commands.Cog):
    def __init__(self, bot): self.bot = bot

    @commands.hybrid_command(name="snipe")
    async def snipe(self, ctx):
        data = self.bot.snipe_data.get(ctx.channel.id)
        if not data:
            return await ctx.send(embed=discord.Embed(description="Nothing to snipe!", color=COLORS["error"]))
        e = discord.Embed(description=data["content"], color=COLORS["main"], timestamp=datetime.now(timezone.utc))
        e.set_author(name=data["author"], icon_url=data["avatar"])
        e.set_footer(text="🔍 Sniped")
        await ctx.send(embed=e)

    @commands.hybrid_command(name="editsnipe")
    async def editsnipe(self, ctx):
        data = self.bot.editsnipe_data.get(ctx.channel.id)
        if not data:
            return await ctx.send(embed=discord.Embed(description="Nothing to edit-snipe!", color=COLORS["error"]))
        e = discord.Embed(color=COLORS["main"])
        e.set_author(name=data["author"], icon_url=data["avatar"])
        e.add_field(name="Before", value=data["before"], inline=False)
        e.add_field(name="After",  value=data["after"],  inline=False)
        await ctx.send(embed=e)

    @commands.hybrid_command(name="avatar")
    async def avatar(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        e = discord.Embed(title=f"🖼️ {member.display_name}'s Avatar", color=COLORS["main"])
        e.set_image(url=member.display_avatar.url)
        await ctx.send(embed=e)

    @commands.hybrid_command(name="dankmemer")
    async def dankmemer(self, ctx):
        async with aiohttp.ClientSession() as session:
            async with session.get("https://meme-api.com/gimme") as r:
                if r.status != 200:
                    return await ctx.send(embed=discord.Embed(description="❌ Couldn't fetch meme.", color=COLORS["error"]))
                data = await r.json()
        e = discord.Embed(title=data.get("title","Meme"), color=COLORS["main"])
        e.set_image(url=data.get("url"))
        e.set_footer(text=f"👍 {data.get('ups',0)} | r/{data.get('subreddit','')}")
        await ctx.send(embed=e)

    @commands.hybrid_command(name="afk")
    async def afk(self, ctx, *, reason: str = "AFK"):
        uid = str(ctx.author.id)
        data = load("afk")
        data[uid] = {"reason":reason,"time":datetime.now(timezone.utc).isoformat()}
        save("afk", data)
        await ctx.send(embed=discord.Embed(description=f"💤 You're now AFK: **{reason}**", color=COLORS["info"]))

    @commands.hybrid_command(name="confess")
    async def confess(self, ctx, *, message: str):
        settings = load("server_settings")
        gs = gdata(settings, ctx.guild.id)
        ch_id = gs.get("confession_channel")
        if not ch_id:
            return await ctx.send(embed=discord.Embed(description="❌ No confession channel set.", color=COLORS["error"]))
        ch = ctx.guild.get_channel(ch_id)
        if not ch:
            return await ctx.send(embed=discord.Embed(description="❌ Confession channel not found.", color=COLORS["error"]))
        data = load("confessions")
        count = data.get(str(ctx.guild.id), 0) + 1
        data[str(ctx.guild.id)] = count
        save("confessions", data)
        e = discord.Embed(title=f"🕵️ Anonymous Confession #{count}", description=message, color=COLORS["main"], timestamp=datetime.now(timezone.utc))
        await ch.send(embed=e)
        await ctx.send(embed=discord.Embed(description="✅ Confession sent anonymously!", color=COLORS["success"]), ephemeral=True)

    @commands.hybrid_command(name="setconfess")
    async def setconfess(self, ctx, channel: discord.TextChannel):
        if not is_staff(ctx.author):
            return await ctx.send(embed=discord.Embed(description="❌ Staff only!", color=COLORS["error"]))
        settings = load("server_settings")
        gs = gdata(settings, ctx.guild.id)
        gs["confession_channel"] = channel.id
        save("server_settings", settings)
        await ctx.send(embed=discord.Embed(description=f"✅ Confession channel set to {channel.mention}.", color=COLORS["success"]))

    @commands.hybrid_command(name="serverinfo")
    async def serverinfo(self, ctx):
        g = ctx.guild
        e = discord.Embed(title=f"📊 {g.name}", color=COLORS["info"])
        e.set_thumbnail(url=g.icon.url if g.icon else None)
        e.add_field(name="👑 Owner",   value=str(g.owner),        inline=True)
        e.add_field(name="👥 Members", value=f"**{g.member_count}**", inline=True)
        e.add_field(name="💬 Channels",value=f"**{len(g.channels)}**", inline=True)
        e.add_field(name="🎭 Roles",   value=f"**{len(g.roles)}**",   inline=True)
        e.add_field(name="📅 Created", value=f"<t:{int(g.created_at.timestamp())}:D>", inline=True)
        await ctx.send(embed=e)

    @commands.hybrid_command(name="userinfo")
    async def userinfo(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        roles = [r.mention for r in member.roles[1:]][:10]
        e = discord.Embed(title=f"👤 {member}", color=member.color)
        e.set_thumbnail(url=member.display_avatar)
        e.add_field(name="🆔 ID",         value=str(member.id),  inline=True)
        e.add_field(name="📅 Joined",     value=f"<t:{int(member.joined_at.timestamp())}:D>", inline=True)
        e.add_field(name="📆 Registered", value=f"<t:{int(member.created_at.timestamp())}:D>", inline=True)
        e.add_field(name=f"🎭 Roles ({len(roles)})", value=" ".join(roles) or "None", inline=False)
        await ctx.send(embed=e)

    @commands.hybrid_command(name="info")
    async def info(self, ctx):
        uptime = datetime.now(timezone.utc) - self.bot._start_time if self.bot._start_time else timedelta(0)
        e = discord.Embed(title=f"🤖 {BOT_NAME}", color=COLORS["pokemon"])
        e.add_field(name="📡 Servers",   value=f"**{len(self.bot.guilds)}**", inline=True)
        e.add_field(name="👥 Users",     value=f"**{sum(g.member_count for g in self.bot.guilds)}**", inline=True)
        e.add_field(name="🏓 Latency",   value=f"**{round(self.bot.latency*1000)}ms**", inline=True)
        e.add_field(name="⏱️ Uptime",    value=str(uptime).split(".")[0], inline=True)
        e.add_field(name="🐍 Python",    value=platform.python_version(), inline=True)
        e.add_field(name="📦 discord.py",value=discord.__version__, inline=True)
        await ctx.send(embed=e)

    @commands.hybrid_command(name="autoresponse")
    async def autoresponse(self, ctx, action: str, trigger: str = "", *, response: str = ""):
        if not is_staff(ctx.author):
            return await ctx.send(embed=discord.Embed(description="❌ Staff only!", color=COLORS["error"]))
        data = load("autoresponses")
        gs = gdata(data, ctx.guild.id)
        if action.lower() == "add":
            if not trigger or not response:
                return await ctx.send(embed=discord.Embed(description="❌ Usage: `,autoresponse add <trigger> <response>`", color=COLORS["error"]))
            gs[trigger.lower()] = response
            save("autoresponses", data)
            await ctx.send(embed=discord.Embed(description=f"✅ Auto-response added for `{trigger}`.", color=COLORS["success"]))
        elif action.lower() == "remove":
            if trigger.lower() in gs:
                del gs[trigger.lower()]
                save("autoresponses", data)
                await ctx.send(embed=discord.Embed(description=f"✅ Removed auto-response for `{trigger}`.", color=COLORS["success"]))
            else:
                await ctx.send(embed=discord.Embed(description="❌ Trigger not found.", color=COLORS["error"]))
        elif action.lower() == "list":
            if not gs:
                return await ctx.send(embed=discord.Embed(description="No auto-responses set.", color=COLORS["info"]))
            e = discord.Embed(title="📝 Auto-Responses", color=COLORS["info"])
            for t, r in list(gs.items())[:15]:
                e.add_field(name=f"`{t}`", value=r[:50], inline=False)
            await ctx.send(embed=e)

    @commands.hybrid_command(name="sticky")
    async def sticky(self, ctx, *, message: str):
        if not is_staff(ctx.author):
            return await ctx.send(embed=discord.Embed(description="❌ Staff only!", color=COLORS["error"]))
        data = load("sticky")
        gs = gdata(data, ctx.guild.id)
        gs[str(ctx.channel.id)] = message
        save("sticky", data)
        await ctx.send(embed=discord.Embed(description=f"📌 Sticky set:\n\n{message}", color=COLORS["info"]))

    @commands.hybrid_command(name="unsticky")
    async def unsticky(self, ctx):
        if not is_staff(ctx.author):
            return await ctx.send(embed=discord.Embed(description="❌ Staff only!", color=COLORS["error"]))
        data = load("sticky")
        gs = gdata(data, ctx.guild.id)
        gs.pop(str(ctx.channel.id), None)
        save("sticky", data)
        await ctx.send(embed=discord.Embed(description="✅ Sticky removed.", color=COLORS["success"]))

    @commands.hybrid_command(name="setstats")
    async def setstats(self, ctx):
        if not is_staff(ctx.author):
            return await ctx.send(embed=discord.Embed(description="❌ Staff only!", color=COLORS["error"]))
        cat = await ctx.guild.create_category("📊 Server Stats")
        await ctx.guild.create_voice_channel(f"👥 Members: {ctx.guild.member_count}", category=cat)
        await ctx.guild.create_voice_channel(f"💬 Channels: {len(ctx.guild.channels)}", category=cat)
        await ctx.guild.create_voice_channel(f"🎭 Roles: {len(ctx.guild.roles)}", category=cat)
        data = load("stats_channels")
        data[str(ctx.guild.id)] = {"category": cat.id}
        save("stats_channels", data)
        await ctx.send(embed=discord.Embed(description="✅ Stat channels created!", color=COLORS["success"]))

    @commands.hybrid_command(name="setps")
    async def setps(self, ctx, *, link: str):
        if not is_staff(ctx.author): return
        settings = load("server_settings")
        gs = gdata(settings, ctx.guild.id)
        gs["ps_link"] = link
        save("server_settings", settings)
        await ctx.send(embed=discord.Embed(description="✅ PS link set!", color=COLORS["success"]))

    @commands.hybrid_command(name="ps")
    async def ps(self, ctx):
        settings = load("server_settings")
        gs = gdata(settings, ctx.guild.id)
        link = gs.get("ps_link")
        if not link:
            return await ctx.send(embed=discord.Embed(description="❌ No PS link set.", color=COLORS["error"]))
        await ctx.send(embed=discord.Embed(title="🎮 Private Server", description=link, color=COLORS["info"]))

# ════════════════════════════════════════════════════════════
#  COG — GAMES
# ════════════════════════════════════════════════════════════
class Games(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.wordchain = {}
        self.hangman_games = {}
        self.fasttype_games = {}

    @commands.hybrid_command(name="rps")
    async def rps(self, ctx):
        choices = ["rock","paper","scissors"]
        emojis  = {"rock":"🪨","paper":"📄","scissors":"✂️"}
        beats   = {"rock":"scissors","paper":"rock","scissors":"paper"}
        view    = discord.ui.View(timeout=30)
        for c in choices:
            btn = discord.ui.Button(label=c.capitalize(), emoji=emojis[c], style=discord.ButtonStyle.primary)
            async def cb(interaction: discord.Interaction, choice=c):
                if interaction.user.id != ctx.author.id:
                    return await interaction.response.send_message("Not yours!", ephemeral=True)
                bot_choice = random.choice(choices)
                if choice == bot_choice:
                    result, color = "**It's a tie!** 🤝", COLORS["warn"]
                elif beats[choice] == bot_choice:
                    result, color = "**You win!** 🏆", COLORS["success"]
                else:
                    result, color = "**Bot wins!** 🤖", COLORS["error"]
                e = discord.Embed(
                    title="Rock Paper Scissors",
                    description=f"You: {emojis[choice]} {choice.capitalize()}\nBot: {emojis[bot_choice]} {bot_choice.capitalize()}\n\n{result}",
                    color=color
                )
                for item in view.children: item.disabled = True
                await interaction.response.edit_message(embed=e, view=view)
            btn.callback = cb
            view.add_item(btn)
        await ctx.send(embed=discord.Embed(title="🪨 Rock Paper Scissors!", description="Choose:", color=COLORS["main"]), view=view)

    @commands.hybrid_command(name="trivia")
    async def trivia(self, ctx):
        async with aiohttp.ClientSession() as session:
            async with session.get("https://opentdb.com/api.php?amount=1&type=multiple") as r:
                if r.status != 200:
                    return await ctx.send(embed=discord.Embed(description="❌ Couldn't fetch trivia.", color=COLORS["error"]))
                data = await r.json()
        item = data["results"][0]
        q = re.sub(r"&[a-z]+;", " ", item["question"])
        correct = re.sub(r"&[a-z]+;", " ", item["correct_answer"])
        wrong = [re.sub(r"&[a-z]+;", " ", a) for a in item["incorrect_answers"]]
        options = wrong + [correct]
        random.shuffle(options)
        view = discord.ui.View(timeout=30)
        answered = [False]
        for opt in options:
            btn = discord.ui.Button(label=opt[:80], style=discord.ButtonStyle.primary)
            async def cb(interaction: discord.Interaction, answer=opt):
                if interaction.user.id != ctx.author.id:
                    return await interaction.response.send_message("Not yours!", ephemeral=True)
                if answered[0]: return
                answered[0] = True
                for item2 in view.children: item2.disabled = True
                if answer == correct:
                    coins = random.randint(15, 40)
                    uid = str(interaction.user.id)
                    ud = get_pokemon_data(uid)
                    ud["balance"] = ud.get("balance",0) + coins
                    save_pokemon_data(uid, ud)
                    e = discord.Embed(title="✅ Correct!", description=f"Answer: **{correct}**!\n💰 +{coins} coins!", color=COLORS["success"])
                else:
                    e = discord.Embed(title="❌ Wrong!", description=f"Correct answer: **{correct}**.", color=COLORS["error"])
                await interaction.response.edit_message(embed=e, view=view)
            btn.callback = cb
            view.add_item(btn)
        e = discord.Embed(title=f"🧠 Trivia — {item['category']}", description=f"**Difficulty:** {item['difficulty'].capitalize()}\n\n{q}", color=COLORS["info"])
        await ctx.send(embed=e, view=view)

    @commands.hybrid_command(name="tictactoe")
    async def tictactoe(self, ctx, member: discord.Member):
        if member.bot or member.id == ctx.author.id:
            return await ctx.send(embed=discord.Embed(description="❌ Invalid opponent.", color=COLORS["error"]))
        board = [None]*9
        turn = [ctx.author]
        view = discord.ui.View(timeout=120)
        def check_winner():
            wins = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
            for a,b,c in wins:
                if board[a] and board[a]==board[b]==board[c]: return board[a]
            return None
        def make_btn(i):
            btn = discord.ui.Button(label="ᅠ", row=i//3, style=discord.ButtonStyle.secondary)
            async def cb(interaction: discord.Interaction, idx=i):
                if interaction.user.id != turn[0].id:
                    return await interaction.response.send_message("Not your turn!", ephemeral=True)
                if board[idx]: return
                mark = "❌" if turn[0]==ctx.author else "⭕"
                board[idx] = mark
                btn.label = mark
                btn.style = discord.ButtonStyle.danger if mark=="❌" else discord.ButtonStyle.primary
                btn.disabled = True
                winner = check_winner()
                if winner:
                    for c in view.children: c.disabled = True
                    wn = "You" if turn[0]==ctx.author else member.display_name
                    return await interaction.response.edit_message(
                        embed=discord.Embed(description=f"🏆 **{wn}** wins Tic Tac Toe!", color=COLORS["success"]),
                        view=view
                    )
                if all(board):
                    for c in view.children: c.disabled = True
                    return await interaction.response.edit_message(
                        embed=discord.Embed(description="🤝 Draw!", color=COLORS["warn"]), view=view
                    )
                turn[0] = member if turn[0]==ctx.author else ctx.author
                await interaction.response.edit_message(
                    embed=discord.Embed(description=f"Tic Tac Toe: {turn[0].mention}'s turn", color=COLORS["main"]),
                    view=view
                )
            btn.callback = cb
            return btn
        for i in range(9): view.add_item(make_btn(i))
        await ctx.send(embed=discord.Embed(description=f"⭕❌ **Tic Tac Toe!**\n{ctx.author.mention} vs {member.mention}", color=COLORS["main"]), view=view)

    @commands.hybrid_command(name="hangman")
    async def hangman(self, ctx):
        words = ["pikachu","bulbasaur","charmander","squirtle","mewtwo","gengar","snorlax","eevee","dragonite","lapras"]
        word = random.choice(words)
        guessed: set = set()
        lives = [6]
        stages = ["😵","😦","😟","😕","😐","🙂","😀"]
        def display():
            return " ".join(l if l in guessed else r"\_" for l in word)
        e = discord.Embed(title="🔤 Hangman", description=f"{stages[lives[0]]} Lives: **{lives[0]}**\n\n`{display()}`\n\nType a letter in chat!", color=COLORS["info"])
        m = await ctx.send(embed=e)
        self.hangman_games[ctx.channel.id] = {"word":word,"guessed":guessed,"lives":lives,"msg":m,"player":ctx.author.id}

    @commands.hybrid_command(name="wordchain")
    async def wordchain(self, ctx):
        self.wordchain[ctx.channel.id] = {"last":None,"used":set(),"active":True}
        await ctx.send(embed=discord.Embed(title="🔗 Word Chain!", description="Each word must start with the last letter of the previous word!", color=COLORS["info"]))

    @commands.hybrid_command(name="stopwc")
    async def stopwc(self, ctx):
        if ctx.channel.id in self.wordchain:
            del self.wordchain[ctx.channel.id]
            await ctx.send(embed=discord.Embed(description="✅ Word chain stopped.", color=COLORS["success"]))

    @commands.hybrid_command(name="fasttype")
    async def fasttype(self, ctx):
        sentences = [
            "The quick brown fox jumps over the lazy dog",
            "Pack my box with five dozen liquor jugs",
            "How vexingly quick daft zebras jump",
        ]
        sentence = random.choice(sentences)
        await ctx.send(embed=discord.Embed(title="⌨️ Fast Type!", description=f"Type:\n```{sentence}```", color=COLORS["info"]))
        start = time.time()
        def check(m): return m.author == ctx.author and m.channel == ctx.channel
        try:
            msg = await self.bot.wait_for("message", check=check, timeout=60)
            elapsed = time.time() - start
            if msg.content.strip().lower() == sentence.lower():
                wpm = int(len(sentence.split()) / (elapsed / 60))
                coins = min(200, int(200 / elapsed * 10))
                uid = str(ctx.author.id)
                ud = get_pokemon_data(uid)
                ud["balance"] = ud.get("balance",0) + coins
                save_pokemon_data(uid, ud)
                await ctx.send(embed=discord.Embed(title="✅ Correct!", description=f"⏱️ {elapsed:.2f}s | WPM: **{wpm}**\n💰 +**{coins}** coins!", color=COLORS["success"]))
            else:
                await ctx.send(embed=discord.Embed(description="❌ Not quite right!", color=COLORS["error"]))
        except asyncio.TimeoutError:
            await ctx.send(embed=discord.Embed(description="⏰ Time's up!", color=COLORS["warn"]))

    @commands.hybrid_command(name="wouldyourather")
    async def wouldyourather(self, ctx):
        scenarios = [
            ("Have infinite money but no love","Find true love but be broke forever"),
            ("Be able to fly but only 1m high","Teleport but only 1m away"),
            ("Fight 100 duck-sized horses","Fight 1 horse-sized duck"),
        ]
        a, b = random.choice(scenarios)
        e = discord.Embed(title="🤔 Would You Rather?", color=COLORS["main"])
        e.add_field(name="🔴 Option A", value=a, inline=False)
        e.add_field(name="🔵 Option B", value=b, inline=False)
        m = await ctx.send(embed=e)
        await m.add_reaction("🔴"); await m.add_reaction("🔵")

    @commands.hybrid_command(name="connect4")
    async def connect4(self, ctx, member: discord.Member):
        if member.bot or member.id == ctx.author.id:
            return await ctx.send(embed=discord.Embed(description="❌ Invalid opponent.", color=COLORS["error"]))
        board = [[None]*7 for _ in range(6)]
        turn = [ctx.author]
        view = discord.ui.View(timeout=180)
        def render():
            rows = []
            for row in board:
                rows.append("".join(("🔴" if c==ctx.author.id else "🔵" if c else "⬛") for c in row))
            return "\n".join(rows)+"\n1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣"
        def check_win():
            for r in range(6):
                for c in range(7):
                    uid2 = board[r][c]
                    if not uid2: continue
                    for dr,dc in [(0,1),(1,0),(1,1),(1,-1)]:
                        try:
                            if all(0<=r+dr*i<6 and 0<=c+dc*i<7 and board[r+dr*i][c+dc*i]==uid2 for i in range(4)):
                                return uid2
                        except: pass
            return None
        for col in range(7):
            btn = discord.ui.Button(label=str(col+1), style=discord.ButtonStyle.primary, row=0)
            async def cb(interaction: discord.Interaction, c=col):
                if interaction.user.id != turn[0].id:
                    return await interaction.response.send_message("Not your turn!", ephemeral=True)
                for r in range(5,-1,-1):
                    if board[r][c] is None:
                        board[r][c] = turn[0].id; break
                else:
                    return await interaction.response.send_message("Column full!", ephemeral=True)
                wid = check_win()
                if wid:
                    wnr = ctx.author if wid==ctx.author.id else member
                    for ch2 in view.children: ch2.disabled = True
                    return await interaction.response.edit_message(embed=discord.Embed(title=f"🏆 {wnr.display_name} wins!", description=render(), color=COLORS["success"]), view=view)
                if all(board[0][cc] for cc in range(7)):
                    for ch2 in view.children: ch2.disabled = True
                    return await interaction.response.edit_message(embed=discord.Embed(title="🤝 Draw!", description=render(), color=COLORS["warn"]), view=view)
                turn[0] = member if turn[0]==ctx.author else ctx.author
                await interaction.response.edit_message(embed=discord.Embed(title="🔴🔵 Connect 4", description=render()+f"\n\n{turn[0].mention}'s turn", color=COLORS["main"]), view=view)
            btn.callback = cb
            view.add_item(btn)
        await ctx.send(embed=discord.Embed(title="🔴🔵 Connect 4", description=render()+f"\n\n{ctx.author.mention} goes first (🔴)", color=COLORS["main"]), view=view)

    @commands.hybrid_command(name="memorymatch")
    async def memorymatch(self, ctx, member: discord.Member):
        if member.bot or member.id == ctx.author.id:
            return await ctx.send(embed=discord.Embed(description="❌ Invalid opponent.", color=COLORS["error"]))
        emojis = ["🍎","🍊","🍋","🍇","🍓","🍉","🎮","⭐"] * 2
        random.shuffle(emojis)
        revealed: set = set(); matched: set = set()
        scores = {ctx.author.id:0, member.id:0}
        turn = [ctx.author]
        def render_board():
            lines = [f"`{i:>2}:` {'❓' if i not in revealed and i not in matched else emojis[i]}" for i in range(16)]
            return "\n".join(lines)
        await ctx.send(embed=discord.Embed(title="🃏 Memory Match!", description=f"{ctx.author.mention} vs {member.mention}\n\n{render_board()}", color=COLORS["main"]))
        while len(matched) < 16:
            def check(m): return m.author.id == turn[0].id and m.channel == ctx.channel and m.content.isdigit()
            try:
                m1 = await self.bot.wait_for("message", check=check, timeout=30)
                i1 = int(m1.content)
                if not (0 <= i1 <= 15) or i1 in revealed or i1 in matched: continue
                revealed.add(i1)
                await ctx.send(embed=discord.Embed(description=f"Flipped `{i1}`: {emojis[i1]}\nPick second card!", color=COLORS["info"]))
                m2 = await self.bot.wait_for("message", check=check, timeout=30)
                i2 = int(m2.content)
                if not (0 <= i2 <= 15) or i2 == i1 or i2 in revealed or i2 in matched:
                    revealed.discard(i1); continue
                revealed.add(i2)
                await asyncio.sleep(1)
                if emojis[i1] == emojis[i2]:
                    matched.add(i1); matched.add(i2)
                    revealed.discard(i1); revealed.discard(i2)
                    scores[turn[0].id] += 1
                    await ctx.send(embed=discord.Embed(description=f"✅ Match! Score: {scores}", color=COLORS["success"]))
                else:
                    revealed.discard(i1); revealed.discard(i2)
                    await ctx.send(embed=discord.Embed(description=f"❌ No match!", color=COLORS["error"]))
                    turn[0] = member if turn[0]==ctx.author else ctx.author
            except asyncio.TimeoutError:
                break
        winner = max(scores, key=scores.get)
        winner_obj = ctx.author if winner==ctx.author.id else member
        await ctx.send(embed=discord.Embed(
            title=f"🏆 Memory Match Over! {winner_obj.display_name} wins!",
            description=f"{ctx.author.display_name}: **{scores[ctx.author.id]}**\n{member.display_name}: **{scores[member.id]}**",
            color=COLORS["success"]
        ))

# ════════════════════════════════════════════════════════════
#  COG — POKEMON
# ════════════════════════════════════════════════════════════
class Pokemon(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.spawn_data = {}
        self.spawn_loop.start()

    def cog_unload(self):
        self.spawn_loop.cancel()

    @tasks.loop(minutes=2)
    async def spawn_loop(self):
        for guild in self.bot.guilds:
            settings = load("server_settings")
            gs = gdata(settings, guild.id)
            ch_id = gs.get("spawn_channel")
            if not ch_id: continue
            ch = guild.get_channel(ch_id)
            if not ch: continue
            pid, name = random.choice(FALLBACK_POKEMON)
            shiny = random.random() < SHINY_RATE
            is_leg = pid in LEGENDARY_IDS
            is_rare = pid in RARE_IDS
            spawn = {"id":pid,"name":name,"shiny":shiny,"legendary":is_leg,"rare":is_rare}
            self.spawn_data[guild.id] = spawn
            shiny_txt = "✨ **Shiny** " if shiny else ""
            leg_txt = " 🌟 **Legendary!**" if is_leg else (" 💫 **Rare!**" if is_rare else "")
            e = discord.Embed(
                title=f"🌿 A wild Pokémon appeared!{leg_txt}",
                description=f"A wild {shiny_txt}**{name.capitalize()}** appeared!\nUse `,poke` to catch it!",
                color=COLORS["pokemon"]
            )
            e.set_image(url=pokemon_sprite_url(pid, shiny))
            await ch.send(embed=e)

    @commands.hybrid_command(name="setspawn", description="Set the Pokémon spawn channel.")
    async def setspawn(self, ctx, channel: discord.TextChannel = None):
        if not is_staff(ctx.author): return
        ch = channel or ctx.channel
        settings = load("server_settings")
        gs = gdata(settings, ctx.guild.id)
        gs["spawn_channel"] = ch.id
        save("server_settings", settings)
        await ctx.send(embed=discord.Embed(description=f"✅ Spawn channel set to {ch.mention}.", color=COLORS["success"]))

    @commands.hybrid_command(name="hint", description="Get a hint for the wild Pokémon.")
    async def hint(self, ctx):
        spawn = self.spawn_data.get(ctx.guild.id)
        if not spawn:
            return await ctx.send(embed=discord.Embed(description="No wild Pokémon right now!", color=COLORS["error"]))
        name = spawn["name"]
        hint = name[0] + "".join("_" if c != " " else " " for c in name[1:])
        await ctx.send(embed=discord.Embed(description=f"🔍 Hint: `{hint}` ({len(name)} letters)", color=COLORS["info"]))

    @commands.hybrid_command(name="poke", description="Catch the wild Pokémon!")
    async def poke(self, ctx):
        uid = str(ctx.author.id)
        ud = get_pokemon_data(uid)
        spawn = self.spawn_data.get(ctx.guild.id)
        if not spawn:
            return await ctx.send(embed=discord.Embed(
                description="🌿 No wild Pokémon right now!\nWait for one to appear in the spawn channel.",
                color=COLORS["warn"]
            ), ephemeral=True)
        total_balls = sum(ud.get("pokeballs", {}).values())
        if total_balls == 0:
            return await ctx.send(embed=discord.Embed(
                description="❌ You have no Pokéballs! Buy some at `,pokeshop`.",
                color=COLORS["error"]
            ), ephemeral=True)
        shiny_txt = "✨ **Shiny** " if spawn.get("shiny") else ""
        leg_txt = " 🌟 **(Legendary)**" if spawn.get("legendary") else (" 💫 **(Rare)**" if spawn.get("rare") else "")
        e = discord.Embed(
            title=f"🌿 Wild {spawn['name'].capitalize()} appeared!{leg_txt}",
            description=f"A wild {shiny_txt}**{spawn['name'].capitalize()}** is here!\nChoose your Pokéball or flee!",
            color=COLORS["pokemon"]
        )
        e.set_image(url=pokemon_sprite_url(spawn["id"], spawn.get("shiny", False)))
        view = CatchView(spawn, ctx.author.id)
        await ctx.send(embed=e, view=view)

    @commands.hybrid_command(name="start", description="Begin your Pokémon journey!")
    async def start(self, ctx):
        uid = str(ctx.author.id)
        ud = get_pokemon_data(uid)
        if ud["pokemon"]:
            return await ctx.send(embed=discord.Embed(description="✅ You already have a starter! Use `,pokemon` to view your team.", color=COLORS["info"]))
        view = StarterGenView(ctx, gen=1)
        await view.start()
        e = view.build_embed()
        await ctx.send(embed=e, view=view)

    @commands.hybrid_command(name="pokemon", aliases=["poke_list"], description="View your Pokémon collection.")
    async def pokemon(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        ud = get_pokemon_data(str(member.id))
        if not ud["pokemon"]:
            return await ctx.send(embed=discord.Embed(description=f"**{member.display_name}** has no Pokémon! Use `,start`.", color=COLORS["info"]))
        view = PokemonListView(member, ud["pokemon"])
        await ctx.send(embed=view.build_embed(), view=view)

    @commands.hybrid_command(name="pinfo", description="View detailed stats of a Pokémon.")
    async def pinfo(self, ctx, pokemon_id: int):
        uid = str(ctx.author.id)
        ud = get_pokemon_data(uid)
        pk = next((p for p in ud["pokemon"] if p["id"] == pokemon_id), None)
        if not pk:
            return await ctx.send(embed=discord.Embed(description=f"❌ No Pokémon with ID `{pokemon_id}`.", color=COLORS["error"]))
        ivs = pk.get("ivs", roll_ivs())
        ivpct = iv_percentage(ivs)
        shiny = pk.get("shiny", False)
        shiny_prefix = "✨ Shiny " if shiny else ""
        nick_suffix = f' \u00ab{pk["nickname"]}\u00bb' if pk.get("nickname") else ""
        e = discord.Embed(title=f"{shiny_prefix}{pk['species'].capitalize()}{nick_suffix}", color=COLORS["pokemon"])
        e.set_thumbnail(url=pokemon_sprite_url(pk["pokedex_id"], shiny))
        e.add_field(name="🆔 ID",      value=f"`#{pk['id']}`",          inline=True)
        e.add_field(name="📊 Level",   value=f"**{pk.get('level',1)}**", inline=True)
        e.add_field(name="🎯 IV%",     value=f"**{ivpct}%**",            inline=True)
        e.add_field(name="✨ Shiny",   value="Yes" if shiny else "No",   inline=True)
        e.add_field(name="⭐ Fav",     value="Yes" if pk.get("favorite") else "No", inline=True)
        e.add_field(name="🏷️ Nickname",value=pk.get("nickname") or "—",  inline=True)
        iv_txt = "\n".join(f"**{k.upper()}:** {v}/31" for k,v in ivs.items())
        e.add_field(name="📈 IVs", value=iv_txt, inline=False)
        ptype = pk.get("type","normal")
        moves = get_pokemon_moves(pk.get("pokedex_id",0), ptype)
        move_txt = "\n".join(f"• {m['name']}" for m in moves)
        e.add_field(name="⚔️ Moves", value=move_txt, inline=False)
        await ctx.send(embed=e)

    @commands.hybrid_command(name="pselect")
    async def pselect(self, ctx, pokemon_id: int):
        uid = str(ctx.author.id)
        ud = get_pokemon_data(uid)
        idx = next((i for i,p in enumerate(ud["pokemon"]) if p["id"]==pokemon_id), None)
        if idx is None:
            return await ctx.send(embed=discord.Embed(description=f"❌ No Pokémon with ID `{pokemon_id}`.", color=COLORS["error"]))
        ud["selected"] = idx
        save_pokemon_data(uid, ud)
        pk = ud["pokemon"][idx]
        await ctx.send(embed=discord.Embed(description=f"✅ **{pk['species'].capitalize()}** is now active!", color=COLORS["success"]))

    @commands.hybrid_command(name="pfav")
    async def pfav(self, ctx, pokemon_id: int):
        uid = str(ctx.author.id)
        ud = get_pokemon_data(uid)
        pk = next((p for p in ud["pokemon"] if p["id"]==pokemon_id), None)
        if not pk:
            return await ctx.send(embed=discord.Embed(description=f"❌ No Pokémon with ID `{pokemon_id}`.", color=COLORS["error"]))
        pk["favorite"] = not pk.get("favorite", False)
        save_pokemon_data(uid, ud)
        status = "⭐ favourited" if pk["favorite"] else "unfavourited"
        await ctx.send(embed=discord.Embed(description=f"✅ **{pk['species'].capitalize()}** {status}!", color=COLORS["success"]))

    @commands.hybrid_command(name="nickname")
    async def nickname(self, ctx, pokemon_id: int, *, name: str = ""):
        uid = str(ctx.author.id)
        ud = get_pokemon_data(uid)
        pk = next((p for p in ud["pokemon"] if p["id"]==pokemon_id), None)
        if not pk:
            return await ctx.send(embed=discord.Embed(description=f"❌ No Pokémon with ID `{pokemon_id}`.", color=COLORS["error"]))
        pk["nickname"] = name[:20] if name else None
        save_pokemon_data(uid, ud)
        txt = f"named **{pk['nickname']}**" if name else "nickname cleared"
        await ctx.send(embed=discord.Embed(description=f"✅ **{pk['species'].capitalize()}** {txt}!", color=COLORS["success"]))

    @commands.hybrid_command(name="release")
    async def release(self, ctx, pokemon_id: int):
        uid = str(ctx.author.id)
        ud = get_pokemon_data(uid)
        pk = next((p for p in ud["pokemon"] if p["id"]==pokemon_id), None)
        if not pk:
            return await ctx.send(embed=discord.Embed(description=f"❌ No Pokémon with ID `{pokemon_id}`.", color=COLORS["error"]))
        if pk.get("favorite"):
            return await ctx.send(embed=discord.Embed(description="❌ Can't release a favourited Pokémon!", color=COLORS["error"]))
        shiny_txt = "✨ Shiny " if pk.get("shiny") else ""
        e = discord.Embed(
            title="⚠️ Release Pokémon?",
            description=f"Are you sure you want to release **{shiny_txt}{pk['species'].capitalize()}** (Lv.{pk.get('level',1)})?\n\n**This cannot be undone!**",
            color=COLORS["warn"]
        )
        e.set_thumbnail(url=pokemon_sprite_url(pk.get("pokedex_id", 0), pk.get("shiny", False)))
        view = discord.ui.View(timeout=30)
        confirm_btn = discord.ui.Button(label="✅ Yes, release", style=discord.ButtonStyle.danger)
        cancel_btn  = discord.ui.Button(label="❌ Cancel",       style=discord.ButtonStyle.secondary)
        async def confirm_cb(interaction: discord.Interaction):
            if interaction.user.id != ctx.author.id:
                return await interaction.response.send_message("Not your Pokémon!", ephemeral=True)
            ud2 = get_pokemon_data(uid)
            pk2 = next((p for p in ud2["pokemon"] if p["id"]==pokemon_id), None)
            if not pk2:
                return await interaction.response.edit_message(
                    embed=discord.Embed(description="❌ Pokémon already gone.", color=COLORS["error"]), view=None
                )
            ud2["pokemon"].remove(pk2)
            if ud2.get("selected", 0) >= len(ud2["pokemon"]): ud2["selected"] = 0
            save_pokemon_data(uid, ud2)
            for c in view.children: c.disabled = True
            await interaction.response.edit_message(
                embed=discord.Embed(description=f"🌿 **{shiny_txt}{pk2['species'].capitalize()}** was released. Goodbye!", color=COLORS["warn"]),
                view=view
            )
        async def cancel_cb(interaction: discord.Interaction):
            if interaction.user.id != ctx.author.id:
                return await interaction.response.send_message("Not your Pokémon!", ephemeral=True)
            for c in view.children: c.disabled = True
            await interaction.response.edit_message(
                embed=discord.Embed(description="✅ Release cancelled.", color=COLORS["success"]),
                view=view
            )
        confirm_btn.callback = confirm_cb
        cancel_btn.callback  = cancel_cb
        view.add_item(confirm_btn)
        view.add_item(cancel_btn)
        await ctx.send(embed=e, view=view)

    @commands.hybrid_command(name="sell")
    async def sell(self, ctx, pokemon_id: int):
        uid = str(ctx.author.id)
        ud = get_pokemon_data(uid)
        pk = next((p for p in ud["pokemon"] if p["id"]==pokemon_id), None)
        if not pk:
            return await ctx.send(embed=discord.Embed(description=f"❌ No Pokémon with ID `{pokemon_id}`.", color=COLORS["error"]))
        if pk.get("favorite"):
            return await ctx.send(embed=discord.Embed(description="❌ Can't sell a favourited Pokémon!", color=COLORS["error"]))
        price = calc_sell_price(pk)
        ivpct = iv_percentage(pk.get("ivs", roll_ivs()))
        await ctx.send(
            embed=discord.Embed(description=f"Sell **{pk['species'].capitalize()}** for **{price:,}** coins? (IV: {ivpct}%)", color=COLORS["warn"]),
            view=SellConfirmView(ctx, pk, price)
        )

    @commands.hybrid_command(name="pokefight", aliases=["pf"])
    async def pokefight(self, ctx):
        uid = str(ctx.author.id)
        ud = get_pokemon_data(uid)
        if not ud["pokemon"]:
            return await ctx.send(embed=discord.Embed(description="❌ You need Pokémon first! Use `,start`.", color=COLORS["error"]))
        med = ud.get("pokefight_medallions", 0)
        curr_waves = ud.get("pokefight_current_waves", 0)
        boss_ready = ud.get("pokefight_boss_ready", False)
        wave = curr_waves + 1
        party_ids = ud.get("party", [])
        if party_ids:
            party = [p for p in ud["pokemon"] if p["id"] in party_ids]
        else:
            idx = ud.get("selected", 0)
            if idx >= len(ud["pokemon"]): idx = 0
            party = [ud["pokemon"][idx]]
        party_pks = []
        for pk in party:
            ppk = dict(pk)
            hp = 80 + ppk.get("level",1)*2
            ppk["max_hp"] = hp
            ppk["hp"] = hp
            party_pks.append(ppk)
        if boss_ready:
            enemy_team = [generate_boss_pokemon(med)]
            n_enemies = 1
            is_boss = True
            reward = random.randint(300, 600) * (med+1)
        else:
            n_enemies = MEDALLION_ENEMY_POKEMON.get(med, 1)
            lvl_name = get_trainer_level_for_wave(med, wave)
            cfg = TRAINER_LEVELS[lvl_name]
            reward = random.randint(*cfg["reward_range"])
            enemy_team = [generate_enemy_pokemon(med, wave) for _ in range(n_enemies)]
            is_boss = False
        waves_needed = MEDALLION_WAVES_REQUIRED.get(med, 999)
        lead = party_pks[0]
        e1 = enemy_team[0]
        if is_boss:
            title = f"🔥 BOSS FIGHT! — {MEDALLION_NAMES.get(med,'Unranked')} Tier"
            color = COLORS["error"]
        else:
            title = f"{MEDALLION_EMOJIS.get(med,'⬛')} Wave {wave} — {MEDALLION_NAMES.get(med,'Unranked')}"
            color = COLORS["pokemon"]
        e = discord.Embed(title=title, color=color)
        e.description = (
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"**Your Pokémon:** {lead['species'].capitalize()} (Lv.{lead.get('level',1)})\n"
            f"HP: {build_hp_bar(lead['hp'], lead['max_hp'])}\n\n"
            f"**{'BOSS' if is_boss else 'Enemy'} 1/{n_enemies}:** {e1['species'].capitalize()} (Lv.{e1['level']})\n"
            f"HP: {build_hp_bar(e1['hp'], e1['max_hp'])}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{'🔥 BOSS! Win to earn next medallion!' if is_boss else f'🌊 Progress: **{curr_waves}/{waves_needed}** waves'}\n"
            f"💰 Reward: **~{apply_medal_boost(uid, reward)}** coins"
        )
        view = AIFightView(ctx, party_pks, enemy_team, med, wave, uid, reward, is_boss=is_boss)
        await ctx.send(embed=e, view=view)

    @commands.hybrid_command(name="mybadges")
    async def mybadges(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        ud = get_pokemon_data(str(member.id))
        med = ud.get("pokefight_medallions", 0)
        waves = ud.get("pokefight_current_waves", 0)
        total = ud.get("pokefight_total_waves", 0)
        boost = MEDALLION_COIN_BOOST.get(med, 1.0)
        e = discord.Embed(title=f"🎖️ {member.display_name}'s Medallions", color=COLORS["pokemon"])
        medals_txt = "\n".join(
            f"{MEDALLION_EMOJIS[i]} **{MEDALLION_NAMES[i]}** {'✅' if med >= i else '🔒'}"
            for i in range(1,6)
        )
        e.add_field(name="Medallions", value=medals_txt, inline=True)
        e.add_field(name="📊 Stats", value=(
            f"**Current:** {MEDALLION_EMOJIS.get(med,'⬛')} {MEDALLION_NAMES.get(med,'None')}\n"
            f"**Waves (this tier):** {waves}/{MEDALLION_WAVES_REQUIRED.get(med,999)}\n"
            f"**Total waves:** {total}\n"
            f"**Coin boost:** {boost}x"
        ), inline=True)
        boss_ready = ud.get("pokefight_boss_ready", False)
        if boss_ready:
            e.set_footer(text="🔥 BOSS FIGHT READY! Use ,pokefight!")
        elif med < 5:
            remaining = MEDALLION_WAVES_REQUIRED.get(med,999) - waves
            e.set_footer(text=f"Win {remaining} more waves for boss fight → {MEDALLION_NAMES.get(med+1,'Master')} Medallion!")
        else:
            e.set_footer(text="👑 Maximum medallion achieved!")
        await ctx.send(embed=e)

    @commands.hybrid_command(name="pdaily")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def pdaily(self, ctx):
        uid = str(ctx.author.id)
        ud = get_pokemon_data(uid)
        now = datetime.now(timezone.utc)
        last_str = ud.get("last_daily")
        if last_str:
            last = datetime.fromisoformat(last_str)
            diff = (now - last).total_seconds()
            if diff < 86400:
                remaining = 86400 - diff
                h, r = divmod(int(remaining), 3600)
                m2 = r // 60
                return await ctx.send(embed=discord.Embed(
                    description=f"⏰ Come back in **{h}h {m2}m** for your next daily!",
                    color=COLORS["warn"]
                ))
            if diff < 172800:
                ud["daily_streak"] = ud.get("daily_streak",0)+1
            else:
                ud["daily_streak"] = 1
        else:
            ud["daily_streak"] = 1
        streak = ud.get("daily_streak", 1)
        bonus = min(streak-1, 6) * 50
        total = apply_medal_boost(uid, DAILY_COINS + bonus)
        ud["balance"] = ud.get("balance",0) + total
        ud["last_daily"] = now.isoformat()
        save_pokemon_data(uid, ud)
        e = discord.Embed(title="🎁 Daily Reward!", color=COLORS["success"])
        e.add_field(name="💰 Coins",  value=f"**+{total:,}**",       inline=True)
        e.add_field(name="🔥 Streak", value=f"**{streak}** day(s)",  inline=True)
        e.add_field(name="💳 Balance",value=f"**{ud['balance']:,}**", inline=True)
        if bonus > 0:
            e.add_field(name="⭐ Streak Bonus", value=f"+{bonus} coins!", inline=False)
        mult = get_coin_multiplier(uid)
        if mult > 1.0:
            e.add_field(name="🎖️ Medallion Boost", value=f"**{mult}x** active!", inline=False)
        e.set_footer(text="Come back tomorrow to keep your streak!")
        await ctx.send(embed=e)

    @commands.hybrid_command(name="pbalance", aliases=["pbal"])
    async def pbalance(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        ud = get_pokemon_data(str(member.id))
        mult = get_coin_multiplier(str(member.id))
        med = ud.get("pokefight_medallions", 0)
        e = discord.Embed(title=f"💰 {member.display_name}'s Balance", color=COLORS["pokemon"])
        e.set_thumbnail(url=member.display_avatar)
        e.add_field(name="PokeCoins",      value=f"**{ud.get('balance',0):,}**",                               inline=True)
        e.add_field(name="Multiplier",     value=f"**{mult}x**",                                               inline=True)
        e.add_field(name="Medallion",      value=f"{MEDALLION_EMOJIS.get(med,'⬛')} **{MEDALLION_NAMES.get(med,'None')}**", inline=True)
        await ctx.send(embed=e)

    @commands.hybrid_command(name="myballs")
    async def myballs(self, ctx):
        uid = str(ctx.author.id)
        ud = get_pokemon_data(uid)
        balls = ud.get("pokeballs", {})
        e = discord.Embed(title="🎒 Your Pokéball Inventory", color=COLORS["pokemon"])
        total = sum(balls.values())
        for ball in POKEBALL_PRICES:
            count = balls.get(ball, 0)
            e.add_field(name=f"{POKEBALL_EMOJI[ball]} {ball.capitalize()}", value=f"**{count}** owned\n{int(POKEBALL_BASE_RATES[ball]*100)}% catch", inline=True)
        e.set_footer(text=f"Total: {total} balls | Buy at ,pokeshop")
        await ctx.send(embed=e)

    @commands.hybrid_command(name="plb")
    async def plb(self, ctx):
        data = load("pokemon")
        entries = [(uid, ud.get("balance",0)) for uid, ud in data.items() if ud.get("balance",0) > 0]
        entries.sort(key=lambda x: x[1], reverse=True)
        medals = ["🥇","🥈","🥉"]
        lines = []
        for i, (uid, bal) in enumerate(entries[:10], 1):
            try:
                user = self.bot.get_user(int(uid)) or await self.bot.fetch_user(int(uid))
                name = user.display_name
            except:
                name = f"User {uid}"
            med = medals[i-1] if i <= 3 else f"**{i}.**"
            lines.append(f"{med} {name} — **{bal:,}** coins")
        e = discord.Embed(title="🏆 PokeCoins Leaderboard", description="\n".join(lines) or "No data!", color=COLORS["pokemon"])
        user_pos = next((i+1 for i,(uid,_) in enumerate(entries) if uid==str(ctx.author.id)), None)
        if user_pos:
            e.set_footer(text=f"Your rank: #{user_pos}")
        await ctx.send(embed=e)

    @commands.hybrid_command(name="ptrade")
    async def ptrade(self, ctx, member: discord.Member, pokemon_id: int):
        if member.bot or member.id == ctx.author.id:
            return await ctx.send(embed=discord.Embed(description="❌ Invalid partner.", color=COLORS["error"]))
        uid = str(ctx.author.id)
        ud = get_pokemon_data(uid)
        pk = next((p for p in ud["pokemon"] if p["id"]==pokemon_id), None)
        if not pk:
            return await ctx.send(embed=discord.Embed(description=f"❌ No Pokémon with ID `{pokemon_id}`.", color=COLORS["error"]))
        if pk.get("favorite"):
            return await ctx.send(embed=discord.Embed(description="❌ Can't trade a favourited Pokémon!", color=COLORS["error"]))
        shiny = "✨ " if pk.get("shiny") else ""
        e = discord.Embed(
            title="🔄 Trade Request!",
            description=f"{ctx.author.mention} wants to trade {shiny}**{pk['species'].capitalize()}** with {member.mention}!\n\n{member.mention}, accept?",
            color=COLORS["warn"]
        )
        await ctx.send(embed=e, view=PokeTradeView(ctx.author, member, pk))

    @commands.hybrid_command(name="pbattle")
    async def pbattle(self, ctx, member: discord.Member):
        if member.bot or member.id == ctx.author.id:
            return await ctx.send(embed=discord.Embed(description="❌ Invalid opponent.", color=COLORS["error"]))
        uid1 = str(ctx.author.id); uid2 = str(member.id)
        ud1 = get_pokemon_data(uid1); ud2 = get_pokemon_data(uid2)
        if not ud1["pokemon"]:
            return await ctx.send(embed=discord.Embed(description="❌ You have no Pokémon!", color=COLORS["error"]))
        if not ud2["pokemon"]:
            return await ctx.send(embed=discord.Embed(description=f"❌ **{member.display_name}** has no Pokémon!", color=COLORS["error"]))
        def get_party(ud):
            pids = ud.get("party", [])
            if pids:
                party = [p for p in ud["pokemon"] if p["id"] in pids]
            else:
                idx = ud.get("selected", 0)
                if idx >= len(ud["pokemon"]): idx = 0
                party = [ud["pokemon"][idx]]
            result = []
            for pk in party:
                ppk = dict(pk)
                hp = 80 + ppk.get("level",1)*2
                ppk["max_hp"] = hp; ppk["hp"] = hp
                result.append(ppk)
            return result
        pks1 = get_party(ud1); pks2 = get_party(ud2)
        e = discord.Embed(
            title="⚔️ Battle Challenge!",
            description=(
                f"{ctx.author.mention} challenges {member.mention}!\n\n"
                f"🔴 **{pks1[0]['species'].capitalize()}** vs 🔵 **{pks2[0]['species'].capitalize()}**\n\n"
                f"{member.mention}, do you accept?"
            ),
            color=COLORS["pokemon"]
        )
        await ctx.send(embed=e, view=BattleChallengeView(ctx.author, member, pks1, pks2))

    @commands.hybrid_command(name="pokeshop")
    async def pokeshop(self, ctx):
        uid = str(ctx.author.id)
        view = PokeshopDropdownView(ctx.author)
        await ctx.send(embed=pokeshop_ball_embed(uid), view=view)

    @commands.hybrid_command(name="mycards")
    async def mycards(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        cd = get_cards_data(str(member.id))
        cards = cd.get("cards", [])
        if not cards:
            return await ctx.send(embed=discord.Embed(
                description=f"**{member.display_name}** has no cards!\nBuy packs from `,pokeshop` then open with `/openpack`.",
                color=COLORS["info"]
            ))
        view = CardListView(member, cards)
        await ctx.send(embed=view.build_embed(), view=view)

    @commands.hybrid_command(name="viewcard")
    async def viewcard(self, ctx, card_id: int):
        cd = get_cards_data(str(ctx.author.id))
        card = next((c for c in cd.get("cards",[]) if c["id"]==card_id), None)
        if not card:
            return await ctx.send(embed=discord.Embed(description=f"❌ No card with ID `{card_id}`.", color=COLORS["error"]))
        ti = CARD_TIERS.get(card["tier"], CARD_TIERS["Common"])
        ptype = card.get("type","normal")
        e = discord.Embed(title=f"{ti['emoji']} {card['species']} — {card['tier']}", color=ti["color"])
        e.set_thumbnail(url=pokemon_official_art(card["pokemon_id"]))
        e.add_field(name="Pokédex #",  value=f"**#{card['pokemon_id']}**", inline=True)
        e.add_field(name="Type",       value=f"{TYPE_EMOJI.get(ptype,'⭐')} {ptype.capitalize()}", inline=True)
        e.add_field(name="Tier",       value=f"{ti['emoji']} **{card['tier']}**", inline=True)
        e.add_field(name="Card ID",    value=f"`#{card_id}`",                     inline=True)
        e.add_field(name="Sell Value", value=f"**{ti['sell']:,}** coins",         inline=True)
        e.add_field(name="Obtained",   value=card.get("obtained_at","?"),         inline=True)
        if card["tier"] == "Godly":
            e.set_footer(text="🌟 GODLY CARD — Owner-gifted rarity!")
        await ctx.send(embed=e)

    @commands.hybrid_command(name="market")
    async def market(self, ctx, pokemon_id: int, price: int):
        if price < 1 or price > 10_000_000:
            return await ctx.send(embed=discord.Embed(description="❌ Price must be 1–10,000,000.", color=COLORS["error"]))
        uid = str(ctx.author.id)
        ud = get_pokemon_data(uid)
        pk = next((p for p in ud["pokemon"] if p["id"]==pokemon_id), None)
        if not pk:
            return await ctx.send(embed=discord.Embed(description=f"❌ No Pokémon with ID `{pokemon_id}`.", color=COLORS["error"]))
        if pk.get("favorite"):
            return await ctx.send(embed=discord.Embed(description="❌ Can't list a favourited Pokémon!", color=COLORS["error"]))
        mdata = load("marketplace")
        if sum(1 for v in mdata.values() if v.get("seller_id")==uid) >= 10:
            return await ctx.send(embed=discord.Embed(description="❌ Max 10 active listings!", color=COLORS["error"]))
        ud["pokemon"].remove(pk)
        if ud.get("selected",0) >= len(ud["pokemon"]): ud["selected"] = 0
        save_pokemon_data(uid, ud)
        lid = ''.join(random.choices(string.ascii_lowercase+string.digits, k=12))
        mdata[lid] = {"seller_id":uid,"seller_name":ctx.author.display_name,"pokemon":pk,"price":price,"listed_at":datetime.now(timezone.utc).isoformat()}
        save("marketplace", mdata)
        await ctx.send(embed=discord.Embed(
            description=f"✅ **{pk['species'].capitalize()}** listed for **{price:,}** coins!\nID: `{lid[:8]}` | Use `,unlist {lid[:8]}` to remove.",
            color=COLORS["success"]
        ))

    @commands.hybrid_command(name="marketplace", aliases=["mp"])
    async def marketplace_cmd(self, ctx):
        mdata = load("marketplace")
        listings = list(mdata.items())
        if not listings:
            return await ctx.send(embed=discord.Embed(description="No listings! Use `,market <id> <price>`.", color=COLORS["info"]))
        view = MarketplaceBrowseView(ctx.author, listings)
        view.rebuild()
        await ctx.send(embed=view._embed(), view=view)

    @commands.hybrid_command(name="mylistings")
    async def mylistings(self, ctx):
        uid = str(ctx.author.id)
        mdata = load("marketplace")
        mine = [(lid,v) for lid,v in mdata.items() if v.get("seller_id")==uid]
        if not mine:
            return await ctx.send(embed=discord.Embed(description="You have no active listings.", color=COLORS["info"]))
        e = discord.Embed(title="📋 Your Listings", color=COLORS["pokemon"])
        for lid, listing in mine[:10]:
            pk = listing["pokemon"]
            ivpct = iv_percentage(pk.get("ivs", roll_ivs()))
            e.add_field(
                name=f"{'✨ ' if pk.get('shiny') else ''}{pk['species'].capitalize()} — {listing['price']:,} coins",
                value=f"Lv.**{pk.get('level',1)}** | IV: **{ivpct}%**\nID: `{lid[:8]}`",
                inline=False
            )
        e.set_footer(text=f"{len(mine)} listing(s) | ,unlist <id> to remove")
        await ctx.send(embed=e)

    @commands.hybrid_command(name="unlist")
    async def unlist(self, ctx, listing_id: str):
        uid = str(ctx.author.id)
        mdata = load("marketplace")
        match = next((k for k in mdata if k.startswith(listing_id) and mdata[k].get("seller_id")==uid), None)
        if not match:
            return await ctx.send(embed=discord.Embed(description="❌ Listing not found or not yours.", color=COLORS["error"]))
        listing = mdata.pop(match)
        save("marketplace", mdata)
        pk = listing["pokemon"]
        ud = get_pokemon_data(uid)
        pk["id"] = ud["next_id"]; ud["next_id"] += 1
        ud["pokemon"].append(pk)
        save_pokemon_data(uid, ud)
        await ctx.send(embed=discord.Embed(description=f"✅ **{pk['species'].capitalize()}** returned to your collection!", color=COLORS["success"]))

# ════════════════════════════════════════════════════════════
#  COG — ANTI NUKE (snapshot-based)
# ════════════════════════════════════════════════════════════
class AntiNukeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.snapshots: dict = {}
        self.deletion_times: dict = {}
        self.recently_banned: dict = {}

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            await self._take_snapshot(guild)

    async def _take_snapshot(self, guild: discord.Guild):
        try:
            icon_bytes = await guild.icon.read() if guild.icon else None
        except Exception:
            icon_bytes = None
        channels_data = []
        for ch in guild.channels:
            cd = {"name": ch.name, "type": str(ch.type), "position": ch.position}
            if isinstance(ch, discord.TextChannel):
                cd["topic"] = ch.topic
                cd["slowmode"] = ch.slowmode_delay
            channels_data.append(cd)
        emoji_data = []
        try:
            for em in guild.emojis:
                try:
                    em_bytes = await em.read()
                    emoji_data.append({"name": em.name, "bytes": list(em_bytes), "animated": em.animated})
                except Exception:
                    pass
        except Exception:
            pass
        self.snapshots[guild.id] = {
            "name": guild.name,
            "icon_bytes": icon_bytes,
            "channels": channels_data,
            "emojis": emoji_data,
            "banned_by_nuke": [],
        }

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        guild = channel.guild
        settings = load("server_settings")
        gs = gdata(settings, guild.id)
        if not gs.get("antinuke_enabled"):
            return
        now = time.time()
        gid = guild.id
        if gid not in self.deletion_times:
            self.deletion_times[gid] = []
        self.deletion_times[gid].append({"channel": channel, "time": now})
        self.deletion_times[gid] = [e for e in self.deletion_times[gid] if now - e["time"] < 6]
        if len(self.deletion_times[gid]) >= 3:
            entries_copy = list(self.deletion_times[gid])
            self.deletion_times[gid] = []
            asyncio.create_task(self._handle_nuke(guild, entries_copy))

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        settings = load("server_settings")
        gs = gdata(settings, guild.id)
        if not gs.get("antinuke_enabled"):
            return
        if user.id == OWNER_ID or user.id == self.bot.user.id:
            return
        now = time.time()
        gid = guild.id
        if gid not in self.recently_banned:
            self.recently_banned[gid] = []
        self.recently_banned[gid].append({"user_id": user.id, "time": now})

    async def _handle_nuke(self, guild: discord.Guild, deleted: list):
        snap = self.snapshots.get(guild.id, {})
        notify_ch = None
        # Try to find and ban the nuking bot
        nuke_bot_found = None
        try:
            async for entry in guild.audit_logs(limit=10, action=discord.AuditLogAction.channel_delete):
                if time.time() - entry.created_at.timestamp() < 10:
                    if entry.user and entry.user.bot and entry.user.id != self.bot.user.id:
                        nuke_bot_found = entry.user
                        break
        except Exception:
            pass
        if nuke_bot_found:
            try:
                await guild.ban(nuke_bot_found, reason="⚠️ AntiNuke: Mass channel deletion")
            except Exception:
                pass
            # Unban only users this bot banned in last 30 seconds
            recently = [e for e in self.recently_banned.get(guild.id,[]) if time.time()-e["time"] < 30]
            for entry in recently:
                try:
                    user = await self.bot.fetch_user(entry["user_id"])
                    await guild.unban(user, reason="AntiNuke: reversing nuke bot bans")
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
        # Restore channels from snapshot
        restored = 0
        for chdata in snap.get("channels", []):
            try:
                if chdata["type"] == "text":
                    new_ch = await guild.create_text_channel(
                        name=chdata["name"],
                        topic=chdata.get("topic"),
                        slowmode_delay=chdata.get("slowmode",0),
                        reason="AntiNuke restore"
                    )
                    if not notify_ch:
                        notify_ch = new_ch
                elif chdata["type"] == "voice":
                    await guild.create_voice_channel(name=chdata["name"], reason="AntiNuke restore")
                elif chdata["type"] == "category":
                    await guild.create_category(name=chdata["name"], reason="AntiNuke restore")
                restored += 1
                await asyncio.sleep(0.5)
            except Exception:
                pass
        # Restore server name
        if snap.get("name") and guild.name != snap["name"]:
            try:
                await guild.edit(name=snap["name"], reason="AntiNuke restore")
            except Exception:
                pass
        # Restore server icon
        if snap.get("icon_bytes"):
            try:
                await guild.edit(icon=bytes(snap["icon_bytes"]) if isinstance(snap["icon_bytes"], list) else snap["icon_bytes"], reason="AntiNuke restore")
            except Exception:
                pass
        # Re-take snapshot after restore
        await asyncio.sleep(2)
        await self._take_snapshot(guild)
        if notify_ch:
            try:
                nuke_txt = f"🤖 **{nuke_bot_found}** has been banned!" if nuke_bot_found else "⚠️ Mass deletion detected!"
                await notify_ch.send(embed=discord.Embed(
                    title="⚠️ AntiNuke Activated!",
                    description=f"{nuke_txt}\n🔧 Restored **{restored}** channel(s).\n🔄 Server name & icon restored.",
                    color=COLORS["error"]
                ))
            except Exception:
                pass

    @commands.hybrid_command(name="antinuke")
    async def antinuke(self, ctx, toggle: str):
        if not is_staff(ctx.author):
            return await ctx.send(embed=discord.Embed(description="❌ Staff only!", color=COLORS["error"]))
        settings = load("server_settings")
        gs = gdata(settings, ctx.guild.id)
        if toggle.lower() in ("on","enable","true"):
            gs["antinuke_enabled"] = True
            save("server_settings", settings)
            await self._take_snapshot(ctx.guild)
            await ctx.send(embed=discord.Embed(description="✅ Anti-nuke **enabled** and server snapshot taken!", color=COLORS["success"]))
        elif toggle.lower() in ("off","disable","false"):
            gs["antinuke_enabled"] = False
            save("server_settings", settings)
            await ctx.send(embed=discord.Embed(description="✅ Anti-nuke **disabled**.", color=COLORS["warn"]))
        else:
            await ctx.send(embed=discord.Embed(description="❌ Use `on` or `off`.", color=COLORS["error"]))

# ════════════════════════════════════════════════════════════
#  BOT SETUP
# ════════════════════════════════════════════════════════════
intents = discord.Intents.all()
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)
bot._start_time = None
bot.snipe_data = {}
bot.editsnipe_data = {}

# Load saved whitelist/pokemonlist on startup
def load_lists():
    wl = load("whitelist")
    if isinstance(wl, list):
        WHITELIST_USERS.update(wl)
    pl = load("pokemonlist")
    if isinstance(pl, list):
        POKEMONLIST_USERS.update(pl)

# ── GLOBAL BOT LOCK CHECK ──────────────────────────────────
@bot.check
async def global_bot_lock(ctx):
    global BOT_LOCKED
    if not BOT_LOCKED:
        return True
    if ctx.author.id == OWNER_ID:
        return True
    if ctx.author.id in WHITELIST_USERS:
        return True
    cmd_name = ctx.command.name if ctx.command else ""
    if cmd_name in EXEMPT_COMMANDS:
        return True
    if ctx.author.id in POKEMONLIST_USERS and cmd_name in POKEMON_COMMANDS:
        return True
    await ctx.send(
        embed=discord.Embed(
            description="🔒 The bot is currently **locked**. Only the owner can use commands.",
            color=COLORS["error"]
        ),
        delete_after=6
    )
    return False

# ── PREFIX LOCKBOT ─────────────────────────────────────────
@bot.command(name="lockbot")
async def lockbot_prefix(ctx):
    global BOT_LOCKED
    if not is_owner(ctx.author):
        return await ctx.send(embed=discord.Embed(description="❌ Owner only!", color=COLORS["error"]))
    BOT_LOCKED = not BOT_LOCKED
    if BOT_LOCKED:
        e = discord.Embed(title="🔒 Bot Locked", description="Only you and whitelisted users can use commands.", color=COLORS["error"])
    else:
        e = discord.Embed(title="🔓 Bot Unlocked", description="All users can use commands again.", color=COLORS["success"])
    await ctx.send(embed=e)

# ── HELP COMMAND ───────────────────────────────────────────
@bot.command(name="help")
async def help_cmd(ctx):
    await ctx.send(embed=build_help_home(), view=HelpView())

# ════════════════════════════════════════════════════════════
#  EVENTS
# ════════════════════════════════════════════════════════════
@bot.event
async def on_ready():
    bot._start_time = datetime.now(timezone.utc)
    load_lists()
    print(f"✅ {BOT_NAME} is online as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"⚡ Synced {len(synced)} slash command(s)")
    except Exception as ex:
        print(f"Sync error: {ex}")

@bot.event
async def on_guild_join(guild: discord.Guild):
    owner = bot.get_user(OWNER_ID)
    if not owner:
        try:
            owner = await bot.fetch_user(OWNER_ID)
        except Exception:
            return
    # Try to get an invite
    invite_url = "No invite available"
    try:
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).create_instant_invite:
                inv = await ch.create_invite(max_age=86400, max_uses=1)
                invite_url = inv.url
                break
    except Exception:
        pass
    e = discord.Embed(
        title=f"🔔 New Server: **{guild.name}**",
        color=COLORS["pokemon"]
    )
    e.add_field(name="🆔 Server ID",   value=f"`{guild.id}`",                     inline=True)
    e.add_field(name="👥 Members",     value=f"**{guild.member_count}**",          inline=True)
    e.add_field(name="👑 Owner",       value=f"{guild.owner} (`{guild.owner_id}`)", inline=False)
    e.add_field(name="🔗 Invite",      value=invite_url,                           inline=False)
    e.set_thumbnail(url=guild.icon.url if guild.icon else None)
    e.set_footer(text="Approve to join and receive PolarBear role | Decline to leave")
    view = GuildApprovalView(guild, invite_url)
    try:
        await owner.send(embed=e, view=view)
    except Exception:
        pass

@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot: return
    bot.snipe_data[message.channel.id] = {
        "content": message.content or "[No text]",
        "author": str(message.author),
        "avatar": str(message.author.display_avatar),
    }

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot: return
    bot.editsnipe_data[before.channel.id] = {
        "before": before.content or "[No text]",
        "after": after.content or "[No text]",
        "author": str(before.author),
        "avatar": str(before.author.display_avatar),
    }

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot: return
    # AFK check
    afk_data = load("afk")
    uid = str(message.author.id)
    if uid in afk_data:
        del afk_data[uid]
        save("afk", afk_data)
        try:
            await message.channel.send(
                embed=discord.Embed(description=f"👋 Welcome back {message.author.mention}! AFK removed.", color=COLORS["info"]),
                delete_after=5
            )
        except Exception:
            pass
    # Notify for AFK mentions
    for mentioned in message.mentions:
        mid = str(mentioned.id)
        if mid in afk_data:
            await message.channel.send(
                embed=discord.Embed(description=f"💤 **{mentioned.display_name}** is AFK: {afk_data[mid]['reason']}", color=COLORS["info"]),
                delete_after=8
            )
    # Auto-responses
    autoresponses = load("autoresponses")
    gs = gdata(autoresponses, message.guild.id) if message.guild else {}
    for trigger, response in gs.items():
        if trigger in message.content.lower():
            await message.channel.send(response)
            break
    # Sticky messages
    sticky_data = load("sticky")
    if message.guild:
        gs2 = gdata(sticky_data, message.guild.id)
        sticky_msg = gs2.get(str(message.channel.id))
        if sticky_msg:
            async for msg in message.channel.history(limit=3):
                if msg.author == bot.user and sticky_msg in (msg.embeds[0].description if msg.embeds else ""):
                    try: await msg.delete()
                    except Exception: pass
                    break
            await message.channel.send(embed=discord.Embed(description=f"📌 {sticky_msg}", color=COLORS["info"]))
    # Hangman
    games_cog = bot.cogs.get("Games")
    if games_cog and message.channel.id in games_cog.hangman_games:
        game = games_cog.hangman_games[message.channel.id]
        if message.author.id == game["player"] and len(message.content) == 1 and message.content.isalpha():
            letter = message.content.lower()
            word = game["word"]
            guessed = game["guessed"]
            lives = game["lives"]
            stages = ["😵","😦","😟","😕","😐","🙂","😀"]
            if letter not in guessed:
                guessed.add(letter)
                if letter not in word:
                    lives[0] -= 1
            display = " ".join(l if l in guessed else r"\_" for l in word)
            if all(l in guessed for l in word):
                del games_cog.hangman_games[message.channel.id]
                await message.channel.send(embed=discord.Embed(title="🎉 You won Hangman!", description=f"The word was **{word}**!", color=COLORS["success"]))
            elif lives[0] <= 0:
                del games_cog.hangman_games[message.channel.id]
                await message.channel.send(embed=discord.Embed(title="💀 Game Over!", description=f"The word was **{word}**.", color=COLORS["error"]))
            else:
                e = discord.Embed(title="🔤 Hangman", description=f"{stages[lives[0]]} Lives: **{lives[0]}**\n\n`{display}`\n\nGuessed: {', '.join(sorted(guessed))}", color=COLORS["info"])
                await game["msg"].edit(embed=e)
    # Word chain
    if games_cog and message.channel.id in games_cog.wordchain:
        wc = games_cog.wordchain[message.channel.id]
        if wc.get("active"):
            word = message.content.lower().strip()
            if word.isalpha():
                last = wc["last"]
                if last and word[0] != last[-1]:
                    await message.channel.send(embed=discord.Embed(description=f"❌ Must start with `{last[-1]}`!", color=COLORS["error"]))
                elif word in wc["used"]:
                    await message.channel.send(embed=discord.Embed(description=f"❌ `{word}` already used!", color=COLORS["error"]))
                else:
                    wc["last"] = word
                    wc["used"].add(word)
                    await message.add_reaction("✅")
    await bot.process_commands(message)

async def setup():
    await bot.add_cog(Moderation(bot))
    await bot.add_cog(AdminCog(bot))
    await bot.add_cog(Giveaways(bot))
    await bot.add_cog(Fun(bot))
    await bot.add_cog(Games(bot))
    await bot.add_cog(Pokemon(bot))
    await bot.add_cog(AntiNukeCog(bot))

async def main():
    async with bot:
        await setup()
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
tx = await self.get_context(message)
            if ctx.command and ctx.command.name not in EXEMPT_COMMANDS:
                return
        await self.process_commands(message)

    async def on_message_delete(self, message: discord.Message):
        if message.author.bot or not message.content: return
        self.snipe_data[message.channel.id] = {
            "content": message.content[:2000],
            "author":  str(message.author),
            "avatar":  str(message.author.display_avatar.url),
            "time":    discord.utils.utcnow().isoformat()
        }

    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.author.bot or before.content == after.content: return
        self.editsnipe_data[before.channel.id] = {
            "before": before.content[:1024],
            "after":  after.content[:1024],
            "author": str(before.author),
            "avatar": str(before.author.display_avatar.url)
        }

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(embed=discord.Embed(description=f"⏱️ Slow down! Try again in **{error.retry_after:.1f}s**.", color=COLORS["warn"]), delete_after=5)
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send(embed=discord.Embed(description="❌ You don't have permission to do that.", color=COLORS["error"]), delete_after=5)
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send(embed=discord.Embed(description="❌ Member not found.", color=COLORS["error"]), delete_after=5)
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(embed=discord.Embed(description=f"❌ Missing argument: `{error.param.name}`. Use `,help` for usage.", color=COLORS["error"]), delete_after=8)
        elif isinstance(error, commands.CheckFailure):
            pass
        else:
            print(f"[Error] {ctx.command}: {error}")

bot = PolarBot()
bot.run(TOKEN)
