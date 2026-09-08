"""Milestone M3 Media Adapter package: Formal Local Asset -> CanonicalMediaAsset."""

from .adapter import CanonicalMediaAssetAdapter, MANIFEST_FILENAME
from .models import (
    AlbumImageArtifact,
    CanonicalMediaAsset,
    CanonicalMediaType,
    InvalidMediaAssetError,
    ManifestInvalidError,
    ManifestNotFoundError,
    MediaAdapterError,
    MediaFileNotFoundError,
    MediaHashMismatchError,
    UnsupportedContentTypeError,
)

__all__ = [
    "AlbumImageArtifact",
    "CanonicalMediaAsset",
    "CanonicalMediaAssetAdapter",
    "CanonicalMediaType",
    "InvalidMediaAssetError",
    "MANIFEST_FILENAME",
    "ManifestInvalidError",
    "ManifestNotFoundError",
    "MediaAdapterError",
    "MediaFileNotFoundError",
    "MediaHashMismatchError",
    "UnsupportedContentTypeError",
]
