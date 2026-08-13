from __future__ import annotations

import logging
from datetime import datetime

from src.config import load_config
from src.pipeline import create_parser, run


def main() -> int:
    arguments = create_parser().parse_args()
    config = load_config(arguments.config)
    logs_directory = config.data_root / "logs"
    logs_directory.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(logs_directory / f"pipeline-{datetime.now():%Y-%m-%d}.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[stream_handler, file_handler])
    return run(config, arguments.input, arguments.video_id, arguments.force)


if __name__ == "__main__":
    raise SystemExit(main())
