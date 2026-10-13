"""The optional container check fails clearly without modifying a bundle."""
from types import SimpleNamespace
from pathlib import Path
from uuid import uuid4

from scripts import check_model_artifacts


def test_unconfigured_bundle(monkeypatch, capsys):
    monkeypatch.setattr(check_model_artifacts, "get_settings",
                        lambda: SimpleNamespace(fraud_model_artifact_dir=None))
    assert check_model_artifacts.main() == 1
    assert "Set FRAUD_MODEL_ARTIFACT_DIR" in capsys.readouterr().out


def test_missing_bundle(monkeypatch, capsys):
    missing = Path(__file__).parent / uuid4().hex
    monkeypatch.setattr(check_model_artifacts, "get_settings",
                        lambda: SimpleNamespace(fraud_model_artifact_dir=str(missing)))
    assert check_model_artifacts.main() == 1
    assert "missing or unreadable" in capsys.readouterr().out
    assert not missing.exists()
