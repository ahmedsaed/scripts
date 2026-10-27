#!/usr/bin/env python3
"""Benchmark llama-swap models via the OpenAI API, using llama-server's timings.

Usage: bench.py [--url URL] [--label LABEL] [--runs N] model [model ...]
Appends one JSON line per run to bench_results.jsonl next to this script.
"""
import argparse
import glob
import json
import time
from pathlib import Path

import requests

HERE = Path(__file__).parent

SHORT_PROMPT = (
    "Write a Python module implementing an LRU cache with TTL expiry, "
    "thread safety, and type hints. Include docstrings and a short usage example."
)


def long_prompt(target_chars=60_000):
    # ~14k tokens of real code per 60k chars, deterministic across runs
    lib = "/usr/lib64/python3.14/"
    first = sorted(glob.glob(lib + "[a-m]*.py"))
    files = first + sorted(set(glob.glob(lib + "**/*.py", recursive=True)) - set(first))
    text = ""
    for f in files:
        text += f"\n# ==== {Path(f).name}\n" + Path(f).read_text(errors="ignore")
        if len(text) >= target_chars:
            break
    return text[:target_chars] + "\n\nSummarize what the modules above do, one line each."


def run(url, model, prompt, max_tokens):
    t0 = time.time()
    r = requests.post(
        f"{url}/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "seed": 42,
            "cache_prompt": False,
            "chat_template_kwargs": {"enable_thinking": False},
        },
        timeout=3600,
    )
    r.raise_for_status()
    t = r.json().get("timings", {})
    res = {
        "prompt_n": t.get("prompt_n"),
        "pp_tps": round(t.get("prompt_per_second") or 0, 1),
        "gen_n": t.get("predicted_n"),
        "tg_tps": round(t.get("predicted_per_second") or 0, 1),
        "wall_s": round(time.time() - t0, 1),
    }
    if t.get("draft_n"):
        res["draft_accept"] = round(t["draft_n_accepted"] / t["draft_n"], 2)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+")
    ap.add_argument("--url", default="http://localhost:8090")
    ap.add_argument("--label", default="")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--tests", default="short,long", help="comma-separated: short,long,deep56k,deep120k")
    a = ap.parse_args()

    all_tests = {
        "short": (SHORT_PROMPT, 512),
        "long": (long_prompt(), 256),          # ~14K tokens
        "deep56k": (long_prompt(240_000), 128),  # ~56K tokens
        "deep120k": (long_prompt(510_000), 128), # ~120K tokens
    }
    tests = {k: all_tests[k] for k in a.tests.split(",")}
    out = open(HERE / "bench_results.jsonl", "a")
    for m in a.models:
        t0 = time.time()
        run(a.url, m, "hi", 1)  # warm-up / load
        print(f"{m}: loaded in {time.time() - t0:.0f}s", flush=True)
        for name, (prompt, n) in tests.items():
            for i in range(a.runs):
                res = run(a.url, m, prompt, n)
                rec = {"label": a.label, "model": m, "test": name, "run": i, **res}
                print(json.dumps(rec), flush=True)
                out.write(json.dumps(rec) + "\n")
                out.flush()


if __name__ == "__main__":
    main()
