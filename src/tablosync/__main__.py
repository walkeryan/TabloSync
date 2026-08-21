"""Console entry point."""

import uvicorn

from .app import load_app
from .config import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(load_app(), host="0.0.0.0", port=settings.port)  # noqa: S104


if __name__ == "__main__":
    main()
