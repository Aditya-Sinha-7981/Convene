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
    config.write_text('[retrieval]\ntop_k = 5\n\n[paths]\ndatabase = "d.db"\n')
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


def test_stt_and_pipeline_sections_load_with_types_checked(tmp_path):
    from server.config import PipelineConfig, SttModelConfig
    config = tmp_path / "c.toml"
    config.write_text('[models.stt]\nruntime = "mlx"\nmodel = "org/some-model"\nrevision = "abc123"\n'
                      'no_speech_threshold = 1\n\n'
                      '[pipeline]\nwindow_ms = 2000\nvad_margin_db = 12\nworkers = 2\nhallucination_blocklist = ["thank you."]\n')
    settings = load_settings(config, root=tmp_path)
    assert (settings.stt.model, settings.stt.revision, settings.stt.no_speech_threshold) == ("org/some-model", "abc123", 1.0)
    assert settings.pipeline.hallucination_blocklist == ("thank you.",)
    assert (settings.pipeline.window_ms, settings.pipeline.vad_margin_db, settings.pipeline.workers) == (2000, 12.0, 2)
    assert settings.pipeline.queue_max == PipelineConfig().queue_max and SttModelConfig().language == "auto"


@pytest.mark.parametrize("text,message", [
    ('[pipeline]\nwindw_ms = 1000', "unknown keys"),
    ('[pipeline]\nwindow_ms = "1000"', "window_ms must be int"),
    ('[pipeline]\nworkers = true', "workers must be int"),
    ('[models.stt]\nmodel = 5', "model must be str"),
    ('[pipeline]\nhallucination_blocklist = "x"', "hallucination_blocklist"),
    ('pipeline = 3', "must be a table"),
    ('[pipeline]\nsegmentation = "words"', "segmentation"),
    ('[pipeline]\nsegment_min_ms = 9000\nsegment_max_ms = 8000', "segment_min_ms"),
    ('[pipeline]\nsegment_end_silence_ms = 0', "segment_end_silence_ms"),
    ('[pipeline]\nsegment_max_ms = "8000"', "segment_max_ms must be int"),
])
def test_bad_stt_or_pipeline_values_fail_loudly(tmp_path, text, message):
    config = tmp_path / "bad.toml"
    config.write_text(text)
    with pytest.raises(ConfigError, match=message):
        load_settings(config, root=tmp_path)


def test_segments_are_the_default_and_fixed_windows_stay_selectable(tmp_path):
    assert load_settings(tmp_path / "missing.toml", root=tmp_path).pipeline.segmentation == "segments"
    config = tmp_path / "c.toml"
    config.write_text('[pipeline]\nsegmentation = "fixed"\nsegment_max_ms = 5000\nlog_transcripts = false')
    pipeline = load_settings(config, root=tmp_path).pipeline
    assert (pipeline.segmentation, pipeline.segment_max_ms, pipeline.log_transcripts) == ("fixed", 5000, False)


def test_rag_cap_must_fit_the_embedding_model_limit(tmp_path):
    config = tmp_path / "bad.toml"
    config.write_text('[models.embedding]\nmax_tokens = 400\n\n[rag]\nhard_max_tokens = 400\n')
    with pytest.raises(ConfigError, match="hard_max_tokens"):
        load_settings(config, root=tmp_path)
