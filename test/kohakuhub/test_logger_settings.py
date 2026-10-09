"""Logger setup on the real file system: a missing log directory is created."""

from __future__ import annotations

import kohakuhub.logger as logger_module
from kohakuhub.config import cfg
from kohakuhub.logger import LoggerFactory


def test_init_logger_settings_creates_a_missing_log_directory(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    assert not log_dir.exists()
    monkeypatch.setattr(cfg.app, "log_dir", str(log_dir))
    # Terminal output only: no file handler is attached to the temporary directory.
    monkeypatch.setattr(cfg.app, "log_format", "terminal")

    try:
        LoggerFactory.init_logger_settings()
        assert log_dir.is_dir()
    finally:
        # Restore the configured sinks, as the module does at import time.
        monkeypatch.undo()
        logger_module.init_logger_settings()
