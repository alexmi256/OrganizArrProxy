# Security policy

## Supported versions

| version | supported |
|---|---|
| 0.x (`main`) | best effort, pre-alpha |

No version is production-ready until the real-stack acceptance checklist in
[`docs/testing.md`](docs/testing.md) has been completed and recorded.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting:
<https://github.com/alexmi256/OrganizArrProxy/security/advisories/new>

Do not open a public issue. Include:

- affected version/image digest,
- a minimal reproduction (redact API keys, passwords, magnet links and
  tracker passkeys),
- impact: credential exposure, path escape, remote code execution, denial of
  service, etc.

We aim to acknowledge within 7 days and to publish an advisory with a fix
once users have a reasonable upgrade path.

## Threat model and deployment assumptions

`seedpath` is designed for an **internal Docker network**. It is not hardened
as a public internet service.

- It holds *arr API keys and rTorrent Basic-auth credentials.
- The XML-RPC ingress accepts unauthenticated traffic only when `RT_USER` is
  empty or `INGRESS_AUTH=false`. Leave auth on where possible.
- Do not publish its port to the host or the internet. If you must expose it,
  put an authenticating reverse proxy in front and restrict source networks.
- XML parsing uses the standard library's `xmlrpc.client`. The project rejects
  `DOCTYPE`/`ENTITY` declarations and caps body size, but stdlib XML parsing is
  not a general-purpose hardened stack. Treat ingress as trusted-network only.
- The container runs as a non-root user with a read-only root filesystem and
  no capabilities. Only `/config` is writable.
- `LIBRARY_ROOTS` is a lexical guard against placing outside the library; it
  is not a filesystem sandbox (seedpath never mounts the media volume).

## Secret handling

- Provide secrets through environment variables or Docker secrets, never
  command-line arguments.
- `seedpath config` redacts `SecretStr` values.
- Logs contain infohashes, release names and library paths; treat them as
  sensitive and ship them to access-controlled storage.
- Rotate API keys if a container image, log bundle or compose file is shared.

## Dependency and supply-chain posture

- `uv.lock` is committed; CI installs with `--locked`.
- Dependabot watches Python packages, GitHub Actions and the Docker base image.
- Images are built with provenance and SBOM attestations and signed by
  GitHub's attestation service in the publish workflow.
- `pip-audit` is available via `just audit`.
