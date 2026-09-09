# MPT Autopilot — Security Policy

## Supported versions

The project does not use major/minor release branches. Versioning follows
`pyproject.toml`: patch bumps (`x.y.Z`) mark bug fixes, minor bumps (`x.Y.0`)
mark new functionality. Only the latest version on `main` is supported; older
tagged versions do not receive security patches.


## Reporting a vulnerability

**Do not file a public GitHub issue for a security vulnerability.**

Please report it privately via a **GitHub Security Advisory**: open a private
advisory using the "Security" → "Advisories" tab on this repository.

Include as much detail as you can:

- a description of the issue and its potential impact,
- steps to reproduce (or a proof-of-concept),
- affected versions,
- any suggested remediations.

The maintainer will acknowledge receipt within **7 days** and follow up with a
timeline once the report is triaged. Credit is given to the reporter unless
requested otherwise.


## Security considerations for contributors

### Secrets and credentials

MPT Autopilot interacts with three categories of credentials:

1. **LLM API keys** (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.) — passed to
   the MoneyPrinterTurbo API or LLM endpoints.
2. **Telegram bot token** (`TELEGRAM_TOKEN`) — used for optional alerts.
3. **YouTube OAuth credentials** (client secrets JSON + token cache) — used by
   `youtubeuploader`.

These **must never appear in `config.yaml`, in example configs, in tests, or in
version control**. They belong in `.env`, which is gitignored. When adding a new
integration that uses secrets, verify that the secret path flows through
`dotenv` and not through a config key.

### Sensitive files produced at runtime

The uploader stage writes a SQLite dedup ledger (configurable via
`uploader.ledger_path`) and an OAuth token cache. Both contain persistence data
that should be excluded from bundles and backups by design, not just convention.
If you change where the ledger or token are stored, check that the new path is
documented and rotated safely.

### Network security

API and LLM endpoints are configured as URLs in `config.yaml`. URLs over plain
HTTP off loopback produce a one-time warning via
`mpt_autopilot.config.warn_cleartext()` — the warning exists because the LLM
Authorization header (and all request payloads) travel unencrypted. This is a
deliberate, accepted trade-off, not a bug. Do not remove or downgrade the
warning without a clear, documented justification.

### External subprocess

The uploader invokes `youtubeuploader` as an external binary. Its path is set
via `uploader_binary` in `config.yaml` and **nowhere else**. Paths originating
from `jobs.yaml` or any user-controlled source must never resolve to this
setting — if `uploader_binary` were ever read from job data, an attacker could
point it to an arbitrary binary (binary substitution / RCE). Treat the binary
as an untrusted surface regardless: validate its version, pin its download
source, and never pass secrets via shell-constructed arguments.

### Input validation

Jobs consumed by the batch stage come from `jobs.yaml`. Malformed or oversized
payloads are forwarded to the MoneyPrinterTurbo API. Request bodies, topic
strings, and any field that originates in a YAML file should be validated
before submission. When changing the batch stage, ensure inputs remain bounded
and DoS-resistant.

**Path traversal.** `jobs.yaml` can contain path-like fields (BGM file paths,
output directories, video sources, etc.). Every such field must be resolved
through `cfg.resolve()` / `cfg.path_value()` — never through raw string
concatenation with user-supplied segments. Without this, a crafted job entry
like `bgm: "../../../etc/"` escapes the working directory and can read or
overwrite arbitrary files.

### Dependency hygiene

All Python dependencies are pinned via uv's lockfile (`uv.lock`). When
proposing a dependency bump, include the upstream security advisory history
(if any) in your PR description. Do not add new optional dependencies without
justifying their attack surface.

---
_Maintained by [korosu](https://github.com/korosu). Last updated: 2026-06-28._