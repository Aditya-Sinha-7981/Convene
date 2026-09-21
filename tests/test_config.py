"""Settings loader: defaults, overrides, and failing loudly on a malformed file."""
import pytest

from server.config import DEFAULT_CONFIG, ROOT, ConfigError, load_settings


def test_missing_file_falls_back_to_defaults(tmp_path):
    settings = load_settings(tmp_path / "absent.toml", root=tmp_path)
    assert settings.database_path == tmp_path / "data" / "convene.db"
    assert settings.exports_dir == tmp_path / "data" / "exports"
    assert settings.root == tmp_path


def test_missing_default_file_under_a_custom_root_falls_back_too(tmp_path):
    assert load_settings(root=tmp_path).database_path == tmp_path / "data" / "convene.db"


def test_the_shipped_config_file_loads_and_matches_the_defaults():
    assert DEFAULT_CONFIG.exists()
    settings = load_settings()
    assert settings.database_path == ROOT / "data" / "convene.db"
    assert settings.exports_dir == ROOT / "data" / "exports"


def test_overrides_and_relative_paths_resolve_from_the_root_not_the_working_directory(tmp_path, monkeypatch):
    config = tmp_path / "convene.toml"
    config.write_text('[paths]\ndatabase = "state/meetings.db"\nexports = "/var/tmp/convene-exports"\n')
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    settings = load_settings(config, root=tmp_path)
    assert settings.database_path == tmp_path / "state" / "meetings.db"
    assert str(settings.exports_dir) == "/var/tmp/convene-exports"


def test_a_partial_file_keeps_the_other_defaults(tmp_path):
    config = tmp_path / "c.toml"
    config.write_text('[paths]\ndatabase = "only.db"\n')
    settings = load_settings(config, root=tmp_path)
    assert settings.database_path == tmp_path / "only.db"
    assert settings.exports_dir == tmp_path / "data" / "exports"


def test_other_sections_are_ignored_so_later_tasks_can_add_theirs(tmp_path):
    config = tmp_path / "c.toml"
    config.write_text('[models]\nstt = "whatever"\n\n[paths]\ndatabase = "d.db"\n')
    assert load_settings(config, root=tmp_path).database_path == tmp_path / "d.db"


@pytest.mark.parametrize("text,message", [
    ("[paths\ndatabase = 1", "not valid TOML"),
    ("paths = 3", "must be a table"),
    ("[paths]\ndatabase = 42", "paths.database"),
    ('[paths]\nexports = ""', "paths.exports"),
])
def test_malformed_or_invalid_files_fail_loudly(tmp_path, text, message):
    config = tmp_path / "bad.toml"
    config.write_text(text)
    with pytest.raises(ConfigError, match=message):
        load_settings(config, root=tmp_path)


def test_data_directory_is_git_ignored():
    ignore = (ROOT / ".gitignore").read_text().splitlines()
    assert "/data/" in ignore
