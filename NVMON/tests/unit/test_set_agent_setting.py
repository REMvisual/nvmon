"""Unit tests for nvmon.config.set_agent_setting.

This is the helper install.bat invokes when --controller URL is passed,
so a regression here breaks zero-touch listener bootstrap. Covered:

  * Writes a fresh nvmon.toml when none exists.
  * Replaces an existing key inside [agent] without disturbing other sections.
  * Appends a new key inside an existing [agent] block.
  * Idempotent re-runs.
  * Refuses keys with non-identifier chars.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nvmon import config as cfgmod


@pytest.fixture
def temp_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect cfgmod.CONFIG_FILE at a tmp file for this test."""
    target = tmp_path / "nvmon.toml"
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", target)
    return target


def test_writes_fresh_file_when_missing(temp_config: Path) -> None:
    assert not temp_config.exists()
    cfgmod.set_agent_setting("controller_url", "http://mon:9997")
    text = temp_config.read_text(encoding="utf-8")
    assert "[agent]" in text
    assert 'controller_url = "http://mon:9997"' in text


def test_replaces_existing_key_in_agent_section(temp_config: Path) -> None:
    temp_config.write_text(
        "[agent]\n"
        'controller_url = "http://old:9997"\n'
        "port = 9998\n"
        "\n"
        "[controller]\n"
        "port = 9997\n",
        encoding="utf-8",
    )
    cfgmod.set_agent_setting("controller_url", "http://new:9997")
    text = temp_config.read_text(encoding="utf-8")
    assert 'controller_url = "http://new:9997"' in text
    assert "http://old:9997" not in text
    # Other [agent] keys preserved.
    assert "port = 9998" in text
    # Other section preserved verbatim.
    assert "[controller]" in text


def test_appends_new_key_to_existing_agent_section(temp_config: Path) -> None:
    temp_config.write_text(
        "[agent]\n"
        "port = 9998\n"
        "\n"
        "[controller]\n"
        "port = 9997\n",
        encoding="utf-8",
    )
    cfgmod.set_agent_setting("controller_url", "http://mon:9997")
    text = temp_config.read_text(encoding="utf-8")
    assert 'controller_url = "http://mon:9997"' in text
    # Appended INSIDE [agent], BEFORE the next section header.
    agent_pos = text.index("[agent]")
    new_pos = text.index("controller_url")
    ctrl_pos = text.index("[controller]")
    assert agent_pos < new_pos < ctrl_pos


def test_appends_section_when_no_agent_block(temp_config: Path) -> None:
    temp_config.write_text(
        "[controller]\n"
        "port = 9997\n",
        encoding="utf-8",
    )
    cfgmod.set_agent_setting("controller_url", "http://mon:9997")
    text = temp_config.read_text(encoding="utf-8")
    assert "[agent]" in text
    assert 'controller_url = "http://mon:9997"' in text
    # Original content preserved.
    assert "[controller]" in text


def test_idempotent_round_trip(temp_config: Path) -> None:
    cfgmod.set_agent_setting("controller_url", "http://mon:9997")
    first = temp_config.read_text(encoding="utf-8")
    cfgmod.set_agent_setting("controller_url", "http://mon:9997")
    second = temp_config.read_text(encoding="utf-8")
    assert first == second


def test_rejects_non_identifier_keys(temp_config: Path) -> None:
    with pytest.raises(ValueError):
        cfgmod.set_agent_setting("[evil] = 1\nbad", "x")


def test_escapes_quotes_and_backslashes(temp_config: Path) -> None:
    cfgmod.set_agent_setting("controller_url", 'http://"weird"\\path')
    text = temp_config.read_text(encoding="utf-8")
    assert 'controller_url = "http://\\"weird\\"\\\\path"' in text


# ---------------------------------------------------------------------------
# monitor.url fallback — zero-touch registration target
# ---------------------------------------------------------------------------


@pytest.fixture
def monitor_url_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect MONITOR_URL_FILE at a tmp file and force toml reload between tests."""
    target = tmp_path / "monitor.url"
    monkeypatch.setattr(cfgmod, "MONITOR_URL_FILE", target)
    # Also redirect CONFIG_FILE so the toml side stays empty/clean for these tests.
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", tmp_path / "nvmon.toml")
    cfgmod.load_config(force_reload=True)
    return target


def test_monitor_url_fallback_used_when_toml_silent(monitor_url_file: Path) -> None:
    monitor_url_file.write_text("http://10.0.0.10:9997\n", encoding="utf-8")
    cfgmod.load_config(force_reload=True)
    cfg = cfgmod.get_agent_config()
    assert cfg["controller_url"] == "http://10.0.0.10:9997"


def test_monitor_url_skips_comments_and_blanks(monitor_url_file: Path) -> None:
    monitor_url_file.write_text(
        "# leading comment\n"
        "\n"
        "   # indented comment\n"
        "http://mon:9997\n"
        "http://other:9997\n",  # later lines ignored
        encoding="utf-8",
    )
    cfgmod.load_config(force_reload=True)
    cfg = cfgmod.get_agent_config()
    assert cfg["controller_url"] == "http://mon:9997"


def test_missing_monitor_url_file_leaves_controller_url_empty(monitor_url_file: Path) -> None:
    # File never created.
    cfgmod.load_config(force_reload=True)
    cfg = cfgmod.get_agent_config()
    assert cfg["controller_url"] == ""


def test_blank_monitor_url_file_leaves_controller_url_empty(monitor_url_file: Path) -> None:
    monitor_url_file.write_text("# only a comment\n\n  \n", encoding="utf-8")
    cfgmod.load_config(force_reload=True)
    cfg = cfgmod.get_agent_config()
    assert cfg["controller_url"] == ""


def test_toml_value_wins_over_monitor_url_file(monitor_url_file: Path) -> None:
    """If both are set, the explicit toml entry wins — operator override."""
    monitor_url_file.write_text("http://fallback:9997\n", encoding="utf-8")
    cfgmod.CONFIG_FILE.write_text(
        '[agent]\ncontroller_url = "http://explicit:9997"\n',
        encoding="utf-8",
    )
    cfgmod.load_config(force_reload=True)
    cfg = cfgmod.get_agent_config()
    assert cfg["controller_url"] == "http://explicit:9997"
