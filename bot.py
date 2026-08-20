"""
GlobaChat
---------
A minimal Discord bot that watches messages and automatically replies with a
translation whenever a message isn't already in the server's target
language. DeepL is used as the primary backend (higher quality, needs an API
key); Google Translate (via the unofficial `googletrans` package) is used as
a free fallback when DeepL is unavailable or its quota is exhausted.

Configuration lives in config.ini next to this file (see config.example.ini).
Secrets (DISCORD_TOKEN, DEEPL_API_KEY) come from environment variables.
"""

import asyncio
import configparser
import logging
import os
import re
import sys
from collections import OrderedDict
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from lingua import Language, LanguageDetectorBuilder
from wordfreq import zipf_frequency

try:
    import deepl
except ImportError:
    deepl = None

try:
    from googletrans import Translator as GoogleTranslator
except ImportError:
    GoogleTranslator = None

BASE_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = BASE_DIR / "config.ini"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "globachat.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("globachat")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY")

if not DISCORD_TOKEN:
    log.error("DISCORD_TOKEN environment variable is required.")
    sys.exit(1)

deepl_client = deepl.Translator(DEEPL_API_KEY) if (deepl and DEEPL_API_KEY) else None
google_client = GoogleTranslator() if GoogleTranslator else None

if not deepl_client and not google_client:
    log.error("No translation backend available. Set DEEPL_API_KEY and/or install googletrans.")
    sys.exit(1)

if not deepl_client:
    log.warning("DEEPL_API_KEY not set - running on the Google Translate fallback only.")

# --------------------------------------------------------------------------
# Config (per-guild overrides via sections named after the guild/server ID)
# --------------------------------------------------------------------------

config = configparser.ConfigParser()
if CONFIG_PATH.exists():
    config.read(CONFIG_PATH)
else:
    config["DEFAULT"] = {
        "target_lang": "EN",
        "mode": "all",
        "min_chars": "2",
        "ignore_bots": "true",
        "confidence_threshold": "7",
    }
    with open(CONFIG_PATH, "w") as f:
        config.write(f)
    log.info("Created default config.ini at %s", CONFIG_PATH)


def guild_settings(guild_id: int) -> dict:
    section = str(guild_id)

    def get(key, fallback=None):
        if config.has_section(section) and config.has_option(section, key):
            return config.get(section, key, fallback=fallback)
        return config.get("DEFAULT", key, fallback=fallback)

    channels_raw = get("channels", "")
    channels = {c.strip() for c in channels_raw.split(",") if c.strip()} if channels_raw else None
    return {
        "target_lang": get("target_lang", "EN").upper(),
        "min_chars": int(get("min_chars", "2")),
        "ignore_bots": get("ignore_bots", "true").lower() in ("1", "true", "yes", "on"),
        "channels": channels,  # None = watch every channel in the server
        "confidence_threshold": int(get("confidence_threshold", "7")) / 100,
    }


# --------------------------------------------------------------------------
# Config mutation helpers (used by slash commands to persist changes back to
# config.ini). A lock guards concurrent writes across simultaneous commands.
# --------------------------------------------------------------------------

config_lock = asyncio.Lock()


async def update_guild_setting(guild_id: int, key: str, value: str) -> None:
    section = str(guild_id)
    async with config_lock:
        if not config.has_section(section):
            config.add_section(section)
        config.set(section, key, value)
        with open(CONFIG_PATH, "w") as f:
            config.write(f)


async def reset_guild_setting(guild_id: int, key: str = None) -> None:
    """Remove a single override (falls back to [DEFAULT]), or the whole
    per-guild section if key is None."""
    section = str(guild_id)
    async with config_lock:
        if not config.has_section(section):
            return
        if key is None:
            config.remove_section(section)
        else:
            config.remove_option(section, key)
            if not config.options(section):
                config.remove_section(section)
        with open(CONFIG_PATH, "w") as f:
            config.write(f)


def get_channels_list(guild_id: int) -> list:
    section = str(guild_id)
    if not config.has_section(section):
        return []
    raw = config.get(section, "channels", fallback="")
    return [c.strip() for c in raw.split(",") if c.strip()]


async def set_channels_list(guild_id: int, channel_ids: list) -> None:
    if channel_ids:
        await update_guild_setting(guild_id, "channels", ",".join(channel_ids))
    else:
        await reset_guild_setting(guild_id, "channels")


# --------------------------------------------------------------------------
# Small in-memory cache so repeated phrases ("gg", "lol", etc.) don't burn
# through API quota every time they're posted.
# --------------------------------------------------------------------------

_CACHE_MAX = 500
_cache: "OrderedDict[tuple, tuple]" = OrderedDict()


def cache_get(key):
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    return None


def cache_set(key, value):
    _cache[key] = value
    _cache.move_to_end(key)
    if len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


# --------------------------------------------------------------------------
# Text cleanup / heuristics
# --------------------------------------------------------------------------

URL_RE = re.compile(r"https?://\S+")
MENTION_RE = re.compile(r"<@!?\d+>|<#\d+>|<@&\d+>")
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")


def strip_noise(text: str) -> str:
    text = URL_RE.sub("", text)
    text = MENTION_RE.sub("", text)
    text = CUSTOM_EMOJI_RE.sub("", text)
    return text.strip()


def is_worth_translating(cleaned: str, min_chars: int) -> bool:
    letters = re.sub(r"[^\w]", "", cleaned, flags=re.UNICODE)
    return len(letters) >= min_chars


# Very short internet slang/interjections don't give language-detection
# models enough signal to work with, so they frequently get misidentified
# as some obscure language instead of being recognized as informal chat
# text. These are near-universal across languages' casual chat anyway, so
# we recognize them and skip translation outright rather than risk a bogus
# detection.
#
# Chat also loves stretching words out for emphasis (repeating a letter
# two, three, or more times), and each extra repeated letter is basically a
# coin flip for throwing the language guess off entirely - not just
# lowering its confidence in the right answer, but landing on a completely
# different language. Rather than list every stretch length of every word
# by hand, collapse any run of 2+ identical letters down to 1 before
# comparing against the slang set below, so every stretched variant of a
# word normalizes to the same base form and only needs one entry. This is
# deliberately only used for the slang comparison, never for the actual
# detection/translation text - collapsing doubled letters globally would
# risk misdetecting real foreign words that happen to have legitimate
# double letters in their normal spelling; limited to matching against this
# small curated set, that risk doesn't apply.
_REPEATED_CHAR_RE = re.compile(r"(.)\1+")  # 2+ repeats of a character collapse to one

COMMON_SLANG = {
    "lol", "lmao", "lmfao", "rofl", "omg", "omfg", "wtf", "brb", "afk",
    "gg", "ggs", "gl", "hf", "glhf", "np", "yw", "idk", "imo", "imho",
    "tbh", "smh", "btw", "fyi", "aye", "ayy", "yo", "yep", "yup", "nope",
    "nah", "no", "noo", "meh", "hmm", "huh", "ok", "okay", "kk", "sup",
    "haha", "hehe", "hihi", "xd", "ez", "gz", "gratz", "ty", "thx", "pls",
    "plz", "bruh", "bro", "sis", "fr", "ngl", "istg", "tho", "cuz", "sus",
    "cap", "nocap", "bet", "vibe", "vibes", "mood", "yeet", "based",
    "cringe", "sheesh", "welp", "damn", "dang", "wow", "woah", "whoa",
    "yay", "aww", "dude", "bud", "mate", "fam", "lit", "rip", "oof",
    "what", "wat", "wut", "why", "who", "so", "yes", "hey", "hi", "bye",
}


def _normalize_slang(word: str) -> str:
    return _REPEATED_CHAR_RE.sub(r"\1", word)


def is_common_slang(cleaned: str) -> bool:
    tokens = re.findall(r"[a-zA-Z]+", cleaned.lower())
    if not tokens:
        return False
    return all(_normalize_slang(t) in COMMON_SLANG for t in tokens)


# --------------------------------------------------------------------------
# Word-frequency check (catches slang/informal words COMMON_SLANG doesn't
# know about yet, without needing to hand-list them)
# --------------------------------------------------------------------------
# COMMON_SLANG only knows what's been manually added to it, so every new
# slang term needs a code change to be recognized. wordfreq
# (https://github.com/rspeer/wordfreq) gives real per-word frequency data
# pulled from actual usage - for English that includes Twitter, Reddit, and
# subtitles, so common internet slang typically already has a real
# frequency score without needing to be added by hand. Rather than ask
# "which of the supported languages does this resemble" (what Lingua does,
# and what's unreliable on very short/informal text), this asks a
# different, more direct question for short messages: "is every word here
# already a recognized, reasonably common word in the target language?" If
# so, skip translating - no statistical guessing needed. Only applied to
# short messages; once there's real sentence structure, Lingua's
# statistical detection has enough signal to be reliable on its own (and
# translating a few too many short words is a much smaller cost than
# mistranslating a real sentence).
WORD_FREQ_MIN_ZIPF = 2.0  # zipf_frequency scale: ~7 for "the", ~0 for unrecognized words
SHORT_MESSAGE_MAX_WORDS = 5


def token_zipf_scores(cleaned: str, target_lang: str) -> list:
    """Returns [(token, zipf_score), ...] for each word in the message,
    scored against the target language. Stretched-out spellings are
    collapsed the same way COMMON_SLANG matching does before lookup."""
    tokens = re.findall(r"[^\W\d_]+", cleaned.lower(), flags=re.UNICODE)
    lang = target_lang.split("-")[0].lower()
    scores = []
    for t in tokens:
        normalized = _normalize_slang(t)
        try:
            score = zipf_frequency(normalized, lang)
        except Exception as e:
            log.warning("wordfreq lookup failed for %r (%s): %s", normalized, lang, e)
            score = 0.0
        scores.append((normalized, score))
    return scores


def is_common_word_in_target(word_scores: list) -> bool:
    """True if this is a short message (<= SHORT_MESSAGE_MAX_WORDS words)
    where every word is already a recognized, common word in the target
    language - meaning it's not worth running through language detection
    at all, let alone translating."""
    if not word_scores or len(word_scores) > SHORT_MESSAGE_MAX_WORDS:
        return False
    return all(score >= WORD_FREQ_MIN_ZIPF for _, score in word_scores)


def langs_match(detected: str, target: str) -> bool:
    # Normalizes variants like EN-US / EN-GB vs EN before comparing.
    return detected.upper().split("-")[0] == target.upper().split("-")[0]


def _deepl_target(target_lang: str) -> str:
    # DeepL requires a region variant for a handful of languages when used
    # as a *target* (but not as a detected source).
    variants = {"EN": "EN-US", "PT": "PT-PT"}
    return variants.get(target_lang.upper(), target_lang.upper())


# --------------------------------------------------------------------------
# Translation backends
# --------------------------------------------------------------------------

async def translate(text: str, target_lang: str):
    """Returns (translated_text, detected_source_lang) or None on failure."""
    cache_key = (text, target_lang)
    cached = cache_get(cache_key)
    if cached:
        return cached

    loop = asyncio.get_running_loop()

    if deepl_client:
        try:
            result = await loop.run_in_executor(
                None,
                lambda: deepl_client.translate_text(text, target_lang=_deepl_target(target_lang)),
            )
            detected = result.detected_source_lang.upper()
            out = (result.text, detected)
            cache_set(cache_key, out)
            return out
        except Exception as e:
            log.warning("DeepL translation failed (%s); falling back to Google Translate.", e)

    if google_client:
        try:
            result = await loop.run_in_executor(
                None, lambda: google_client.translate(text, dest=target_lang.lower())
            )
            detected = (result.src or "?").upper()
            out = (result.text, detected)
            cache_set(cache_key, out)
            return out
        except Exception as e:
            log.error("Google Translate fallback failed: %s", e)

    return None


# --------------------------------------------------------------------------
# Bot
# --------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True  # must also be enabled in the Dev Portal
bot = commands.Bot(command_prefix="!globachat ", intents=intents, help_command=None)

# --------------------------------------------------------------------------
# Slash commands: /autotranslate ... - lets server admins adjust settings
# without touching config.ini by hand. Requires the "Manage Server"
# permission and the applications.commands OAuth2 scope on invite.
# --------------------------------------------------------------------------

LANGUAGE_CHOICES = [
    ("EN", "English"), ("EN-GB", "English (UK)"), ("FR", "French"),
    ("DE", "German"), ("ES", "Spanish"), ("IT", "Italian"),
    ("PT", "Portuguese"), ("PT-BR", "Portuguese (Brazil)"), ("NL", "Dutch"),
    ("PL", "Polish"), ("RU", "Russian"), ("JA", "Japanese"),
    ("ZH", "Chinese"), ("KO", "Korean"), ("AR", "Arabic"),
    ("TR", "Turkish"), ("SV", "Swedish"), ("DA", "Danish"),
    ("NB", "Norwegian"), ("FI", "Finnish"), ("CS", "Czech"),
    ("EL", "Greek"), ("HU", "Hungarian"), ("RO", "Romanian"),
    ("UK", "Ukrainian"), ("BG", "Bulgarian"),
]

# --------------------------------------------------------------------------
# Local language-detection confidence gate
# --------------------------------------------------------------------------
# DeepL/Google's own detected-source-language guess has no exposed
# confidence score, and considers dozens of languages this bot doesn't even
# support as a translation target - which is exactly how short, ambiguous
# messages end up misidentified as some obscure language neither the server
# nor the bot has any use for. Lingua runs entirely locally (no API call
# needed), restricted to only the languages in LANGUAGE_CHOICES above, and
# gives an actual confidence score per language so we can require a
# minimum lead before ever calling the translation API - and skip mixed
# messages that are overwhelmingly one language with a single
# foreign-looking word in them.
_LINGUA_BY_CODE = {
    "EN": Language.ENGLISH, "FR": Language.FRENCH, "DE": Language.GERMAN,
    "ES": Language.SPANISH, "IT": Language.ITALIAN, "PT": Language.PORTUGUESE,
    "NL": Language.DUTCH, "PL": Language.POLISH, "RU": Language.RUSSIAN,
    "JA": Language.JAPANESE, "ZH": Language.CHINESE, "KO": Language.KOREAN,
    "AR": Language.ARABIC, "TR": Language.TURKISH, "SV": Language.SWEDISH,
    "DA": Language.DANISH, "NB": Language.BOKMAL, "FI": Language.FINNISH,
    "CS": Language.CZECH, "EL": Language.GREEK, "HU": Language.HUNGARIAN,
    "RO": Language.ROMANIAN, "UK": Language.UKRAINIAN, "BG": Language.BULGARIAN,
}
_CODE_BY_LINGUA = {v: k for k, v in _LINGUA_BY_CODE.items()}

_detector = (
    LanguageDetectorBuilder.from_languages(*_LINGUA_BY_CODE.values())
    .with_preloaded_language_models()  # load n-gram models now, not on first message
    .build()
)


def detect_with_confidence(text: str):
    """Returns (language_code, top_confidence, runner_up_code, runner_up_confidence),
    using only the languages this bot supports as translation targets, or
    (None, 0.0, None, 0.0) if nothing could be identified at all (e.g. purely
    numbers/symbols). The runner-up is included so callers can look at the
    *margin* between 1st and 2nd place, not just the raw top score - short
    text spreads probability thin across every candidate language, so the
    raw top score alone doesn't reliably separate a correct-but-diluted guess
    from a genuinely ambiguous/wrong one; how far ahead the leader is does."""
    values = _detector.compute_language_confidence_values(text)
    if not values:
        return None, 0.0, None, 0.0
    top = values[0]
    second = values[1] if len(values) > 1 else None
    top_code = _CODE_BY_LINGUA.get(top.language)
    second_code = _CODE_BY_LINGUA.get(second.language) if second else None
    second_value = second.value if second else 0.0
    return top_code, top.value, second_code, second_value


translate_group = app_commands.Group(
    name="autotranslate",
    description="Configure the auto-translator for this server.",
    default_permissions=discord.Permissions(manage_guild=True),
)
channels_group = app_commands.Group(
    name="channels",
    description="Control which channels are watched.",
    parent=translate_group,
)


def _require_guild(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None


@translate_group.command(name="language", description="Set the auto-translate target language for this server.")
@app_commands.describe(language="The language messages should be translated into (start typing to search)")
@app_commands.checks.has_permissions(manage_guild=True)
async def cmd_set_language(interaction: discord.Interaction, language: str):
    if not _require_guild(interaction):
        return await interaction.response.send_message("This only works in a server.", ephemeral=True)
    match = next((name for code, name in LANGUAGE_CHOICES if code == language), None)
    if match is None:
        return await interaction.response.send_message(
            "❌ Unrecognized language code - please pick one of the suggestions from the list.", ephemeral=True
        )
    await update_guild_setting(interaction.guild.id, "target_lang", language)
    await interaction.response.send_message(
        f"✅ Target language for this server set to **{match}** (`{language}`).", ephemeral=True
    )


@cmd_set_language.autocomplete("language")
async def language_autocomplete(interaction: discord.Interaction, current: str):
    query = current.strip().lower()
    matches = [
        app_commands.Choice(name=name, value=code)
        for code, name in LANGUAGE_CHOICES
        if query in code.lower() or query in name.lower()
    ]
    return matches[:25]


@translate_group.command(name="min-chars", description="Minimum letters a message needs before it's translated.")
@app_commands.describe(count="Minimum number of letters (default: 2)")
@app_commands.checks.has_permissions(manage_guild=True)
async def cmd_set_min_chars(interaction: discord.Interaction, count: app_commands.Range[int, 1, 50]):
    if not _require_guild(interaction):
        return await interaction.response.send_message("This only works in a server.", ephemeral=True)
    await update_guild_setting(interaction.guild.id, "min_chars", str(count))
    await interaction.response.send_message(f"✅ Minimum message length set to **{count}** letters.", ephemeral=True)


@translate_group.command(name="ignore-bots", description="Whether to ignore messages sent by other bots.")
@app_commands.describe(value="True to ignore other bots' messages (default), false to translate them too")
@app_commands.checks.has_permissions(manage_guild=True)
async def cmd_set_ignore_bots(interaction: discord.Interaction, value: bool):
    if not _require_guild(interaction):
        return await interaction.response.send_message("This only works in a server.", ephemeral=True)
    await update_guild_setting(interaction.guild.id, "ignore_bots", "true" if value else "false")
    await interaction.response.send_message(
        f"✅ Ignore other bots' messages: **{value}**.", ephemeral=True
    )


@translate_group.command(name="confidence", description="Minimum lead the detected language needs over the runner-up before translating.")
@app_commands.describe(percent="0-100 - how far ahead the detected language must be over the next-best guess. Higher means fewer false positives, but risks missing some short/ambiguous foreign messages (default: 7)")
@app_commands.checks.has_permissions(manage_guild=True)
async def cmd_set_confidence(interaction: discord.Interaction, percent: app_commands.Range[int, 0, 100]):
    if not _require_guild(interaction):
        return await interaction.response.send_message("This only works in a server.", ephemeral=True)
    await update_guild_setting(interaction.guild.id, "confidence_threshold", str(percent))
    await interaction.response.send_message(
        f"✅ Detection margin threshold set to **{percent}%**.", ephemeral=True
    )


@translate_group.command(name="status", description="Show the current auto-translate settings for this server.")
async def cmd_status(interaction: discord.Interaction):
    if not _require_guild(interaction):
        return await interaction.response.send_message("This only works in a server.", ephemeral=True)
    settings = guild_settings(interaction.guild.id)
    channels_desc = (
        "All channels" if settings["channels"] is None
        else ", ".join(f"<#{c}>" for c in settings["channels"]) or "All channels"
    )
    embed = discord.Embed(title="Auto-translate settings", color=discord.Color.blurple())
    embed.add_field(name="Target language", value=settings["target_lang"], inline=True)
    embed.add_field(name="Min characters", value=str(settings["min_chars"]), inline=True)
    embed.add_field(name="Ignore bots", value=str(settings["ignore_bots"]), inline=True)
    embed.add_field(name="Detection margin threshold", value=f"{int(settings['confidence_threshold'] * 100)}%", inline=True)
    embed.add_field(name="Watched channels", value=channels_desc, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@translate_group.command(name="reset", description="Reset this server's settings back to the global defaults.")
@app_commands.checks.has_permissions(manage_guild=True)
async def cmd_reset(interaction: discord.Interaction):
    if not _require_guild(interaction):
        return await interaction.response.send_message("This only works in a server.", ephemeral=True)
    await reset_guild_setting(interaction.guild.id)
    await interaction.response.send_message("✅ Reset to the global default settings.", ephemeral=True)


@channels_group.command(name="add", description="Restrict auto-translation to this channel (plus any others already added).")
@app_commands.checks.has_permissions(manage_guild=True)
async def cmd_channels_add(interaction: discord.Interaction, channel: discord.TextChannel):
    if not _require_guild(interaction):
        return await interaction.response.send_message("This only works in a server.", ephemeral=True)
    current = get_channels_list(interaction.guild.id)
    cid = str(channel.id)
    if cid not in current:
        current.append(cid)
    await set_channels_list(interaction.guild.id, current)
    await interaction.response.send_message(
        f"✅ Now watching {channel.mention} (only listed channels are watched now).", ephemeral=True
    )


@channels_group.command(name="remove", description="Stop watching a specific channel.")
@app_commands.checks.has_permissions(manage_guild=True)
async def cmd_channels_remove(interaction: discord.Interaction, channel: discord.TextChannel):
    if not _require_guild(interaction):
        return await interaction.response.send_message("This only works in a server.", ephemeral=True)
    current = get_channels_list(interaction.guild.id)
    cid = str(channel.id)
    if cid in current:
        current.remove(cid)
    await set_channels_list(interaction.guild.id, current)
    desc = "All channels (whitelist is now empty)" if not current else f"removed {channel.mention}"
    await interaction.response.send_message(f"✅ Updated watched channels - {desc}.", ephemeral=True)


@channels_group.command(name="all", description="Watch every channel in the server again (clears the whitelist).")
@app_commands.checks.has_permissions(manage_guild=True)
async def cmd_channels_all(interaction: discord.Interaction):
    if not _require_guild(interaction):
        return await interaction.response.send_message("This only works in a server.", ephemeral=True)
    await reset_guild_setting(interaction.guild.id, "channels")
    await interaction.response.send_message("✅ Now watching every channel in the server.", ephemeral=True)


@channels_group.command(name="list", description="Show which channels are currently watched.")
async def cmd_channels_list(interaction: discord.Interaction):
    if not _require_guild(interaction):
        return await interaction.response.send_message("This only works in a server.", ephemeral=True)
    current = get_channels_list(interaction.guild.id)
    desc = "All channels" if not current else ", ".join(f"<#{c}>" for c in current)
    await interaction.response.send_message(f"Watched channels: {desc}", ephemeral=True)


bot.tree.add_command(translate_group)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
        message = "❌ You need the **Manage Server** permission to use this."
    else:
        log.error("Slash command error: %s", error, exc_info=error)
        message = "⚠️ Something went wrong running that command."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@bot.event
async def on_ready():
    log.info("Logged in as %s (id: %s)", bot.user, bot.user.id)
    log.info("Watching %d guild(s).", len(bot.guilds))
    try:
        # One-time cleanup: wipe any previously-registered GLOBAL commands so
        # they stop showing up as duplicates alongside the guild-specific ones
        # below. Safe to leave in permanently - it's a no-op once cleared.
        await bot.http.bulk_upsert_global_commands(bot.application_id, payload=[])
        for guild in bot.guilds:
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
        log.info("Slash commands synced.")
    except discord.HTTPException as e:
        log.error("Failed to sync slash commands: %s", e)


@bot.event
async def on_guild_join(guild: discord.Guild):
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    log.info("Synced slash commands for newly joined guild: %s (%s)", guild.name, guild.id)


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user or not message.guild:
        return

    settings = guild_settings(message.guild.id)

    if settings["ignore_bots"] and message.author.bot:
        return

    if settings["channels"] is not None and str(message.channel.id) not in settings["channels"]:
        return

    if not message.content:
        return

    cleaned = strip_noise(message.content)
    if not is_worth_translating(cleaned, settings["min_chars"]):
        return

    if is_common_slang(cleaned):
        return  # recognized chat slang/interjection - not worth (mis)translating

    word_scores = token_zipf_scores(cleaned, settings["target_lang"])
    if len(word_scores) <= SHORT_MESSAGE_MAX_WORDS:
        log.info(
            "[wordfreq] %r -> %s target=%s min_zipf=%.1f",
            cleaned, word_scores, settings["target_lang"], WORD_FREQ_MIN_ZIPF,
        )
    if is_common_word_in_target(word_scores):
        return  # short message, every word already a recognized word in the target language

    detected_code, confidence, runner_up_code, runner_up_conf = detect_with_confidence(cleaned)
    margin = confidence - runner_up_conf
    log.info(
        "[detect] %r -> top=%s conf=%.0f%% runner_up=%s runner_up_conf=%.0f%% "
        "margin=%.0f%% target=%s threshold=%.0f%%",
        cleaned, detected_code, confidence * 100, runner_up_code, runner_up_conf * 100,
        margin * 100, settings["target_lang"], settings["confidence_threshold"] * 100,
    )
    if detected_code is None:
        return  # couldn't confidently identify any supported language at all

    if langs_match(detected_code, settings["target_lang"]):
        return  # already (confidently) in the target language - skip the API call entirely

    # Raw top-1 confidence alone isn't reliable for short/medium text - it
    # gets diluted across every candidate language regardless of whether the
    # top guess is right or wrong (a real foreign phrase and a misidentified
    # English fragment can land at the same low raw score). Whether there's
    # a clear *leader* over the runner-up is a much cleaner signal: genuine
    # foreign text tends to have a real gap over 2nd place even when its own
    # raw score is modest, while noisy/wrong guesses tend to be close races.
    if margin < settings["confidence_threshold"]:
        return  # top guess isn't clearly ahead of the runner-up - too uncertain to trust

    result = await translate(cleaned, settings["target_lang"])
    if not result:
        return

    translated_text, detected_lang = result

    if langs_match(detected_lang, settings["target_lang"]):
        return  # already in the target language

    if translated_text.strip().lower() == cleaned.strip().lower():
        return  # nothing meaningful changed (e.g. proper nouns only)

    if len(translated_text) > 1900:
        translated_text = translated_text[:1900] + "…"

    try:
        await message.reply(
            f"🌐 *({detected_lang} → {settings['target_lang']})* {translated_text}",
            mention_author=False,
        )
    except discord.HTTPException as e:
        log.error("Failed to send translation reply: %s", e)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)