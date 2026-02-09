"""Entry point: python -m slaude"""

import asyncio

from .app import start

if __name__ == "__main__":
    asyncio.run(start())
