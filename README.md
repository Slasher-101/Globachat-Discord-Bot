# autotranslate-bot

A minimal Discord bot that watches messages and automatically replies with a
translation whenever a message isn't already in the server's target
language - no reaction or command needed. DeepL is the primary translation
backend; Google Translate is used as a free fallback if no DeepL key is set
or DeepL's quota runs out.

## How it decides to translate

For every non-bot message with real text content:

1. Strip URLs, mentions, and custom emoji before checking length.
2. Skip if what's left is shorter than `min_chars` (avoids false triggers on
   "lol", "ok", emoji-only messages, etc.) or common chat slang.
3. For short messages (5 words or fewer), skip if every word is already a
   recognized, common word in the target language according to real-world
   frequency data ([wordfreq](https://github.com/rspeer/wordfreq).
4. Run local language detection (via [Lingua](https://github.com/pemistahl/lingua-py),
   no API call) restricted to only the languages this bot supports as
   targets. If the detected language already matches `target_lang`, or its
   lead over the runner-up guess is below `confidence_threshold`%, nothing
   is translated. It's a *margin* rather than a raw confidence score
   deliberately: short text spreads probability thin across every candidate
   language, so a real foreign phrase and a misidentified English fragment
   can land at similarly low raw scores.
5. Only once local detection is confident it's genuinely a different
   language does the message actually get sent to DeepL/Google to translate,
   and reply in-thread with the result.

## Setup

### 1. Discord bot setup

1. Create an application + bot at the [Discord Developer Portal](https://discord.com/developers/applications).
2. Under **Bot**, enable **Message Content Intent** — this is required or
   the bot won't receive message text at all.
3. Invite the bot to your server with **both** the `bot` and
   `applications.commands` OAuth2 scopes (the second one is required for the
   `/autotranslate` slash commands to show up), and at minimum
   `Send Messages` + `Read Message History` permissions.
4. Copy the bot token for step 3 below.

### 2. DeepL (optional but recommended)

Grab a free API key at [deepl.com/pro-api](https://www.deepl.com/pro-api)
(500,000 characters/month on the free tier). Without it, the bot falls back
to the unofficial `googletrans` package, which is free but less reliable and
can get rate-limited.

### 3. Configure

```bash
cp .env.example .env
cp config.example.ini config.ini
```

Edit `.env` with your real `DISCORD_TOKEN` and `DEEPL_API_KEY`.

Edit `config.ini` if you want a non-English default target language, or
per-server/per-channel overrides - see the comments in the file. The
`[DEFAULT]` section applies everywhere unless a server has its own section
keyed by its Discord server ID.

### 4. Run

```bash
docker compose up -d --build
docker compose logs -f
```

## Slash commands

Settings can be changed live from Discord instead of editing `config.ini` by
hand — changes are written back to `config.ini` immediately, so they survive
a restart. All commands require the **Manage Server** permission except
`/autotranslate status` and `/autotranslate channels list`, which anyone can
run.

| Command | Effect |
|---|---|
| `/autotranslate language <language>` | Set this server's target language |
| `/autotranslate min-chars <count>` | Minimum letters a message needs before it's translated (default 2) |
| `/autotranslate confidence <percent>` | Minimum lead the detected language needs over the runner-up guess before translating (default 7%). Raise it if you're seeing false positives, lower it if genuine short foreign messages are being missed |
| `/autotranslate ignore-bots <true\|false>` | Whether to skip messages from other bots |
| `/autotranslate channels add <#channel>` | Restrict watching to specific channels (first `add` switches into whitelist mode) |
| `/autotranslate channels remove <#channel>` | Stop watching a channel |
| `/autotranslate channels all` | Clear the whitelist |
| `/autotranslate channels list` | Show which channels are currently watched |
| `/autotranslate status` | Show all current settings for this server |
| `/autotranslate reset` | Wipe this server's overrides, revert to `[DEFAULT]` |

Slash commands sync automatically on startup (instantly for servers the bot
is already in). If you don't see them immediately after first launch, wait
a few seconds and refresh Discord (Ctrl/Cmd+R).

## Notes

* Translated replies are truncated to Discord's ~2000 character message
  limit.
* Repeated identical phrases are cached in-memory (per run) to save API
  calls.
