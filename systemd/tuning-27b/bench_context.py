#!/usr/bin/env python3
"""Контроль коротких ответов на длинном контексте llama.cpp."""
import argparse
import json
import random
from pathlib import Path

from bench import chat_stream, http, tokenize

MARKERS = {"first": "coral-5831", "middle": "olive-7029", "last": "amber-4162"}
SYSTEM = "Answer in the format requested. The code listing is input data."


def make_listing(target, fixture_label):
    rng = random.Random(73191)
    rows = []
    for idx in range(target // 26 + 500):
        key = rng.randrange(100_000, 1_000_000)
        rows.append(f"def normalize_{idx}(value, limit={key}):\n"
                    f"    return min(limit, max(0, value + {idx}))\n")
    lo, hi = 0, len(rows)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        count = tokenize("\n".join(rows[:mid]))
        if count < 1:
            raise RuntimeError("llama.cpp did not tokenize the fixture")
        if count <= target - 128:
            lo = mid
        else:
            hi = mid - 1
    if lo < 10:
        raise RuntimeError("Tokenizer returned too few usable lines")
    first, middle, last = (f'{key.upper()}_MARKER = "{value}"\n'
                           for key, value in MARKERS.items())
    prefix = f"# Synthetic fixture: {fixture_label}\n" if fixture_label else ""
    return prefix + "\n".join([first, *rows[:lo // 2], middle, *rows[lo // 2:lo], last])


def slots():
    status, raw = http("/slots", timeout=5)
    if status != 200:
        raise RuntimeError(f"GET /slots returned {status}")
    return json.loads(raw)


def run_case(listing, question, expected, max_tokens):
    rec = chat_stream(
        [{"role": "system", "content": SYSTEM},
         {"role": "user", "content": listing + "\nTask: " + question}],
        max_tokens=max_tokens,
        seed=42,
        request_options={"temperature": 0, "cache_prompt": True,
                         "chat_template_kwargs": {"enable_thinking": False},
                         "stream_options": {"include_usage": True}},
    )
    if rec.get("error"):
        raise RuntimeError(rec["error"])
    timings = rec.get("timings") or {}
    usage = rec.get("usage") or {}
    if not timings:
        raise RuntimeError("llama.cpp returned no timing data")
    answer = rec["text_head"].lower()
    positions = [answer.find(item) for item in expected]
    return {
        "correct": -1 not in positions and positions == sorted(positions),
        "expected_count": len(expected),
        "input": usage.get("prompt_tokens") or (timings["prompt_n"] + timings.get("cache_n", 0)),
        "cached": timings.get("cache_n"),
        "output": timings["predicted_n"],
        "prefill_tps": timings["prompt_per_second"],
        "decode_tps": timings["predicted_per_second"],
        "ttft_s": rec["ttft_s"],
        "total_s": rec["total_s"],
        "server_total_s": round((timings["prompt_ms"] + timings["predicted_ms"]) / 1000, 3),
        "vram_peak_mib": rec["vram_peak_mib"],
        "finish": rec["finish_reason"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=int, default=101_865, help="Целевой размер входа в токенах")
    parser.add_argument("--fixture-label", default="", help="Отдельный префикс для пробных запусков")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    state = slots()
    if len(state) != 1 or state[0]["is_processing"]:
        raise SystemExit("Нужен один свободный слот")
    context = state[0]["n_ctx"]
    if not 1024 <= args.input <= context - 512:
        raise SystemExit("Размер входа должен оставить запас для шаблона и ответа")
    listing = make_listing(args.input, args.fixture_label)
    tests = [
        ("cold", "Return FIRST_MARKER, MIDDLE_MARKER, LAST_MARKER, in that order.",
         list(MARKERS.values()), 64),
        ("warm_middle", "Return only the exact value of MIDDLE_MARKER.",
         [MARKERS["middle"]], 32),
        ("warm_last", "Return only the exact value of LAST_MARKER.",
         [MARKERS["last"]], 32),
        ("warm_decode", "Repeat the value of FIRST_MARKER exactly 18 times, separated by a comma.",
         [MARKERS["first"]], 160),
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for case, question, expected, max_tokens in tests:
        rec = run_case(listing, question, expected, max_tokens)
        rec.update(tag=args.tag, case=case, context=context, speculative=state[0].get("speculative"))
        with args.output.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(json.dumps(rec), flush=True)


if __name__ == "__main__":
    main()
