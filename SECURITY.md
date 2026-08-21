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
