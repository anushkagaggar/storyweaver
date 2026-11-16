"""Composition root: builds the app's long-lived services from settings, and tears them down."""

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta

from groq import AsyncGroq
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from storyweaver.adapters.faster_whisper import FasterWhisper
from storyweaver.adapters.groq_llm import GroqLLM
from storyweaver.adapters.groq_orpheus import GroqOrpheus
from storyweaver.adapters.groq_whisper import GroqWhisper
from storyweaver.adapters.kokoro_tts import Kokoro
from storyweaver.config import Settings
from storyweaver.imaging.sd15 import SD15Local, nvidia_gpu_present
from storyweaver.jobs.worker import Handler, Worker
from storyweaver.llm.planner import StoryPlanner
from storyweaver.observability.tracing import NoopTracer, Tracer
from storyweaver.orchestrator.illustration_jobs import (
    IllustrateSceneJob,
    Illustrator,
    ImageSettings,
    LocalIllustrator,
)
from storyweaver.orchestrator.narration import Narrator
from storyweaver.orchestrator.narration_jobs import NarrateSceneJob, StoryNarration
from storyweaver.orchestrator.story_deletion import RetentionSweeper, StoryDeletion
from storyweaver.orchestrator.story_service import PlanStoryJob, StoryService, make_story_failure_hook
from storyweaver.routing.limits import VisitorLimiter
from storyweaver.routing.llm import RoutedLLM
from storyweaver.routing.router import ModelRouter
from storyweaver.routing.store import InMemoryQuotaStore, QuotaStore, RedisQuotaStore
from storyweaver.routing.stt import RoutedSTT
from storyweaver.routing.tts import TTSRouter
from storyweaver.safety.guards import GuardModels, InputGuard, OutputGuard, PolicyChecker
from storyweaver.storage.blob import BlobStore, S3BlobStore, S3Config
from storyweaver.storage.db import create_engine, create_session_factory
from storyweaver.storage.models import JobKind

logger = logging.getLogger(__name__)

# Gradio's launch() gives the server about 5 s to come up and reports "Cannot find empty port" if it is late.
# Loading the picture model holds Python's GIL for seconds, so it starts only once the server is surely up.
WARM_UP_DELAY_SECONDS = 15.0


@dataclass
class Services:
    engine: AsyncEngine | None = None
    sessions: async_sessionmaker[AsyncSession] | None = None
    redis: Redis | None = None
    groq: AsyncGroq | None = None
    stories: StoryService | None = None
    stt: RoutedSTT | None = None
    narrator: Narrator | None = None
    blobs: BlobStore | None = None
    deletion: StoryDeletion | None = None
    sweeper: RetentionSweeper | None = None
    worker: Worker | None = None
    tracer: Tracer = field(default_factory=NoopTracer)
    # Loads the picture model in the background after start-up (IMAGE_ENGINE=sd15).
    warm_up: Callable[[], Awaitable[None]] | None = None
    _worker_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _sweeper_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _warm_up_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def start_worker(self, warm_up_delay_seconds: float = WARM_UP_DELAY_SECONDS) -> None:
        if self.worker is not None and self._worker_task is None:
            self._worker_task = asyncio.create_task(self.worker.run_forever(self._stop), name="job-worker")
        if self.sweeper is not None and self._sweeper_task is None:
            self._sweeper_task = asyncio.create_task(
                self.sweeper.run_forever(self._stop), name="story-expiry"
            )
        if self.warm_up is not None and self._warm_up_task is None:
            self._warm_up_task = asyncio.create_task(
                self._warm_up_later(self.warm_up, warm_up_delay_seconds), name="picture-warm-up"
            )

    async def _warm_up_later(self, warm_up: Callable[[], Awaitable[None]], delay_seconds: float) -> None:
        """Wait until the server is up, then warm up. A failure is logged, never raised: the first
        picture then loads the model itself (and retries a failed download)."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay_seconds)
            return  # shutting down before the delay ended
        except TimeoutError:
            pass
        try:
            await warm_up()
        except Exception as exc:
            logger.warning("Picture warm-up failed (%s); the first picture will load the model.", exc)

    async def close(self) -> None:
        self._stop.set()
        if self._warm_up_task is not None:
            self._warm_up_task.cancel()  # the load itself runs on in its thread; nothing waits for it
        for task in (self._worker_task, self._sweeper_task):
            if task is not None:
                await task
        self.tracer.shutdown()
        if self.groq is not None:
            await self.groq.close()
        if self.redis is not None:
            await self.redis.aclose()
        if self.engine is not None:
            await self.engine.dispose()


def build_services(settings: Settings) -> Services:
    services = Services()
    store: QuotaStore
    if settings.redis_url:
        services.redis = Redis.from_url(settings.redis_url)
        store = RedisQuotaStore(services.redis)
    else:
        # Single-process fallback: quota is not shared across workers or restarts.
        logger.warning("REDIS_URL is not set: using in-memory quota counters.")
        store = InMemoryQuotaStore()
    router = ModelRouter(store, margin=settings.quota_safety_margin)
    if settings.groq_api_key is not None:
        services.groq = AsyncGroq(
            api_key=settings.groq_api_key.get_secret_value(), max_retries=0, timeout=120.0
        )
    # Speech-to-text needs no database, so it is available even when story storage isn't.
    services.stt = build_stt(settings, services.groq, router)
    services.narrator = build_narrator(settings, services.groq, router)

    if settings.database_url is None:
        logger.warning("DATABASE_URL is not set: story endpoints are disabled.")
        return services

    services.engine = create_engine(settings.database_url)
    services.sessions = create_session_factory(services.engine)
    services.blobs = build_blob_store(settings)
    services.deletion = StoryDeletion(services.sessions, services.blobs)
    if settings.worker_enabled and settings.story_retention_days > 0:
        services.sweeper = RetentionSweeper(
            services.deletion,
            retention=timedelta(days=settings.story_retention_days),
            interval_seconds=settings.retention_sweep_seconds,
        )

    if services.groq is None:
        # Fail closed: without the safety models no story can be accepted.
        logger.warning("GROQ_API_KEY is not set: story creation is disabled (no safety checks, no worker).")
        services.stories = StoryService(services.sessions, input_guard=None)
        return services

    services.tracer = build_tracer(settings)
    llm = RoutedLLM(
        GroqLLM(services.groq, reasoning_effort=settings.planner_reasoning_effort),
        router,
        tracer=services.tracer,
    )
    guard_models = GuardModels(
        prompt_guard=settings.prompt_guard_model,
        safeguard=settings.safeguard_model,
        prompt_guard_threshold=settings.prompt_guard_threshold,
    )
    policy = PolicyChecker(llm, guard_models)
    visitor_limiter = VisitorLimiter(
        store,
        per_hour=settings.visitor_stories_per_hour,
        per_day=settings.visitor_stories_per_day,
        secret=settings.storyweaver_api_key.get_secret_value(),
    )
    services.stories = StoryService(
        services.sessions, input_guard=InputGuard(llm, policy, guard_models), visitor_limiter=visitor_limiter
    )
    planner = StoryPlanner(
        llm,
        planner_model=settings.planner_model_id,
        repair_model=settings.teacher_model_id,
        max_tokens=settings.planner_max_tokens,
        temperature=settings.planner_temperature,
    )
    narration = None
    if services.blobs is None:
        logger.warning("Object storage is not configured (S3_*): stories will have no narration.")
    elif services.narrator is not None:
        narration = StoryNarration(services.narrator)
    illustrate = settings.image_engine != "none" and services.blobs is not None
    if settings.image_engine != "none" and services.blobs is None:
        logger.warning("Object storage is not configured (S3_*): stories will have no pictures.")
    if settings.worker_enabled:
        handlers: dict[JobKind, Handler] = {
            JobKind.PLAN: PlanStoryJob(
                services.sessions, planner, OutputGuard(policy), narration, illustrate=illustrate
            )
        }
        if narration is not None and services.blobs is not None:
            handlers[JobKind.TTS] = NarrateSceneJob(services.sessions, narration.narrator, services.blobs)
        if illustrate and services.blobs is not None:
            # No illustrator (IMAGE_ENGINE=placeholder, or no usable GPU): every page gets the placeholder.
            illustrator = build_illustrator(settings, services.blobs)
            handlers[JobKind.IMAGE] = IllustrateSceneJob(
                services.sessions, services.blobs, image_settings(settings), illustrator=illustrator
            )
            if isinstance(illustrator, LocalIllustrator) and isinstance(illustrator.generator, SD15Local):
                services.warm_up = functools.partial(
                    illustrator.generator.warm_up,
                    settings.image_model,
                    settings.image_model_revision,
                    settings.image_sampler,
                )
        services.worker = Worker(
            services.sessions, handlers, on_final_failure=make_story_failure_hook(services.sessions)
        )
    return services


def build_illustrator(settings: Settings, blobs: BlobStore) -> Illustrator | None:
    """The picture maker for IMAGE_ENGINE, or None for placeholders.

    sd15 needs a CUDA GPU in this process. On the ZeroGPU Space there is none outside a `@spaces.GPU` call
    (and its CUDA checks are patched), so APP_ENV=prod never uses it: GPU pictures there are step 4's job.
    Without a GPU it logs why and falls back to placeholders rather than drawing for minutes on the CPU.
    """
    if settings.image_engine != "sd15":
        return None
    if settings.app_env == "prod":
        logger.warning("IMAGE_ENGINE=sd15 is for local Docker only; using placeholder pictures.")
        return None
    if not nvidia_gpu_present():
        logger.warning("IMAGE_ENGINE=sd15 but no NVIDIA GPU here; using placeholder pictures.")
        return None
    logger.info("Pictures: Stable Diffusion 1.5 on the local GPU (%s).", settings.image_model)
    return LocalIllustrator(SD15Local(), blobs)


def image_settings(settings: Settings) -> ImageSettings:
    return ImageSettings(
        model=settings.image_model,
        model_revision=settings.image_model_revision,
        width=settings.image_width,
        height=settings.image_height,
        sampler=settings.image_sampler,
        steps=settings.image_steps,
        guidance=settings.image_guidance,
    )


def build_blob_store(settings: Settings) -> BlobStore | None:
    """The audio store, or None when it is not configured or cannot be built.

    A misconfigured store must not take the whole app down: without it stories still plan and play back as
    text, and narration degrades. v2.0.1 crashed on start because boto3 rejected a scheme-less endpoint
    inside the lifespan, so every route died over one feature's setting.
    """
    if not settings.blob_storage_enabled:
        return None
    assert settings.s3_endpoint_url and settings.s3_access_key_id and settings.s3_secret_access_key  # noqa: S101
    key_id = settings.s3_access_key_id.get_secret_value()
    # What the process actually resolved, so a wrong Space setting is visible without guessing. The key is
    # identified by its last 4 characters and its length only; the secret is never logged at all.
    logger.info(
        "Object storage: endpoint=%s region=%s bucket=%s key=...%s (%d chars)",
        settings.s3_endpoint_url,
        settings.s3_region,
        settings.s3_bucket,
        key_id[-4:],
        len(key_id),
    )
    try:
        return S3BlobStore(
            S3Config(
                endpoint_url=settings.s3_endpoint_url,
                public_endpoint_url=settings.s3_public_endpoint_url,
                region=settings.s3_region,
                bucket=settings.s3_bucket,
                access_key_id=settings.s3_access_key_id.get_secret_value(),
                secret_access_key=settings.s3_secret_access_key.get_secret_value(),
                create_bucket=settings.s3_create_bucket,
            )
        )
    except Exception:
        # The endpoint or region is unusable. Log it loudly (never the credentials) and carry on without
        # audio storage; /health/ready reports the store as unavailable.
        logger.exception(
            "Object storage is misconfigured (endpoint %r, region %r, bucket %r): narration audio is "
            "disabled until it is fixed.",
            settings.s3_endpoint_url,
            settings.s3_region,
            settings.s3_bucket,
        )
        return None


def build_stt(settings: Settings, groq_client: AsyncGroq | None, router: ModelRouter) -> RoutedSTT | None:
    """Groq Whisper with a faster-whisper fallback, or faster-whisper alone (STT_ENGINE=local).

    faster-whisper loads lazily on first use, so building it here costs nothing at startup.
    """
    language = settings.stt_language or None
    local = (
        FasterWhisper(model_size=settings.stt_local_model, language=language)
        if settings.stt_engine == "local" or settings.stt_local_fallback
        else None
    )
    primary = None
    if settings.stt_engine == "groq":
        if groq_client is None:
            logger.warning("GROQ_API_KEY is not set: speech-to-text runs locally only.")
        else:
            primary = GroqWhisper(groq_client, model=settings.stt_model, language=language)
    if primary is None and local is None:
        logger.warning("Speech-to-text is disabled (no Groq key and STT_LOCAL_FALLBACK=false).")
        return None
    return RoutedSTT(router=router, primary=primary, primary_model=settings.stt_model, fallback=local)


def build_narrator(settings: Settings, groq_client: AsyncGroq | None, router: ModelRouter) -> Narrator:
    """Orpheus within quota, Kokoro otherwise (TTS_ENGINE picks or forces one).

    Kokoro loads lazily on first use, so building it here costs nothing at startup.
    """
    orpheus = (
        GroqOrpheus(groq_client, model=settings.tts_model)
        if groq_client is not None and settings.tts_engine != "kokoro"
        else None
    )
    if orpheus is None and settings.tts_engine == "orpheus":
        logger.warning("TTS_ENGINE=orpheus but GROQ_API_KEY is not set: using Kokoro.")
    kokoro = (
        Kokoro(directory=settings.kokoro_dir, variant=settings.kokoro_variant)
        if orpheus is None or settings.tts_engine != "orpheus"
        else None
    )
    return Narrator(
        TTSRouter(
            router=router,
            orpheus=orpheus,
            orpheus_model=settings.tts_model,
            kokoro=kokoro,
            mode=settings.tts_engine if orpheus is not None else "kokoro",
            max_wait_seconds=settings.tts_max_wait_seconds,
        )
    )


def build_tracer(settings: Settings) -> Tracer:
    """Langfuse when both keys are set; otherwise a tracer that records nothing."""
    if not settings.tracing_enabled:
        return NoopTracer()
    from storyweaver.adapters.langfuse_tracer import LangfuseTracer  # imported only when tracing is on

    assert settings.langfuse_public_key is not None and settings.langfuse_secret_key is not None  # noqa: S101
    logger.info(
        "Langfuse tracing on (host=%s, content capture=%s).",
        settings.langfuse_host,
        settings.langfuse_capture_content,
    )
    return LangfuseTracer(
        public_key=settings.langfuse_public_key.get_secret_value(),
        secret_key=settings.langfuse_secret_key.get_secret_value(),
        host=settings.langfuse_host,
        capture_content=settings.langfuse_capture_content,
        environment=settings.app_env,
    )
