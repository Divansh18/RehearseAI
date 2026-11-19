"""The running voice sessions of this server process.

A voice session is one live WebRTC interview connection (``LiveSession``). It is distinct from a
candidate session (restricted invitation access, ``invitations.access``) and from a database
session (SQLAlchemy). This registry only tracks which voice sessions are running so the server
can find, stop and tell them about storage failures; it holds no interview rules.
"""

import asyncio

from rehearse_ai.voice.pipeline import LiveSession


class LiveSessionRegistry:
    def __init__(self) -> None:
        self.sessions: set[LiveSession] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self, live: LiveSession) -> None:
        """Register a voice session and run it until it ends; it removes itself when done."""
        self.sessions.add(live)
        task = asyncio.create_task(live.run())
        self._tasks.add(task)

        def finished(done: asyncio.Task[None]) -> None:
            self._tasks.discard(done)
            self.sessions.discard(live)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)

    def is_running(self, *, session_id: str, assignment_id: str) -> bool:
        return any(
            live.log.session_id == session_id and live.assignment_id == assignment_id
            for live in self.sessions
        )

    async def report_storage_failure(self, session_id: str) -> None:
        """Tell the browser of a running voice session that its record is not being saved."""
        for live in tuple(self.sessions):
            if live.log.session_id == session_id:
                await live.connection.send_app_message(
                    {"type": "rehearseai", "event": {"event": "persistence_error"}}
                )

    async def disconnect_owned_by(self, owner_id: str) -> None:
        for live in tuple(self.sessions):
            if live.assignment_id == owner_id:
                await live.stop()
                await live.connection.disconnect()

    async def shutdown(self) -> None:
        for live in tuple(self.sessions):
            await live.stop()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
