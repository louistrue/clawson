"""Tests for the clawson config loader."""

import os
from pathlib import Path

import pytest

from reachy_mini_openclaw.clawson_config import ClawsonConfig, load_clawson_config


def test_loads_token_from_toml(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[github]\ntoken = "ghp_from_toml"\n')
    cfg = load_clawson_config(cfg_file)
    assert cfg.github_token == "ghp_from_toml"
    assert cfg.github_enabled is True


def test_env_overrides_toml(tmp_path: Path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[github]\ntoken = "ghp_toml"\n')
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_env")
    cfg = load_clawson_config(cfg_file)
    assert cfg.github_token == "ghp_env"


def test_missing_file_returns_empty_config(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    cfg = load_clawson_config(tmp_path / "nope.toml")
    assert cfg.github_token is None
    assert cfg.github_enabled is False


def test_malformed_toml_falls_back_to_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_env")
    bad = tmp_path / "config.toml"
    bad.write_text("][")
    cfg = load_clawson_config(bad)
    assert cfg.github_token == "ghp_env"


def test_loads_vercel_token_from_toml(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[vercel]\ntoken = "vrc_from_toml"\n')
    cfg = load_clawson_config(cfg_file)
    assert cfg.vercel_token == "vrc_from_toml"
    assert cfg.vercel_enabled is True


def test_vercel_env_overrides_toml(tmp_path: Path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[vercel]\ntoken = "vrc_toml"\n')
    monkeypatch.setenv("VERCEL_TOKEN", "vrc_env")
    cfg = load_clawson_config(cfg_file)
    assert cfg.vercel_token == "vrc_env"
