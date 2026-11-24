#!/usr/bin/env python3
"""Controlled vLLM benchmark for the Qwen3-14B catalog assistant."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
from threading import Barrier, Event, Thread
import time
import urllib.request

SYSTEM = ("Ты помощник по каталогу. Используй только переданные учебные данные. "
          "Отвечай одним JSON-объектом с полями status и free. "
          "Если источник устарел, status=stale и free=null. "
          "Если по паре артикул/склад нет строки, status=unknown и free=null. "
          "Иначе status=known и free=физический остаток минус резерв.")
GOLD = (
    ("Снимок текущий. Товар D100, склад Север: физически 12, резерв 7. "
     "Товар D100, склад Центр: физически 1, резерв 1. Сколько свободно D100 на Север?",
     {"status": "known", "free": 5}),
    ("Снимок текущий. Товар T742, склад Юг: физически 6, резерв 6. "
     "Товар T742, склад Север: физически 10, резерв 1. Сколько свободно T742 на Юг?",
     {"status": "known", "free": 0}),
    ("Снимок текущий. Товар C182 есть только в строке склада Центр: "
     "физически 8, резерв 2. Сколько свободно C182 на Юг?",
     {"status": "unknown", "free": None}),
    ("Снимок устарел. Товар D822, склад Север: физически 11, резерв 1. "
     "Можно подтвердить, что сейчас свободно D822 на Север?",
     {"status": "stale", "free": None}),
)


class Client:
    def __init__(self, port, model, key):
        self.url = f"http://127.0.0.1:{port}"
        self.model = model
        self.headers = {"Content-Type": "application/json"}
        if key:
            self.headers["Authorization"] = f"Bearer {key}"

    def open(self, path, body=None, timeout=180):
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        return urllib.request.urlopen(urllib.request.Request(self.url + path, data, self.headers), timeout=timeout)

    def token_count(self, prompt):
        with self.open("/tokenize", {"model": self.model, "prompt": prompt}, timeout=30) as response:
            return json.load(response)["count"]

    def metrics(self):
        with self.open("/metrics", timeout=8) as response:
            lines = response.read().decode().splitlines()
        names = ("num_preemptions_total", "prefix_cache_queries_total", "prefix_cache_hits_total")
        return {name: sum(float(line.split()[-1]) for line in lines
                          if line.startswith(f"vllm:{name}"+ "{")) for name in names}

    def chat(self, prompt, gold, label):
        ttft = None
        text = []
        usage = {}
        finish = None
        started = time.perf_counter()
        try:
            with self.open("/v1/chat/completions", {
                "model": self.model,
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                "temperature": 0, "seed": 37, "max_tokens": 96,
                "chat_template_kwargs": {"enable_thinking": False},
                "stream": True, "stream_options": {"include_usage": True},
            }) as response:
                for raw in response:
                    if not raw.startswith(b"data:"):
                        continue
                    data = raw[5:].strip()
                    if data == b"[DONE]":
                        break
                    chunk = json.loads(data)
                    if "error" in chunk:
                        raise RuntimeError("vLLM SSE error")
                    usage = chunk.get("usage") or usage
                    for choice in chunk.get("choices", []):
                        finish = choice.get("finish_reason") or finish
                        if part := choice.get("delta", {}).get("content"):
                            if ttft is None:
                                ttft = time.perf_counter() - started
                            text.append(part)
            elapsed = time.perf_counter() - started
            try:
                parsed = json.loads("".join(text).strip().removeprefix("```json").removesuffix("```").strip())
            except json.JSONDecodeError:
                parsed = None
            correct = (parsed == gold and finish == "stop" and
                       type(parsed["free"]) is type(gold["free"]))
            output = usage.get("completion_tokens", 0)
            return {
                "case": label, "correct": correct, "finish": finish,
                "input": usage.get("prompt_tokens"), "output": output,
                "cached": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
                "ttft_s": ttft, "total_s": elapsed,
                "decode_tps": ((output - 1) / (elapsed - ttft)
                               if output > 1 and ttft is not None and elapsed > ttft else None),
            }
        except (OSError, ValueError, RuntimeError) as exc:
            return {"case": label, "correct": False, "error": type(exc).__name__,
                    "http_status": getattr(exc, "code", None)}


def corpus(client, target):
    rows = [f"Артикул S{i:05d}, склад Резерв-{i % 23}, физически {(i * 17) % 143}, резерв {i % 8}.\n"
            for i in range(target // 10)]
    low, high = 0, len(rows)
    while low < high:
        middle = (low + high + 1) // 2
        if client.token_count("".join(rows[:middle])) <= target:
            low = middle
        else:
            high = middle - 1
    return "".join(rows[:low])


def wave(client, jobs):
    barrier = Barrier(len(jobs))
    def submit(job):
        barrier.wait(timeout=5)
        return client.chat(*job)
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        rows = list(pool.map(submit, jobs))
    elapsed = time.perf_counter() - started
    return rows, elapsed


def benchmark(client, tag, gpu, rapid=False):
    stop = Event()
    peak = 0
    def poll():
        nonlocal peak
        while not stop.is_set():
            try:
                result = subprocess.run(["nvidia-smi", "-i", str(gpu), "--query-gpu=memory.used",
                                         "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
                if result.returncode == 0 and result.stdout.strip().isdigit():
                    peak = max(peak, int(result.stdout.strip()))
            except subprocess.TimeoutExpired:
                pass
            stop.wait(.5)

    check = client.chat(GOLD[0][0], GOLD[0][1], "warmup")
    if not check["correct"]:
        raise RuntimeError(f"short warmup failed: {check}")
    short_text = corpus(client, 7600)
    long_text = corpus(client, 37_800) if not rapid else ""
    before = client.metrics()
    cases = []
    waves = []
    watcher = Thread(target=poll, daemon=True)
    watcher.start()
    try:
        for i in range(8):
            data, answer = GOLD[i % len(GOLD)]
            cases.append(client.chat(f"Проверка {tag}-S{i}.\n{data}", answer, "short_serial"))
        for i in range(3):
            jobs = [(f"Запрос {tag}-P{i}-{j}.\n{data}", expected, "short_parallel4")
                    for j, (data, expected) in enumerate(GOLD)]
            rows, sec = wave(client, jobs)
            cases.extend(rows)
            waves.append({"case": "short_parallel4", "seconds": sec, "output": sum(r.get("output", 0) for r in rows)})
        for i in range(2):
            data, expected = GOLD[i]
            prefix = f"Пакет {tag}-M{i}. Ниже посторонние артикулы для сверки.\n"
            jobs = [(prefix + short_text + data, expected, "mixed_long")]
            jobs.extend((f"Запрос {tag}-Q{i}-{j}.\n{q}", a, "mixed_short") for j, (q, a) in enumerate(GOLD[1:]))
            rows, sec = wave(client, jobs)
            cases.extend(rows)
            waves.append({"case": "mixed4", "seconds": sec, "output": sum(r.get("output", 0) for r in rows)})
        if long_text:
            prefix = f"Отчёт {tag}-L. Ниже посторонние артикулы для сверки.\n" + long_text
            for case, item in zip(("long_cold", "long_warm"), GOLD[:2]):
                cases.append(client.chat(prefix + item[0], item[1], case))
    finally:
        stop.set()
        watcher.join(timeout=6)
    if not peak:
        raise RuntimeError("nvidia-smi returned no VRAM samples")
    after = client.metrics()
    return {"tag": tag, "gpu": gpu, "peak_mib": peak, "cases": cases, "waves": waves,
            "metrics_delta": {k: after[k] - before[k] for k in before}}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--model", default="manager")
    p.add_argument("--gpu", type=int, default=0)
    p.add_argument("--tag", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--rapid", action="store_true")
    args = p.parse_args()
    out = benchmark(Client(args.port, args.model, os.environ.get("VLLM_API_KEY")), args.tag, args.gpu, args.rapid)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("a", encoding="utf-8") as f:
        f.write(json.dumps(out) + "\n")
    print(f"{args.tag}: {sum(c['correct'] for c in out['cases'])}/{len(out['cases'])} correct; VRAM {out['peak_mib']} MiB", flush=True)

if __name__ == "__main__":
    main()
