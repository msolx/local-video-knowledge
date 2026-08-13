# Optional CUDA runtime directory

This repository does not include CUDA, cuDNN, FFmpeg, NVIDIA DLLs, or model
weights. Install a compatible NVIDIA driver and CUDA/cuDNN runtime yourself.

If `faster-whisper` needs a private runtime directory on your machine, place it
under this directory and set `asr.cuda_runtime_dir` in `config/config.json`.
Otherwise set that field to `null` and rely on your system PATH.
