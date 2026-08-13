"""Media normalization boundary between downloaders/manual files and the knowledge pipeline."""

from .media_mux import MediaAsset, prepare_assets

__all__ = ["MediaAsset", "prepare_assets"]
