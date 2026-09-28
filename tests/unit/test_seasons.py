import pytest

from nhl_edge.backtest.seasons import (
    DEVELOPMENT_SEASONS,
    FIRST_LIVE_SEASON,
    FIRST_SEASON,
    SEASON_ROLES,
    SeasonRole,
    season_role,
)


def test_every_history_season_has_one_role() -> None:
    expected = [FIRST_SEASON + 10001 * i for i in range(16)]
    assert list(SEASON_ROLES) == expected
    assert expected[-1] + 10001 == FIRST_LIVE_SEASON


def test_roles_match_plan_section_5() -> None:
    assert DEVELOPMENT_SEASONS == (20182019, 20212022)
    assert season_role(20172018) is SeasonRole.TRAINING
    assert season_role(20192020) is SeasonRole.TRAINING_FLAGGED
    assert season_role(20202021) is SeasonRole.TRAINING_FLAGGED
    assert season_role(20222023) is SeasonRole.MARKET_VALIDATION
    assert season_role(20232024) is SeasonRole.HOCKEY_VALIDATION
    assert season_role(20242025) is SeasonRole.HOCKEY_VALIDATION
    assert season_role(20252026) is SeasonRole.ONE_TIME_TEST


def test_live_from_2026_27() -> None:
    assert season_role(20262027) is SeasonRole.LIVE
    assert season_role(20302031) is SeasonRole.LIVE


def test_unknown_season_raises() -> None:
    with pytest.raises(ValueError):
        season_role(20092010)
