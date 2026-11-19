"""The services the routes share, built once by ``create_app``."""

from dataclasses import dataclass

from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequestHandler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rehearse_ai.accounts.service import AccountService
from rehearse_ai.config import Settings
from rehearse_ai.jobs.runner import JobRunner
from rehearse_ai.records.store import InterviewStore
from rehearse_ai.voice.registry import LiveSessionRegistry


@dataclass
class ServerContext:
    settings: Settings
    build_id: str
    prompt_version: str
    db_sessions: async_sessionmaker[AsyncSession]  # SQLAlchemy sessions, not candidate or voice
    accounts: AccountService
    store: InterviewStore
    jobs: JobRunner
    live: LiveSessionRegistry
    webrtc: SmallWebRTCRequestHandler
