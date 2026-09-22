"""Every knob, read from the environment (Coolify) or ``api/.env`` locally.

Names map to env vars case-insensitively: ``api_511_key`` is ``API_511_KEY``.
See ``.env.example`` for what each one means.
"""

from functools import cached_property
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_511_key: str = ""
    """Empty means no 511 calls at all: the server serves data and fixtures only."""
    fixtures: bool = False
    """Replay ``tests/fixtures/realtime`` instead of polling 511."""

    poll_arrivals_seconds: int = 180
    poll_vehicles_seconds: int = 180
    poll_alerts_seconds: int = 1200
    budget_per_hour: int = 55
    """Hard ceiling on 511 calls in any rolling hour, below the dev key's 60."""
    operators: list[str] = ["SF"]
    """511 operator codes to poll and load snapshots for."""

    data_dir: Path = Path("/data")
    data_repo: str = "git@github.com:narenh/sf-transit.git"
    data_branch: str = "main"
    data_deploy_key: str = ""
    git_author_name: str = ""
    git_author_email: str = ""

    editor_password: str = ""
    """Empty disables login entirely: /editor then serves read-only."""
    session_secret: str = ""

    @cached_property
    def transit_dir(self) -> Path:
        """The sf-transit checkout."""
        return self.data_dir / "sf-transit"

    @cached_property
    def db_path(self) -> Path:
        return self.data_dir / "muni.db"

    @property
    def polling_enabled(self) -> bool:
        return self.fixtures or bool(self.api_511_key)
