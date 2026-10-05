#!/usr/bin/env bash
# Load a llama-server config on :5900 and print the VRAM breakdown. Leaves it running on success.
docker rm -f llama-ab >/dev/null 2>&1
docker run -d --name llama-ab --gpus all -p 5900:5900 -v ~/apps/llama_cpp/models:/models --entrypoint /app/llama-server \
  ghcr.io/mostlygeek/llama-swap:v262-cuda-b11382 --host 0.0.0.0 --port 5900 "$@" >/dev/null
until curl -sf localhost:5900/health >/dev/null; do
  docker ps -q --filter name=llama-ab | grep -q . || { echo "FAILED:"; docker logs llama-ab 2>&1 | grep -E 'cudaMalloc|failed to allocate' | head -2; exit 1; }
  sleep 1
done
echo "OK, VRAM used: $(nvidia-smi --query-gpu=memory.used --format=csv,noheader)"
