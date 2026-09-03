# TabloSync

[![CI](https://github.com/walkeryan/TabloSync/actions/workflows/ci.yml/badge.svg)](https://github.com/walkeryan/TabloSync/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

TabloSync exposes a **Tablo Gen 4** as an HDHomeRun-compatible network tuner that Plex can
add under Live TV & DVR. It logs in to the Tablo account, obtains the OTA channel lineup,
starts local Tablo HLS streams on demand, and remuxes them to the continuous MPEG-TS stream
Plex expects.

```text
Tablo antenna + tuner  --local HLS-->  TabloSync  --MPEG-TS-->  Plex Media Server
                                                                      |
                                                               Plex remote access
```

Plex remains the DVR, guide UI, and remote-access layer. TabloSync does not record or expose
the Tablo directly to the internet.

This replaces Channels DVR as a **Tablo-to-Plex tuner bridge**. An optional, experimental
TV Everywhere service also exposes Big Ten Network through FOX with your Xfinity subscription.
It does not reproduce Channels' virtual channels, commercial detection, or DVR clients.

## Scope

- Tablo Gen 4 devices linked to a Tablo account
- OTA channels by default
- Manual Plex tuner discovery by IP address and port
- Lossless remuxing (`-c copy`), not video transcoding
- Linux/Docker deployment on the same LAN as Tablo and Plex

Legacy Tablo models have a different API and are not supported in this first version. Tablo
FAST/OTT channels can be enabled, but Plex guide matching for those channels is not guaranteed.

## How guide data works

TabloSync supplies the channel lineup and live video URLs; it does not scrape or redistribute
program-guide data. During DVR setup, Plex downloads the local broadcast guide for the selected
postal code and maps each TabloSync channel number to a station in that guide. Plex then owns
guide refreshes, program metadata, recording rules, and artwork.

If a station changes channel numbers or a Tablo rescan adds channels, open Plex's Live TV & DVR
settings and review the tuner channel mapping.

## Run with Docker Compose

1. Copy the sample configuration:

   ```bash
   cp .env.example .env
   ```

2. Set `TABLO_EMAIL` and `TABLO_PASSWORD` in `.env`. If the account has more than one
   device, also set `TABLO_DEVICE_SID`.

3. Build and start:

   ```bash
   docker compose up -d --build
   docker compose logs -f tablosync
   ```

4. Confirm the bridge is ready:

   ```bash
   curl http://SERVER_LAN_IP:5004/healthz
   curl http://SERVER_LAN_IP:5004/discover.json
   curl http://SERVER_LAN_IP:5004/lineup.json
   ```

If the generated channel URLs contain the wrong host (usually because a reverse proxy is in
front of the service), set `TABLOSYNC_ADVERTISE_URL=http://SERVER_LAN_IP:5004`.

## Add it to Plex

1. Open Plex Web as the server administrator.
2. Go to **Settings -> Manage -> Live TV & DVR -> Set Up Plex DVR**.
3. If TabloSync is not discovered automatically, choose the option to enter the network
   address manually and use `SERVER_LAN_IP:5004`.
4. Choose **Antenna**, enter the local postal code, and map the detected major/minor channel
   numbers to Plex's guide lineup.
5. Finish setup, then test one channel before scheduling recordings.

Watching OTA channels live does not require TabloSync to be internet-facing. For away-from-home
viewing, enable and verify Plex Remote Access. Plex DVR recording requires Plex Pass.

## Big Ten Network with Xfinity (experimental)

`tablosync-tve` is a separate one-tuner **Cable** device on port **5005**. It does not change
the Tablo antenna device on port 5004 and does not need Tablo account credentials. Your
Xfinity TV package must include BTN and permit FOX TV Everywhere access. This is not an
integration with the full Xfinity Stream lineup.

1. Copy `.env.example` to `.env` if it does not already exist; TVE uses only its own settings.
2. Start only the optional TVE service:

   ```bash
   docker compose up -d --build tablosync-tve
   ```

3. Open `http://SERVER_LAN_IP:5005/auth/fox` on your trusted LAN and start activation.
   Follow the official FOX activation link, enter the displayed code, select Xfinity,
   and sign in there. TabloSync never receives your Xfinity password.
4. Keep the activation page open until authorization is confirmed. This confirms provider
   login, **not** BTN entitlement or successful playback; test a live stream next.
5. Add `SERVER_LAN_IP:5005` manually in Plex's Live TV & DVR settings as a cable tuner.
   Select the appropriate cable guide and map virtual channel **6100 — Big Ten Network**
   to BTN. The virtual number is not your local Xfinity channel number. Plex supplies the
   guide, and support for multiple guide lineups depends on your Plex setup/version.
6. Verify local playback, remote playback through Plex, and a short recording before relying
   on it for games. Leave your existing antenna tuner and guide intact.

FOX uses an undocumented, changeable API. Only standard HLS (including ordinary AES-128
segment encryption) is supported. Widevine, PlayReady, FairPlay, and other protected streams
are not decrypted. A FOX free preview is never treated as subscription authorization.
Availability, blackouts, session limits, and expired provider authorization remain FOX/Xfinity's
decisions. If reauthorization is required, return to the activation page.

Authorization tokens are stored with owner-only permissions in the `tablosync-tve-data`
Docker volume. Treat that volume as a secret. Keep port 5005 on your trusted LAN; neither
the tuner endpoints nor the activation page have a separate login. Never publish them
through a public proxy. Use Plex Remote Access for away-from-home viewing.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TABLOSYNC_TVE_PORT` | `5005` | HTTP port (host port under Compose) |
| `TABLOSYNC_TVE_FRIENDLY_NAME` | `TabloSync TV Everywhere` | Name Plex sees |
| `TABLOSYNC_TVE_ADVERTISE_URL` | request URL | Base URL placed in the Plex lineup |
| `TABLOSYNC_TVE_STATE_FILE` | `/data/fox-auth.json` | Private token file; Compose mounts `/data` |
| `TABLOSYNC_TVE_BTN_GUIDE_NUMBER` | `6100` | Virtual BTN channel number |
| `TABLOSYNC_TVE_TUNER_COUNT` | `1` | Concurrent local stream slots; does not override provider limits |
| `TABLOSYNC_TVE_AUTH_UI` | `true` | Set `false` after setup to disable activation endpoints |
| `TABLOSYNC_TVE_FFMPEG_PATH` | `ffmpeg` | FFmpeg executable path |

For non-Docker deployment, install the package and run `tablosync-tve` with a writable
`TABLOSYNC_TVE_STATE_FILE` path. No `TABLO_EMAIL` or `TABLO_PASSWORD` is needed.

## Migrate safely from Channels

1. Run TabloSync beside Channels on port 5004.
2. Add TabloSync as a second Plex tuner and map the OTA lineup.
3. Verify live playback locally, live playback through Plex Remote Access, and one short DVR
   recording.
4. Leave the old bridge available for a day of normal use.
5. Stop Channels only after those checks pass; cancel the subscription separately after the
   replacement is proven.

## Configuration

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `TABLO_EMAIL` | yes | - | Tablo account email |
| `TABLO_PASSWORD` | yes* | - | Tablo account password |
| `TABLO_PASSWORD_FILE` | yes* | - | Read the password from a Docker secret instead |
| `TABLO_DEVICE_SID` | for multiple devices | - | Select one Tablo from the account |
| `TABLOSYNC_INCLUDE_OTT` | no | `false` | Include Tablo FAST/OTT channels |
| `TABLOSYNC_FRIENDLY_NAME` | no | `TabloSync` | Name Plex sees |
| `TABLOSYNC_PORT` | no | `5004` | HTTP listen port |
| `TABLOSYNC_TUNER_COUNT` | no | device value | Override concurrent stream slots |
| `TABLOSYNC_CHANNEL_CACHE_SECONDS` | no | `3600` | Lineup cache lifetime |
| `TABLOSYNC_ADVERTISE_URL` | no | request URL | Base URL placed in the Plex lineup |
| `TABLOSYNC_FFMPEG_PATH` | no | `ffmpeg` | FFmpeg executable path |

\* Set exactly one of `TABLO_PASSWORD` or `TABLO_PASSWORD_FILE`.

## Security and reliability notes

- Keep port 5004 on the trusted LAN. HDHomeRun's tuner protocol does not give Plex a way to
  supply application credentials, so the compatibility endpoints intentionally have no login.
- The Tablo password is used for cloud authentication and is not written to disk by TabloSync.
  Docker administrators can still inspect ordinary environment variables; use
  `TABLO_PASSWORD_FILE` when Docker secrets are available.
- Account login and channel metadata use Tablo's cloud service. Video flows locally from Tablo
  through TabloSync to Plex.
- Tablo's Gen 4 API is undocumented and can change with firmware or cloud updates. The bridge
  uses the MIT-licensed [`tablo-api`](https://github.com/trevor-viljoen/tablo-api) client and
  should be tested again after Tablo firmware changes.
- Each Plex live view or recording occupies one Tablo tuner. TabloSync rejects additional
  streams when the device-reported tuner count is exhausted.

## Development

FFmpeg must be installed locally.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/pytest
```

This is an unofficial personal interoperability project and is not affiliated with Plex,
Tablo/Nuvyyo, SiliconDust, FOX, BTN, or Xfinity. Use it only with devices and streams you are
authorized to access and in accordance with your provider's terms.
