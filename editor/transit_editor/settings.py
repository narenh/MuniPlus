"""Every knob, read from the environment (Coolify) or ``editor/.env`` locally.

Names map to env vars case-insensitively: ``data_repo`` is ``DATA_REPO``.
See ``.env.example`` for what each one means.
"""

from functools import cached_property
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Empty means unset, so a blank line copied from .env.example keeps the default.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    operators: list[str] = ["SF"]
    """511 operator codes to load snapshots for and refresh."""

    relay_url: str = "https://transit-relay.canopysf.com"
    """The Canopy Transit Relay, which holds the 511 key. A snapshot refresh
    downloads ``/static/datafeeds`` and ``/static/lines`` from it."""

    data_dir: Path = Path("/data")
    data_repo: str = "git@github.com:narenh/canopy-transit-editor.git"
    data_branch: str = "static-data"
    data_deploy_key: str = ""
    git_author_name: str = ""
    git_author_email: str = ""

    editor_password: str = ""
    """Empty disables login entirely: / then serves the editor read-only."""
    session_secret: str = ""

    @cached_property
    def transit_dir(self) -> Path:
        """The data repo's checkout."""
        return self.data_dir / "data"
