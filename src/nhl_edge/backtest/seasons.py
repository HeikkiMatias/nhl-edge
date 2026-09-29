"""Season roles from docs/plan.md section 5: which seasons may influence design decisions."""

from enum import StrEnum


class SeasonRole(StrEnum):
    TRAINING = "training"
    TRAINING_FLAGGED = "training_flagged"  # bubble and empty arenas
    DEVELOPMENT = "development"  # tuning plus market experiments E1 and E2
    MARKET_VALIDATION = "market_validation"  # counts as development once inspected
    HOCKEY_VALIDATION = "hockey_validation"  # B2 versus B3 on outcomes, no free odds
    ONE_TIME_TEST = "one_time_test"  # hockey-only test at gate 2, used once
    LIVE = "live"  # the only untouched market test


FIRST_SEASON = 20102011
FIRST_LIVE_SEASON = 20262027

SEASON_ROLES: dict[int, SeasonRole] = {
    20102011: SeasonRole.TRAINING,
    20112012: SeasonRole.TRAINING,
    20122013: SeasonRole.TRAINING,
    20132014: SeasonRole.TRAINING,
    20142015: SeasonRole.TRAINING,
    20152016: SeasonRole.TRAINING,
    20162017: SeasonRole.TRAINING,
    20172018: SeasonRole.TRAINING,
    20182019: SeasonRole.DEVELOPMENT,
    20192020: SeasonRole.TRAINING_FLAGGED,
    20202021: SeasonRole.TRAINING_FLAGGED,
    20212022: SeasonRole.DEVELOPMENT,
    20222023: SeasonRole.MARKET_VALIDATION,
    20232024: SeasonRole.HOCKEY_VALIDATION,
    20242025: SeasonRole.HOCKEY_VALIDATION,
    20252026: SeasonRole.ONE_TIME_TEST,
}

# The roles whose seasons may be inspected now: held-out seasons wait for their phase (#10).
OPEN_ROLES = frozenset({SeasonRole.TRAINING, SeasonRole.TRAINING_FLAGGED, SeasonRole.DEVELOPMENT})

DEVELOPMENT_SEASONS: tuple[int, ...] = tuple(
    season for season, role in SEASON_ROLES.items() if role is SeasonRole.DEVELOPMENT
)


def season_role(season: int) -> SeasonRole:
    """Role of a season given as 20252026. Seasons from 2026-27 on are live."""
    if season >= FIRST_LIVE_SEASON:
        return SeasonRole.LIVE
    try:
        return SEASON_ROLES[season]
    except KeyError:
        raise ValueError(f"season {season} is outside the model's history") from None
