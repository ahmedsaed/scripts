# llama-swap optimization plan

Researched 2026-10-05. Hardware: RTX 3060 12 GB, 30 GB RAM, Ryzen 5 8500G (6c/12t, has iGPU).

## Findings in the current setup

1. **Stale llama.cpp**: old `:cuda` image was llama.cpp b9544 (2026-06-07). Updated and pinned to `ghcr.io/mostlygeek/llama-swap:v262-cuda-b11382` (llama-swap v262, llama.cpp b11382, 2026-10-05). Prune old v206/v208 images.
2. **`qwen-3.6-27B` offload is backwards**: `-ngl 22` puts half the layers (incl. attention) on CPU → 2–4 tok/s. Better: `-ngl 99` + keep only FFN weights on CPU via the new **`--n-cpu-ffn N`** (replaces hand-written `-ot "blk\.(…)\.ffn_.*=CPU"` regexes) + quantized KV. Current 27B GGUF likely has no MTP head.
3. **`qwen-3.5-high-ctx` is redundant**: Qwen3.5 is hybrid (KV on 1/4 layers). With `-ctk q8_0 -ctv q8_0 -fa on` the 9B should fit 262K fully on GPU. Baseline shows high-ctx is ~4x slower than the normal 9B entry.
4. **Old entries lack `-fa on` / `--jinja`** (matters for tool calling).
5. **No sampling params**: Qwen recommends thinking `temp 1.0, top_p 0.95, top_k 20`; non-thinking `temp 0.7, top_p 0.8, top_k 20, presence_penalty 1.5`.
6. **Threads**: `--threads 12` on 6 physical cores hurts decode — **confirmed**: 27B on b11382 goes 2.5 → 5.4 tok/s with `-t 6`. The new build is more sensitive to this than the old one. Use `-t 6 --threads-batch 12`.
7. **35B MoE tuning**:
   - prompt processing is the bottleneck (~230 tok/s → a 14K prompt takes ~60 s); try larger `-b`/`-ub` (e.g. 2048)
   - enable q8_0 KV → lower `--n-cpu-moe` (currently 30)
   - `--no-mmap` was **removed** in the new build → replaced by `--load-mode none` (already changed in config). A 22.8 GB file fully in RAM uses ~75% of RAM; compare against `--load-mode mmap`.
   - `--slot-save-path` (manual-only) is superseded by the automatic RAM prompt cache `--cache-ram` (see below)
8. **`globalTTL: 60`** is aggressive given 35B reload time — use per-model `ttl`.
9. **Free VRAM**: GNOME + VS Code currently use ~600 MB on the 3060. Running the desktop on the 8500G iGPU frees it.
10. **Config hygiene**: llama-swap `macros` for shared flags; fix odd ctx values (162144 → 163840, 122144 → 131072); `logLevel: info`.
11. **Host port 8080 is taken** by another service (`pbiload` uvicorn), so the compose stack can't start as-is. Benchmarks use 8090. Pick a new port for compose.

## New in llama.cpp b11382 (vs b9544)

Things that replace or extend the old approach:

| Flag | What it does | Replaces / use for |
|---|---|---|
| `--load-mode {auto,none,mmap,mlock,mmap+mlock}` | Model loading mode | Replaces `--no-mmap` (= `none`) |
| lazy mode (`LLAMA_ARG_LAZY_MODE`, default `auto`) | Reads rows of tensors >4 GiB from disk on demand (requires mmap) | Lower RAM use for huge embedding tables |
| `--n-cpu-ffn N` (`-ncffn`) | Keep dense FFN weights of the first N layers on CPU | Replaces `-ot ...ffn_.*=CPU` regex for dense models (27B) |
| `--n-cpu-moe N` | Same for MoE experts (already used) | — |
| `--fit on` (default), `--fit-target MiB`, `--fit-ctx N` | Auto-adjusts unset args (layers, ctx) to fit VRAM. Aborts if `-ngl` is set manually | Alternative to hand-tuning `-ngl` / `--n-cpu-moe`; worth an A/B |
| `--cache-ram N` (default 8192 MiB) + `--cache-idle-slots` | Automatic prompt cache in host RAM | Replaces `--slot-save-path`; tune size since RAM is tight with the 35B |
| `--cache-reuse N` | Reuse cached chunks via KV shifting | Long chat / agent sessions |
| `--spec-type` now: `draft-simple, draft-eagle3, draft-mtp, draft-dflash, draft-dspark, ngram-simple, ngram-map-k, ngram-map-k4v, ngram-mod, ngram-cache` | More speculative decoding methods; types can be comma-combined | MTP for Qwen; draft model for Gemma 4 12B; n-gram for code editing (repetitive output) |
| `-ctkd/-ctvd` | KV cache type for the draft model | Shave VRAM when using a draft model |

Not in mainline: TurboQuant KV (`tbq3/4`) — PR rejected, only in forks. Stick with `q8_0`/`q4_0` KV.

## Models

### Strong candidates

| Slot | Candidate | Notes |
|---|---|---|
| Replace 27B | **Qwen3.8-27B** (Aug 2026, Apache 2.0, MTP head in GGUF) | SWE-bench Pro 61.7 vs 53.5, Terminal Bench 73.0 vs 63.4 over 3.6-27B. Reported on a 3060: Q4_K_S, 96K, q4_0 KV, FFN offload, MTP n-max 2, `-b 256 -ub 128` → ~10 tok/s, 88% acceptance. |
| Keep 35B MoE | **Qwen3.6-35B-A3B MTP** (current) | No Qwen3.8 MoE at this size. Fast workhorse, a few points below dense 27B. |
| Replace 9B / OmniCoder | **Gemma 4 12B QAT q4_0** + `gemma-4-12B-it-assistant` (0.4B draft) | Fully in VRAM, claims up to 3x decode via speculative decoding. Qwen3.5-9B is still the newest small Qwen. |

### Worth testing

- **Gemma 4 26B-A4B QAT q4_0** (~14 GB): less CPU offload than the 35B, faster. Good chat/writing; reports of tool refusals and weak SWE-bench for agentic coding.
- **Nemotron 3.5 Lightning 30B-A3B** (Mamba hybrid, 1M ctx, ~25 GB Q4): faster but weaker than Qwen3.6-35B; only for very long context.

### Doesn't fit (skip)

Qwen3.8-Flash-Next (smallest quant 72 GB), GLM-5.3-Flash (93 GB+), DeepSeek V4 Flash, MiniMax H3.

## Steps

- [x] 1a. Baseline benchmark on old image (b9544)
- [x] 1b. Re-benchmark on new image (b11382) + A/B to explain regressions
- [x] 2. Quick wins — config written, validated through llama-swap (see Step 2 final)
- [x] 3. Qwen3.8-27B + `--n-cpu-ffn` + MTP — done, old Qwen3.6-27B and OmniCoder deleted (see Step 3 results)
- [x] 3b. `qwen-3.8-27B-262k`: slow long-context entry with KV in system RAM
- [ ] 4. ISTA-DASLab GSQ-RCO IQ2_XS (2-bit) 27B: `qwen-3.8-27B-iq2` added, pending real-workflow testing (see Step 4)
- [x] Cleanup: old llama-swap images (v206/v208/b9544) and the stale `ai-stack/llama_cpp/` compose removed
- [-] 4. ~~Gemma 4 12B + draft model~~ — skipped (128K ctx, weaker tool use; not practical here). Keep Qwen3.5-9B as the small 262K model; delete OmniCoder.
- [ ] 5. (Optional) Gemma 4 26B-A4B vs 35B-A3B

## Benchmarking

Tool: `bench.py` — hits llama-swap's `/v1/chat/completions` with llama-server timings, appends to `bench_results.jsonl`.

```sh
# separate container on 8090 so the compose setup is untouched
docker run -d --name llama-swap-bench --gpus all -p 8090:8080 \
  -v ~/apps/llama_cpp/models:/models -v $PWD/config.yaml:/app/config.yaml -v $PWD/cache:/prompt_cache \
  ghcr.io/mostlygeek/llama-swap:v262-cuda-b11382
python3 bench.py --label <name> <model> [<model> ...]
```

Tests (2 runs each, non-thinking, seed 42, `cache_prompt: false`):
- **short**: 41-token coding prompt, 512 generated tokens → decode speed
- **long**: ~14K-token code prompt, up to 256 generated tokens → prompt processing + decode at depth

Gotchas:
- `config.yaml` is a single-file bind mount. Editors / `sed -i` that replace the file (new inode) aren't seen by the running container; `docker restart` it.
- Watch swap: during the first new-image run swap went 0 → 3.7 GB, which may have skewed the 27B numbers.

## Benchmark results

Averages of 2 runs. pp = prompt tok/s, tg = generation tok/s.

| Model | Build | short tg | long pp | long tg | MTP accept |
|---|---|---|---|---|---|
| qwen-3.5 (9B) | b9544 | 53.7 | 1604 | 50.5 | – |
| qwen-3.5 (9B) | b11382 | 55.8 | 1622 | 51.8 | – |
| qwen-3.5-high-ctx | b9544 | 16.5 | 745 | 12.4 | – |
| qwen-3.5-high-ctx | b11382 | 13.6 | 701 | 9.8 | – |
| OmniCoder-9B | b9544 | 53.0 | 1593 | 49.4 | – |
| OmniCoder-9B | b11382 | 52.0 | 1533 | 48.2 | – |
| qwen-3.6-27B | b9544 | 4.2 | 174 | 2.7 | – |
| qwen-3.6-27B | b11382 | 3.0 | 164 | 2.2 | – |
| qwen-3.6-35B | b9544 | 54.3 | 231 | 53.5 | 0.79 / 0.69 |
| qwen-3.6-35B | b11382 | 48.9–55.9 (recheck) | 232 | 19.7* | 0.68 / 0.50 |

\* run happened during a ~5 GB swap-out; short test in the same run was 25.7, recheck without swapping 48.9–55.9.

### A/B: llama-server direct, identical args, short test (`ab.sh`)

| Model | Variant | tg tok/s | MTP accept |
|---|---|---|---|
| 35B | old b9544, `--no-mmap`, MTP | 50.3 | 0.79 |
| 35B | new b11382, `--load-mode none`, MTP | 47.4 | 0.68 |
| 35B | new b11382, `--load-mode mmap`, MTP | 34.6 | 0.68 |
| 35B | old b9544, no MTP | 35.4 | – |
| 35B | new b11382, no MTP | 36.6 | – |
| 27B | old b9544, `-ngl 22 -t 12` | 4.1 | – |
| 27B | new b11382, `-ngl 22 -t 12` | 2.5 | – |
| 27B | new b11382, `-ngl 22 -t 12 --load-mode none` | 3.2 | – |
| 27B | new b11382, `-ngl 22 -t 6` | **5.4** | – |

Takeaways:
- The update is neutral for models fully on GPU (±3%) and for the 35B once noise is removed.
- New build is more sensitive to oversubscribed threads: with `-t 6` it beats the old build (27B 5.4 vs 4.1).
- MTP is worth ~+35% on the 35B (36 → 48–55). Acceptance is lower on the new build (0.68 vs 0.79), consistent with the changelog's probabilistic draft acceptance, but speed holds.
- Keep `--load-mode none` for the 35B; `mmap` costs ~30% decode.
- **RAM is the real constraint**: the 35B's CPU experts (~17 GB) push ~5 GB of desktop apps to swap, and benchmarks taken during that swap-out are ~2x slower. Keep `--cache-ram` small for the 35B and avoid `--fit`-style choices that grow host memory.

### Step 3 results: Qwen3.8-27B (UD-Q4_K_S 15.4 GB + separate MTP drafter 1.37 GB)

Files: `unsloth/Qwen3.8-27B-GGUF` → `Qwen3.8-27B-UD-Q4_K_S.gguf`, `MTP/mtp-Qwen3.8-27B-Q4_0.gguf` (the unsloth GGUF does not embed the MTP head; pass it with `--model-draft`). Download with `HF_HUB_DISABLE_XET=1` — the xet client stalled at 0 MB/s here, plain HTTP did 8 MB/s.

Validated: correct answers (arithmetic, code, facts), thinking mode works, MTP drafter accepted ~0.9 on short outputs.

Memory facts:
- KV is large: 16 attention layers × 4 KV heads × 256 dim → ~34 KB/token at q8_0 (2.2 GB at 64K). q4_0 KV halves it.
- Drafter duplicates `token_embd` + `output` (521 MB each, Q3_K). `-otd "token_embd\.weight=CPU"` saves ~130 MB in practice.

Sweep (`sweep27.sh`; lowest `--n-cpu-ffn` that loads, 2 runs):

| Variant | min ncffn | short tg | long pp | long tg | MTP accept |
|---|---|---|---|---|---|
| 64K, q8 KV, ub 512 | 58 | 7.0 | 225 | 5.8 | 0.88 / 0.66 |
| 64K, q4 KV, ub 256 | 48 | 7.1 | 166 | 6.7 | 0.91 / 0.77 |
| 96K, q4 KV, ub 128 | 54 | 6.7 | 90 | 5.7 | 0.90 / 0.75 |
| 32K, q8 KV, ub 512 | 46 | 7.1 | 252 | 5.8 | 0.87 / 0.64 |
| 64K, q4 KV, ub 256, `-otd` drafter embd on CPU | 48 | 7.55 | 166 | 6.9 | 0.91 / 0.77 |
| same, `--spec-draft-n-max 3` | 50 | 7.0 | 161 | 5.5 | 0.85 / 0.59 |

All variants land at ~7 tok/s: decode is bound by reading the CPU-side FFN weights from system RAM. Context/KV choices barely matter.

**KV quant decision: q8_0 everywhere.** Published data: q8_0 KV costs +0.002–0.05 perplexity (negligible); q4_0 is a cliff (~+7.6% PPL, worse at long context, reports of math/precise recall breaking). Draft-model KV quant only affects acceptance, not output.

Chosen (config `qwen-3.8-27B`, alias `qwen-3.6-27B`): 64K, **q8_0 KV** (main + draft), `-b 512 -ub 256`, `-otd` drafter embd on CPU, n-max 2, **`--n-cpu-ffn 58`** (56 is the minimum at q8_0; +2 for desktop VRAM headroom).

Through llama-swap: short tg 7.0, long pp 145 / tg 6.1 (acc 0.88 / 0.74) — same speed as the q4_0 variant (7.1 / 162 / 6.1). vs old Qwen3.6-27B (step 2): 5.6 / 177 / 3.2 → +25% short decode, ~2x decode at depth, much stronger model. The 35B MoE remains the fast default (50–60 tok/s).

### Step 3b: 27B long-context entry (`qwen-3.8-27B-262k`)

262K ctx with KV in system RAM (`--no-kv-offload`, q8_0 → 8.7 GB RAM).

Finding: with `-nkvo`, each context (main and MTP draft) still reserves a GPU compute buffer that scales with ctx: ~6.5 KB/token (464 MB at 64K, 888 MB at 128K, 1.7 GB at 262K), independent of `-ub`. There's no separate draft ctx size, so MTP costs ~2.5 GB VRAM at 262K.

| Variant | ncffn | short tg | 14K pp / tg | 55K pp / tg | 55K wall |
|---|---|---|---|---|---|
| with MTP | 56 | 5.1 | 114 / 3.7 | 105 / 2.6 | 572 s |
| **no MTP** | 40 | 5.0 | **160** / 3.6 | **142** / 2.0 | **451 s** |
| no MTP | 36 | 5.2 | crashed on 14K prompt (VRAM headroom ~0.4 GB) | | |

Chosen: no MTP, `--n-cpu-ffn 40`, `--cache-ram 2048`, `timeouts.responseHeader: 0`. Prompt processing dominates at long context, so the faster prefill beats MTP's decode gain.

Validated through llama-swap: short tg 5.0; **119K-token prompt: pp 121 tok/s (~16.5 min prefill), tg 1.1 tok/s, 18 min total.** Works, but it's a "start it and walk away" option; in agent sessions the prompt cache means only new tokens are processed each turn. RAM use is high (KV 8.7 GB + CPU FFN), swap reached 6 GB with desktop apps open.

Ideas not tried: IQ4_XS (14.25 GB, −1.1 GB → ~6 more FFN layers on GPU, maybe +10%); moving the desktop to the iGPU (+~0.8 GB VRAM).

### Step 4: ISTA-DASLab GSQ-RCO IQ2_XS (2.5 bpw) 27B

File: `ISTA-DASLab/Qwen3.8-27B-GSQ-RCO-GGUF` → `Qwen3.8-27B-GSQ-RCO-IQ2_XS-mtp.gguf` (8.77 GB, MTP head embedded: `--spec-type draft-mtp` with no `--model-draft`). Their card claims IQ2_XS ≈ BF16 on zero-shot avg, AIME25 96.7 vs 100, GPQA-D 84.9 vs 89.9, LCB v6 76.6 vs 85.7 (self-reported); IQ2_S (9.6 GB with MTP) is closer to BF16 but leaves less room for KV.

VRAM at 64K: model 8.1 GB + KV 2.2 GB (q8_0) + recurrent state 0.45 GB (doubled by MTP rollback, `n_rs_seq = 2`) + compute 0.33 GB. With the desktop holding ~1.1 GB, all-GPU doesn't fit.

Gotcha: a config that loads can still OOM on the **first request**: `cudaGraphInstantiate` needs ~0.5 GB free after load. Load-only checks (`tryload.sh`) aren't enough; the sweep sends real requests.

Sweep (64K, `-b 512 -ub 256`, lowest `--n-cpu-ffn` that survives short+long requests, llama-server direct):

| KV (K / V) | min ncffn | short tg | long pp / tg | MTP accept |
|---|---|---|---|---|
| q8_0 / q8_0 | 18 | 12.5 | 331 / 11.6 | 0.79 / 0.74 |
| q8_0 / q4_0 | 10 | 17.6 | 369 / 14.6 | 0.80 / 0.63 |
| q4_0 / q4_0 | 2 | **30.7** | **421 / 25.4** | 0.85 / 0.72 |

Chosen, each at +2 ncffn headroom: `qwen-3.8-27B-iq2` (q8_0 KV, ncffn 20) and `qwen-3.8-27B-iq2-fast` (q4_0 KV, ncffn 4). `-iq2-fast` was later removed (q4_0 KV quality concern; same reasoning as step 3). Through llama-swap:

| Entry | short tg | 14K pp / tg | 55K pp / tg | 55K wall |
|---|---|---|---|---|
| qwen-3.8-27B (Q4_K_S, step 3) | 7.0 | 145 / 6.1 | – | – |
| qwen-3.8-27B-iq2 | 11.0 | 323 / 11.4 | 286 / 10.4 | 204 s |
| qwen-3.8-27B-iq2-fast | 25.2 | 408 / 23.7 | 349 / 19.3 | 164 s |

Sanity checks (arithmetic, bat-and-ball, `is_prime` executed against 17 primes, tool call, passphrase needle at ~3/5 depth of a ~50K-token prompt): all pass on iq2, iq2-fast and the Q4_K_S baseline. These are smoke tests, not a quality benchmark; real-workflow comparison pending.

Follow-up: the 64K entry OOMed in real use (`cuMemCreate` in the CUDA pool mid-request; desktop had grown to ~1.25 GB). Peak VRAM with a prompt filling ~85% of ctx (ncffn 20, q8_0 KV):

| ctx | server self | card total | result |
|---|---|---|---|
| 64K | 10.6 GB+ | 11.8 GB+ | OOM on 47K-token prompt |
| 48K | 9.9 GB | 11.1 GB | ok, 40K prompt: pp 299 / tg 10.7 |
| **40K** | **9.5 GB** | **10.8 GB** | ok, 33K prompt: pp 305 / tg 10.1 |
| 32K | 9.1 GB | 10.4 GB | ok |

Set `qwen-3.8-27B-iq2` to 40K so the card stays under 11 GB total.

Revised cap: **the model** at most ~11 GB, leaving ~1.3 GB for the desktop (now 1.3–1.5 GB). Full-GPU tests, server-only peak:

| Setup | ctx | ncffn | self peak | short tg | near-full pp / tg |
|---|---|---|---|---|---|
| no MTP | 64K | 0 | – | load fails | |
| no MTP | 56K | 0 | – | OOM on request | |
| **no MTP** | **48K** | **0** | **10.2 GB** | **21.5** | **400 / 16.2** (40K prompt) |
| MTP | 32K | 0 | – | OOM on request | |
| MTP | 32K | 4 | 10.2 GB | 21.4 | 378 / 20.8 (27K prompt) |
| MTP | 32K | 8 | 10.0 GB | 17.5 | 361 / 16.7 |

MTP costs ~0.8 GB VRAM (recurrent state doubled for rollback + draft buffers), roughly cancelling its decode gain here. Chosen: no MTP, 48K, fully on GPU. Without `--spec-type`, the MTP tensors in the `-mtp` GGUF aren't loaded to GPU (model buffer 7.76 vs 8.09 GB).

## Sources

- https://huggingface.co/Qwen/Qwen3.8-27B
- https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/discussions/61
- https://github.com/sudoingX/qwen38-mtp
- https://huggingface.co/google/gemma-4-12B-it-assistant
- https://gist.github.com/hungson175/61a805368649c225026a69adf2ad87e0
- https://huggingface.co/unsloth/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF
- https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF
- https://github.com/ggml-org/llama.cpp/blob/master/docs/speculative.md
- https://github.com/ggml-org/llama.cpp/issues/22442 (`--fit` behaviour)
- https://github.com/ggml-org/llama.cpp/pull/16391 (`--cache-ram`)
- https://sotaaz.com/post/turboquant-status-2026-en
- https://github.com/mostlygeek/llama-swap/releases

### Step 2 results (b11382, llama-server direct via `ab.sh`)

**9B merge** (`-ngl 99 --ctx-size 262144 -ctk q8_0 -ctv q8_0 -fa on -t 6 -tb 12`): short tg 54.3, long pp 1560 / tg 48.9, peak VRAM 11.7 GB. Replaces both `qwen-3.5` (54.8 / 1604 / 51.2) and `qwen-3.5-high-ctx` (13.6 / 701 / 9.8). f16 KV at 262K does not fit.

**35B** (median of 3; tg is noisy, likely desktop CPU/RAM contention with CPU-side experts):

| Variant | short tg | long pp | long tg | peak VRAM |
|---|---|---|---|---|
| A: original (ctx 122144, moe 30, -t 12) | 31.6 | 231 | 29.9 | 11.9 GB |
| B: A + `-t 6 -tb 12` | 53.9 | 231 | 35.6 | 11.9 GB |
| C: ctx 131072, moe 32, q8 KV (+ `-ctkd/-ctvd q8_0`), `-ub 1024` | 43.2 | 373 | 35.7 | 10.5 GB |
| **D: ctx 131072, moe 34, q8 KV (+draft q8), `-b 2048 -ub 2048`** | 47.5 | **585** | 36.2 | 10.2 GB |

Notes:
- Original config is at the VRAM edge (11.9 GB); ctx 131072 with it OOMs. Desktop apps on the 3060 vary 0.6–0.85 GB.
- MTP draft has its own context: 256 MB f16 KV + compute buffer (~670 MB at ub 2048). `-ctkd/-ctvd q8_0` shrinks the KV part.
- `--fit` picks a split similar to manual (32 layers of experts on CPU at ub 2048), but it also overflowed part of one layer's attention to the CPU, and decode collapsed (6 tok/s). Prefer a manual `--n-cpu-moe`.
- Tentative pick: **D** (2.5x prompt processing, same decode, 1.7 GB VRAM headroom → room to lower moe to ~32).

D with `--n-cpu-moe 33`: short tg 61.3, long pp 598 / tg 50.0, peak VRAM 11.1 GB (moe 32: 11.5 GB, not faster). First run after each load is consistently slow (warm-up).

### Step 2 final (new config.yaml, through llama-swap on :8080, median of 3)

| Model | Version | short tg | long pp | long tg | MTP accept |
|---|---|---|---|---|---|
| qwen-3.5 (9B) | original | 53.7 | 1604 | 50.5 | – |
| qwen-3.5 (9B, 262K, q8 KV) | step 2 | 53.0 | 1562 | 48.5 | – |
| qwen-3.5-high-ctx | original | 16.5 | 745 | 12.4 | – |
| qwen-3.5-high-ctx (alias of above) | step 2 | **54.9** | **1553** | **48.7** | – |
| qwen-3.6-27B | original | 4.2 | 174 | 2.7 | – |
| qwen-3.6-27B (`-t 6`) | step 2 | 5.6 | 177 | 3.2 | – |
| qwen-3.6-35B | original | 54.3 | 231 | 53.5 | 0.79 / 0.69 |
| qwen-3.6-35B (config D, moe 33) | step 2 | 46.4 | **597** | 56.8 | 0.78 / 0.75 |

- 262K context on the 9B is now free (4x faster than the old high-ctx entry, same speed as the 128K one).
- 35B: 2.6x faster prompt processing (14K prompt: ~60 s → ~23 s), +9K context, decode equal within noise (35B short tg is the noisiest number we have: 41–51 in this run).
- 27B: +30%, still unusable; replaced in step 3.
