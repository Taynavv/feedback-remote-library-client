# Contributing

Thanks for your interest in Remote Library Client, a
[FeedBack](https://github.com/got-feedback/feedBack) plugin.

## Ground rules

- **License:** contributions are accepted under **AGPL-3.0-or-later** (see
  [LICENSE](LICENSE)). By submitting a change you agree it may be distributed under that
  license.
- **No song content, ever.** Tests and fixtures must be content-free — synthetic packages
  and fake servers only. Never commit real songs, packages, or audio.
- **Keep the load-bearing contracts intact.** See [CLAUDE.md](CLAUDE.md) for the specifics:
  the exact `get_sloppak_cache_dir` cache-callback key, the forgiving base-URL parsing
  ladder, the `slopsmith.nam-tone-sync.v1` schema literal, the `format` → suffix inference
  fallback, and the best-effort / non-fatal NAM-tone-sync path.

## Development setup

```bash
python -m venv .venv
# Activate:  Windows: .venv\Scripts\activate  |  macOS/Linux: source .venv/bin/activate
pip install pytest fastapi httpx ruff
```

## Before you open a pull request

Run the same gates CI runs:

```bash
ruff check .
pytest -q
```

- Add or update tests for any behavior change.
- Match the surrounding style; keep lines within the 120-character limit (ruff enforces
  `E`, `F`, and `I` rules).
- If you cut a release, keep `plugin.json` `version` in sync with the release tag — the
  release workflow fails the build if they disagree. Update `feedback_target` when you
  verify against a new FeedBack version.

## Reporting issues

- **Functional bugs:** open a GitHub issue with reproduction steps and your environment.
- **Security vulnerabilities:** do **not** open a public issue — follow
  [SECURITY.md](SECURITY.md).
