# E2B deployment notes

This project is a long-running Discord bot, so it should run in a persistent E2B sandbox/container rather than as a one-shot serverless function.

## Quick start

From this folder:

```bash
chmod +x start.sh
./start.sh
```

This will:
- create a local virtual environment
- install dependencies from the project root `requirements.txt`
- check that `.env` exists
- start `python main.py` in the background

## Required environment

Create a `.env` file in the project root before running:

```env
DISCORD_TOKEN=your_discord_bot_token
```

## Logs

The bot writes logs to:

```text
logs/e2b-bot.log
```

## Important

This app is designed to keep a live Discord connection open, so it is not a good match for Firebase Cloud Functions as the main runtime.

Use E2B or another long-lived Linux runtime for the actual bot process.
