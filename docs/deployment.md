# CheeseCave deployment architecture

CheeseCave has three independently released repositories:

| Repository | Ownership |
| --- | --- |
| cheesecave-backend | FastAPI, Git/LFS/HF APIs, worker, models, migrations, gateway and infrastructure Compose |
| cheesecave-web | Public Vue UI, its tests and static web image |
| cheesecave-admin | Admin Vue UI, its tests and static admin image |

The backend Compose deploys separate web/admin images, proxies their original
paths, and runs API plus worker on the same backend image. An update to one UI
uses native `docker compose up -d --no-deps` for just that service. See
[Docker deployment](deployment/docker.md) for configuration, source builds,
independent image updates, rollback and preserved storage identities.

The API identity remains `kohakuhub` for downstream client detection; the
package distribution is `cheesecave-backend`. Site name defaults to CheeseCave,
while existing config and stored branding overrides remain authoritative.
There is no CLI package in this backend snapshot; CLI documentation inherited
from upstream does not imply a packaged `cheesecave-cli` command.

Initial split releases share the recorded upstream baseline. Subsequent API
changes need explicit consumer compatibility checks against web/admin releases
and the existing huggingface_hub matrix. `/api/version` reports backend build
identity, not an automatic frontend capability negotiation protocol.
