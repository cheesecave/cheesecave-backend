"""How the service names itself to clients and readers.

``/api/version`` and ``/api/whoami-v2`` tell a client library such as hfutils which kind of hub it
talks to. The ``api`` is ``cheesecave``. Builds before this one said ``kohakuhub`` there, but they
also carried a ``build`` object, which KohakuHub itself never had: ``cheesecave`` is CheeseCave,
``kohakuhub`` with a ``build`` is an older CheeseCave, ``kohakuhub`` without one is KohakuHub.
"""

from importlib import metadata

from kohakuhub.site_branding import get_public_branding

SITE_API = "cheesecave"
PRODUCT_NAME = "CheeseCave"


def service_version() -> str:
    """The version of the installed package, the one place it is written (pyproject.toml)."""
    try:
        return metadata.version("cheesecave-backend")
    except metadata.PackageNotFoundError:
        return "0.0.0+unknown"


SERVICE_VERSION = service_version()


def site_name() -> str:
    """The name the administrator gave the site, else the configured one, else the product's."""
    name = get_public_branding()[0].get("site_name") or ""
    return name.strip() or PRODUCT_NAME
