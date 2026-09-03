# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub Security Advisories](https://github.com/walkeryan/TabloSync/security/advisories/new).
Do not open a public issue containing credentials, tokens, private channel metadata, device
identifiers, LAN addresses, or diagnostic logs.

Include a concise description, affected version or commit, reproduction steps using synthetic
data where possible, and the expected impact.

## Deployment boundary

TabloSync's HDHomeRun-compatible endpoints intentionally do not require authentication because
Plex cannot supply application credentials when using them. Keep the service on a trusted LAN;
do not expose its port directly to the internet. Use Plex Remote Access for viewing away from
home.

Prefer `TABLO_PASSWORD_FILE` over a plain environment variable when the deployment platform
supports secrets. Never publish a real `.env` file.

The optional TV Everywhere service has the same LAN-only boundary on port 5005. Its FOX
activation endpoints are intentionally local and unauthenticated. Disable them with
`TABLOSYNC_TVE_AUTH_UI=false` after setup if desired; re-enable them when reauthorization
is needed. Do not expose the activation page through a public reverse proxy.

The FOX token file and `tablosync-tve-data` Docker volume contain account-access tokens.
Keep them out of Git, diagnostic bundles, screenshots, and public backups. Never publish
signed playback URLs or FOX API request URLs: some include credentials in their paths.
Xfinity passwords should only be entered on the provider's own sign-in page.
