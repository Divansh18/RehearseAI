"""Brief noise must not cancel the interviewer; genuine speech must still interrupt promptly.

Two kinds of evidence, kept apart on purpose:

* Audio-driven tests push labelled synthetic audio (``tests/audio``, see its README) through the
  real Silero VAD, the real ``UserTurnProcessor`` and the real ``InterviewInput`` in real time. The
  speech is machine-synthesised and the "noise" is a truncated syllable, because Silero (correctly)
  does not fire on synthetic claps, thumps or hiss: the false triggers that matter are
  speech-like sounds. STT is the one stub: transcript frames are injected at labelled times.
* Frame-level tests drive the strategy directly to pin down races.

None of this proves robustness to a particular home. A real-microphone check is in
MANUAL_VOICE_TEST.md.
"""

import asyncio
import time
import wave
from pathlib import Path

import numpy as np
import pytest
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    StartFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import WorkerRunner
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start.base_user_turn_start_strategy import BaseUserTurnStartStrategy
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_processor import UserTurnProcessor
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from rehearse_ai.config import Settings
from rehearse_ai.voice.interruption import ConfirmedSpeechStartStrategy
from rehearse_ai.voice.pipeline import InterviewInput

AUDIO = Path(__file__).parent / "audio"
RATE = 16000
CHUNK = 320  # 20 ms


def load(name: str) -> np.ndarray:
    with wave.open(str(AUDIO / f"{name}.wav")) as wav:
        assert wav.getframerate() == RATE and wav.getnchannels() == 1
        return np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(RATE * seconds), dtype=np.int16)


def brief_speech_like_burst() -> np.ndarray:
    """The first 0.45 s of a synthesised sentence: passes the VAD, has no complete word."""
    return load("answer")[: int(0.45 * RATE)]


def quiet(samples: np.ndarray, gain: float) -> np.ndarray:
    return (samples.astype(np.float32) * gain).astype(np.int16)


class FakeSession:
    """What InterviewInput reports to: records what the interview would have seen."""

    def __init__(self) -> None:
        self.started = []
        self.stopped = []
        self.signals = []

    async def user_started(self):
        self.started.append(time.monotonic())

    async def user_stopped(self, text, **_):
        self.stopped.append(text)

    async def detect_stop_from_segments(self, _text):
        return None

    async def late_transcript(self, *_a, **_k):
        return None

    def transcript_segment(self, *_a, **_k):
        return None

    def turn_signal(self, signal, **details):
        self.signals.append((signal, details))


class Probe(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.interruptions: list[float] = []
        self.user_started: list[float] = []

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            self.interruptions.append(time.monotonic())
        elif isinstance(frame, UserStartedSpeakingFrame):
            self.user_started.append(time.monotonic())
        await self.push_frame(frame, direction)


class UpstreamBot(FrameProcessor):
    """Stands in for transport.output(): announces the interviewer's speech *upstream*."""

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, StartFrame):
            await self.push_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)


class AudioRun:
    """One real-time run through VAD -> turn processor -> InterviewInput."""

    def __init__(self, confirm=0.5, grace=0.5) -> None:
        self.events: list[tuple[str, dict, float]] = []
        self.t0 = 0.0
        self.session = FakeSession()
        self.probe = Probe()
        self.strategy = ConfirmedSpeechStartStrategy(
            confirm_secs=confirm,
            transcript_grace_secs=grace,
            observer=lambda name, **d: self.events.append((name, d, time.monotonic() - self.t0)),
        )

    def names(self) -> list[str]:
        return [name for name, _d, _t in self.events]

    def event(self, name: str) -> tuple[dict, float]:
        return next((d, t) for n, d, t in self.events if n == name)

    async def play(
        self,
        audio: np.ndarray,
        *,
        bot_speaks: bool = True,
        bot_via_pipeline: bool = False,
        bot_stops_at: float | None = None,
        interim: tuple[float, str] | None = None,
        final: tuple[float, str] | None = None,
        tail: float = 1.6,
    ) -> None:
        adapter = InterviewInput()
        adapter.session = self.session
        pipeline = Pipeline(
            [
                VADProcessor(
                    vad_analyzer=SileroVADAnalyzer(
                        params=VADParams(confidence=0.7, start_secs=0.2, stop_secs=0.35)
                    )
                ),
                UserTurnProcessor(
                    user_turn_stop_timeout=30.0,
                    user_turn_strategies=UserTurnStrategies(
                        start=[self.strategy],
                        stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.4)],
                    ),
                ),
                adapter,
                self.probe,
                *([UpstreamBot()] if bot_via_pipeline else []),
            ]
        )
        worker = PipelineWorker(pipeline, params=PipelineParams(audio_in_sample_rate=RATE))
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        run = asyncio.create_task(runner.run())
        await asyncio.sleep(0.4)  # pipeline start-up; the timeline below starts after it
        self.t0 = time.monotonic()
        if bot_speaks and not bot_via_pipeline:
            await worker.queue_frame(BotStartedSpeakingFrame())
        stream = np.concatenate([silence(0.5), audio, silence(tail)])

        async def transcripts() -> None:
            for spec, cls in ((interim, InterimTranscriptionFrame), (final, TranscriptionFrame)):
                if spec is None:
                    continue
                at, text = spec
                await asyncio.sleep(max(0.0, at - (time.monotonic() - self.t0)))
                await worker.queue_frame(cls(text=text, user_id="candidate", timestamp="t"))

        async def bot_stop() -> None:
            if bot_stops_at is not None:
                await asyncio.sleep(max(0.0, bot_stops_at - (time.monotonic() - self.t0)))
                await worker.queue_frame(BotStoppedSpeakingFrame())

        helpers = [asyncio.create_task(transcripts()), asyncio.create_task(bot_stop())]
        for index in range(0, len(stream), CHUNK):
            due = self.t0 + index / RATE
            await asyncio.sleep(max(0.0, due - time.monotonic()))
            await worker.queue_frame(
                InputAudioRawFrame(stream[index : index + CHUNK].tobytes(), RATE, 1)
            )
        await asyncio.gather(*helpers)
        await worker.queue_frame(EndFrame())
        await asyncio.wait_for(run, timeout=10)


# ---------------------------------------------------------------------------------------------
# Audio-driven


@pytest.mark.asyncio
async def test_brief_speech_like_burst_during_interviewer_speech_is_rejected_without_interrupting():
    run = AudioRun()
    await run.play(brief_speech_like_burst())
    assert "interruption_tentative" in run.names(), run.names()
    assert "interruption_rejected" in run.names(), run.names()
    assert "interruption_committed" not in run.names()
    assert run.probe.interruptions == [] and run.probe.user_started == []
    # The interview never heard about it: no "Listening", no cancelled or advanced question.
    assert run.session.started == [] and run.session.stopped == []
    detail, _ = run.event("interruption_rejected")
    assert detail["reason"] == "brief_sound_no_words"


@pytest.mark.asyncio
async def test_the_interviewer_speaking_signal_reaches_the_strategy_from_downstream_of_it():
    # In the live pipeline the output transport announces BotStartedSpeaking upstream, through
    # InterviewInput, to the turn processor. A burst must then be tentative, not immediate.
    run = AudioRun()
    await run.play(brief_speech_like_burst(), bot_via_pipeline=True)
    assert "interruption_tentative" in run.names() and "interruption_rejected" in run.names()
    assert run.probe.interruptions == [] and run.session.started == []


@pytest.mark.asyncio
async def test_sustained_speech_interrupts_after_the_confirmation_window():
    run = AudioRun()
    await run.play(load("answer"), final=(4.4, "I led the migration."), tail=1.8)
    assert run.names()[:2] == ["interruption_tentative", "interruption_committed"], run.names()
    detail, _ = run.event("interruption_committed")
    assert detail["source"] == "sustained_sound"
    assert 400 <= detail["waited_ms"] <= 750  # the documented cost of the check
    assert len(run.probe.interruptions) == 1 and len(run.session.started) == 1
    assert run.session.stopped == ["I led the migration."]


@pytest.mark.asyncio
async def test_a_short_genuine_word_with_a_transcript_still_interrupts():
    # "Stop." is about as long as the noise burst above; only the words tell them apart.
    run = AudioRun()
    await run.play(load("stop"), interim=(1.55, "stop"), final=(1.75, "Stop."))
    assert "interruption_committed" in run.names(), run.names()
    assert "interruption_rejected" not in run.names()
    detail, _ = run.event("interruption_committed")
    assert detail["source"] in {"transcript", "sustained_sound"}
    assert len(run.probe.interruptions) == 1 and len(run.session.started) == 1
    assert run.session.stopped == ["Stop."]


@pytest.mark.asyncio
async def test_a_short_answer_transcribed_after_the_sound_ended_is_not_lost():
    run = AudioRun()
    await run.play(load("yes"), final=(1.7, "Yes."))
    assert "interruption_committed" in run.names(), run.names()
    assert run.session.stopped == ["Yes."]
    assert len(run.probe.interruptions) == 1


@pytest.mark.asyncio
async def test_interviewer_silent_means_immediate_commit_with_no_added_delay():
    run = AudioRun()
    await run.play(load("answer"), bot_speaks=False, tail=1.8)
    assert "interruption_tentative" not in run.names()
    detail, _ = run.event("interruption_committed")
    assert detail["source"] == "vad_immediate" and detail["waited_ms"] is None
    assert len(run.session.started) == 1


@pytest.mark.asyncio
async def test_disabling_the_check_restores_the_previous_immediate_interruption():
    run = AudioRun(confirm=0.0)
    await run.play(brief_speech_like_burst())
    detail, _ = run.event("interruption_committed")
    assert detail["source"] == "vad_immediate"
    assert len(run.probe.interruptions) == 1  # the old, noise-prone behaviour


@pytest.mark.asyncio
async def test_quiet_speech_that_the_vad_misses_still_interrupts_through_its_transcript():
    run = AudioRun()
    await run.play(quiet(load("answer"), 0.01), interim=(1.4, "I led the migration"), tail=1.0)
    assert "interruption_committed" in run.names(), run.names()
    detail, _ = run.event("interruption_committed")
    assert detail["source"] in {"transcript_without_vad", "transcript"}
    assert len(run.probe.interruptions) == 1


@pytest.mark.asyncio
async def test_echo_like_input_follows_the_same_duration_rule_and_is_not_speaker_aware():
    # Echo is the interviewer's own voice returning at a low level. A clipped leak is a brief
    # burst; a long one is indistinguishable from speech here (browser echo cancellation is the
    # defence), which is documented rather than hidden.
    leak = quiet(load("interviewer"), 0.5)
    run = AudioRun()
    await run.play(leak[: int(0.45 * RATE)])
    assert "interruption_rejected" in run.names() and run.probe.interruptions == []
    run = AudioRun()
    await run.play(leak, tail=1.8)
    assert "interruption_committed" in run.names()


@pytest.mark.asyncio
async def test_interviewer_finishing_while_the_sound_continues_hands_over_the_turn():
    run = AudioRun()
    await run.play(load("answer"), bot_stops_at=1.0, tail=1.8)
    detail, at = run.event("interruption_committed")
    assert detail["source"] == "interviewer_finished"
    assert len(run.session.started) == 1


# ---------------------------------------------------------------------------------------------
# Frame-level races


class Harness:
    def __init__(self, confirm=0.12, grace=0.12) -> None:
        self.events: list[tuple[str, dict]] = []
        self.turns = 0
        self.strategy = ConfirmedSpeechStartStrategy(
            confirm_secs=confirm,
            transcript_grace_secs=grace,
            observer=lambda n, **d: self.events.append((n, d)),
        )

        async def turn_started(_strategy, _params):
            self.turns += 1
            await self.strategy.handle_user_turn_started()

        self.strategy.add_event_handler("on_user_turn_started", turn_started)

    def names(self):
        return [n for n, _ in self.events]

    async def send(self, frame) -> ProcessFrameResult:
        return await self.strategy.process_frame(frame)


def word(text="yes", cls=TranscriptionFrame):
    return cls(text=text, user_id="c", timestamp="t")


@pytest.mark.asyncio
async def test_vad_stop_before_the_window_then_silence_rejects_exactly_once():
    h = Harness()
    await h.send(BotStartedSpeakingFrame())
    await h.send(VADUserStartedSpeakingFrame())
    await asyncio.sleep(0.03)
    await h.send(VADUserStoppedSpeakingFrame(stop_secs=0.35))
    await asyncio.sleep(0.3)
    assert h.turns == 0
    assert h.names().count("interruption_rejected") == 1


@pytest.mark.asyncio
async def test_transcript_racing_the_window_commits_exactly_once():
    h = Harness(confirm=0.1)
    await h.send(BotStartedSpeakingFrame())
    await h.send(VADUserStartedSpeakingFrame())
    await asyncio.sleep(0.095)  # transcript lands as the window closes
    await h.send(word("stop", InterimTranscriptionFrame))
    await asyncio.sleep(0.2)
    assert h.turns == 1 and h.names().count("interruption_committed") == 1


@pytest.mark.asyncio
async def test_transcript_after_a_rejection_still_starts_the_turn_so_words_are_not_lost():
    h = Harness(grace=0.05)
    await h.send(BotStartedSpeakingFrame())
    await h.send(VADUserStartedSpeakingFrame())
    await h.send(VADUserStoppedSpeakingFrame(stop_secs=0.35))
    await asyncio.sleep(0.2)
    assert h.turns == 0 and "interruption_rejected" in h.names()
    await h.send(word("actually wait"))
    assert h.turns == 1  # late, but the candidate's words are never discarded


@pytest.mark.asyncio
async def test_interviewer_stopping_during_grace_does_not_turn_noise_into_a_turn():
    h = Harness()
    await h.send(BotStartedSpeakingFrame())
    await h.send(VADUserStartedSpeakingFrame())
    await h.send(VADUserStoppedSpeakingFrame(stop_secs=0.35))
    await h.send(BotStoppedSpeakingFrame())  # sound already over: nothing to hand over
    await asyncio.sleep(0.25)
    assert h.turns == 0 and "interruption_rejected" in h.names()


@pytest.mark.asyncio
async def test_interviewer_stopping_while_sound_continues_commits_without_waiting():
    h = Harness(confirm=5.0)
    await h.send(BotStartedSpeakingFrame())
    await h.send(VADUserStartedSpeakingFrame())
    result = await h.send(BotStoppedSpeakingFrame())
    assert result is ProcessFrameResult.STOP and h.turns == 1
    detail = dict(h.events)["interruption_committed"]
    assert detail["source"] == "interviewer_finished"


@pytest.mark.asyncio
async def test_sound_resuming_during_the_grace_period_is_one_continuing_sound():
    h = Harness(confirm=0.2, grace=0.15)
    await h.send(BotStartedSpeakingFrame())
    await h.send(VADUserStartedSpeakingFrame())
    await asyncio.sleep(0.05)
    await h.send(VADUserStoppedSpeakingFrame(stop_secs=0.35))
    await asyncio.sleep(0.05)
    await h.send(VADUserStartedSpeakingFrame())
    await asyncio.sleep(0.3)
    assert h.turns == 1
    assert dict(h.events)["interruption_committed"]["source"] == "sustained_sound"
    assert h.names().count("interruption_tentative") == 1


@pytest.mark.asyncio
async def test_a_turn_already_in_progress_is_not_restarted_by_more_frames():
    h = Harness()
    await h.send(VADUserStartedSpeakingFrame())  # interviewer silent: immediate
    assert h.turns == 1
    await h.send(word("more words", InterimTranscriptionFrame))
    await h.send(word("more words"))
    await h.send(VADUserStartedSpeakingFrame())
    assert h.turns == 1 and h.names().count("interruption_committed") == 1


@pytest.mark.asyncio
async def test_cleanup_cancels_a_pending_decision():
    h = Harness(confirm=0.05)
    await h.send(BotStartedSpeakingFrame())
    await h.send(VADUserStartedSpeakingFrame())
    await h.strategy.cleanup()
    await asyncio.sleep(0.15)
    assert h.turns == 0 and not h.strategy.tentative


@pytest.mark.asyncio
async def test_empty_transcripts_do_not_count_as_words():
    h = Harness()
    await h.send(BotStartedSpeakingFrame())
    await h.send(VADUserStartedSpeakingFrame())
    await h.send(word("   ", InterimTranscriptionFrame))
    assert h.turns == 0 and h.strategy.tentative


def test_observer_details_never_carry_transcript_or_audio():
    h = Harness()

    async def go():
        await h.send(BotStartedSpeakingFrame())
        await h.send(VADUserStartedSpeakingFrame())
        await h.send(word("my secret salary is high"))

    asyncio.run(go())
    blob = repr(h.events)
    assert "secret" not in blob and "salary" not in blob
    assert all(
        isinstance(v, (int, float, str, bool, type(None))) for _, d in h.events for v in d.values()
    )


# ---------------------------------------------------------------------------------------------
# Configuration and wiring


def test_settings_default_to_a_half_second_window_and_validate_bounds(tmp_path):
    required = {
        "DATABASE_URL": "postgresql+asyncpg://rehearse:local@127.0.0.1:5433/rehearse_test",
        "REHEARSE_AUTH_SECRET": "test-only-secret-at-least-thirty-two-characters",
        "REHEARSE_PUBLIC_BASE_URL": "http://127.0.0.1:7860",
        "REHEARSE_SMTP_HOST": "127.0.0.1",
        "REHEARSE_SMTP_FROM": "RehearseAI <no-reply@localhost>",
        "DEEPGRAM_API_KEY": "test-dg",
        "OPENAI_API_KEY": "test-oa",
        "CARTESIA_API_KEY": "test-ca",
    }
    missing = tmp_path / "none.env"
    base = Settings.load(required, env_file=missing)
    assert base.interrupt_confirm_secs == 0.5 and base.interrupt_transcript_grace_secs == 0.5
    off = Settings.load({**required, "REHEARSE_INTERRUPT_CONFIRM_SECS": "0"}, env_file=missing)
    assert off.interrupt_confirm_secs == 0.0
    with pytest.raises(Exception):  # noqa: B017, PT011 - any validation error is acceptable
        Settings.load({**required, "REHEARSE_INTERRUPT_CONFIRM_SECS": "9"}, env_file=missing)


def test_the_live_pipeline_uses_the_confirming_strategy_not_the_stock_vad_strategy():
    source = (Path(__file__).parents[2] / "src/rehearse_ai/voice/pipeline.py").read_text()
    assert "ConfirmedSpeechStartStrategy(" in source
    assert "VADUserTurnStartStrategy" not in source
    assert issubclass(ConfirmedSpeechStartStrategy, BaseUserTurnStartStrategy)
