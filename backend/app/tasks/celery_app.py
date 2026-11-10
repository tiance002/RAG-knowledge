"""
Celery 应用配置
"""

import os
from celery import Celery
from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "rag_tasks",
    broker=settings.CELERY_BROKER_URL or settings.REDIS_URL,
    backend=settings.CELERY_RESULT_BACKEND or settings.CELERY_BROKER_URL or settings.REDIS_URL,
    include=[
        "app.tasks.parse_tasks",
        "app.tasks.dispatch",
    ],
)

# Celery 配置
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=settings.CELERY_TASK_TIME_LIMIT,
    task_soft_time_limit=(settings.CELERY_TASK_TIME_LIMIT - min(300, settings.CELERY_TASK_TIME_LIMIT // 2)
                          if settings.CELERY_TASK_TIME_LIMIT > 1 else None),
    worker_concurrency=settings.CELERY_WORKER_CONCURRENCY,
    task_default_queue=settings.CELERY_QUEUE_NAME,
    task_publish_retry=False,
    result_expires=86400,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": settings.CELERY_TASK_TIME_LIMIT + 600},
    worker_prefetch_multiplier=1,
)


# This runs after CLI defaults, before pool bootsteps. The setting page owns
# the effective pool size; changing it requires restarting this Worker.
from celery.signals import worker_init


@worker_init.connect
def apply_runtime_worker_settings(sender=None, **kwargs):
    import logging
    from app.services.runtime_settings import get_runtime_settings
    try:
        role = os.getenv('FILE_TASK_WORKER_ROLE', '').strip().lower()
        if role not in ('parse', 'index'):
            raise ValueError('FILE_TASK_WORKER_ROLE_MUST_BE_PARSE_OR_INDEX')
        if not settings.CHROMA_HOST:
            raise ValueError('FILE_TASK_WORKERS_REQUIRE_CHROMA_HOST')
        expected = 'document.' + role
        selected = set(sender.app.amqp.queues.consume_from.keys())
        if selected != {expected}:
            raise ValueError('FILE_TASK_WORKER_QUEUE_MUST_MATCH_ROLE')
        snapshot = get_runtime_settings()
        if getattr(sender, 'autoscale', None):
            raise ValueError('AUTOSCALE_CONFLICTS_WITH_PERSISTED_CONCURRENCY')
        concurrency = 1 if role == 'index' else snapshot.max_concurrency
        sender.concurrency = concurrency
        sender.app.conf.worker_concurrency = concurrency
        logging.getLogger(__name__).info('Worker concurrency=%s settings_revision=%s degraded=%s',
            concurrency, snapshot.revision, snapshot.degraded)
    except Exception:
        logging.getLogger(__name__).exception('Worker settings unavailable; refusing an unconfigured pool')
        # Celery signals catch Exception, so SystemExit prevents silently
        # continuing with a different CLI/environment concurrency.
        raise SystemExit(78)

# Forked workers must not inherit parent process database connections.
from celery.signals import worker_process_init

@worker_process_init.connect
def reset_child_database_pool(**kwargs):
    from app.core.database import engine
    engine.dispose(close=False)
