"""Fixtures for the editor tests; the machinery is in ``editor_world``."""

from pathlib import Path

import pytest

from editor_world import GIT_ENV, World, make_origin, make_world


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch):
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def origin(tmp_path) -> tuple[Path, Path]:
    return make_origin(tmp_path)


@pytest.fixture
def world(tmp_path, origin) -> World:
    return make_world(tmp_path, origin)


@pytest.fixture
def make(tmp_path, origin):
    return lambda **overrides: make_world(tmp_path, origin, **overrides)
