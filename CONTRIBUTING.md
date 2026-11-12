# Contributing

Thanks for helping. This project is a small, protocol-sensitive daemon; the
most valuable contributions are tests, real-stack reports and careful
compatibility findings.

## Setup

```bash
git clone git@github.com:alexmi256/OrganizArrProxy.git
cd OrganizArrProxy
uv sync --locked
pre-commit install
just check
```

Requirements: Python 3.12+ (uv downloads it), [uv](https://docs.astral.sh/uv/)
and [just](https://github.com/casey/just). Docker is only needed for image
work.

Read [`AGENTS.md`](AGENTS.md) for the invariants and architecture map, and
[`BRIEF.md`](BRIEF.md) for why the design looks the way it does.

## Workflow

1. Open an issue for anything larger than a typo; bugs should include a
   redacted log excerpt and `seedpath preflight --json`.
2. Branch from `main`; keep commits focused and in the imperative mood. A
   Conventional Commits prefix (`fix:`, `feat:`, `docs:`, `test:`, `chore:`)
   is welcome but not required.
3. Add tests with the change. Unit tests for logic, mock-stack integration
   tests for protocol/worker behaviour.
4. Run `just check`. CI runs the same on Python 3.12–3.14.
5. Update docs and `CHANGELOG.md` when behaviour or configuration changes.
6. Open a PR using the template; call out anything that needs a real stack.

## What reviewers look for

- Raw XML-RPC passthrough preserved (no re-encoding of non-load traffic).
- Fail-open paths actually start the torrent and record why.
- The reconciler never touches a seeding torrent.
- Paths remain inside `LIBRARY_ROOTS`.
- No new blocking I/O in async code; timeouts on outbound calls.
- Secrets never logged; tests use obvious dummy keys.

## Testing on a real stack

If you have Radarr/Sonarr + rTorrent, run the acceptance checklist in
[`docs/testing.md`](docs/testing.md) and report the versions you used. This is
currently the biggest gap: the design is implemented and unit-tested, but no
real-stack run is recorded yet.

## Documentation

- `README.md` — pitch, quick start, required settings.
- `docs/` — architecture, configuration, deployment, operations, testing.
- `docs/adr/` — decisions and the framework research.
- `AGENTS.md` — instructions for agents and maintainers.

## Reporting security issues

Do not open a public issue. See [`SECURITY.md`](SECURITY.md).

## Code of conduct

Be decent. See [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).

## Licence

By contributing you agree your changes are licensed under the MIT licence.
