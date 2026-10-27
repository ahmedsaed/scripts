#!/usr/bin/env bash
# For each variant: find the lowest --n-cpu-ffn that loads, then benchmark it.
cd "$(dirname "$0")"
Q="-m /models/Qwen3.8-27B-UD-Q4_K_S.gguf --model-draft /models/MTP/mtp-Qwen3.8-27B-Q4_0.gguf --spec-type draft-mtp --spec-draft-n-max 2 -ngl 99 -fa on --jinja -t 6 -tb 12 --parallel 1"
variant() {
  label=$1; start=$2; shift 2
  for n in $(seq $start 2 64); do
    if ./tryload.sh $Q "$@" --n-cpu-ffn $n >/dev/null 2>&1; then
      echo "$label: fits at --n-cpu-ffn $n, VRAM $(nvidia-smi --query-gpu=memory.used --format=csv,noheader)"
      python3 bench.py --url http://localhost:5900 --label "s3-$label-ffn$n" --tests short,long --runs 2 ab 2>&1 | grep -v loaded | python3 -c "
import sys,json
for l in sys.stdin:
    r=json.loads(l); print(f\"  {r['test']:5} pp={r['pp_tps']:6} tg={r['tg_tps']:5} acc={r.get('draft_accept')}\")"
      docker rm -f llama-ab >/dev/null; return
    fi
  done
  echo "$label: does not fit"
}
V="--ctx-size 65536 -ctk q4_0 -ctv q4_0 -ctkd q4_0 -ctvd q4_0 -b 512 -ub 256"
variant ctx64k-q4-otd      42 $V -otd "token_embd\.weight=CPU"
variant ctx64k-q4-otd-n3   42 $V -otd "token_embd\.weight=CPU" --spec-draft-n-max 3
