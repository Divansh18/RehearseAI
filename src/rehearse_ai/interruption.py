"""Decide when candidate sound becomes a committed interruption of the interviewer.

Why this exists. Pipecat's stock ``VADUserTurnStartStrategy`` commits a user turn, and with it an
interruption that cancels the interviewer's audio, on the very first VAD "speech started" frame
(Silero, after ``vad_start_secs`` of voiced audio). A door slam, cough, clatter or a short burst of
household noise can pass that bar, so the interviewer stopped talking and the page showed
"Listening" although nobody had spoken to it.

What this does. While the interviewer is audibly speaking, a VAD start is only *tentative*. The
interviewer keeps talking, nothing is cancelled and nothing reaches the interview, until one of:

- a transcript with at least one word arrives (STT heard words: commit at once, however short);
- the sound is still going after ``confirm_secs`` (sustained: commit);
- the interviewer stops by itself while the sound continues (it is simply the candidate's turn).

If the VAD reports silence first, the strategy waits ``transcript_grace_secs`` for a transcript
(short genuine answers such as "yes" or "stop" are transcribed just after they end), and otherwise
rejects the burst: no interruption, no state change, the pending question stays authoritative.

What it cannot do. It judges duration and whether STT heard words. It cannot tell the candidate
from anyone else speaking nearby, and it cannot tell sustained noise (TV, a vacuum cleaner, a long
echo) from speech. A transcript always commits, so noise that STT mis-hears as words still
interrupts. ``confirm_secs=0`` restores the previous behaviour exactly.

When the interviewer is silent there is nothing to cancel, so a VAD start commits immediately.
"""

import asyncio
import time
from collections.abc import Callable
from typing import Any

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start.base_user_turn_start_strategy import BaseUserTurnStartStrategy

Observer = Callable[..., None]


class ConfirmedSpeechStartStrategy(BaseUserTurnStartStrategy):
    """Commit a user turn (and its interruption) only for confirmed candidate sound."""

    def __init__(
        self,
        *,
        confirm_secs: float,
        transcript_grace_secs: float,
        observer: Observer | None = None,
        clock: Callable[[], float] = time.monotonic,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._confirm_secs = max(0.0, confirm_secs)
        self._grace_secs = max(0.0, transcript_grace_secs)
        self._observer = observer
        self._clock = clock
        self._bot_speaking = False
        self._vad_speaking = False
        self._turn_active = False
        self._tentative_at: float | None = None
        self._stopped_at: float | None = None
        self._tentative_words = 0
        self._timer: asyncio.Task[None] | None = None

    # -- diagnostics -------------------------------------------------------------------------

    def _observe(self, name: str, **details: Any) -> None:
        """Report timings and counts only; never audio and never transcript text."""
        if self._observer is not None:
            self._observer(name, **details)

    def _since_tentative_ms(self) -> int | None:
        if self._tentative_at is None:
            return None
        return round((self._clock() - self._tentative_at) * 1000)

    # -- state -------------------------------------------------------------------------------

    @property
    def tentative(self) -> bool:
        return self._tentative_at is not None

    def _cancel_timer(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None and not timer.done() and timer is not asyncio.current_task():
            timer.cancel()

    def _clear(self) -> None:
        self._cancel_timer()
        self._tentative_at = None
        self._stopped_at = None
        self._tentative_words = 0

    async def handle_user_turn_started(self) -> None:
        self._turn_active = True
        self._clear()
        await super().handle_user_turn_started()

    async def handle_user_turn_stopped(self) -> None:
        self._turn_active = False
        self._clear()
        await super().handle_user_turn_stopped()

    async def cleanup(self) -> None:
        self._clear()
        await super().cleanup()

    # -- decisions ---------------------------------------------------------------------------

    async def _commit(self, source: str) -> ProcessFrameResult:
        if self._turn_active:
            # Already inside a candidate turn: nothing to decide and nothing to cancel.
            self._clear()
            return ProcessFrameResult.CONTINUE
        waited = self._since_tentative_ms()
        self._observe(
            "interruption_committed",
            source=source,
            bot_speaking=self._bot_speaking,
            waited_ms=waited,
            words=self._tentative_words,
        )
        self._clear()
        await self.trigger_user_turn_started()
        return ProcessFrameResult.STOP

    def _reject(self, reason: str) -> None:
        self._observe(
            "interruption_rejected",
            reason=reason,
            bot_speaking=self._bot_speaking,
            tentative_ms=self._since_tentative_ms(),
            vad_active_at_decision=self._vad_speaking,
        )
        self._clear()

    def _arm(self, delay: float, action: Callable[[], Any]) -> None:
        self._cancel_timer()

        async def run() -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            await action()

        self._timer = asyncio.create_task(run())

    async def _confirm_window_ended(self) -> None:
        if self.tentative and self._vad_speaking:
            await self._commit("sustained_sound")

    async def _grace_ended(self) -> None:
        if self.tentative and not self._vad_speaking:
            self._reject("brief_sound_no_words")

    # -- frames ------------------------------------------------------------------------------

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            if self.tentative and self._vad_speaking:
                # Nothing is left to cancel and the sound continues: it is the candidate's turn.
                return await self._commit("interviewer_finished")
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            return await self._vad_started()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._vad_stopped()
        elif isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            words = len(frame.text.split())
            if words:
                self._tentative_words = max(self._tentative_words, words)
                # Words were heard. This also covers speech too quiet for the VAD to flag.
                return await self._commit(
                    "transcript" if self.tentative else "transcript_without_vad"
                )
        return ProcessFrameResult.CONTINUE

    async def _vad_started(self) -> ProcessFrameResult:
        self._vad_speaking = True
        if not self._bot_speaking or self._confirm_secs <= 0:
            return await self._commit("vad_immediate")
        if not self.tentative:
            self._tentative_at = self._clock()
            self._observe("interruption_tentative", bot_speaking=True)
        # Speech resuming during the grace period is the same sound continuing.
        self._stopped_at = None
        remaining = max(0.0, self._confirm_secs - (self._since_tentative_ms() or 0) / 1000)
        self._arm(remaining, self._confirm_window_ended)
        return ProcessFrameResult.CONTINUE

    def _vad_stopped(self) -> None:
        self._vad_speaking = False
        if not self.tentative:
            return
        self._stopped_at = self._clock()
        self._observe("interruption_tentative_ended", vad_active_ms=self._since_tentative_ms())
        if self._grace_secs <= 0:
            self._reject("brief_sound_no_words")
            return
        self._arm(self._grace_secs, self._grace_ended)
