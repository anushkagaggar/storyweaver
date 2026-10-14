"""Composition root: builds the app's long-lived services from settings, and tears them down."""

import asyncio
import logging
from dataclasses import dataclass, field

from groq import AsyncGroq
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from storyweaver.adapters.groq_llm import GroqLLM
from storyweaver.config import Settings
from storyweaver.jobs.worker import Worker
from storyweaver.llm.planner import StoryPlanner
from storyweaver.orchestrator.story_service import PlanStoryJob, StoryService, make_story_failure_hook
from storyweaver.routing.llm import RoutedLLM
from storyweaver.routing.router import ModelRouter
from storyweaver.routing.store import InMemoryQuotaStore, QuotaStore, RedisQuotaStore
from storyweaver.safety.guards import GuardModels, InputGuard, OutputGuard, PolicyChecker
from storyweaver.storage.db import create_engine, create_session_factory
from storyweaver.storage.models import JobKind

logger = logging.getLogger(__name__)


@dataclass
class Services:
    engine: AsyncEngine | None = None
    sessions: async_sessionmaker[AsyncSession] | None = None
    redis: Redis | None = None
    groq: AsyncGroq | None = None
    stories: StoryService | None = None
    worker: Worker | None = None
    _worker_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def start_worker(self) -> None:
        if self.worker is not None and self._worker_task is None:
            self._worker_task = asyncio.create_task(self.worker.run_forever(self._stop), name="job-worker")

    async def close(self) -> None:
        self._stop.set()
        if self._worker_task is not None:
            await self._worker_task
        if self.groq is not None:
            await self.groq.close()
        if self.redis is not None:
            await self.redis.aclose()
        if self.engine is not None:
            await self.engine.dispose()


def build_services(settings: Settings) -> Services:
    services = Services()
    if settings.database_url is None:
        logger.warning("DATABASE_URL is not set: story endpoints are disabled.")
        return services

    services.engine = create_engine(settings.database_url)
    services.sessions = create_session_factory(services.engine)
    store: QuotaStore
    if settings.redis_url:
        services.redis = Redis.from_url(settings.redis_url)
        store = RedisQuotaStore(services.redis)
    else:
        # Single-process fallback: quota is not shared across workers or restarts.
        logger.warning("REDIS_URL is not set: using in-memory quota counters.")
        store = InMemoryQuotaStore()

    if settings.groq_api_key is None:
        # Fail closed: without the safety models no story can be accepted.
        logger.warning("GROQ_API_KEY is not set: story creation is disabled (no safety checks, no worker).")
        services.stories = StoryService(services.sessions, input_guard=None)
        return services

    services.groq = AsyncGroq(api_key=settings.groq_api_key.get_secret_value(), max_retries=0, timeout=120.0)
    llm = RoutedLLM(
        GroqLLM(services.groq, reasoning_effort=settings.planner_reasoning_effort),
        ModelRouter(store, margin=settings.quota_safety_margin),
    )
    guard_models = GuardModels(
        prompt_guard=settings.prompt_guard_model,
        safeguard=settings.safeguard_model,
        prompt_guard_threshold=settings.prompt_guard_threshold,
    )
    policy = PolicyChecker(llm, guard_models)
    services.stories = StoryService(services.sessions, input_guard=InputGuard(llm, policy, guard_models))
    planner = StoryPlanner(
        llm,
        planner_model=settings.planner_model_id,
        repair_model=settings.teacher_model_id,
        max_tokens=settings.planner_max_tokens,
        temperature=settings.planner_temperature,
    )
    if settings.worker_enabled:
        services.worker = Worker(
            services.sessions,
            {JobKind.PLAN: PlanStoryJob(services.sessions, planner, OutputGuard(policy))},
            on_final_failure=make_story_failure_hook(services.sessions),
        )
    return services
