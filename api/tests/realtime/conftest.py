"""Fixtures for the realtime tests; helpers live in ``rt_support``, which has a
name no other track's tests will reuse."""

import pytest
from rt_support import FakeNetwork, make_settings

from app.db import Database
from app.realtime.state import Realtime


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def db(settings):
    database = Database(settings.db_path)
    yield database
    database.close()


@pytest.fixture
def network():
    return FakeNetwork()


@pytest.fixture
def realtime(settings, db, network):
    return Realtime(lambda: network, settings, db=db)
