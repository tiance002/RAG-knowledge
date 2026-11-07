"""
Celery 应用配置
"""

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
        snapshot = get_runtime_settings()
        if getattr(sender, 'autoscale', None):
            raise ValueError('AUTOSCALE_CONFLICTS_WITH_PERSISTED_CONCURRENCY')
        sender.concurrency = snapshot.max_concurrency
        sender.app.conf.worker_concurrency = snapshot.max_concurrency
        logging.getLogger(__name__).info('Worker concurrency=%s settings_revision=%s degraded=%s',
            snapshot.max_concurrency, snapshot.revision, snapshot.degraded)
    except Exception:
        logging.getLogger(__name__).exception('Worker settings unavailable; refusing an unconfigured pool')
        # Celery signals catch Exception, so SystemExit prevents silently
        # continuing with a different CLI/environment concurrency.
        raise SystemExit(78)
