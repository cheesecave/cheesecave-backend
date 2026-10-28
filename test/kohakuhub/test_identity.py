"""How the service names itself: the identity a client such as hfutils reads from /api/version."""

from importlib import metadata

import pytest

from kohakuhub import identity
from kohakuhub.config import cfg


def test_the_service_calls_itself_cheesecave():
    assert identity.SITE_API == "cheesecave"
    assert identity.PRODUCT_NAME == "CheeseCave"


def test_the_version_is_the_installed_package_version():
    assert identity.service_version() == metadata.version("cheesecave-backend")
    assert identity.SERVICE_VERSION == identity.service_version()


def test_a_missing_package_record_is_a_version_that_says_so(monkeypatch):
    def missing(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(identity.metadata, "version", missing)

    assert identity.service_version() == "0.0.0+unknown"


def test_the_site_name_is_the_configured_one(monkeypatch, db_fresh):
    monkeypatch.setattr(cfg.app, "site_name", "Configured Hub")

    assert identity.site_name() == "Configured Hub"


def test_the_site_name_follows_what_the_administrator_set(monkeypatch, db_fresh):
    from kohakuhub.site_branding import update_branding

    monkeypatch.setattr(cfg.app, "site_name", "Configured Hub")
    update_branding({"site_name": "Example Hub"})

    assert identity.site_name() == "Example Hub"


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_site_name_is_the_product_name(monkeypatch, blank):
    monkeypatch.setattr(identity, "get_public_branding", lambda: ({"site_name": blank}, False))

    assert identity.site_name() == "CheeseCave"


def test_the_site_name_survives_an_unavailable_database(monkeypatch):
    from peewee import OperationalError

    def broken():
        raise OperationalError("down")

    monkeypatch.setattr(cfg.app, "site_name", "Configured Hub")
    monkeypatch.setattr("kohakuhub.site_branding.get_branding", broken)

    assert identity.site_name() == "Configured Hub"
