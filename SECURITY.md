# Security Policy

## Supported versions

Only the latest release of `kb` is supported.

## Reporting a vulnerability

Please report vulnerabilities privately using GitHub's "Report a vulnerability" button on the repository's Security tab. Do not open a public issue.

## Security posture

- `kb` is read-only: it never writes outside its data directory.
- It makes one-time downloads from only two hosts: `https://support.apple.com` and `https://code.visualstudio.com`. Set `KB_NO_AUTO_SYNC=1` to disable auto-sync.
- It has no runtime dependencies (Python standard library only).
