#!/usr/bin/env python3
"""Измерить почти заполненное окно уже запущенного локального llama-server."""
import argparse
import hashlib
import json
import random
import subprocess
import threading
import time
import urllib.request


def post(port, path, body):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/{path}",
        json.dumps(body).encode(),
        {"Content-Type": "application/json"},
    )
    return urllib.request.urlopen(request, timeout=1200)


def make_prompt(port, length):
    rng = random.Random(408)
    cities = ["North", "East", "West", "South", "Central"]
    products = ["shoes", "belts", "socks", "gloves", "shirts", "pads", "shorts", "bags"]
    rows = []
    for index in range(length // 12):
        rows.append(
            f"Record {index}: depot {rng.choice(cities)} received {rng.randrange(10, 96)} "
            f"{rng.choice(products)} and shipped {rng.randrange(1, 10)}."
        )
    question = "\nWrite a practical explanation in English of how to audit these stock records, in complete sentences.\n"
    with post(port, "tokenize", {"content": "\n".join(rows), "add_special": True}) as response:
        records = json.load(response)["tokens"]
    with post(port, "tokenize", {"content": question, "add_special": False}) as response:
        suffix = json.load(response)["tokens"]
    tokens = records[:length - len(suffix)] + suffix
    if len(tokens) != length:
        raise ValueError("Токенизатор вернул меньше токенов, чем нужно для измерения")
    return tokens


def gpu_memory():
    result = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        text=True, timeout=10,
    )
    values = list(map(int, result.split()))
    if len(values) != 3:
        raise ValueError("Тест рассчитан на три видимые GPU")
    return values


def benchmark(port, context):
    with post(port, "completion", {"prompt": "Warm-up: name one sport.", "n_predict": 8, "cache_prompt": False}) as response:
        json.load(response)
    tokens = make_prompt(port, context - 256)
    query = {"prompt": tokens, "n_predict": 128, "temperature": 0, "seed": 41,
             "ignore_eos": True, "stream": True, "cache_prompt": False}
    idle = gpu_memory()
    peak = idle.copy()
    errors = []
    stop = threading.Event()

    def sample():
        try:
            while not stop.wait(0.5):
                for index, value in enumerate(gpu_memory()):
                    peak[index] = max(peak[index], value)
        except Exception as exc:
            errors.append(exc)

    sampler = threading.Thread(target=sample)
    sampler.start()
    first = None
    timings = {}
    started = time.monotonic()
    try:
        with post(port, "completion", query) as response:
            for line in response:
                if not line.startswith(b"data: {"):
                    continue
                event = json.loads(line[6:])
                if "error" in event:
                    raise RuntimeError(event["error"])
                if first is None and event.get("content"):
                    first = time.monotonic()
                if "timings" in event:
                    timings = event["timings"]
    finally:
        stop.set()
        sampler.join()
    if errors:
        raise RuntimeError("Ошибка измерения VRAM") from errors[0]
    if (first is None or timings.get("prompt_n") != len(tokens)
            or timings.get("predicted_n") != 128 or timings.get("cache_n", 0)):
        raise RuntimeError(f"Неполный прогон или переиспользован префикс: {timings}")
    return {
        "ctx": context, "prompt_n": len(tokens), "input_sha": hashlib.sha256(json.dumps(tokens).encode()).hexdigest(),
        "ttft_s": first - started, "decode_tps": timings["predicted_per_second"],
        "prompt_tps": timings["prompt_per_second"], "idle_mib": idle, "peak_mib": peak,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--ctx-size", required=True, type=int, choices=[16384, 65536, 131072])
    options = parser.parse_args()
    print(json.dumps(benchmark(options.port, options.ctx_size), ensure_ascii=False))
