# weewx-sftp

WeeWX report generator that uploads a rendered report to a server over SFTP using Paramiko. Single module: `bin/user/sftp.py` (`SFTPGenerator` for WeeWX, `SFTPUploader` for the work and the command line). `install.py` is the WeeWX extension installer; `skins/sftp/skin.conf` holds the defaults it installs.

## Priorities (in this order when they conflict)
1. **Security.** This code reads a report directory and writes to a remote server with the user's credentials. Treat everything in the report directory as untrusted input (e.g. the cache is JSON, never pickle; symlinks leaving the report directory are skipped unless `allow_external_symlinks`). Host-key verification stays opt-in only for backward compatibility; don't weaken any other default.
2. **Privacy.** Never log, print or store passwords, passphrases or key material. Secrets may come from `weewx.conf` or from environment variables (`password_env`, `private_key_pass_env`); keep it that way. Don't add telemetry or network calls other than the SFTP transfer.
3. **WeeWX compatibility.** Support WeeWX 3.5.1 through 5.x and Python 3.9+ (CI tests both ends). No newer syntax or WeeWX-only APIs without a fallback. Keep existing option names, defaults and destination paths working.
4. **Simplicity.** No new dependencies (runtime needs only Paramiko and WeeWX), no new abstractions, no refactors beyond the task. Match the surrounding upstream style (printf-style logging, `class Foo(object)`); ruff is configured to allow it.

A security fix may break compatibility, but it must be called out in the README upgrade notes and the changelog.

## Build and test
```bash
pip install -r requirements-dev.txt
ruff check .
pytest -q
python tests/integration_sftp.py   # real SSH/SFTP; needs weewx and paramiko installed
```
The integration test runs separately from pytest on purpose, because its Paramiko fake would leak into the unit tests. CI runs everything on every PR; read the run before calling work done.

## Rules for changes
- New behavior gets a test in `tests/`.
- A new option goes in the README option table and in `skins/sftp/skin.conf` with a safe default.
- A change to defaults, cache format or failure handling gets a README upgrade note and a changelog line.
- Releases: the version must match in `VERSION` (`bin/user/sftp.py`), `version=` (`install.py`) and the first line of `changelog`. `release.yml` rejects the tag otherwise. Changelog format is `X.Y.Z DDmonYYYY` followed by `* ` bullets.

## Workflow
Remote is GitHub (`ziti/weewx-sftp`). Never push to `main`; work on `claude/<topic>` and open a PR with `gh pr create`.
