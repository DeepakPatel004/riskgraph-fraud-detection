"""Explicit read-only validation of a trusted model bundle; never trains a model."""
from pathlib import Path

from app.core.config import get_settings


def main() -> int:
    configured = get_settings().fraud_model_artifact_dir
    if not configured:
        print("Set FRAUD_MODEL_ARTIFACT_DIR to a trusted readable bundle directory.")
        return 1
    directory = Path(configured)
    required = ("manifest.json", "configuration.json", "schema.json", "metrics.json",
                "model.ubj", "preprocessing.joblib")
    try:
        for name in required:
            with (directory / name).open("rb") as stream:
                stream.read(1)
    except OSError:
        print("Model bundle is missing or unreadable; check the bind mount and permissions.")
        return 1
    try:
        from app.services.fraud_inference_service import get_fraud_inference_service
        get_fraud_inference_service(directory)
    except ImportError:
        print("Model runtime dependencies are unavailable; build with docker-compose.model.yml.")
        return 1
    except Exception:
        print("Model validation failed; check bundle integrity and saved dependency versions.")
        return 1
    print("Model bundle loaded and validated. Scoring and Neptune remain separately gated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
