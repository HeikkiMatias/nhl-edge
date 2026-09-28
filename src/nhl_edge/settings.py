"""Environment settings. Secrets come from the environment, or from .env on a laptop."""

import os

from dotenv import find_dotenv, load_dotenv


class MissingSettingError(RuntimeError):
    """A required environment variable is not set. The message names it, never a value."""


def load_env() -> None:
    """Load .env from the working directory upward. Variables already set win."""
    load_dotenv(find_dotenv(usecwd=True), override=False)


def optional(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def require(name: str) -> str:
    value = optional(name)
    if value is None:
        raise MissingSettingError(f"{name} is not set. Add it to .env locally or to the workflow.")
    return value
