"""Local filesystem storage implementation."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Final
from uuid import uuid4

from app.storage.base import SavePhotoResult, Storage


class LocalStorage(Storage):
    backend: Final[str] = "local"

    def __init__(self, base_dir: str) -> None:
        self.base_path = Path(base_dir)
        self.base_path.mkdir(parents=True, exist_ok=True)

    async def save_photo(
        self,
        user_id: int,
        tasting_id: int,
        data: bytes,
        content_type: str,
    ) -> SavePhotoResult:
        extension = _guess_extension(content_type)
        filename = f"{uuid4()}{extension}"
        target_dir = self.base_path / str(user_id) / str(tasting_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / filename
        file_path.write_bytes(data)

        return SavePhotoResult(
            backend="local",
            key=str(file_path),
            url=file_path.as_uri() if os.name != "nt" else None,
        )


def _guess_extension(content_type: str) -> str:
    mapping = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }
    return mapping.get(content_type, ".bin")
