"""Suite-wide guards."""

from app.settings import Settings

# Settings() reads .env from the working directory, and tests run from api/, where
# .env holds the real 511 key. No test may ever see it: a test that forgot
# ``_env_file=None`` would otherwise be one bug away from spending the key.
Settings.model_config["env_file"] = None
