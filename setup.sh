#!/bin/bash
# Setup script for Slack listener

set -e

cd "$(dirname "$0")"

echo "Creating virtual environment..."
python3 -m venv .venv

echo "Installing dependencies..."
.venv/bin/pip install slack-bolt python-dotenv

echo "Setup complete!"
echo ""
echo "Next steps:"
echo "1. Copy .env.example to .env and add your Slack tokens"
echo "2. Test: .venv/bin/python listener.py"
echo "3. Load daemon: launchctl load ~/Library/LaunchAgents/com.claude.slack-bot.plist"
