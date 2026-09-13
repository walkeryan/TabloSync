"""Console entry point for the TV Everywhere tuner."""

import uvicorn

from .tve_app import load_tve_app
from .tve_config import TVESettings


def main() -> None:
    settings = TVESettings.from_env()
    uvicorn.run(load_tve_app(), host="0.0.0.0", port=settings.port)  # noqa: S104


if __name__ == "__main__":
    main()
