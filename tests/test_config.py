"""Config loading and validation. A bad config must fail at startup, loudly."""

from __future__ import annotations

import json

import pytest

from agent.config import AgentConfig, ConfigError, load_config


def test_the_shipped_config_loads(config):
    assert config.underlyings
    assert config.max_abs_delta > 0


def test_thresholds_are_exposed_with_unambiguous_units(config):
    assert config.max_loss_pct == 0.02
    assert config.min_credit_usd == 25.0
    assert config.min_dte == 7
    assert config.max_dte == 14
    assert config.kill_switch_pct == 0.05
    assert config.min_bp_reserve_pct == 0.20


def test_delta_range_is_ordered_low_to_high(config):
    low, high = config.delta_range
    assert low < high < 0


def test_spread_widths_are_ordered(config):
    low, high = config.spread_widths
    assert 0 < low <= high


def test_loading_without_credentials_works_offline(config):
    """The pure-computation paths must not require API keys."""
    assert config.credentials is None


def test_a_missing_risk_key_is_rejected(tmp_path, options_config, risk_config):
    broken = {k: v for k, v in risk_config.items() if k != "delta"}
    with pytest.raises(ConfigError, match="missing required key"):
        from agent.config import _validate, _REQUIRED_RISK_KEYS

        _validate(broken, _REQUIRED_RISK_KEYS, "risk_config.json")


def test_an_inverted_dte_window_is_rejected(monkeypatch, tmp_path, risk_config, options_config):
    risk_config["dte"]["min_days"] = 30
    _write_and_expect(monkeypatch, tmp_path, risk_config, options_config, "min_days cannot exceed")


def test_a_nonsensical_loss_percentage_is_rejected(monkeypatch, tmp_path, risk_config, options_config):
    risk_config["notional"]["max_loss_pct"] = 5.0  # 500%
    _write_and_expect(monkeypatch, tmp_path, risk_config, options_config, "fraction between 0 and 1")


def test_a_nonsensical_kill_switch_is_rejected(monkeypatch, tmp_path, risk_config, options_config):
    risk_config["daily_loss"]["kill_switch_pct"] = 0.0
    _write_and_expect(monkeypatch, tmp_path, risk_config, options_config, "fraction between 0 and 1")


def test_an_inverted_delta_range_is_rejected(monkeypatch, tmp_path, risk_config, options_config):
    options_config["spread_builder"]["delta_range"] = [-0.15, -0.20]
    _write_and_expect(monkeypatch, tmp_path, risk_config, options_config, "must be \\[low, high\\]")


VALID_AGENT_CONFIG = {
    "cycle_interval_seconds": 300,
    "max_turns_per_ticker": 8,
    "cycle_timeout_seconds": 240,
    "llm": {"provider": "openrouter", "model": "test-model"},
    "chain": {"feed": "indicative", "strike_bracket_pct": 0.15, "limit": 1000},
}


def _write_and_expect(monkeypatch, tmp_path, risk, options, message):
    """Point the loader at a temp config directory holding a broken config.

    The agent config is written valid every time: these cases assert on the
    risk and options files, and a missing third file would mask the error each
    one is actually testing for.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "risk_config.json").write_text(json.dumps(risk))
    (config_dir / "options_config.json").write_text(json.dumps(options))
    (config_dir / "agent_config.json").write_text(json.dumps(VALID_AGENT_CONFIG))

    import agent.config as config_module

    monkeypatch.setattr(config_module, "CONFIG_DIR", config_dir)
    with pytest.raises(ConfigError, match=message):
        config_module.load_config(with_credentials=False)


def test_a_missing_config_file_is_reported_clearly(monkeypatch, tmp_path):
    import agent.config as config_module

    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path / "nowhere")
    with pytest.raises(ConfigError, match="not found"):
        config_module.load_config(with_credentials=False)


def test_malformed_json_is_reported_clearly(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "risk_config.json").write_text("{not json")

    import agent.config as config_module

    monkeypatch.setattr(config_module, "CONFIG_DIR", config_dir)
    with pytest.raises(ConfigError, match="not valid JSON"):
        config_module.load_config(with_credentials=False)
