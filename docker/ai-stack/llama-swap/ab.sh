#!/usr/bin/env bash
# A/B llama-server builds/flags directly (no llama-swap).
# Usage: [TESTS=short,long] [RUNS=2] ab.sh <label> <image> <llama-server args...>
set -u
label=$1; image=$2; shift 2
docker rm -f llama-ab >/dev/null 2>&1
docker run -d --name llama-ab --gpus all -p 5900:5900 -v ~/apps/llama_cpp/models:/models \
  --entrypoint /app/llama-server "$image" --host 0.0.0.0 --port 5900 "$@" >/dev/null
for i in $(seq 300); do
  curl -sf localhost:5900/health >/dev/null && break
  docker ps -q --filter name=llama-ab | grep -q . || { echo "$label: container died"; docker logs llama-ab 2>&1 | grep -iE 'error|fail|out of memory' | tail -5; exit 1; }
  sleep 1
done
swap0=$(free -m | awk '/Swap/{print $3}')
vram_log=$(mktemp)
( while true; do nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits >> "$vram_log"; sleep 1; done ) &
poller=$!
python3 "$(dirname "$0")/bench.py" --url http://localhost:5900 --label "$label" --tests "${TESTS:-short}" --runs "${RUNS:-2}" ab
kill $poller
echo "$label: peak VRAM $(sort -n "$vram_log" | tail -1) MiB, swap delta $(( $(free -m | awk '/Swap/{print $3}') - swap0 )) MiB"
rm -f "$vram_log"
docker logs llama-ab 2>&1 | grep -E "common_fit_params|CUDA0 (model|KV|compute) buffer|n_cpu_moe|override" | sed "s/^/  /" | tail -8
docker rm -f llama-ab >/dev/null
