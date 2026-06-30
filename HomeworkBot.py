Discord Humanizer + Homework Bot
Deploy on Railway — single file, no crashes.
Required env: DISCORD_BOT_TOKEN, OPENAI_API_KEY
Optional env: DB_PATH (default: premium.db)

Works in: servers, DMs, group DMs — anywhere Discord allows.
Users can install this bot to their own account via User Install
so it follows them everywhere without needing to be in the server.

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
(token)
