from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .backends import transcribe
from .backends.llm import ensure_segment_ids, knowledge_fingerprint
from .config import AppConfig
from .intake import MediaAsset, prepare_assets
from .knowledge import build_knowledge, ensure_source_schema, visual_usage_summary
from .knowledge.lifecycle import ensure_lm_studio_loaded, record_lifecycle, unload_lm_studio
from .publishing import publish_completed_media
from .provenance import source_for_media
from .render import render_markdown, render_transcript
from .storage import atomic_write_json, atomic_write_text, copy_file_atomically, load_json, sha256_file, utc_now
from .visual import build_visual_evidence
from .visual.service import visual_pipeline_fingerprint


STAGES = ("source", "audio", "asr", "visual", "knowledge", "publish_media")
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi"}


class StageError(RuntimeError):
    pass


def _run(command: list[str]) -> str:
    # FFmpeg/ffprobe emit UTF-8 metadata while many Windows consoles default to GBK.
    # Read bytes explicitly so non-ASCII filenames and tags cannot crash the worker.
    process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout = process.stdout.decode("utf-8", errors="replace")
    stderr = process.stderr.decode("utf-8", errors="replace")
    if process.returncode:
        raise StageError(f"Command failed ({process.returncode}): {' '.join(command)}\n{stderr[-2000:]}")
    return stdout


def _probe(config: AppConfig, source: Path) -> dict[str, Any]:
    if not config.ffprobe.exists():
        logging.warning("ffprobe was not found; using the less precise FFmpeg metadata fallback.")
        return _probe_with_ffmpeg(config, source)
    output = _run([str(config.ffprobe), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source)])
    details = __import__("json").loads(output)
    video_stream = next((stream for stream in details.get("streams", []) if stream.get("codec_type") == "video"), {})
    audio_stream = next((stream for stream in details.get("streams", []) if stream.get("codec_type") == "audio"), {})
    return {
        "duration": round(float(details["format"].get("duration", 0)), 3),
        "format": details["format"].get("format_name"),
        "size_bytes": int(details["format"].get("size", 0)),
        "video": {key: video_stream.get(key) for key in ("codec_name", "width", "height", "r_frame_rate")},
        "audio": {key: audio_stream.get(key) for key in ("codec_name", "sample_rate", "channels")},
    }


def _probe_with_ffmpeg(config: AppConfig, source: Path) -> dict[str, Any]:
    """Extract enough metadata from FFmpeg when a portable package lacks ffprobe."""
    process = subprocess.run([str(config.ffmpeg), "-hide_banner", "-i", str(source), "-f", "null", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output = process.stderr.decode("utf-8", errors="replace")
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    duration = 0.0
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        duration = round(int(hours) * 3600 + int(minutes) * 60 + float(seconds), 3)
    video_match = re.search(r"Video:\s*([^,]+).*?(\d{2,5})x(\d{2,5}).*?(\d+(?:\.\d+)?)\s*fps", output)
    audio_match = re.search(r"Audio:\s*([^,]+).*?(\d+)\s*Hz,\s*([^,\r\n]+)", output)
    if not duration:
        raise StageError(f"FFmpeg could not determine the duration for {source.name}.")
    return {
        "duration": duration, "format": source.suffix.lstrip("."), "size_bytes": source.stat().st_size,
        "video": {
            "codec_name": video_match.group(1).strip() if video_match else None,
            "width": int(video_match.group(2)) if video_match else None,
            "height": int(video_match.group(3)) if video_match else None,
            "r_frame_rate": video_match.group(4) if video_match else None,
        },
        "audio": {
            "codec_name": audio_match.group(1).strip() if audio_match else None,
            "sample_rate": audio_match.group(2) if audio_match else None,
            "channels": audio_match.group(3).strip() if audio_match else None,
        },
    }


def _empty_state(video_id: str, source_hash: str, source_path: Path, config: AppConfig) -> dict[str, Any]:
    return {
        "video_id": video_id,
        "content_hash": source_hash,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "status": "NEW",
        "config_fingerprint": config.fingerprint,
        "original_input_path": str(source_path.resolve()),
        "stages": {stage: {"status": "pending"} for stage in STAGES},
        "error": None,
    }


def _save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    stage_statuses = [state["stages"][stage]["status"] for stage in STAGES]
    state["status"] = "COMPLETED" if all(status in {"completed", "skipped"} for status in stage_statuses) else "PROCESSING"
    atomic_write_json(path, state)


def _stage(state: dict[str, Any], state_path: Path, name: str, force: bool, action: Callable[[], dict[str, Any]]) -> None:
    previous = state["stages"][name]
    if previous.get("status") == "completed" and not force:
        logging.info("[%s] already complete; skipped", name)
        return
    started = time.perf_counter()
    state["stages"][name] = {"status": "running", "started_at": utc_now()}
    state["error"] = None
    _save_state(state_path, state)
    try:
        detail = action()
    except Exception as error:
        state["stages"][name] = {
            "status": "failed", "started_at": state["stages"][name]["started_at"], "finished_at": utc_now(),
            "error": f"{type(error).__name__}: {error}", "duration_seconds": round(time.perf_counter() - started, 3)
        }
        state["status"], state["error"] = "FAILED", {"stage": name, "message": state["stages"][name]["error"]}
        atomic_write_json(state_path, state)
        raise
    state["stages"][name] = {
        "status": "completed", "started_at": state["stages"][name]["started_at"], "finished_at": utc_now(),
        "duration_seconds": round(time.perf_counter() - started, 3), **detail
    }
    _save_state(state_path, state)
    logging.info("[%s] completed in %.2fs", name, state["stages"][name]["duration_seconds"])


def _acquire_lock(directory: Path) -> Path:
    lock = directory / ".processing.lock"
    if lock.exists():
        pid: int | None = None
        try:
            contents = lock.read_text(encoding="utf-8")
            pid = int(next(line.split("=", 1)[1] for line in contents.splitlines() if line.startswith("pid=")))
        except (OSError, StopIteration, ValueError):
            pass
        probe = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE) if pid else None
        # A timeout/crash must not permanently prevent resumable processing.
        if pid is None or str(pid).encode() not in probe.stdout:
            logging.warning("Removing stale processing lock: %s", lock)
            lock.unlink(missing_ok=True)
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise StageError(f"Video is already being processed: {directory.name}") from error
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(f"pid={os.getpid()} started_at={utc_now()}\n")
    return lock


def _update_index(config: AppConfig, source_hash: str, video_id: str) -> None:
    index_path = config.data_root / "database" / "content_index.json"
    index = load_json(index_path, {"content_hashes": {}})
    index.setdefault("content_hashes", {})[source_hash] = {"video_id": video_id, "updated_at": utc_now()}
    atomic_write_json(index_path, index)


def process_asset(config: AppConfig, asset: MediaAsset, force: bool = False) -> Path:
    """Run ASR/knowledge only after Intake has produced a verified complete A/V source."""
    source = asset.normalized_source
    if not source.is_file():
        raise FileNotFoundError(source)
    if not asset.probe.audio or not asset.probe.video:
        raise StageError("Pipeline requires Intake to provide a verified audio+video normalized source.")
    video_id, source_hash, video_dir = asset.video_id, asset.content_hash, asset.video_dir
    lock = _acquire_lock(video_dir)
    try:
        state_path = video_dir / "processing.json"
        state = load_json(state_path) or _empty_state(video_id, source_hash, source, config)
        if state.get("content_hash") != source_hash:
            raise StageError("Existing processing directory does not match source content hash.")
        # Existing v2.2 assets have no visual stage. Add it without changing their
        # completed source/audio/ASR records.
        state.setdefault("stages", {})
        for stage in STAGES:
            state["stages"].setdefault(stage, {"status": "pending"})
        atomic_write_json(state_path, state)
        previous_asr = state["stages"].get("asr", {})
        expected_asr_settings = config.raw["asr"].get("faster_whisper", {})
        actual_asr_settings = previous_asr.get("model", {}).get("settings")
        if previous_asr.get("status") == "completed" and actual_asr_settings != expected_asr_settings:
            logging.info("ASR configuration changed; re-running ASR and knowledge without reprocessing media.")
            state["stages"]["asr"] = {"status": "pending", "reason": "asr_configuration_changed"}
            state["stages"]["knowledge"] = {"status": "pending", "reason": "upstream_asr_configuration_changed"}
            atomic_write_json(state_path, state)
        metadata_path = video_dir / "metadata.json"
        audio_path = video_dir / "audio.wav"
        transcript_path = video_dir / "transcript.json"
        visual_dir = video_dir / "visual"
        visual_transcript_path = visual_dir / "visual_transcript.json"
        knowledge_json_path = video_dir / "knowledge.json"
        knowledge_md_path = video_dir / "knowledge.md"

        def archive_source() -> dict[str, Any]:
            existing_metadata = load_json(metadata_path, {})
            discovered_source = source_for_media(asset.media.get("video_source"), asset.media.get("audio_source"))
            source_record = discovered_source or {
                "platform": "other", "source_type": "manual_file", "source_url": None, "platform_content_id": None,
                "author_name": None, "author_id": None, "title": asset.title, "published_at": None,
                "collected_at": utc_now(), "original_filename": Path(asset.media.get("video_source") or source).name,
            }
            metadata = {
                "video_id": video_id, "content_hash": source_hash, "title": asset.title, "author": None,
                "source": source_record, "source_url": source_record.get("source_url"), "publish_time": source_record.get("published_at"),
                "download_time": source_record.get("collected_at") or utc_now(),
                "language": config.raw["asr"].get("faster_whisper", {}).get("language", "zh"),
                "local_video_path": str(source), "original_input_path": asset.media.get("video_source"),
                "media": asset.media, "duration": asset.probe.duration, "format": asset.probe.format_name,
                "size_bytes": asset.probe.size_bytes, "video": asset.probe.video, "audio": asset.probe.audio,
            }
            # Publishing is a user-facing mirror of the authoritative source.
            # Preserve it when a forced source stage refreshes metadata.
            if isinstance(existing_metadata.get("published_media"), dict):
                metadata["published_media"] = existing_metadata["published_media"]
            atomic_write_json(metadata_path, metadata)
            _update_index(config, source_hash, video_id)
            return {"artifacts": [str(metadata_path), str(video_dir / "media.json"), str(source)]}

        _stage(state, state_path, "source", force, archive_source)

        def extract_audio() -> dict[str, Any]:
            if not config.ffmpeg.exists():
                raise StageError(f"FFmpeg was not found: {config.ffmpeg}")
            temporary = audio_path.with_suffix(".tmp.wav")
            temporary.unlink(missing_ok=True)
            _run([str(config.ffmpeg), "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(temporary)])
            os.replace(temporary, audio_path)
            return {"artifacts": [str(audio_path)], "audio_format": "mono/16kHz/pcm_s16le"}

        _stage(state, state_path, "audio", force, extract_audio)

        def run_asr() -> dict[str, Any]:
            segments, provenance = transcribe(audio_path, config.raw["asr"], video_dir / "asr-command-output.json")
            segments, _ = ensure_segment_ids(segments)
            payload = {"video_id": video_id, "language": config.raw["asr"].get("faster_whisper", {}).get("language", "zh"), "segments": segments, "provenance": provenance}
            atomic_write_json(transcript_path, payload)
            atomic_write_text(video_dir / "transcript.md", render_transcript(segments))
            return {"artifacts": [str(transcript_path), str(video_dir / "transcript.md")], "model": provenance}

        _stage(state, state_path, "asr", force, run_asr)

        # This is a compatibility migration for existing ASR assets; it does not invoke ASR.
        transcript_payload = load_json(transcript_path)
        if transcript_payload:
            identified_segments, ids_added = ensure_segment_ids(transcript_payload["segments"])
            if ids_added:
                transcript_payload["segments"] = identified_segments
                atomic_write_json(transcript_path, transcript_payload)
                atomic_write_text(video_dir / "transcript.md", render_transcript(identified_segments))

        transcript_segments = load_json(transcript_path)["segments"]
        visual_config = config.raw.get("visual_evidence", {})
        current_visual = load_json(visual_transcript_path, {})
        desired_visual_fingerprint = visual_pipeline_fingerprint(transcript_segments, visual_config)
        if state["stages"].get("visual", {}).get("status") == "completed" and current_visual.get("pipeline_fingerprint") != desired_visual_fingerprint:
            logging.info("Visual-evidence configuration changed; re-running visual and knowledge only.")
            state["stages"]["visual"] = {"status": "pending", "reason": "visual_fingerprint_changed"}
            state["stages"]["knowledge"] = {"status": "pending", "reason": "upstream_visual_configuration_changed"}
            atomic_write_json(state_path, state)

        def run_visual() -> dict[str, Any]:
            if not visual_config.get("enabled", False):
                document = {"schema_version": "visual-evidence-v1", "pipeline_fingerprint": desired_visual_fingerprint,
                            "fingerprints": {}, "requests": [], "visual_evidence": [], "disabled": True, "generated_at": utc_now()}
                atomic_write_json(visual_transcript_path, document)
            else:
                document = build_visual_evidence(source, transcript_segments, config.ffmpeg, visual_dir, visual_config)
            return {"artifacts": [str(visual_dir / "requests.json"), str(visual_transcript_path)],
                    "request_count": len(document.get("requests", [])),
                    "completed_evidence_count": sum(item.get("status") == "completed" for item in document.get("visual_evidence", [])),
                    "unresolved_count": sum(item.get("status") == "unresolved_visual_reference" for item in document.get("visual_evidence", [])),
                    "pipeline_fingerprint": desired_visual_fingerprint}

        _stage(state, state_path, "visual", force, run_visual)
        visual_document = load_json(visual_transcript_path, {"visual_evidence": [], "requests": [], "fingerprints": {}})

        current_knowledge = load_json(knowledge_json_path, {})
        visual_identity = hashlib.sha256(json.dumps(visual_document.get("fingerprints", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        knowledge_identity = {**config.raw.get("knowledge", {}), "visual_fingerprint": visual_identity}
        desired_knowledge_fingerprint = knowledge_fingerprint(transcript_segments, config.raw["llm"], knowledge_identity)
        actual_knowledge_fingerprint = current_knowledge.get("provenance", {}).get("knowledge_fingerprint")
        if state["stages"].get("knowledge", {}).get("status") == "completed" and actual_knowledge_fingerprint != desired_knowledge_fingerprint:
            logging.info("Knowledge configuration/schema changed; re-running knowledge only.")
            state["stages"]["knowledge"] = {"status": "pending", "reason": "knowledge_fingerprint_changed"}
            atomic_write_json(state_path, state)

        def run_knowledge() -> dict[str, Any]:
            load_detail = ensure_lm_studio_loaded(config.raw["llm"], config.raw.get("knowledge", {}).get("lifecycle", {}))
            metadata, metadata_changed = ensure_source_schema(load_json(metadata_path))
            if metadata_changed:
                atomic_write_json(metadata_path, metadata)
            transcript = load_json(transcript_path)["segments"]
            generated, provenance = build_knowledge(metadata, transcript, config.raw["llm"], config.raw.get("knowledge", {}), video_dir, visual_document)
            existing_document = load_json(knowledge_json_path, {})
            document = {
                "schema_version": provenance["schema_version"], "video_id": video_id, "generated_at": utc_now(),
                "provenance": provenance, "knowledge": generated,
            }
            if isinstance(existing_document.get("media"), dict):
                document["media"] = existing_document["media"]
            atomic_write_json(knowledge_json_path, document)
            atomic_write_text(knowledge_md_path, render_markdown(metadata, generated))
            return {"artifacts": [str(knowledge_json_path), str(knowledge_md_path)], "model": provenance, "model_load": load_detail}

        _stage(state, state_path, "knowledge", force, run_knowledge)

        def publish_media() -> dict[str, Any]:
            if not knowledge_json_path.is_file() or not knowledge_md_path.is_file():
                raise StageError("Completed-media publishing requires successful knowledge.json and knowledge.md artifacts.")
            settings = config.raw.get("publishing", {})
            if not bool(settings.get("enabled", True)):
                return {"enabled": False, "status": "disabled"}
            published = publish_completed_media(source, video_id, config.data_root, settings, config.ffprobe)
            metadata = load_json(metadata_path)
            metadata["published_media"] = published.as_metadata_reference()
            atomic_write_json(metadata_path, metadata)
            document = load_json(knowledge_json_path)
            generated = document["knowledge"]
            # Existing assets may predate v2.3.3.  Add the program-derived
            # usage summary while publishing; no ASR or LLM rerun is needed.
            generated["visual_usage"] = visual_usage_summary(generated, visual_document)
            document["knowledge"] = generated
            document["media"] = published.as_knowledge_reference(video_id)
            atomic_write_json(knowledge_json_path, document)
            atomic_write_text(knowledge_md_path, render_markdown(metadata, generated))
            return {
                "artifacts": [str(published.path), str(knowledge_json_path), str(knowledge_md_path), str(metadata_path)],
                "publish_mode": published.publish_mode,
                "relative_path": published.relative_path,
                "skipped": published.skipped,
            }

        _stage(state, state_path, "publish_media", force, publish_media)
        return video_dir
    finally:
        lock.unlink(missing_ok=True)


def discover_inputs(config: AppConfig) -> list[Path]:
    incoming = config.data_root / "incoming" / "manual"
    incoming.mkdir(parents=True, exist_ok=True)
    minimum_age = int(config.raw["pipeline"].get("minimum_file_age_seconds", 30))
    now = time.time()
    extensions = {extension.lower() for extension in config.raw["pipeline"].get("accepted_extensions", VIDEO_EXTENSIONS)}
    return [
        item for item in sorted(incoming.iterdir())
        if item.is_file() and item.suffix.lower() in extensions and now - item.stat().st_mtime >= minimum_age
    ]


def run(config: AppConfig, input_path: Path | None, video_id: str | None, force: bool) -> int:
    # Pairing needs sibling files, so explicit --input still probes its containing directory.
    inputs = discover_inputs(config)
    requested: Path | None = None
    if input_path:
        requested = input_path.resolve()
        if requested not in inputs:
            inputs.append(requested)
    if not inputs:
        logging.info("No eligible video in %s", config.data_root / "incoming" / "manual")
        return 0
    assets, media_reports = prepare_assets(config, inputs, force=force)
    if requested:
        assets = [asset for asset in assets if Path(asset.media["video_source"]).resolve() == requested]
        media_reports = [report for report in media_reports if Path(report.get("video_source", "NUL")).resolve() == requested]
    if video_id:
        assets = [asset for asset in assets if asset.video_id == video_id]
        if not assets:
            raise ValueError(f"No normalized media asset found for video ID: {video_id}")
    for report in media_reports:
        logging.warning("Intake state: %s (%s)", report.get("status"), report.get("video_source") or report.get("audio_source") or report.get("source"))
    failures = 0
    completed_dirs: list[Path] = []
    lifecycle = config.raw.get("knowledge", {}).get("lifecycle", {})
    unload_policy = lifecycle.get("unload_policy", "after_task")

    def release_model(directories: list[Path]) -> None:
        if not directories or unload_policy == "never":
            return
        if not all((directory / "knowledge.json").is_file() and (directory / "knowledge.md").is_file() for directory in directories):
            logging.warning("LM Studio unload skipped because knowledge artifacts are incomplete.")
            return
        detail = unload_lm_studio(config.raw["llm"], lifecycle)
        for directory in directories:
            record_lifecycle(directory, detail)
        logging.info("LM Studio lifecycle: %s; post-unload VRAM MiB=%s", detail.get("status"), detail.get("gpu_memory_after_unload_mib"))

    for asset in assets:
        try:
            result = process_asset(config, asset, force=force)
            completed_dirs.append(result)
            (config.data_root / "failed" / f"{asset.video_id}.json").unlink(missing_ok=True)
            logging.info("Completed: %s", result)
            if unload_policy == "after_task":
                release_model([result])
        except Exception as error:
            failures += 1
            try:
                atomic_write_json(config.data_root / "failed" / f"{asset.video_id}.json", {
                    "video_id": asset.video_id, "content_hash": asset.content_hash, "input_path": str(asset.normalized_source),
                    "failed_at": utc_now(), "error": f"{type(error).__name__}: {error}",
                    "recovery": "Fix the configuration or model issue, then run the same command again."
                })
            except Exception:
                logging.exception("Could not write failure report for: %s", asset.normalized_source)
            logging.exception("Failed: %s", asset.normalized_source)
    if unload_policy == "after_batch":
        release_model(completed_dirs)
    return 1 if failures else 0


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract durable knowledge notes from local video files.")
    parser.add_argument("--config", default="config/config.json", help="Path to JSON configuration.")
    parser.add_argument("--input", type=Path, help="One video file. Omit to scan data_root/incoming/manual.")
    parser.add_argument("--video-id", help="Run one already-normalized asset by its stable video ID.")
    parser.add_argument("--force", action="store_true", help="Re-run already completed stages for the selected video(s).")
    return parser
