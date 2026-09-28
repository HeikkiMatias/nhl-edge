"""Hard rule 1: every feature module has its own point-in-time test in tests/leakage/.

A top-level module features/rest.py needs test_rest.py. A subpackage such as features/xg/ counts as
one unit, however deeply it nests, and needs test_xg.py.
"""

from pathlib import Path

import nhl_edge.features

FEATURES_DIR = Path(nhl_edge.features.__file__).parent
LEAKAGE_DIR = Path(__file__).parent


def feature_units(features_dir: Path) -> list[str]:
    modules = {p.stem for p in features_dir.glob("*.py") if p.stem != "__init__"}
    packages = {
        p.name
        for p in features_dir.iterdir()
        if p.is_dir() and p.name != "__pycache__" and any(p.rglob("*.py"))
    }
    return sorted(modules | packages)


def missing_leakage_tests(features_dir: Path, leakage_dir: Path) -> list[str]:
    return [
        unit
        for unit in feature_units(features_dir)
        if not (leakage_dir / f"test_{unit}.py").exists()
    ]


def test_every_feature_module_has_a_leakage_test() -> None:
    missing = missing_leakage_tests(FEATURES_DIR, LEAKAGE_DIR)
    assert not missing, f"feature units without a tests/leakage/test_<unit>.py: {missing}"


def test_nested_feature_packages_are_found(tmp_path: Path) -> None:
    features = tmp_path / "features"
    (features / "xg" / "models").mkdir(parents=True)
    (features / "__init__.py").touch()
    (features / "rest.py").touch()
    (features / "xg" / "models" / "shot_model.py").touch()
    (features / "__pycache__").mkdir()
    leakage = tmp_path / "leakage"
    leakage.mkdir()
    (leakage / "test_rest.py").touch()

    assert feature_units(features) == ["rest", "xg"]
    assert missing_leakage_tests(features, leakage) == ["xg"]
