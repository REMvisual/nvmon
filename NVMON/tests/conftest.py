"""Shared pytest fixtures for nvmon tests.

Exposes:
    fixtures_dir           - absolute path to tests/fixtures/
    nvdd_output_dir        - absolute path to tests/fixtures/nvdd_output/
    load_fixture(name)     - read a fixture file as text
    tmp_config_dir         - isolated tmp dir for config files
    fake_password_file     - tmp file with a test password
    empty_password_file    - tmp file that is empty (no-auth mode)
    missing_password_file  - path to a file that does not exist
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import pytest


TESTS_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = TESTS_DIR / "fixtures"
NVDD_OUTPUT_DIR = FIXTURES_DIR / "nvdd_output"


@pytest.fixture(scope="session", autouse=True)
def _isolate_log_file(tmp_path_factory):
    """Never write test log lines into the live logs/nvmon-<date>.log.

    The tests run from the deployed tree (site: the control box share), and
    nvmon.log.setup() opens config.LOGS_DIR — so test chatter ("server N5
    did not lock — restart Windows on N5") landed in the controller's real
    log on 2026-11-04. Point LOGS_DIR at a temp dir before any logger exists.
    """
    from nvmon import config, log
    saved = config.LOGS_DIR
    config.LOGS_DIR = tmp_path_factory.mktemp("nvmon-test-logs")
    if log._logger is not None:            # already set up by an import: drop its file handler
        import logging
        for h in list(log._logger.handlers):
            if isinstance(h, logging.FileHandler):
                log._logger.removeHandler(h)
                h.close()
    yield
    config.LOGS_DIR = saved


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """Absolute path to tests/fixtures/."""
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def nvdd_output_dir() -> Path:
    """Absolute path to tests/fixtures/nvdd_output/."""
    return NVDD_OUTPUT_DIR


@pytest.fixture(scope="session")
def load_fixture() -> Callable[[str], str]:
    """Return a callable: load_fixture('dd_status.txt') -> str."""
    def _load(name: str) -> str:
        path = NVDD_OUTPUT_DIR / name
        if not path.is_file():
            # Try the fixtures dir directly as a fallback.
            alt = FIXTURES_DIR / name
            if alt.is_file():
                path = alt
            else:
                raise FileNotFoundError(f"fixture not found: {name}")
        return path.read_text(encoding="utf-8")
    return _load


@pytest.fixture
def tmp_config_dir(tmp_path: Path) -> Path:
    """Fresh empty directory for config file round-trips."""
    d = tmp_path / "nvmon_cfg"
    d.mkdir()
    return d


@pytest.fixture
def fake_password_file(tmp_path: Path) -> Path:
    """Password file containing a known test password."""
    p = tmp_path / "controller.password"
    p.write_text("test-password-123\n", encoding="utf-8")
    return p


@pytest.fixture
def empty_password_file(tmp_path: Path) -> Path:
    """Empty password file - should trigger no-auth mode."""
    p = tmp_path / "empty.password"
    p.write_text("", encoding="utf-8")
    return p


@pytest.fixture
def missing_password_file(tmp_path: Path) -> Path:
    """Path to a file that does not exist."""
    return tmp_path / "missing.password"


@pytest.fixture
def smoke_url() -> str | None:
    """Return NVMON_SMOKE_URL env var if set, else None.

    Smoke tests should call `pytest.skip(...)` when this is None.
    """
    return os.environ.get("NVMON_SMOKE_URL")
