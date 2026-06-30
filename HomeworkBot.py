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
     except: pass
        try:
            await message.author.send(embed=make_embed(title="🚫 You Triggered AutoMod",
                description=f"**Server:** {message.guild.name}\n\nYour message was removed for a **banned word**.\n🔇 Timed out **10 minutes** · 🏷️ Blacklisted role assigned",
                color=COLORS["error"]))
        except: pass

# ================================================================
#  GAMES COG
# ================================================================
RPS_EMOJIS = {"rock":"🪨","paper":"📄","scissors":"✂️"}
RPS_BEATS  = {"rock":"scissors","paper":"rock","scissors":"paper"}

class RPSView(ui.View):
    def __init__(self, author):
        super().__init__(timeout=30); self.author = author
    async def interaction_check(self, interaction):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("❌ This isn't your game!", ephemeral=True); return False
        return True
    async def _play(self, interaction, choice):
        bc = random.choice(["rock","paper","scissors"]); pe,be = RPS_EMOJIS[choice],RPS_EMOJIS[bc]
        if choice==bc: result,color="🤝 It's a Tie!",COLORS["info"]
        elif RPS_BEATS[choice]==bc: result,color="🎉 You Win!",COLORS["success"]
        else: result,color="😈 Bot Wins!",COLORS["error"]
        e = discord.Embed(title="🪨📄✂️ Rock Paper Scissors",
            description=f"**You:** {pe} `{choice.capitalize()}`\n**Bot:** {be} `{bc.capitalize()}`\n\n**{result}**",color=color)
        for child in self.children: child.disabled=True
        await interaction.response.edit_message(embed=e,view=self); self.stop()
    @ui.button(label="🪨 Rock",style=discord.ButtonStyle.secondary)
    async def rock(self,i,b): await self._play(i,"rock")
    @ui.button(label="📄 Paper",style=discord.ButtonStyle.secondary)
    async def paper(self,i,b): await self._play(i,"paper")
    @ui.button(label="✂️ Scissors",style=discord.ButtonStyle.secondary)
    async def scissors(self,i,b): await self._play(i,"scissors")

class TTTButton(ui.Button):
    def __init__(self,row,col):
        super().__init__(style=discord.ButtonStyle.secondary,label="\u200b",row=row)
        self.row_idx=row; self.col_idx=col
    async def callback(self,interaction):
        view=self.view
        if interaction.user!=view.current_player():
            await interaction.response.send_message("❌ Not your turn!",ephemeral=True); return
        if self.label!="\u200b":
            await interaction.response.send_message("❌ Taken!",ephemeral=True); return
        symbol="❌" if view.turn==0 else "⭕"
        self.label=symbol; self.style=discord.ButtonStyle.danger if view.turn==0 else discord.ButtonStyle.primary
        self.disabled=True; view.board[self.row_idx][self.col_idx]=symbol
        if view.check_winner():
            view.disable_all()
            await interaction.response.edit_message(embed=make_embed(title="🎮 Tic-Tac-Toe",
                description=f"🏆 **{view.current_player().display_name}** wins!",color=COLORS["success"]),view=view)
            view.stop(); return
        if all(view.board[r][c]!="" for r in range(3) for c in range(3)):
            view.disable_all()
            await interaction.response.edit_message(embed=make_embed(title="🎮 Tic-Tac-Toe",description="🤝 Draw!",color=COLORS["info"]),view=view)
            view.stop(); return
        view.turn^=1; nxt=view.current_player(); sym="❌" if view.turn==0 else "⭕"
        await interaction.response.edit_message(embed=make_embed(title="🎮 Tic-Tac-Toe",
            description=f"**{nxt.display_name}'s turn** — {sym}",color=COLORS["primary"]),view=view)

class TTTView(ui.View):
    def __init__(self,p1,p2):
        super().__init__(timeout=120); self.players=[p1,p2]; self.turn=0
        self.board=[["","",""] for _ in range(3)]
        for r in range(3):
            for c in range(3): self.add_item(TTTButton(r,c))
    def current_player(self): return self.players[self.turn]
    def check_winner(self):
        b=self.board
        lines=[*b,*[[b[r][c] for r in range(3)] for c in range(3)],[b[0][0],b[1][1],b[2][2]],[b[0][2],b[1][1],b[2][0]]]
        for ln in lines:
            if ln[0] and ln[0]==ln[1]==ln[2]: return ln[0]
        return None
    def disable_all(self):
        for child in self.children: child.disabled=True

C4_ROWS,C4_COLS=6,7; C4_EMPTY="⬛"; C4_TOKENS=["🔴","🟡"]

class Connect4View(ui.View):
    def __init__(self,p1,p2):
        super().__init__(timeout=180); self.players=[p1,p2]; self.turn=0
        self.board=[[C4_EMPTY]*C4_COLS for _ in range(C4_ROWS)]
        for col in range(C4_COLS):
            btn=ui.Button(label=str(col+1),style=discord.ButtonStyle.primary,row=0 if col<4 else 1)
            btn.callback=self._make_callback(col); self.add_item(btn)
    def _make_callback(self,col):
        async def callback(interaction):
            if interaction.user!=self.players[self.turn]:
                await interaction.response.send_message("❌ Not your turn!",ephemeral=True); return
            row=self._drop(col)
            if row is None:
                await interaction.response.send_message("❌ Column full!",ephemeral=True); return
            self.board[row][col]=C4_TOKENS[self.turn]; wt=self._check_win(row,col); bs=self._render()
            if wt:
                for child in self.children: child.disabled=True
                await interaction.response.edit_message(embed=make_embed(title="🔴🟡 Connect 4",
                    description=f"{bs}\n\n🏆 **{self.players[self.turn].display_name}** wins!",color=COLORS["success"]),view=self)
                self.stop(); return
            if all(self.board[0][c]!=C4_EMPTY for c in range(C4_COLS)):
                for child in self.children: child.disabled=True
                await interaction.response.edit_message(embed=make_embed(title="🔴🟡 Connect 4",
                    description=f"{bs}\n\n🤝 Draw!",color=COLORS["info"]),view=self); self.stop(); return
            self.turn^=1
            await interaction.response.edit_message(embed=make_embed(title="🔴🟡 Connect 4",
                description=f"{bs}\n\n{C4_TOKENS[self.turn]} **{self.players[self.turn].display_name}'s turn**",
                color=COLORS["primary"]),view=self)
        return callback
    def _drop(self,col):
        for row in range(C4_ROWS-1,-1,-1):
            if self.board[row][col]==C4_EMPTY: return row
        return None
    def _render(self):
        return "".join(f"`{c+1}`" for c in range(C4_COLS))+"\n"+"\n".join("".join(self.board[r]) for r in range(C4_ROWS))
    def _check_win(self,r,c):
        token=self.board[r][c]
        def count(dr,dc):
            n,nr,nc=0,r+dr,c+dc
            while 0<=nr<C4_ROWS and 0<=nc<C4_COLS and self.board[nr][nc]==token: n+=1;nr+=dr;nc+=dc
            return n
        for dr,dc in [(0,1),(1,0),(1,1),(1,-1)]:
            if count(dr,dc)+count(-dr,-dc)>=3: return token
        return None

class WordChainGame:
    def __init__(self,cid): self.channel_id=cid; self.players=[]; self.used_words=set(); self.current_word=None; self.turn_idx=0; self.active=False
    def current_player(self): return self.players[self.turn_idx%len(self.players)]
    def next_turn(self): self.turn_idx+=1

class WordChainJoinView(ui.View):
    def __init__(self,game,starter):
        super().__init__(timeout=30); game.players.append(starter); self._game=game
    @ui.button(label="✋ Join Game",style=discord.ButtonStyle.success)
    async def join(self,interaction,button):
        if interaction.user in self._game.players:
            await interaction.response.send_message("✅ Already joined!",ephemeral=True); return
        self._game.players.append(interaction.user)
        await interaction.response.send_message(f"✅ **{interaction.user.display_name}** joined! ({len(self._game.players)} players)")

class Games(commands.Cog):
    def __init__(self,bot): self.bot=bot; self.wordchain_games={}; self.active_games=set()

    @commands.hybrid_command(name="rps",description="🪨📄✂️ Play Rock Paper Scissors!")
    @commands.cooldown(1,5,commands.BucketType.user)
    async def rps(self,ctx):
        view=RPSView(ctx.author)
        await ctx.send(embed=make_embed(title="🪨📄✂️ Rock Paper Scissors",
            description=f"**{ctx.author.display_name}** — Choose! *(30s)*",
            color=COLORS["primary"],thumbnail=str(ctx.author.display_avatar.url)),view=view)

    @commands.hybrid_command(name="trivia",description="🧠 Answer a random trivia question!")
    @commands.cooldown(1,10,commands.BucketType.user)
    async def trivia(self,ctx):
        await ctx.defer()
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get("https://opentdb.com/api.php?amount=1&type=multiple",
                    timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    data=await resp.json(content_type=None)
            except:
                await ctx.send(embed=make_embed(description="❌ Trivia API unavailable. Try again!",color=COLORS["error"])); return
        if data.get("response_code",1)!=0 or not data.get("results"):
            await ctx.send(embed=make_embed(description="⚠️ API rate limited. Try in a few seconds!",color=COLORS["warning"])); return
        q=data["results"][0]; question=html.unescape(q["question"]); correct=html.unescape(q["correct_answer"])
        incorrect=[html.unescape(a) for a in q["incorrect_answers"]]; answers=incorrect+[correct]; random.shuffle(answers)
        letters=["🅰️","🅱️","🇨","🇩"]; letter_map={"A":"🅰️","B":"🅱️","C":"🇨","D":"🇩"}
        opts="\n".join(f"{letters[i]} {answers[i]}" for i in range(len(answers)))
        diff_colors={"easy":COLORS["success"],"medium":COLORS["warning"],"hard":COLORS["error"]}
        await ctx.send(embed=discord.Embed(title=f"🧠 Trivia — {q['category']}",
            description=f"**{question}**\n\n{opts}\n\n*20 seconds to answer!*",
            color=diff_colors.get(q["difficulty"],COLORS["primary"])).set_footer(text="Type A, B, C or D"))
        valid={letters[i]:answers[i] for i in range(len(answers))}
        def check(m): return m.author==ctx.author and m.channel==ctx.channel and m.content.upper().strip() in ["🅰️","🅱️","🇨","🇩","A","B","C","D"]
        try:
            reply=await self.bot.wait_for("message",check=check,timeout=20); ans=reply.content.upper().strip()
            if ans in letter_map: ans=letter_map[ans]
            chosen=valid.get(ans,"")
            if chosen==correct: result_e=make_embed(title="✅ Correct!",description=f"🎉 **{ctx.author.display_name}** got it!\n**Answer:** {correct}",color=COLORS["success"])
            else: result_e=make_embed(title="❌ Wrong!",description=f"Chose `{chosen or ans}`\n**Correct:** {correct}",color=COLORS["error"])
        except asyncio.TimeoutError:
            result_e=make_embed(title="⏰ Time's Up!",description=f"**Correct Answer:** {correct}",color=COLORS["warning"])
        await ctx.send(embed=result_e)

    @commands.hybrid_command(name="tictactoe",description="❌⭕ Challenge someone to Tic-Tac-Toe!")
    @app_commands.describe(opponent="Your opponent")
    @commands.cooldown(1,10,commands.BucketType.channel)
    async def tictactoe(self,ctx,opponent:discord.Member):
        if opponent.bot or opponent==ctx.author:
            await ctx.send(embed=make_embed(description="❌ Invalid opponent!",color=COLORS["error"])); return
        await ctx.send(embed=make_embed(title="🎮 Tic-Tac-Toe",
            description=f"❌ **{ctx.author.display_name}** vs ⭕ **{opponent.display_name}**\n\n**{ctx.author.display_name}'s turn** — ❌",
            color=COLORS["primary"]),view=TTTView(ctx.author,opponent))

    @commands.hybrid_command(name="connect4",description="🔴🟡 Challenge someone to Connect 4!")
    @app_commands.describe(opponent="Your opponent")
    @commands.cooldown(1,10,commands.BucketType.channel)
    async def connect4(self,ctx,opponent:discord.Member):
        if opponent.bot or opponent==ctx.author:
            await ctx.send(embed=make_embed(description="❌ Invalid opponent!",color=COLORS["error"])); return
        game=Connect4View(ctx.author,opponent)
        await ctx.send(embed=make_embed(title="🔴🟡 Connect 4",
            description=f"🔴 **{ctx.author.display_name}** vs 🟡 **{opponent.display_name}**\n\n{game._render()}\n\n🔴 **{ctx.author.display_name}'s turn**\n*Row 1 = cols 1–4 · Row 2 = cols 5–7*",
            color=COLORS["primary"]),view=game)

    @commands.hybrid_command(name="wordchain",description="🔤 Start a multiplayer word chain!")
    @commands.cooldown(1,10,commands.BucketType.channel)
    async def wordchain(self,ctx):
        cid=ctx.channel.id
        if cid in self.wordchain_games and self.wordchain_games[cid].active:
            await ctx.send(embed=make_embed(description="❌ A game is already running!",color=COLORS["error"])); return
        game=WordChainGame(cid); self.wordchain_games[cid]=game
        join_view=WordChainJoinView(game,ctx.author)
        await ctx.send(embed=make_embed(title="🔤 Word Chain — Join Phase",
            description=f"Each player says a word starting with the **last letter** of the previous word!\n\n**{ctx.author.display_name}** joined!\n\nClick **Join Game**! Starts in **30 seconds**.",
            color=COLORS["purple"]),view=join_view)
        await asyncio.sleep(30); join_view.stop()
        if len(game.players)<2:
            self.wordchain_games.pop(cid,None)
            await ctx.send(embed=make_embed(description="❌ Not enough players (need 2+). Cancelled.",color=COLORS["error"])); return
        starter=random.choice(["apple","orange","banana","grape","melon","tiger","eagle","shark","wolf","bear"])
        game.current_word=starter; game.used_words.add(starter); game.active=True
        names=", ".join(m.display_name for m in game.players)
        await ctx.send(embed=make_embed(title="🔤 Word Chain — Started!",
            description=f"**Players:** {names}\n\n🎯 **Starting word:** `{starter}`\n🔤 **Next starts with:** `{starter[-1].upper()}`\n\n👉 {game.current_player().mention} — you're first! *(30s)*",
            color=COLORS["success"]))
        try:
            while game.active and len(game.players)>=2:
                current=game.current_player(); last_letter=game.current_word[-1].lower()
                def wc_check(m,_cur=current): return m.author==_cur and m.channel==ctx.channel and m.content.isalpha()
                try:
                    reply=await self.bot.wait_for("message",check=wc_check,timeout=30)
                    word=reply.content.lower().strip()
                    if not word.startswith(last_letter):
                        await ctx.send(embed=make_embed(description=f"❌ **{current.display_name}** — `{word}` doesn't start with `{last_letter.upper()}`! 💀 Eliminated!",color=COLORS["error"]))
                        game.players.remove(current)
                    elif word in game.used_words:
                        await ctx.send(embed=make_embed(description=f"❌ **{current.display_name}** — `{word}` already used! 💀 Eliminated!",color=COLORS["error"]))
                        game.players.remove(current)
                    else:
                        game.used_words.add(word); game.current_word=word; game.next_turn(); nxt=game.current_player()
                        await ctx.send(embed=make_embed(description=f"✅ **{current.display_name}** said `{word}`!\n🔤 Next: **`{word[-1].upper()}`**\n👉 {nxt.mention} *(30s)*",color=COLORS["primary"]))
                except asyncio.TimeoutError:
                    await ctx.send(embed=make_embed(description=f"⏰ **{current.display_name}** too slow! 💀 Eliminated!",color=COLORS["warning"]))
                    game.players.remove(current)
            game.active=False; winner=game.players[0] if game.players else None
            if winner:
                await ctx.send(embed=make_embed(title="🏆 Word Chain — Winner!",
                    description=f"🎉 **{winner.display_name}** wins!\n🔤 {len(game.used_words)} words used!",color=COLORS["gold"]))
        finally: self.wordchain_games.pop(cid,None)

# ================================================================
#  PARTY GAMES COG
# ================================================================
WYR_QUESTIONS=[
    ("Have the ability to fly ✈️","Have the ability to be invisible 👻"),
    ("Always speak your mind 🗣️","Never be able to speak again 🤫"),
    ("Live in a world with no music 🎵","Live in a world with no internet 🌐"),
    ("Have unlimited money 💰","Have unlimited time ⏰"),
    ("Be famous but poor 🌟","Be rich but unknown 💎"),
    ("Fight 100 duck-sized horses 🐴","Fight 1 horse-sized duck 🦆"),
    ("Always be cold ❄️","Always be hot 🔥"),
    ("Speak all languages fluently 🌍","Play all instruments perfectly 🎸"),
    ("Lose all your memories 🧠","Lose all your friends 👥"),
    ("Go to the past ⏪","Go to the future ⏩"),
]
HANGMAN_WORDS=["python","discord","programming","keyboard","monitor","internet","gaming","adventure",
    "treasure","mystery","elephant","universe","chocolate","pineapple","dinosaur","butterfly","lightning","mountain"]
MEMORY_EMOJIS=["🍎","🍊","🍇","🍓","🍒","🍋","🍉","🥝","🐶","🐱","🐭","🐹","🐸","🦊","🐻","🐼"]
HANGMAN_STAGES=["```\n  +---+\n  |   |\n      |\n      |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n      |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n  |   |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|   |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|\\  |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|\\  |\n /    |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|\\  |\n / \\  |\n      |\n=========```"]
FAST_TYPE_SENTENCES=["The quick brown fox jumps over the lazy dog",
    "Pack my box with five dozen liquor jugs","Gaming is not just a hobby it is a lifestyle",
    "Discord bots make server management so much easier","The five boxing wizards jump quickly",
    "Coding every day will make you a better programmer"]

class WYRView(ui.View):
    def __init__(self): super().__init__(timeout=30); self.votes_a=[]; self.votes_b=[]
    @ui.button(label="🅰️ Option A",style=discord.ButtonStyle.primary,row=0)
    async def vote_a(self,interaction,button):
        uid=interaction.user.id
        if uid in self.votes_a: await interaction.response.send_message("✅ Already voted A!",ephemeral=True); return
        if uid in self.votes_b: self.votes_b.remove(uid)
        self.votes_a.append(uid); await interaction.response.send_message("✅ Voted **Option A**!",ephemeral=True)
    @ui.button(label="🅱️ Option B",style=discord.ButtonStyle.danger,row=0)
    async def vote_b(self,interaction,button):
        uid=interaction.user.id
        if uid in self.votes_b: await interaction.response.send_message("✅ Already voted B!",ephemeral=True); return
        if uid in self.votes_a: self.votes_a.remove(uid)
        self.votes_b.append(uid); await interaction.response.send_message("✅ Voted **Option B**!",ephemeral=True)

class MafiaJoinView(ui.View):
    def __init__(self): super().__init__(timeout=60); self.players=[]
    @ui.button(label="🎭 Join Mafia",style=discord.ButtonStyle.success)
    async def join(self,interaction,button):
        if interaction.user in self.players: await interaction.response.send_message("✅ Already in!",ephemeral=True); return
        self.players.append(interaction.user)
        await interaction.response.send_message(f"🎭 **{interaction.user.display_name}** joined! ({len(self.players)} players)")
    @ui.button(label="🚀 Start Game",style=discord.ButtonStyle.danger)
    async def start(self,interaction,button):
        if len(self.players)<4: await interaction.response.send_message("❌ Need 4+ players!",ephemeral=True); return
        for child in self.children: child.disabled=True
        await interaction.response.edit_message(view=self); self.stop()

class MafiaVoteView(ui.View):
    def __init__(self,players):
        super().__init__(timeout=60); self.votes={}
        for p in players:
            btn=ui.Button(label=p.display_name[:20],style=discord.ButtonStyle.secondary,emoji="🗳️")
            btn.callback=self._make_vote(p.id,p.display_name); self.add_item(btn)
    def _make_vote(self,tid,name):
        async def cb(interaction):
            self.votes[interaction.user.id]=tid
            await interaction.response.send_message(f"🗳️ Voted to eliminate **{name}**!",ephemeral=True)
        return cb

class PartyGames(commands.Cog):
    def __init__(self,bot): self.bot=bot; self.active_games=set()
    def _lock(self,cid):
        if cid in self.active_games: return False
        self.active_games.add(cid); return True
    def _unlock(self,cid): self.active_games.discard(cid)

    @commands.hybrid_command(name="wouldyourather",description="🤔 Vote on a Would You Rather question!")
    @commands.cooldown(1,10,commands.BucketType.channel)
    async def would_you_rather(self,ctx):
        opt_a,opt_b=random.choice(WYR_QUESTIONS); view=WYRView()
        e=discord.Embed(title="🤔 Would You Rather…",color=COLORS["pink"])
        e.add_field(name="🅰️ Option A",value=opt_a,inline=True); e.add_field(name="🅱️ Option B",value=opt_b,inline=True)
        e.set_footer(text="Vote within 30 seconds!"); msg=await ctx.send(embed=e,view=view)
        await asyncio.sleep(30); view.stop()
        for child in view.children: child.disabled=True
        total=len(view.votes_a)+len(view.votes_b); pct_a=round(len(view.votes_a)/total*100) if total else 0; pct_b=round(len(view.votes_b)/total*100) if total else 0
        re=discord.Embed(title="🤔 Would You Rather — Results!",color=COLORS["gold"])
        re.add_field(name=f"🅰️ {opt_a}",value=f"**{pct_a}%** — {len(view.votes_a)} vote(s)",inline=True)
        re.add_field(name=f"🅱️ {opt_b}",value=f"**{pct_b}%** — {len(view.votes_b)} vote(s)",inline=True)
        re.set_footer(text=f"Total votes: {total}")
        try: await msg.edit(embed=re,view=view)
        except: await ctx.send(embed=re)

    @commands.hybrid_command(name="hangman",description="🪢 Play multiplayer hangman!")
    @commands.cooldown(1,10,commands.BucketType.channel)
    async def hangman(self,ctx):
        cid=ctx.channel.id
        if not self._lock(cid): await ctx.send(embed=make_embed(description="❌ A game is already running!",color=COLORS["error"])); return
        try:
            word=random.choice(HANGMAN_WORDS); guessed=set(); attempts=0; max_att=6
            def display(): return " ".join(l if l in guessed else "_" for l in word)
            msg=await ctx.send(embed=discord.Embed(title="🪢 Hangman — Guess the Word!",
                description=f"{HANGMAN_STAGES[0]}\n**Word:** `{display()}`\n**Guessed:** None\n**Attempts left:** {max_att} ❤️",
                color=COLORS["purple"]))
            while attempts<max_att:
                if "_" not in display(): break
                def check(m): return m.channel==ctx.channel and not m.author.bot and len(m.content)==1 and m.content.isalpha()
                try:
                    reply=await self.bot.wait_for("message",check=check,timeout=30); letter=reply.content.lower()
                    if letter in guessed:
                        await ctx.send(embed=make_embed(description=f"🔁 `{letter.upper()}` already guessed!",color=COLORS["warning"]),delete_after=3); continue
                    guessed.add(letter)
                    if letter in word: title=f"✅ `{letter.upper()}` is in the word!"; color=COLORS["success"]
                    else: attempts+=1; title=f"❌ `{letter.upper()}` not in the word!"; color=COLORS["error"]
                    won="_" not in display()
                    e=discord.Embed(title="🪢 Hangman"+(" — YOU WIN! 🎉" if won else (" — GAME OVER 💀" if attempts>=max_att else "")),
                        description=f"{HANGMAN_STAGES[min(attempts,6)]}\n**Word:** `{display()}`\n**Guessed:** {', '.join(sorted(guessed)).upper() or 'None'}\n**Attempts left:** {max_att-attempts} ❤️\n\n*{title}*",
                        color=COLORS["success"] if won else (COLORS["error"] if attempts>=max_att else color))
                    await msg.edit(embed=e)
                    if won or attempts>=max_att: break
                except asyncio.TimeoutError:
                    await ctx.send(embed=make_embed(description=f"⏰ Time's up! Word was **`{word}`**!",color=COLORS["warning"])); return
            if "_" not in display(): await ctx.send(embed=make_embed(title="🎉 Hangman Won!",description=f"Word was **`{word}`**! 🏆",color=COLORS["success"]))
            else: await ctx.send(embed=make_embed(title="💀 Hangman Over!",description=f"Word was **`{word}`**.",color=COLORS["error"]))
        finally: self._unlock(cid)

    @commands.hybrid_command(name="fasttype",description="⌨️ Race to type the sentence fastest!")
    @commands.cooldown(1,15,commands.BucketType.channel)
    async def fasttype(self,ctx):
        cid=ctx.channel.id
        if not self._lock(cid): await ctx.send(embed=make_embed(description="❌ A game is already running!",color=COLORS["error"])); return
        try:
            sentence=random.choice(FAST_TYPE_SENTENCES)
            await ctx.send(embed=make_embed(title="⌨️ Fast Type — Get Ready!",description="Type the sentence as fast as you can!\nStarts in **3 seconds…**",color=COLORS["info"]))
            await asyncio.sleep(3)
            await ctx.send(embed=discord.Embed(title="⌨️ GO GO GO!",description=f"```{sentence}```",color=COLORS["success"]).set_footer(text="First to type it correctly wins!"))
            start=time.time(); winners=[]
            def check(m): return m.channel==ctx.channel and not m.author.bot
            try:
                deadline=asyncio.get_event_loop().time()+35
                while True:
                    remaining=deadline-asyncio.get_event_loop().time()
                    if remaining<=0: break
                    reply=await self.bot.wait_for("message",check=check,timeout=min(remaining,35))
                    if reply.content.lower().strip()==sentence.lower().strip():
                        elapsed=round(time.time()-start,2); winners.append((reply.author,elapsed))
                        if len(winners)==1:
                            await ctx.send(embed=make_embed(title="🏆 First Place!",description=f"⚡ **{reply.author.mention}** wins in **{elapsed}s**!\n*Waiting 5s for others…*",color=COLORS["gold"]))
                            await asyncio.sleep(5); break
            except asyncio.TimeoutError: pass
            if not winners:
                await ctx.send(embed=make_embed(description="😮 Nobody typed it correctly!",color=COLORS["error"])); return
            medals=["🥇","🥈","🥉"]
            lines="\n".join(f"{medals[i] if i<3 else f'{i+1}.'} **{w[0].display_name}** — {w[1]}s" for i,w in enumerate(winners))
            await ctx.send(embed=make_embed(title="⌨️ Fast Type — Results!",description=f"```{sentence}```\n\n{lines}",color=COLORS["primary"]))
        finally: self._unlock(cid)

    @commands.hybrid_command(name="memorymatch",description="🧩 Multiplayer emoji memory matching!")
    @commands.cooldown(1,15,commands.BucketType.channel)
    async def memorymatch(self,ctx):
        cid=ctx.channel.id
        if not self._lock(cid): await ctx.send(embed=make_embed(description="❌ A game is already running!",color=COLORS["error"])); return
        try:
            pairs=random.sample(MEMORY_EMOJIS,8); cards=pairs*2; random.shuffle(cards)
            revealed=[False]*16; matched=[False]*16; scores={}
            def render():
                rows=[]
                for r in range(4):
                    row_str=""
                    for c in range(4):
                        idx=r*4+c
                        row_str+="✅ " if matched[idx] else (f"{cards[idx]} " if revealed[idx] else f"`{str(idx+1).zfill(2)}` ")
                    rows.append(row_str)
                return "\n".join(rows)
            msg=await ctx.send(embed=discord.Embed(title="🧩 Memory Match",
                description=f"{render()}\n\n**Type two numbers** (e.g. `3 7`) to reveal cards!\nMatch all 8 pairs to win! *(60s per turn)*",
                color=COLORS["purple"]))
            def check(m): return m.channel==ctx.channel and not m.author.bot
            matched_pairs=0
            while matched_pairs<8:
                try:
                    reply=await self.bot.wait_for("message",check=check,timeout=60)
                    parts=reply.content.strip().split()
                    if len(parts)!=2: continue
                    try: a,b=int(parts[0])-1,int(parts[1])-1
                    except ValueError: continue
                    if not(0<=a<16 and 0<=b<16) or a==b: continue
                    if matched[a] or matched[b]:
                        await ctx.send(embed=make_embed(description="❌ Already matched!",color=COLORS["error"]),delete_after=3); continue
                    revealed[a]=revealed[b]=True
                    await msg.edit(embed=discord.Embed(title="🧩 Memory Match",
                        description=f"{render()}\n\n🔍 **{reply.author.display_name}** revealed `{a+1}` and `{b+1}`!",color=COLORS["info"]))
                    await asyncio.sleep(2)
                    if cards[a]==cards[b]:
                        matched[a]=matched[b]=True; matched_pairs+=1; uid=reply.author.id; scores[uid]=scores.get(uid,0)+1
                        await msg.edit(embed=discord.Embed(title="🧩 Memory Match",
                            description=f"{render()}\n\n✅ **{reply.author.display_name}** matched **{cards[a]}**! (Score: {scores[uid]})\nPairs left: {8-matched_pairs}",
                            color=COLORS["success"]))
                    else:
                        revealed[a]=revealed[b]=False
                        await msg.edit(embed=discord.Embed(title="🧩 Memory Match",description=f"{render()}\n\n❌ **No match!** Try again.",color=COLORS["error"]))
                except asyncio.TimeoutError:
                    await ctx.send(embed=make_embed(description="⏰ Game timed out!",color=COLORS["warning"])); return
            if scores:
                winner_id=max(scores,key=scores.get); winner=ctx.guild.get_member(winner_id) or ctx.author
                board_str="\n".join(f"**{ctx.guild.get_member(uid).display_name if ctx.guild.get_member(uid) else uid}** — {s} pairs" for uid,s in sorted(scores.items(),key=lambda x:-x[1]))
                await ctx.send(embed=make_embed(title="🧩 Memory Match — Game Over!",description=f"🏆 **{winner.display_name}** wins!\n\n{board_str}",color=COLORS["gold"]))
        finally: self._unlock(cid)

    @commands.hybrid_command(name="mafia",description="🎭 Social deduction — find the hidden Mafia!")
    @commands.cooldown(1,30,commands.BucketType.channel)
    async def mafia(self,ctx):
        cid=ctx.channel.id
        if not self._lock(cid): await ctx.send(embed=make_embed(description="❌ A game is already running!",color=COLORS["error"])); return
        try:
            view=MafiaJoinView()
            await ctx.send(embed=discord.Embed(title="🎭 Mafia — Join Phase!",
                description="🔪 One player is secretly the **Mafia**.\n🕊️ The rest are **Civilians** — vote to eliminate the Mafia!\n\nClick **Join Mafia** *(min 4)* then **Start Game**.",
                color=COLORS["dark"]).set_footer(text="60 seconds to join!"),view=view)
            try: await asyncio.wait_for(view.wait(),timeout=60)
            except asyncio.TimeoutError: pass
            players=view.players
            if len(players)<4:
                await ctx.send(embed=make_embed(description="❌ Not enough players (need 4+).",color=COLORS["error"])); return
            mafia_count=max(1,len(players)//4); mafia_members=random.sample(players,mafia_count)
            for p in players:
                role_text="🔪 **MAFIA**" if p in mafia_members else "🕊️ **CIVILIAN**"
                hint="Stay hidden and mislead the civilians!" if p in mafia_members else "Find and vote out the Mafia!"
                try:
                    await p.send(embed=make_embed(title="🎭 Your Mafia Role",
                        description=f"**Your Role:** {role_text}\n\n{hint}",
                        color=COLORS["error"] if p in mafia_members else COLORS["info"]))
                except: pass
            names=", ".join(p.display_name for p in players)
            await ctx.send(embed=discord.Embed(title="🎭 Mafia — Game Started!",
                description=f"**Players ({len(players)}):** {names}\n\n📬 Check your DMs!\n💬 Discuss then vote!",color=COLORS["dark"]))
            eliminated=[]
            while True:
                alive=[p for p in players if p not in eliminated]
                mafia_alive=[p for p in mafia_members if p not in eliminated]
                civilians_alive=[p for p in alive if p not in mafia_members]
                if not mafia_alive:
                    await ctx.send(embed=make_embed(title="🎭 Civilians Win! 🕊️",
                        description=f"🎉 Mafia eliminated!\n🔪 **Mafia was:** {', '.join(m.display_name for m in mafia_members)}",color=COLORS["success"])); break
                if len(mafia_alive)>=len(civilians_alive):
                    await ctx.send(embed=make_embed(title="🎭 Mafia Wins! 🔪",
                        description=f"😈 Mafia outnumbers!\n🔪 **Mafia was:** {', '.join(m.display_name for m in mafia_members)}",color=COLORS["error"])); break
                vote_view=MafiaVoteView(alive)
                await ctx.send(embed=discord.Embed(title="🗳️ Voting Phase",
                    description=f"**Alive:** {', '.join(p.display_name for p in alive)}\n\n🗳️ Vote! *(60s)*",color=COLORS["warning"]))
                vote_msg=await ctx.send(view=vote_view); await asyncio.sleep(60)
                vote_view.stop()
                for child in vote_view.children: child.disabled=True
                try: await vote_msg.edit(view=vote_view)
                except: pass
                tally={}
                for _,target in vote_view.votes.items(): tally[target]=tally.get(target,0)+1
                if not tally: await ctx.send(embed=make_embed(description="🤷 No votes! Skipping.",color=COLORS["info"])); continue
                top_id=max(tally,key=tally.get); victim=next((p for p in alive if p.id==top_id),None)
                if victim:
                    eliminated.append(victim); is_m=victim in mafia_members
                    await ctx.send(embed=make_embed(title="☠️ Player Eliminated!",
                        description=f"**{victim.display_name}** eliminated with **{tally[top_id]}** votes!\nThey were a {'🔪 **MAFIA**' if is_m else '🕊️ **CIVILIAN**'}!",
                        color=COLORS["error"]))
        finally: self._unlock(cid)

# ================================================================
#  GUESS THE SPY COG
# ================================================================
GTS_WORD_PAIRS=[
    ("Orange","Tangerine"),("Dog","Wolf"),("Piano","Keyboard"),("Ocean","Sea"),("Car","Automobile"),
    ("Happy","Joyful"),("Fire","Flame"),("Castle","Fortress"),("Phone","Telephone"),("Couch","Sofa"),
    ("Diamond","Gem"),("Chef","Cook"),("Movie","Film"),("Forest","Jungle"),("Ship","Vessel"),
    ("Apple","Pear"),("Lion","Tiger"),("Sun","Moon"),("Rain","Snow"),("Guitar","Violin"),
]

class GTSLobbyView(ui.View):
    def __init__(self,host):
        super().__init__(timeout=120); self.players=[host]; self.mode=None
    @ui.button(label="✋ Join",style=discord.ButtonStyle.success,row=0)
    async def join(self,interaction,button):
        if interaction.user in self.players: await interaction.response.send_message("✅ Already joined!",ephemeral=True); return
        self.players.append(interaction.user)
        await interaction.response.send_message(f"✅ **{interaction.user.display_name}** joined! ({len(self.players)} players)")
    @ui.button(label="🕵️ Normal",style=discord.ButtonStyle.primary,row=1)
    async def normal(self,interaction,button):
        if interaction.user!=self.players[0]: await interaction.response.send_message("❌ Only the host can start!",ephemeral=True); return
        if len(self.players)<3: await interaction.response.send_message("❌ Normal needs 3+ players!",ephemeral=True); return
        self.mode="normal"
        for child in self.children: child.disabled=True
        await interaction.response.edit_message(view=self); self.stop()
    @ui.button(label="👻 Wordless Spy",style=discord.ButtonStyle.danger,row=1)
    async def wordless(self,interaction,button):
        if interaction.user!=self.players[0]: await interaction.response.send_message("❌ Only the host can start!",ephemeral=True); return
        if len(self.players)<4: await interaction.response.send_message("❌ Wordless needs 4+ players!",ephemeral=True); return
        self.mode="wordless"
        for child in self.children: child.disabled=True
        await interaction.response.edit_message(view=self); self.stop()

class GTSVoteView(ui.View):
    def __init__(self,players):
        super().__init__(timeout=60); self.votes={}
        for p in players:
            btn=ui.Button(label=p.display_name[:20],style=discord.ButtonStyle.secondary,emoji="🗳️")
            btn.callback=self._make_cb(p.id,p.display_name); self.add_item(btn)
    def _make_cb(self,tid,name):
        async def cb(interaction):
            self.votes[interaction.user.id]=tid
            await interaction.response.send_message(f"🗳️ Voted for **{name}**!",ephemeral=True)
        return cb

class CloseChannelView(ui.View):
    def __init__(self,host): super().__init__(timeout=300); self.host=host
    @ui.button(label="🗑️ Close Channel",style=discord.ButtonStyle.danger)
    async def close(self,interaction,button):
        if interaction.user!=self.host: await interaction.response.send_message("❌ Only the host can close this.",ephemeral=True); return
        await interaction.response.send_message("🗑️ Closing in **3 seconds**…")
        await asyncio.sleep(3)
        try: await interaction.channel.delete(reason="GTS game ended")
        except: pass
        self.stop()

async def _create_gts_channel(guild,category,players):
    overwrites={
        guild.default_role: discord.PermissionOverwrite(view_channel=False,send_messages=False),
        guild.me: discord.PermissionOverwrite(view_channel=True,send_messages=True,manage_channels=True),
    }
    for player in players:
        overwrites[player]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)
    staff_roles=[]; cfg=get_guild("config",guild.id); rid=cfg.get("staff_role_id")
    if rid:
        role=guild.get_role(int(rid))
        if role: staff_roles.append(role)
    for role in guild.roles:
        if role in staff_roles: continue
        if any(getattr(role.permissions,p,False) for p in ("kick_members","ban_members","manage_guild","administrator")):
            staff_roles.append(role)
    for role in staff_roles:
        overwrites[role]=discord.PermissionOverwrite(view_channel=True,send_messages=False,read_message_history=True)
    try:
        return await guild.create_text_channel(name="guess-the-spy",overwrites=overwrites,
            category=category,topic="🕵️ Guess The Spy — private game channel",reason="GTS game started")
    except: return None

class GTS(commands.Cog):
    def __init__(self,bot): self.bot=bot; self.active_channels=set()

    @commands.hybrid_command(name="gts",description="🕵️ Guess The Spy — find the secret spy!")
    @commands.cooldown(1,30,commands.BucketType.channel)
    async def guess_the_spy(self,ctx):
        cid=ctx.channel.id
        if cid in self.active_channels:
            await ctx.reply(embed=make_embed(description="❌ A GTS game is already running!",color=COLORS["error"])); return
        self.active_channels.add(cid)
        try:
            view=GTSLobbyView(ctx.author)
            await ctx.send(embed=discord.Embed(title="🕵️ Guess The Spy — Lobby",
                description="**🟢 Normal Mode** *(3+ players)*\nEveryone gets the same word — the spy gets a *similar* word!\n\n**👻 Wordless Spy** *(4+ players)*\nThe spy has **NO word** and must bluff!\n\n━━━━━━━━━━━━━━━━━━━━\n"+f"👑 **Host:** {ctx.author.mention}\n\n✋ Click **Join**, then the **host** picks a mode!",
                color=COLORS["purple"]).set_footer(text="Lobby closes in 2 minutes"),view=view)
            try: await asyncio.wait_for(view.wait(),timeout=120)
            except asyncio.TimeoutError:
                await ctx.send(embed=make_embed(description="⏰ Lobby timed out.",color=COLORS["warning"])); return
            if not view.mode: return
            players=view.players; mode=view.mode
            gts_channel=await _create_gts_channel(ctx.guild,ctx.channel.category,players)
            if not gts_channel:
                await ctx.send(embed=make_embed(description="❌ Couldn't create private channel! Make sure bot has **Manage Channels** permission.",color=COLORS["error"])); return
            await ctx.send(embed=discord.Embed(title="🕵️ Game Starting!",
                description=f"Private channel created: {gts_channel.mention}\n\n**Players:** {' '.join(p.mention for p in players)}\n\nHead over there — roles are in your DMs!",
                color=COLORS["success"]))
            word_pair=random.choice(GTS_WORD_PAIRS); main_word=word_pair[0]
            spy_word=word_pair[1] if mode=="normal" else None; spy=random.choice(players); dm_failures=[]
            for p in players:
                if p==spy:
                    word_msg="🕵️ **You are the SPY!**\n\n"+(f"Your word: **`{spy_word}`**\n*Give clues — don't say it directly!*" if mode=="normal" else "⚠️ **You have NO word — bluff your way through!**")
                    color=COLORS["error"]
                else:
                    word_msg=f"🟢 **You are a Civilian!**\n\nYour word: **`{main_word}`**\n*Give clues — don't say it directly!*"
                    color=COLORS["success"]
                try: await p.send(embed=make_embed(title="🕵️ Guess The Spy — Your Role",description=word_msg,color=color))
                except: dm_failures.append(p.display_name)
            names=", ".join(p.display_name for p in players)
            await gts_channel.send(content=" ".join(p.mention for p in players),
                embed=discord.Embed(title=f"🕵️ Guess The Spy — {'Normal' if mode=='normal' else 'Wordless Spy'} Mode!",
                    description=f"**Players ({len(players)}):** {names}\n\n📬 Check your DMs for your role!\n\n🗣️ **Rules:**\n• Give one-word clues about your word\n• The spy must blend in!\n• After 3 min discussion → vote!\n\n⏱️ **3 minutes to discuss!**"+(f"\n\n⚠️ DM failed for: {', '.join(dm_failures)}" if dm_failures else ""),
                    color=COLORS["purple"]).set_footer(text="🔒 Staff can see but cannot chat"))
            for mins_left in [2,1]:
                await asyncio.sleep(60)
                await gts_channel.send(embed=make_embed(description=f"⏰ **{mins_left} minute{'s' if mins_left>1 else ''} left** before voting!",color=COLORS["warning"]))
            await asyncio.sleep(60)
            vote_view=GTSVoteView(players)
            vote_msg=await gts_channel.send(content=" ".join(p.mention for p in players),
                embed=discord.Embed(title="🗳️ Guess The Spy — Vote!",
                    description="⏰ **Discussion over!**\n\n🗳️ Vote for who you think is the **Spy!**\nYou have **60 seconds**!",
                    color=COLORS["warning"]),view=vote_view)
            await asyncio.sleep(60); vote_view.stop()
            for child in vote_view.children: child.disabled=True
            try: await vote_msg.edit(view=vote_view)
            except: pass
            tally={}
            for _,tid in vote_view.votes.items(): tally[tid]=tally.get(tid,0)+1
            if not tally:
                await gts_channel.send(embed=make_embed(description="🤷 No votes! The spy escapes!",color=COLORS["error"]))
            else:
                top_id=max(tally,key=tally.get); accused=next((p for p in players if p.id==top_id),None)
                if accused:
                    was_spy=accused==spy
                    spy_reveal=f"**Spy Word:** `{spy_word}`\n" if mode=="normal" and spy_word else "**Spy Word:** None *(Wordless mode)*\n"
                    await gts_channel.send(embed=discord.Embed(
                        title="✅ Spy Caught! Civilians Win! 🎉" if was_spy else "❌ Wrong! Spy Wins! 😈",
                        description=f"🗳️ **Most votes:** {accused.display_name} ({tally[top_id]} votes)\n🕵️ **The Spy was:** {spy.mention}\n🟢 **Main Word:** `{main_word}`\n{spy_reveal}"+("🎉 Civilians correctly identified the spy!" if was_spy else f"😈 **{spy.display_name}** successfully blended in!"),
                        color=COLORS["success"] if was_spy else COLORS["error"]).set_thumbnail(url=str(spy.display_avatar.url)))
            close_view=CloseChannelView(ctx.author)
            await gts_channel.send(embed=make_embed(description="🎮 **Game Over!** Thanks for playing.\n\nHost can close the channel, or it auto-deletes in **5 minutes**.",color=COLORS["primary"]),view=close_view)
            try: await asyncio.wait_for(close_view.wait(),timeout=300)
            except asyncio.TimeoutError:
                try: await gts_channel.delete(reason="GTS channel auto-deleted")
                except: pass
        finally: self.active_channels.discard(cid)

# ================================================================
#  MISC COG  (/google + /invite)
# ================================================================
HEADERS={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36","Accept-Language":"en-US,en;q=0.9"}
BLACKLISTED_FROM_INVITE=set()
_SAFE_OPS={ast.Add:operator.add,ast.Sub:operator.sub,ast.Mult:operator.mul,ast.Div:operator.truediv,ast.Pow:operator.pow,ast.Mod:operator.mod,ast.FloorDiv:operator.floordiv,ast.USub:operator.neg}

def _safe_eval(node):
    if isinstance(node,ast.Constant) and isinstance(node.value,(int,float)): return node.value
    if isinstance(node,ast.BinOp) and type(node.op) in _SAFE_OPS: return _SAFE_OPS[type(node.op)](_safe_eval(node.left),_safe_eval(node.right))
    if isinstance(node,ast.UnaryOp) and type(node.op) in _SAFE_OPS: return _SAFE_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("Unsafe")

def try_math(query):
    q=query.lower().strip().rstrip("?=")
    for pat,repl in [(r"\btimes\b","*"),(r"\bmultiplied by\b","*"),(r"\bdivided by\b","/"),(r"\bover\b","/"),(r"\bplus\b","+"),(r"\bminus\b","-"),(r"\bsquared\b","**2"),(r"\bcubed\b","**3"),(r"\bto the power of\b","**"),(r"\bwhat is\b",""),(r"\bwhat's\b",""),(r"\bcalculate\b",""),(r"\bsolve\b",""),(r"[,]","")]:
        q=re.sub(pat,repl,q)
    q=q.strip()
    if not q: return None
    try:
        result=_safe_eval(ast.parse(q,mode="eval").body)
        if isinstance(result,float) and result==int(result): result=int(result)
        return result
    except: return None

class Misc(commands.Cog):
    def __init__(self,bot): self.bot=bot

    @app_commands.command(name="invite",description="🤖 Get the bot invite link (owner only)")
    async def invite(self,interaction:discord.Interaction):
        if interaction.user.id in BLACKLISTED_FROM_INVITE:
            await interaction.response.send_message(embed=make_embed(description="🚫 You are blacklisted.",color=COLORS["error"]),ephemeral=True); return
        if interaction.user.id!=OWNER_ID:
            await interaction.response.send_message(embed=make_embed(description="🔒 Owner only.",color=COLORS["error"]),ephemeral=True); return
        url=discord.utils.oauth_url(str(self.bot.user.id),permissions=discord.Permissions(administrator=True))
        await interaction.response.send_message(embed=make_embed(title="🤖 Invite PolarBot",
            description=f"[**Click here to invite PolarBot!**]({url})\n\n`{url}`",color=COLORS["primary"]),ephemeral=True)

    @app_commands.command(name="google",description="🔍 Ask anything — get a direct answer or top results")
    @app_commands.describe(query="Your question or search query")
    async def google(self,interaction:discord.Interaction,query:str):
        await interaction.response.defer()
        encoded=urllib.parse.quote_plus(query); answer_block=""; search_block=""
        math_result=try_math(query)
        if math_result is not None: answer_block=f"🧮 **Answer:** `{math_result}`"
        if not answer_block:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"https://api.duckduckgo.com/?q={encoded}&format=json&no_html=1&skip_disambig=1",timeout=aiohttp.ClientTimeout(total=6)) as resp:
                        data=await resp.json(content_type=None)
                instant=(data.get("Answer") or "").strip(); abstract=(data.get("AbstractText") or "").strip(); definition=(data.get("Definition") or "").strip()
                if instant: answer_block=f"💡 **Answer:** {re.sub(r'<[^>]+>','',instant).strip()}"
                elif abstract:
                    short=abstract[:400]+("…" if len(abstract)>400 else ""); src=data.get("AbstractSource",""); src_url=data.get("AbstractURL","")
                    answer_block=f"📖 {short}"; answer_block+=f"\n— [*{src}*]({src_url})" if src and src_url else ""
                elif definition: answer_block=f"📚 **Definition:** {definition}"
            except: pass
        search_block=await self._web_search(query,encoded)
        parts=[]
        if answer_block: parts.append(answer_block)
        if search_block:
            if answer_block: parts.append("─────────────────\n🌐 **Top Results**")
            parts.append(search_block)
        description="\n\n".join(parts) if parts else f"😕 No results for **{query}**.\n[Search on Google](https://www.google.com/search?q={encoded})"
        e=discord.Embed(title=f"🔍 {query[:80]}",description=description[:3900],color=0x4285F4)
        e.set_thumbnail(url="https://www.google.com/favicon.ico")
        e.add_field(name="\u200b",value=f"[🔵 Google](https://www.google.com/search?q={encoded})  [🟠 DuckDuckGo](https://duckduckgo.com/?q={encoded})",inline=False)
        e.set_footer(text="PolarBot Search")
        await interaction.followup.send(embed=e)

    async def _web_search(self,query,encoded):
        try:
            from googlesearch import search as gsearch
            loop=asyncio.get_event_loop()
            raw=await asyncio.wait_for(loop.run_in_executor(None,lambda:list(gsearch(query,num_results=4,lang="en",advanced=True))),timeout=6)
            if raw:
                lines=[]
                for i,r in enumerate(raw[:4],1):
                    if hasattr(r,"title") and r.title and hasattr(r,"url"):
                        desc=(r.description[:80]+"…") if getattr(r,"description",None) else ""
                        lines.append(f"**{i}.** [{r.title[:65]}]({r.url})"+(f"\n> {desc}" if desc else ""))
                    elif hasattr(r,"url"): lines.append(f"**{i}.** {r.url}")
                if lines: return "\n\n".join(lines)
        except: pass
        try:
            async with aiohttp.ClientSession(headers=HEADERS) as session:
                async with session.get(f"https://html.duckduckgo.com/html/?q={encoded}",timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    html_content=await resp.text()
            results_list=[]; seen=set()
            for match in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',html_content,re.DOTALL):
                raw_url=match.group(1); raw_title=match.group(2); decoded=raw_url
                if "uddg=" in raw_url:
                    m=re.search(r"uddg=([^&]+)",raw_url)
                    if m: decoded=urllib.parse.unquote(m.group(1))
                title=re.sub(r"<[^>]+>","",raw_title).strip()[:65]
                if decoded not in seen and decoded.startswith("http") and title:
                    seen.add(decoded); results_list.append((title,decoded))
                if len(results_list)>=4: break
            if results_list: return "\n\n".join(f"**{i}.** [{t}]({u})" for i,(t,u) in enumerate(results_list,1))
        except: pass
        return ""

    @commands.Cog.listener()
    async def on_message(self,message:discord.Message):
        if message.author.bot: return
        if isinstance(message.channel,discord.DMChannel): await self.bot.process_commands(message)

# ================================================================
#  BOT EVENTS
# ================================================================
@bot.event
async def on_ready():
    await bot.tree.sync()
    await bot.change_presence(status=discord.Status.online,
        activity=discord.Activity(type=discord.ActivityType.watching,name=f"{len(bot.guilds)} servers | ,help"))
    print(f"✅  Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"✅  Slash commands synced.")
    print(f"✅  Serving {len(bot.guilds)} guilds.")

@bot.event
async def on_message_delete(message:discord.Message):
    if message.author.bot: return
    bot.snipe_cache[message.channel.id]={"content":message.content or "[No text]","author_name":str(message.author),"author_avatar":str(message.author.display_avatar.url),"timestamp":message.created_at.isoformat()}

@bot.event
async def on_message_edit(before:discord.Message,after:discord.Message):
    if before.author.bot or before.content==after.content: return
    bot.editsnipe_cache[before.channel.id]={"before":before.content or "[No text]","after":after.content or "[No text]","author_name":str(before.author),"author_avatar":str(before.author.display_avatar.url),"timestamp":before.created_at.isoformat()}

@bot.event
async def on_message(message:discord.Message):
    if message.author.bot: return
    afk_data=load("afk"); uid=str(message.author.id)
    if uid in afk_data:
        del afk_data[uid]; save("afk",afk_data)
        try:
            await message.channel.send(embed=make_embed(description=f"👋 Welcome back, {message.author.mention}! AFK removed.",color=0x57F287),delete_after=5)
        except: pass
    if message.mentions and message.guild:
        for mentioned in message.mentions:
            mid=str(mentioned.id)
            if mid in afk_data:
                entry=afk_data[mid]
                await message.channel.send(embed=make_embed(description=f"💤 **{mentioned.display_name}** is AFK\n**Reason:** {entry.get('reason','No reason')}\n**Since:** <t:{entry.get('since',0)}:R>",color=0xFEE75C),delete_after=8)
    if message.guild:
        sticky_data=get_guild("sticky",message.guild.id); cid=str(message.channel.id)
        if cid in sticky_data:
            info=sticky_data[cid]
            try:
                old=await message.channel.fetch_message(info["message_id"]); await old.delete()
            except: pass
            new_msg=await message.channel.send(embed=make_embed(title="📌 Sticky Message",description=info["content"],color=0xF1C40F))
            info["message_id"]=new_msg.id; sticky_data[cid]=info; set_guild("sticky",message.guild.id,sticky_data)
    await bot.process_commands(message)

@bot.event
async def on_command_error(ctx,error):
    if isinstance(error,commands.CommandNotFound): return
    if isinstance(error,commands.MissingRequiredArgument):
        await ctx.reply(embed=make_embed(description=f"❌ **Missing argument:** `{error.param.name}`\nUse `,help` for usage.",color=0xED4245))
    elif isinstance(error,commands.MemberNotFound):
        await ctx.reply(embed=make_embed(description="❌ **Member not found.**",color=0xED4245))
    elif isinstance(error,commands.CommandOnCooldown):
        await ctx.reply(embed=make_embed(description=f"⏳ **Slow down!** Try again in **{error.retry_after:.1f}s**.",color=0xFEE75C),delete_after=4)
    elif isinstance(error,commands.CheckFailure): pass
    else:
        await ctx.reply(embed=make_embed(description=f"⚠️ An error occurred: `{error}`",color=0xED4245))

# ================================================================
#  HELP COMMAND
# ================================================================
@bot.command(name="help")
async def help_cmd(ctx):
    e=discord.Embed(title="❄️ PolarBot — Commands",
        description="> Prefix: `,` or `!`  ·  All commands also work as `/slash`\n─────────────────────────────────",
        color=0x5865F2)
    e.set_thumbnail(url=bot.user.display_avatar.url)
    e.add_field(name="🛡️ Moderation  *(Staff only)*",
        value="`rn`  `delete`  `ps`  `warn`  `unwarn`  `warnings`  `kick`  `ban`  `unban`  `mute`  `unmute`  `userinfo`  `purge`",inline=False)
    e.add_field(name="🔧 Utility",
        value="`afk`  `vouch`  `snipe`  `editsnipe`  `sticky`  `unsticky`  `setstats`  `confess`",inline=False)
    e.add_field(name="🤖 Auto Response  *(Staff only)*",
        value="`autoresponse add`  `autoresponse remove`  `autoresponse list`",inline=False)
    e.add_field(name="🚫 AutoMod  *(Staff only)*",
        value="`automodadd`  `automodremove`  `automodwhitelist`  `automodunwhitelist`  `automodlist`",inline=False)
    e.add_field(name="🎮 Games",
        value="`rps`\n`trivia`\n`tictactoe`\n`connect4`\n`wordchain`\n`hangman`\n`fasttype`\n`memorymatch`\n`wouldyourather`\n`mafia`\n`gts`",
        inline=False)
    e.add_field(name="⚙️ Misc",value="`/google`  `/invite`",inline=False)
    e.set_footer(text=f"PolarBot 2.0  ·  {len(bot.guilds)} servers")
    await ctx.send(embed=e)

# ================================================================
#  ENTRY POINT
# ================================================================
async def main():
    async with bot:
        await bot.add_cog(Moderation(bot))
        await bot.add_cog(Utility(bot))
        await bot.add_cog(AutoResponse(bot))
        await bot.add_cog(AutoMod(bot))
        await bot.add_cog(Games(bot))
        await bot.add_cog(PartyGames(bot))
        await bot.add_cog(GTS(bot))
        await bot.add_cog(Misc(bot))
        print("✅  All cogs loaded.")
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
