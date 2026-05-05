#!/usr/bin/env bash
set -e
cd /opt/bot-sui
source venv/bin/activate
export BOT_SUI_BASE_DIR=/opt/bot-sui
export BOT_SUI_CONFIG=/opt/bot-sui/config.json
python3 bot.py
