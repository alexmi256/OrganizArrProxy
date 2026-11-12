# Architecture decision records

Why the project looks the way it does, in the order the decisions were made.

| ADR | Decision |
|---|---|
| [0001](0001-python-framework.md) | Starlette + uvicorn, with stdlib XML-RPC marshalling; why not stdlib `xmlrpc.server`, FastAPI, Flask/Django, aiohttp, Litestar or Twisted |
| [0002](0002-raw-xmlrpc-passthrough.md) | Forward XML-RPC bodies verbatim and patch only the method name |

Template for a new ADR:

```markdown
# ADR NNNN — Title

- Status: proposed | accepted | superseded by ADR NNNN
- Date: YYYY-MM-DD

## Context
## Decision
## Consequences
## Alternatives considered
```
