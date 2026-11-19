import asyncio
import json
import os
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pipecat.frames.frames import ErrorFrame
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection

from rehearse_ai.config import AuthConfig, ConfigurationError, Settings
from rehearse_ai.interview.controller import Stage
from rehearse_ai.server import create_app
from rehearse_ai.voice.pipeline import InterviewInput, LiveSession

AUTH_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://rehearse:local@127.0.0.1:5433/rehearse_test",
    "REHEARSE_AUTH_SECRET": "test-only-secret-at-least-thirty-two-characters",
    "REHEARSE_PUBLIC_BASE_URL": "http://127.0.0.1:7860",
    "REHEARSE_SMTP_HOST": "127.0.0.1",
    "REHEARSE_SMTP_FROM": "RehearseAI <no-reply@localhost>",
}

ASSIGNED_PLAN = {
    "title": "Synthetic interview",
    "purpose": "Discuss a project",
    "questions": [
        {
            "topic": "Project",
            "question": "Describe a project you built.",
            "required": True,
            "follow_up_guidance": "Ask about ownership",
            "max_probes": 1,
        }
    ],
    "focus_areas": [],
    "target_minutes": 10,
    "interviewer_style": "Concise and curious",
    "closing_instructions": "Thank the candidate",
}


def test_missing_credentials_name_only(tmp_path):
    with pytest.raises(ConfigurationError, match="DEEPGRAM_API_KEY, OPENAI_API_KEY"):
        Settings.load(env={}, env_file=tmp_path / "missing.env")


def test_bad_local_config_is_actionable(tmp_path):
    env = {
        **AUTH_ENV,
        "DEEPGRAM_API_KEY": "test-dg",
        "OPENAI_API_KEY": "test-oa",
        "CARTESIA_API_KEY": "test-ca",
    }
    with pytest.raises(ConfigurationError, match="REHEARSE_HOST"):
        Settings.load({**env, "REHEARSE_HOST": "0.0.0.0"}, tmp_path / "none")
    with pytest.raises(ConfigurationError, match="REHEARSE_TTS_PROVIDER"):
        Settings.load({**env, "REHEARSE_TTS_PROVIDER": "unknown"}, tmp_path / "none")
    with pytest.raises(ConfigurationError, match="REHEARSE_CARTESIA_TTS_MODEL"):
        Settings.load({**env, "REHEARSE_CARTESIA_TTS_MODEL": "unknown"}, tmp_path / "none")
    with pytest.raises(ConfigurationError, match="REHEARSE_CARTESIA_TTS_VOICE"):
        Settings.load({**env, "REHEARSE_CARTESIA_TTS_VOICE": "unknown"}, tmp_path / "none")
    with pytest.raises(ConfigurationError, match="REHEARSE_TURN_PAUSE_SECS"):
        Settings.load({**env, "REHEARSE_TURN_PAUSE_SECS": "0"}, tmp_path / "none")
    with pytest.raises(ConfigurationError, match="REHEARSE_OPENAI_TTS_SPEED"):
        Settings.load(
            {**env, "REHEARSE_TTS_PROVIDER": "openai", "REHEARSE_OPENAI_TTS_SPEED": "4.5"},
            tmp_path / "none",
        )
    with pytest.raises(
        ConfigurationError, match="REHEARSE_OPENAI_TTS_VOICE is unavailable with tts-1"
    ):
        Settings.load(
            {
                **env,
                "REHEARSE_TTS_PROVIDER": "openai",
                "REHEARSE_OPENAI_TTS_MODEL": "tts-1",
                "REHEARSE_OPENAI_TTS_VOICE": "marin",
            },
            tmp_path / "none",
        )


def test_only_selected_tts_provider_needs_its_credential(tmp_path):
    shared = {**AUTH_ENV, "DEEPGRAM_API_KEY": "test-dg", "OPENAI_API_KEY": "test-oa"}
    with pytest.raises(ConfigurationError, match="CARTESIA_API_KEY"):
        Settings.load(shared, tmp_path / "none")
    settings = Settings.load({**shared, "REHEARSE_TTS_PROVIDER": "openai"}, tmp_path / "none")
    assert settings.cartesia_api_key is None
    assert settings.tts_provider == "openai"


def test_static_nova3_keyterms_can_be_disabled_without_changing_turn_settings(tmp_path):
    env = {
        **AUTH_ENV,
        "DEEPGRAM_API_KEY": "test-dg",
        "OPENAI_API_KEY": "test-oa",
        "CARTESIA_API_KEY": "test-ca",
        "REHEARSE_STT_KEYTERMS": "",
    }
    settings = Settings.load(env, tmp_path / "none")
    assert settings.stt_keyterms == ()
    assert settings.turn_pause_secs == 1.1


def test_account_configuration_requires_database_secret_and_production_transport(tmp_path):
    voice = {"DEEPGRAM_API_KEY": "dg", "OPENAI_API_KEY": "oa", "CARTESIA_API_KEY": "ca"}
    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        Settings.load(voice, tmp_path / "none")
    with pytest.raises(ConfigurationError, match="REHEARSE_AUTH_SECRET"):
        Settings.load({**voice, **AUTH_ENV, "REHEARSE_AUTH_SECRET": "short"}, tmp_path / "none")
    with pytest.raises(ConfigurationError, match="HTTPS"):
        Settings.load({**voice, **AUTH_ENV, "REHEARSE_ENV": "production"}, tmp_path / "none")
    with pytest.raises(ConfigurationError, match="SMTP username"):
        Settings.load(
            {
                **voice,
                **AUTH_ENV,
                "REHEARSE_ENV": "production",
                "REHEARSE_PUBLIC_BASE_URL": "https://example.com",
            },
            tmp_path / "none",
        )


def test_local_browser_assets_and_health_load_without_provider_calls(tmp_path):
    test_url = os.environ.get(
        "REHEARSE_TEST_DATABASE_URL",
        "postgresql+asyncpg://rehearse:rehearse_local_only@127.0.0.1:5433/rehearse_test",
    )
    assert test_url.rsplit("/", 1)[-1].endswith("_test")
    settings = Settings(
        deepgram_api_key="test-dg",
        openai_api_key="test-oa",
        session_dir=tmp_path,
        auth=AuthConfig(database_url=test_url, secret="test-secret-at-least-thirty-two-characters"),
    )
    with TestClient(create_app(settings)) as client:
        health_response = client.get("/healthz")
        health = health_response.json()
        assert health["status"] == "ready"
        assert health["build_id"].startswith("0.1.0+")
        assert health_response.headers["cache-control"] == "no-store"
        assert client.get("/", follow_redirects=False).status_code == 303
        page = client.get("/login")
        assert page.status_code == 200
        assert "Sign in" in page.text
        assert page.headers["cache-control"] == "no-store"
        assert client.get("/assets/scripts/candidate/interview_room.js").status_code == 200
        assert client.get("/assets/scripts/candidate/media_setup.js").status_code == 200
        assert client.get("/assets/styles/interview_room.css").status_code == 200
        theme = client.get("/assets/styles/theme.css")
        assert theme.status_code == 200 and theme.headers["content-type"].startswith("text/css")
        assert client.get("/assets/scripts/admin/sign_in.js").status_code == 200
        assert client.get("/interview", follow_redirects=False).status_code == 401
        assert client.post("/api/offer", json={"type": "bad"}).status_code == 401


@pytest.mark.asyncio
async def test_locked_pipecat_pipeline_assembles_without_provider_calls(tmp_path):
    settings = Settings(
        deepgram_api_key="test-dg",
        openai_api_key="test-oa",
        cartesia_api_key="test-ca",
        session_dir=tmp_path,
    )
    connection = SmallWebRTCConnection()
    with pytest.raises(TypeError, match="interview_plan"):
        LiveSession(connection, settings)
    live = LiveSession(connection, settings, interview_plan=ASSIGNED_PLAN)
    names = [type(processor).__name__ for processor in live.pipeline.processors]
    assert names.index("DeepgramSTTService") < names.index("InterviewInput")
    assert names.index("InterviewInput") < names.index("ObservedCartesiaTTSService")
    assert names.index("ObservedCartesiaTTSService") < names.index("DeliveryObserver")
    tts = next(p for p in live.pipeline.processors if isinstance(p, CartesiaTTSService))
    stt = next(p for p in live.pipeline.processors if type(p).__name__ == "DeepgramSTTService")
    assert stt._settings.keyterm == ["RehearseAI", "GrepAI", "GitHub", "webhooks"]
    assert tts._settings.model == "sonic-3.6"
    assert tts._settings.voice == "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4"
    assert tts._stop_frame_timeout_s == 3.0
    assert tts._max_buffer_delay_ms == 0
    await live.stop()
    await connection.disconnect()
    assert live.log.path.exists()


@pytest.mark.asyncio
async def test_voice_session_waits_for_explicit_begin_control(tmp_path):
    settings = Settings(
        deepgram_api_key="test-dg",
        openai_api_key="test-oa",
        cartesia_api_key="test-ca",
        session_dir=tmp_path,
    )
    connection = SmallWebRTCConnection()
    live = LiveSession(connection, settings, interview_plan=ASSIGNED_PLAN)
    live.session._queue_speech = AsyncMock()
    assert live.session.controller.stage is Stage.NEW
    await live._transport._call_event_handler("on_client_connected", connection)
    assert live.session.controller.stage is Stage.NEW
    await live._transport._call_event_handler(
        "on_app_message", {"type": "rehearseai_media", "action": "pause"}, "candidate"
    )
    await asyncio.sleep(0)
    assert live.session.controller.stage is Stage.NEW
    assert not live.session.media_paused
    assert not next(
        processor for processor in live.pipeline.processors if isinstance(processor, InterviewInput)
    )._media_paused
    await live._transport._call_event_handler(
        "on_app_message", {"type": "rehearseai_media", "action": "begin"}, "candidate"
    )
    await asyncio.sleep(0)
    assert live.session.controller.stage is Stage.DELIVERING
    await live._transport._call_event_handler(
        "on_app_message", {"type": "rehearseai_media", "action": "begin"}, "candidate"
    )
    await asyncio.sleep(0)
    assert live.session._queue_speech.await_count == 1
    await live.stop()
    await connection.disconnect()


@pytest.mark.asyncio
async def test_tts_one_does_not_receive_unsupported_style_instructions(tmp_path):
    settings = Settings(
        deepgram_api_key="test-dg",
        openai_api_key="test-oa",
        session_dir=tmp_path,
        tts_provider="openai",
        openai_tts_model="tts-1",
    )
    connection = SmallWebRTCConnection()
    live = LiveSession(connection, settings, interview_plan=ASSIGNED_PLAN)
    tts = next(p for p in live.pipeline.processors if isinstance(p, OpenAITTSService))
    assert tts._settings.instructions is None
    assert tts._stop_frame_timeout_s == 20.0
    await live.stop()
    await connection.disconnect()


@pytest.mark.asyncio
async def test_pipeline_provider_error_reaches_session_log(tmp_path):
    settings = Settings(
        deepgram_api_key="test-dg",
        openai_api_key="test-oa",
        cartesia_api_key="test-ca",
        session_dir=tmp_path,
    )
    connection = SmallWebRTCConnection()
    live = LiveSession(connection, settings, interview_plan=ASSIGNED_PLAN)
    tts = next(p for p in live.pipeline.processors if isinstance(p, CartesiaTTSService))
    await live.worker._call_event_handler(
        "on_pipeline_error", ErrorFrame(error="synthetic TTS failure", processor=tts)
    )
    await asyncio.sleep(0)
    events = [json.loads(line) for line in live.log.path.read_text().splitlines()]
    assert events[-1]["event"] == "response_error"
    assert events[-1]["failure_stage"] == "pipeline"
    assert events[-1]["processor"] == "CartesiaTTSService"
    await live.stop()
    await connection.disconnect()
