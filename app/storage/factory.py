"""Factory helpers for choosing the correct storage backend."""
from __future__ import annotations

import logging

from app import config
from app.config import S3Settings
from app.storage.base import Storage
from app.storage.local import LocalStorage
from app.storage.s3 import S3Health, S3Storage

logger = logging.getLogger(__name__)


def get_storage_from_env() -> tuple[Storage, S3Health | None]:
    if config.is_s3_enabled():
        settings: S3Settings = config.get_s3_settings()
        storage = S3Storage(settings)
        health = storage.health()
        if health.ok:
            logger.info("[S3] OK (bucket=%s)", settings.bucket)
        else:
            logger.warning("[S3] WARN: %s", health.message)
        return storage, health

    local_settings = config.get_local_storage_settings()
    storage = LocalStorage(local_settings.base_dir)
    logger.info("[S3] Disabled -> using local storage at %s", local_settings.base_dir)
    return storage, None
