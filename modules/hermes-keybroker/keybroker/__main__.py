"""Entry point for python -m keybroker."""

import asyncio
import sys

from keybroker.app import main


def run() -> None:
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)


if __name__ == "__main__":
    run()
