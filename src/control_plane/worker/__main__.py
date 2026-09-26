"""Worker entrypoint: ``uv run python -m control_plane.worker``."""

import asyncio

from control_plane.config import get_settings
from control_plane.logging import configure_logging
from control_plane.worker.main import Worker


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    asyncio.run(Worker(settings).run())


if __name__ == "__main__":
    main()
