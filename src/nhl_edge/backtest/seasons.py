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
OPEN_SEASONS: tuple[int, ...] = tuple(
    season for season, role in SEASON_ROLES.items() if role in OPEN_ROLES
)

# The roles the hockey-only mode may score: gate 2 opens the hockey validation seasons for B2
# against B3 on outcomes (#107). 2022-23 waits for phase 4 (#66), and 2025-26 for the owner's
# go-ahead.
HOCKEY_ROLES = OPEN_ROLES | {SeasonRole.HOCKEY_VALIDATION}
# Gate 2's one-time hockey-only test (docs/plan.md section 5): run once, on the owner's go-ahead.
ONE_TIME_SEASONS: tuple[int, ...] = tuple(
    season for season, role in SEASON_ROLES.items() if role is SeasonRole.ONE_TIME_TEST
)

# The seasons a phase 2 design choice may rest on: training seasons without the bubble and empty
# arenas, so the development seasons stay unseen until gate 1 (phase 2 plan, #11).
TRAINING_SEASONS: tuple[int, ...] = tuple(
    season for season, role in SEASON_ROLES.items() if role is SeasonRole.TRAINING
)

DEVELOPMENT_SEASONS: tuple[int, ...] = tuple(
    season for season, role in SEASON_ROLES.items() if role is SeasonRole.DEVELOPMENT
)


# The first season with out-of-sample B2 and B3 predictions: its fold is the first to start after
# the tuning cutoff (ADR 0011, 2018-04-09), and every earlier fold is refused. The market blend
# learns only from such predictions of earlier folds (hard rule 6, #138).
FIRST_OUT_OF_SAMPLE_SEASON = 20182019


def blend_training_seasons(season: int) -> list[int]:
    """The seasons whose out-of-sample predictions the market blend of season learns from: the
    open seasons from FIRST_OUT_OF_SAMPLE_SEASON up to the season before it (the whole-season
    folds of the phase 4 plan, #13). None for the first out-of-sample season, which has no
    earlier fold. The flagged seasons are training seasons, so they teach the blend too."""
    return [s for s in OPEN_SEASONS if FIRST_OUT_OF_SAMPLE_SEASON <= s < season]


def season_role(season: int) -> SeasonRole:
    """Role of a season given as 20252026. Seasons from 2026-27 on are live."""
    if season >= FIRST_LIVE_SEASON:
        return SeasonRole.LIVE
    try:
        return SEASON_ROLES[season]
    except KeyError:
        raise ValueError(f"season {season} is outside the model's history") from None
