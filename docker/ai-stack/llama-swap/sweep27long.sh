#!/usr/bin/env bash
# 27B long-context entry: KV cache in system RAM (-nkvo), 262K ctx. With vs without MTP.
cd "$(dirname "$0")"
BASE="-m /models/Qwen3.8-27B-UD-Q4_K_S.gguf -ngl 99 -fa on --jinja -t 6 -tb 12 --parallel 1 --ctx-size 262144 --no-kv-offload -ctk q8_0 -ctv q8_0 -b 512 -ub 256"
MTP="--model-draft /models/MTP/mtp-Qwen3.8-27B-Q4_0.gguf --spec-type draft-mtp --spec-draft-n-max 2 -ctkd q8_0 -ctvd q8_0"
variant() {
  label=$1; start=$2; shift 2
  for n in $(seq $start 2 64); do
    if ./tryload.sh "$@" --n-cpu-ffn $n >/dev/null 2>&1; then
      echo "$label: fits at --n-cpu-ffn $n, VRAM $(nvidia-smi --query-gpu=memory.used --format=csv,noheader), RAM avail $(free -m | awk '/Mem/{print $7}') MB"
      python3 bench.py --url http://localhost:5900 --label "s3long-$label-ffn$n" --tests "${TESTS:-short,long,deep56k}" --runs 1 ab 2>&1 | grep -v loaded | python3 -c "
import sys,json
for l in sys.stdin:
    try: r=json.loads(l)
    except Exception: print(' ', l.strip()); continue
    print(f\"  {r['test']:8} prompt_n={r['prompt_n']:6} pp={r['pp_tps']:6} tg={r['tg_tps']:5} acc={r.get('draft_accept')} wall={r['wall_s']}s\")"
      docker logs llama-ab 2>&1 | grep -E " E |cudaMalloc|out of memory" | head -3
      docker rm -f llama-ab >/dev/null; return
    fi
  done
  echo "$label: does not fit"
}
variant ${VARIANT:-nomtp} ${START:-40} $BASE ${EXTRA:-}
