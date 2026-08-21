# Contributing

Thanks for helping improve TabloSync.

## Scope

TabloSync is an unofficial interoperability bridge for Tablo Gen 4 devices and Plex. Changes
must preserve the boundary that Plex remains responsible for guide data, DVR management, and
remote access. Do not add code that bypasses access controls or redistributes subscription-only
content.

## Development

Install Python 3.12, FFmpeg, and uv, then run:

```bash
uv sync --locked --extra dev
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv build
docker build --target test .
```

Never commit a real `.env`, Tablo account credentials, Plex tokens, device identifiers, channel
metadata, recordings, or diagnostic logs. Use `.env.example` and synthetic test data.

## Pull requests

Keep changes focused, add tests for behavioral changes, and explain any device or Plex versions
used for live verification. The Tablo Gen 4 API is undocumented, so changes based on observed
firmware behavior should include enough detail to reproduce the result without exposing private
account or device information.
