"""Common storage interfaces for photo persistence."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


PhotoBackend = Literal["local", "s3"]


@dataclass(slots=True)
class SavePhotoResult:
    backend: PhotoBackend
    key: str
    url: str | None


class Storage(Protocol):
    """Photo storage protocol."""

    async def save_photo(
        self,
        user_id: int,
        tasting_id: int,
        data: bytes,
        content_type: str,
    ) -> SavePhotoResult:
        """Persist photo bytes and return metadata."""
        raise NotImplementedError
