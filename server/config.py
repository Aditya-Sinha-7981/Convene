"""Settings loader: config/convene.toml with defaults. Stdlib only (tomllib)."""
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "convene.toml"
DEFAULT_PATHS = {"database": "data/convene.db", "exports": "data/exports"}


class ConfigError(Exception):
    """The configuration file is malformed or holds an invalid value. Fails startup loudly."""


@dataclass(frozen=True)
class SttModelConfig:
    """``[models.stt]``: the one place the STT model is named (ADR-14). Nothing in code names a model."""
    runtime: str = "mlx"                     # docs/data-model.md: mlx | groq | gemini
    model: str = ""                          # repository id of the model
    revision: str = ""                       # exact pinned revision of the weights
    # ``auto`` maps to ``None`` for mlx-whisper, which selects a language for each speech segment.
    # Any explicit Whisper language code (for example ``en``) remains available for a known monolingual run.
    language: str = "auto"
    no_speech_threshold: float = 0.6         # a window Whisper is this sure has no speech is returned empty
    logprob_threshold: float = -1.0          # below this average log-probability the window is treated as unreliable
    compression_ratio_threshold: float = 2.4  # above this the text is repetitive (a decoding failure)
    min_confidence: float = 0.0              # windows scoring below this are returned empty


@dataclass(frozen=True)
class EmbeddingModelConfig:
    """``[models.embedding]``: one local vector space for chunks and questions (ADR-14)."""
    runtime: str = "sentence_transformers"
    model: str = "BAAI/bge-small-en-v1.5"
    revision: str = "main"
    dimension: int = 384
    max_tokens: int = 512


@dataclass(frozen=True)
class PipelineConfig:
    """``[pipeline]``: VAD, windowing, scheduling and priority. Every value was set from measurement (logs/stt.md)."""
    target_rate: int = 16000
    # "segments" (default, ADR-19): one segment per stretch of speech, ended by a pause. "fixed": the earlier
    # fixed windows of ``window_ms``, kept as a fallback that needs only this one line changed.
    segmentation: str = "segments"
    window_ms: int = 1000                    # fixed mode only
    segment_end_silence_ms: int = 600        # a pause this long ends a segment
    segment_max_ms: int = 8000               # a longer stretch is cut at its quietest point near the end
    segment_min_ms: int = 300                # less voiced audio than this is a blip, not speech
    segment_pre_roll_ms: int = 200           # audio kept from before speech starts, so the first word is not clipped
    segment_tail_ms: int = 200               # silence kept after speech ends
    segment_cut_search_ms: int = 1500        # where the cap looks for a quiet frame to cut at
    min_speech_fraction: float = 0.2         # a window or segment passes if at least this share of its frames is voiced
    resync_s: float = 0.25                   # timestamp accuracy bound: re-anchor to the wall clock beyond this drift
    vad_frame_ms: int = 20
    vad_min_db: float = -50.0
    vad_margin_db: float = 9.0
    vad_noise_floor_db: float = -60.0
    vad_noise_window_s: float = 5.0
    vad_noise_percentile: float = 10.0
    vad_hangover_frames: int = 5
    workers: int = 1
    queue_max: int = 8                       # windows queued per device before the oldest is dropped
    overload_policy: str = "drop_oldest"
    window_timeout_s: float = 20.0
    # Whisper answers non-speech with a stock phrase ("Thank you.") at high confidence, so its own score cannot catch
    # it. A blocklisted phrase is dropped only when the VAD judged the window mostly non-speech (logs/stt.md).
    hallucination_blocklist: tuple[str, ...] = ("thank you", "thanks for watching", "thank you for watching",
                                                "you", "bye", "thanks")
    hallucination_max_speech_fraction: float = 0.5   # fixed windows: below this share of speech
    hallucination_min_strength_db: float = 10.0      # segments: below this margin over the background (measured: noise
                                                     # false positives sit at 5 to 9 dB, normal speech at 11 to 28 dB)
    log_transcripts: bool = True             # print each transcribed line on the server console
    priority_high_backlog_windows: int = 4   # reasoning/embedding wait while more than this many windows are pending
    priority_max_wait_s: float = 3.0         # ...but never longer than this


@dataclass(frozen=True)
class AttributionConfig:
    """Device attribution is structural; 1.0 is reserved for human confirmation."""
    device_confidence: float = 0.95
    unresolved_confidence: float = 0.2
    low_confidence_threshold: float = 0.8


@dataclass(frozen=True)
class RagConfig:
    """Chunk finalization bounds. The hard cap includes rendered speaker/time metadata."""
    target_tokens: int = 400
    hard_max_tokens: int = 448
    settle_delay_s: float = 2.0
    quiet_flush_s: float = 3.0
    retry_delay_s: float = 1.0


@dataclass(frozen=True)
class NetworkConfig:
    """``[network]``: optional public hostname used in participant join URLs.

    DNS-provider credentials deliberately do not belong here.  They are read only by the explicit operator
    preflight script, never by the Convene server process.
    """
    public_host: str = ""


def _check_attribution(config: "AttributionConfig") -> None:
    values = (config.device_confidence, config.unresolved_confidence, config.low_confidence_threshold)
    if any(not 0.0 <= value <= 1.0 for value in values):
        raise ValueError("confidence values must be between 0 and 1")
    if config.device_confidence >= 1.0:
        raise ValueError("device_confidence must be below 1.0, which is reserved for manual correction")


def _check_embedding(config: "EmbeddingModelConfig") -> None:
    if not config.model or not config.revision:
        raise ValueError("model and revision must be non-empty")
    if config.dimension < 1 or config.max_tokens < 8:
        raise ValueError("dimension must be positive and max_tokens must be at least 8")


def _check_rag(config: "RagConfig") -> None:
    if not 1 <= config.target_tokens <= config.hard_max_tokens:
        raise ValueError("target_tokens must be positive and no greater than hard_max_tokens")
    if config.hard_max_tokens >= 512:
        raise ValueError("hard_max_tokens must leave margin below the embedding model input limit")
    if min(config.settle_delay_s, config.quiet_flush_s, config.retry_delay_s) <= 0:
        raise ValueError("timing values must be positive")


def _check_pipeline(config: "PipelineConfig") -> None:
    if config.segmentation not in ("segments", "fixed"):
        raise ValueError(f"segmentation must be 'segments' or 'fixed', got {config.segmentation!r}")
    if config.segment_min_ms >= config.segment_max_ms or config.segment_end_silence_ms <= 0:
        raise ValueError("segment_min_ms must be below segment_max_ms and segment_end_silence_ms must be positive")
    if config.overload_policy != "drop_oldest":
        raise ValueError("overload_policy must be 'drop_oldest' (the only policy implemented)")


@dataclass(frozen=True)
class Settings:
    root: Path
    database_path: Path
    exports_dir: Path
    stt: SttModelConfig = SttModelConfig()
    embedding: EmbeddingModelConfig = EmbeddingModelConfig()
    pipeline: PipelineConfig = PipelineConfig()
    attribution: AttributionConfig = AttributionConfig()
    rag: RagConfig = RagConfig()
    network: NetworkConfig = NetworkConfig()


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
    settings = Settings(root=root, database_path=resolved["database"], exports_dir=resolved["exports"],
                        stt=_section(SttModelConfig, data.get("models", {}).get("stt", {}), path, "models.stt"),
                        embedding=_section(EmbeddingModelConfig, data.get("models", {}).get("embedding", {}), path,
                                           "models.embedding"),
                        pipeline=_section(PipelineConfig, data.get("pipeline", {}), path, "pipeline"),
                        attribution=_section(AttributionConfig, data.get("attribution", {}), path, "attribution"),
                        rag=_section(RagConfig, data.get("rag", {}), path, "rag"),
                        network=_section(NetworkConfig, data.get("network", {}), path, "network"))
    if settings.rag.hard_max_tokens >= settings.embedding.max_tokens:
        raise ConfigError(f"{path}: [rag].hard_max_tokens must leave margin below [models.embedding].max_tokens")
    return settings


def _section(cls, table, path: Path, name: str):
    """Build a config dataclass from a TOML table: unknown keys and wrongly typed values fail loudly."""
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: [{name}] must be a table")
    defaults = cls()
    known = {f.name for f in fields(cls)}
    unknown = set(table) - known
    if unknown:
        raise ConfigError(f"{path}: [{name}] has unknown keys {sorted(unknown)}; known keys: {sorted(known)}")
    values = {}
    for key, value in table.items():
        expected = type(getattr(defaults, key))
        if expected is tuple:
            ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
            value = tuple(value) if ok else value
        elif expected is float:
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            value = float(value) if ok else value
        else:
            ok = isinstance(value, expected) and not (expected is int and isinstance(value, bool))
        if not ok:
            raise ConfigError(f"{path}: [{name}].{key} must be {expected.__name__}, got {value!r}")
        values[key] = value
    try:
        built = cls(**values)
        if cls is PipelineConfig:
            _check_pipeline(built)
        elif cls is AttributionConfig:
            _check_attribution(built)
        elif cls is EmbeddingModelConfig:
            _check_embedding(built)
        elif cls is RagConfig:
            _check_rag(built)
    except ValueError as exc:
        raise ConfigError(f"{path}: [{name}] {exc}") from exc
    return built
