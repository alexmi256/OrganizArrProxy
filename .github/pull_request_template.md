## Summary

<!-- What does this change and why? Link the issue or brief section. -->

## Checklist

- [ ] `just check` passes locally (ruff format, ruff, mypy, pytest)
- [ ] New behaviour has tests; bug fixes have a regression test
- [ ] `uv.lock` is updated if dependencies changed
- [ ] Docs updated (`README.md`, `docs/`, `AGENTS.md`) if behaviour or configuration changed
- [ ] No secrets, API keys, magnet links or library paths with personal data in logs/tests
- [ ] Changes respect the invariants in `AGENTS.md` (raw passthrough, no filesystem writes, never retarget a seeding torrent)

## Notes for the reviewer

<!-- Anything risky, intentionally deferred, or worth verifying on a real stack. -->
