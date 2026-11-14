"""Validated local runtime settings; secrets never enter browser responses."""

import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

DEFAULT_STT_KEYTERMS = ("RehearseAI", "GrepAI", "GitHub", "webhooks")


def parse_stt_keyterms(raw: str) -> tuple[str, ...]:
    """Parse a short static Nova-3 vocabulary; an empty value disables hints."""
    terms = tuple(term.strip() for term in raw.split(",") if term.strip())
    if len(terms) > 12 or len({term.casefold() for term in terms}) != len(terms):
        raise ConfigurationError("REHEARSE_STT_KEYTERMS needs at most 12 unique terms")
    if any(len(term) > 48 or any(char in term for char in "\n\r:;") for term in terms):
        raise ConfigurationError("REHEARSE_STT_KEYTERMS contains an invalid term")
    return terms


class ConfigurationError(ValueError):
    """A required setting is missing or invalid."""


@dataclass(frozen=True)
class AuthConfig:
    database_url: str = field(
        default="postgresql+asyncpg://rehearse:rehearse_local_only@127.0.0.1:5433/rehearse_dev",
        repr=False,
    )
    secret: str = field(default="test-only-secret-replace-before-serving-accounts", repr=False)
    public_base_url: str = "http://127.0.0.1:7860"
    environment: str = "local"
    smtp_host: str = "127.0.0.1"
    smtp_port: int = 1025
    smtp_from: str = "RehearseAI <no-reply@localhost>"
    smtp_username: str | None = field(default=None, repr=False)
    smtp_password: str | None = field(default=None, repr=False)
    smtp_starttls: bool = False
    session_seconds: int = 43200

    @property
    def secure_cookie(self) -> bool:
        return self.environment == "production"


@dataclass(frozen=True)
class Settings:
    deepgram_api_key: str = field(repr=False)
    openai_api_key: str = field(repr=False)
    cartesia_api_key: str | None = field(default=None, repr=False)
    host: str = "127.0.0.1"
    port: int = 7860
    session_dir: Path = Path("sessions")
    pdf_dir: Path = Path("private_pdfs")
    snapshot_dir: Path = Path("private_snapshots")
    stt_model: str = "nova-3-general"
    stt_keyterms: tuple[str, ...] = DEFAULT_STT_KEYTERMS
    llm_model: str = "gpt-4.1-mini"
    tts_provider: str = "cartesia"
    cartesia_tts_model: str = "sonic-3.6"
    cartesia_tts_voice: str = "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4"
    openai_tts_model: str = "gpt-4o-mini-tts"
    openai_tts_voice: str = "shimmer"
    openai_tts_speed: float = 1.0
    vad_confidence: float = 0.7
    vad_start_secs: float = 0.2
    vad_stop_secs: float = 0.35
    turn_pause_secs: float = 1.1
    auth: AuthConfig = field(default_factory=AuthConfig)

    @classmethod
    def load(
        cls,
        env: Mapping[str, str] | None = None,
        env_file: str | Path = ".env",
    ) -> "Settings":
        file_values = dotenv_values(env_file)
        values = {key: value for key, value in file_values.items() if value is not None}
        values.update(os.environ if env is None else env)
        provider = values.get("REHEARSE_TTS_PROVIDER", "cartesia").strip().lower()
        if provider not in {"cartesia", "openai"}:
            raise ConfigurationError("REHEARSE_TTS_PROVIDER must be cartesia or openai")
        missing = [
            name
            for name in (
                "DEEPGRAM_API_KEY",
                "OPENAI_API_KEY",
                *(("CARTESIA_API_KEY",) if provider == "cartesia" else ()),
            )
            if not values.get(name, "").strip()
        ]
        if missing:
            names = ", ".join(missing)
            raise ConfigurationError(
                f"Missing {names}. Add the key(s) to .env using .env.example, then restart."
            )

        def number(name: str, default: str, *, low: float, high: float) -> float:
            raw = values.get(name, default)
            try:
                result = float(raw)
            except ValueError as exc:
                raise ConfigurationError(f"{name} must be a number") from exc
            if not low <= result <= high:
                raise ConfigurationError(f"{name} must be between {low} and {high}")
            return result

        host = values.get("REHEARSE_HOST", "127.0.0.1")
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ConfigurationError("REHEARSE_HOST must be a loopback address for local use")
        port = int(number("REHEARSE_PORT", "7860", low=1, high=65535))
        if str(port) != values.get("REHEARSE_PORT", "7860"):
            raise ConfigurationError("REHEARSE_PORT must be a whole number")

        environment = values.get("REHEARSE_ENV", "local").strip().lower()
        if environment not in {"local", "production"}:
            raise ConfigurationError("REHEARSE_ENV must be local or production")
        database_url = values.get("DATABASE_URL", "").strip()
        if not database_url.startswith("postgresql+asyncpg://"):
            raise ConfigurationError("DATABASE_URL must be a PostgreSQL asyncpg URL")
        secret = values.get("REHEARSE_AUTH_SECRET", "")
        if len(secret) < 32:
            raise ConfigurationError("REHEARSE_AUTH_SECRET must contain at least 32 characters")
        public_base_url = values.get("REHEARSE_PUBLIC_BASE_URL", "").rstrip("/")
        parsed_base = urlparse(public_base_url)
        if parsed_base.path or parsed_base.query or parsed_base.fragment or not parsed_base.netloc:
            raise ConfigurationError("REHEARSE_PUBLIC_BASE_URL must be an origin without a path")
        if environment == "local":
            if parsed_base.scheme != "http" or parsed_base.hostname not in {
                "localhost",
                "127.0.0.1",
                "::1",
            }:
                raise ConfigurationError("Local REHEARSE_PUBLIC_BASE_URL must use loopback HTTP")
        elif parsed_base.scheme != "https":
            raise ConfigurationError("Production REHEARSE_PUBLIC_BASE_URL must use HTTPS")
        smtp_host = values.get("REHEARSE_SMTP_HOST", "").strip()
        smtp_from = values.get("REHEARSE_SMTP_FROM", "").strip()
        if not smtp_host or not smtp_from or "@" not in smtp_from:
            raise ConfigurationError("REHEARSE_SMTP_HOST and REHEARSE_SMTP_FROM are required")
        smtp_username = values.get("REHEARSE_SMTP_USERNAME", "").strip() or None
        smtp_password = values.get("REHEARSE_SMTP_PASSWORD", "").strip() or None
        if environment == "production" and (not smtp_username or not smtp_password):
            raise ConfigurationError("Production SMTP username and password are required")
        smtp_port = int(number("REHEARSE_SMTP_PORT", "1025", low=1, high=65535))
        if str(smtp_port) != values.get("REHEARSE_SMTP_PORT", "1025"):
            raise ConfigurationError("REHEARSE_SMTP_PORT must be a whole number")
        smtp_starttls = values.get("REHEARSE_SMTP_STARTTLS", "false").lower()
        if smtp_starttls not in {"true", "false"}:
            raise ConfigurationError("REHEARSE_SMTP_STARTTLS must be true or false")
        if environment == "production" and smtp_starttls != "true":
            raise ConfigurationError("Production SMTP must use STARTTLS")

        model_options = {
            "REHEARSE_STT_MODEL": {"nova-3-general", "nova-3"},
            "REHEARSE_LLM_MODEL": {"gpt-4.1-mini", "gpt-4.1"},
        }
        for name, allowed in model_options.items():
            if values.get(name, next(iter(sorted(allowed)))) not in allowed:
                raise ConfigurationError(f"{name} must be one of: {', '.join(sorted(allowed))}")
        voices = {
            "alloy",
            "ash",
            "ballad",
            "cedar",
            "coral",
            "echo",
            "fable",
            "marin",
            "nova",
            "onyx",
            "sage",
            "shimmer",
            "verse",
        }
        cartesia_model = values.get("REHEARSE_CARTESIA_TTS_MODEL", "sonic-3.6")
        cartesia_voice = values.get(
            "REHEARSE_CARTESIA_TTS_VOICE", "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4"
        )
        openai_model = values.get("REHEARSE_OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
        openai_voice = values.get("REHEARSE_OPENAI_TTS_VOICE", "shimmer")
        if provider == "cartesia":
            if cartesia_model != "sonic-3.6":
                raise ConfigurationError("REHEARSE_CARTESIA_TTS_MODEL must be sonic-3.6")
            try:
                uuid.UUID(cartesia_voice)
            except ValueError as exc:
                raise ConfigurationError(
                    "REHEARSE_CARTESIA_TTS_VOICE must be a voice UUID"
                ) from exc
        elif openai_model not in {"gpt-4o-mini-tts", "tts-1"}:
            raise ConfigurationError("REHEARSE_OPENAI_TTS_MODEL is unsupported")
        elif openai_voice not in voices:
            raise ConfigurationError("REHEARSE_OPENAI_TTS_VOICE is unsupported")
        elif openai_model == "tts-1" and openai_voice not in {
            "alloy",
            "ash",
            "coral",
            "echo",
            "fable",
            "nova",
            "onyx",
            "shimmer",
            "sage",
        }:
            raise ConfigurationError("REHEARSE_OPENAI_TTS_VOICE is unavailable with tts-1")

        return cls(
            deepgram_api_key=values["DEEPGRAM_API_KEY"],
            openai_api_key=values["OPENAI_API_KEY"],
            cartesia_api_key=values.get("CARTESIA_API_KEY") or None,
            host=host,
            port=port,
            session_dir=Path(values.get("REHEARSE_SESSION_DIR", "sessions")),
            pdf_dir=Path(values.get("REHEARSE_PDF_DIR", "private_pdfs")),
            snapshot_dir=Path(values.get("REHEARSE_SNAPSHOT_DIR", "private_snapshots")),
            stt_model=values.get("REHEARSE_STT_MODEL", "nova-3-general"),
            stt_keyterms=parse_stt_keyterms(
                values.get("REHEARSE_STT_KEYTERMS", ",".join(DEFAULT_STT_KEYTERMS))
            ),
            llm_model=values.get("REHEARSE_LLM_MODEL", "gpt-4.1-mini"),
            tts_provider=provider,
            cartesia_tts_model=cartesia_model,
            cartesia_tts_voice=cartesia_voice,
            openai_tts_model=openai_model,
            openai_tts_voice=openai_voice,
            openai_tts_speed=(
                number("REHEARSE_OPENAI_TTS_SPEED", "1.0", low=0.25, high=4.0)
                if provider == "openai"
                else 1.0
            ),
            vad_confidence=number("REHEARSE_VAD_CONFIDENCE", "0.7", low=0.1, high=1.0),
            vad_start_secs=number("REHEARSE_VAD_START_SECS", "0.2", low=0.05, high=2.0),
            vad_stop_secs=number("REHEARSE_VAD_STOP_SECS", "0.35", low=0.1, high=2.0),
            turn_pause_secs=number("REHEARSE_TURN_PAUSE_SECS", "1.1", low=0.2, high=5.0),
            auth=AuthConfig(
                database_url=database_url,
                secret=secret,
                public_base_url=public_base_url,
                environment=environment,
                smtp_host=smtp_host,
                smtp_port=smtp_port,
                smtp_from=smtp_from,
                smtp_username=smtp_username,
                smtp_password=smtp_password,
                smtp_starttls=smtp_starttls == "true",
            ),
        )
