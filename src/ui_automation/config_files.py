"""Reading the YAML files in config/: app profiles and the safety policy.

Config files may refer to deployment values as ${NAME}, for example ${BANK_URL}. They are
filled in from settings when the file is read. Lower-case references (${member_number})
and secret references (${secret:NAME}) are left for replay to resolve.
"""

import re
from pathlib import Path

import yaml

from .models.app_profile import AppProfile
from .settings import CONFIG_DIR, CONFIG_VALUES

CONFIG_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


class ConfigError(Exception):
    """A config file is missing or refers to a value that settings do not define."""


def read_config(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"no config file at {path}")

    def fill(m: re.Match) -> str:
        if m.group(1) not in CONFIG_VALUES:
            raise ConfigError(f"{path.name} refers to ${{{m.group(1)}}}, which settings do not define")
        return CONFIG_VALUES[m.group(1)]

    return yaml.safe_load(CONFIG_REF.sub(fill, path.read_text(encoding="utf-8")))


def load_app_profile(app_id: str) -> AppProfile:
    return AppProfile.model_validate(read_config(CONFIG_DIR / "apps" / f"{app_id}.yaml"))
