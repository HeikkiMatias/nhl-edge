"""Hard rule 1: every feature module has its own point-in-time test in tests/leakage/."""

from pathlib import Path

import nhl_edge.features

FEATURES_DIR = Path(nhl_edge.features.__file__).parent
LEAKAGE_DIR = Path(__file__).parent


def test_every_feature_module_has_a_leakage_test() -> None:
    modules = sorted(p.stem for p in FEATURES_DIR.glob("*.py") if p.stem != "__init__")
    missing = [m for m in modules if not (LEAKAGE_DIR / f"test_{m}.py").exists()]
    assert not missing, f"feature modules without a tests/leakage/test_<module>.py: {missing}"
