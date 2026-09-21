"""Settings loader: config/convene.toml with defaults. Stdlib only (tomllib)."""
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "convene.toml"
DEFAULT_PATHS = {"database": "data/convene.db", "exports": "data/exports"}


class ConfigError(Exception):
    """The configuration file is malformed or holds an invalid value. Fails startup loudly."""


@dataclass(frozen=True)
class Settings:
    root: Path
    database_path: Path
    exports_dir: Path


def load_settings(config_path: Path | None = None, *, root: Path | None = None) -> Settings:
    """Load settings. A missing file falls back to defaults; a malformed one raises ConfigError.

    Relative paths resolve from ``root`` (the repository root by default), not the working directory.
    """
    root = Path(root) if root is not None else ROOT
    path = Path(config_path) if config_path is not None else root / "config" / "convene.toml"
    data: dict = {}
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise ConfigError(f"{path}: not valid TOML: {exc}") from exc
    paths = data.get("paths", {})
    if not isinstance(paths, dict):
        raise ConfigError(f"{path}: [paths] must be a table")
    merged = {**DEFAULT_PATHS, **paths}
    resolved = {}
    for key in ("database", "exports"):
        value = merged[key]
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{path}: paths.{key} must be a non-empty string")
        candidate = Path(value).expanduser()
        resolved[key] = candidate if candidate.is_absolute() else root / candidate
    return Settings(root=root, database_path=resolved["database"], exports_dir=resolved["exports"])
