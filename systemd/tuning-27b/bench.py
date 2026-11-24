#!/usr/bin/env python3
"""HARD-PRO 27B benchmark suite.

Повторяемый замер llama.cpp-сервера (OpenAI-совместимый /v1/chat/completions).
Сравнивает конфигурации запуска: baseline, speculative decoding draft-mtp
с разной глубиной черновика, другие ускорители — по одному и тому же набору.

Случаи:
  short_code  — генерация/исправление кода, короткий вход (~1-2K токенов)
  short_tool  — структурированный ответ / tool call (JSON-схема инструмента)
  long32      — ~32K реальных входных токенов (дамп кода из HOME/hardpro)
  long60      — ~60K входных токенов, окно 65536
  <case>_rep  — повтор того же запроса (prefix cache hit)

Каждый прогон пишет JSON-строку в $HP_TUNING_DIR/results/<tag>.jsonl:
TTFT до первого сгенерированного токена (reasoning тоже считается),
серверные timings (prompt/predicted n, ms, tokens/s, cache_n), пиковую VRAM
GPU 1, finish_reason, счётчик tool calls. Sampling фиксирован: temperature 0.6,
top_p 0.95, top_k 20, min_p 0.05, seed 42 — одинаковый для всех прогонов.

Пути настраиваются переменными окружения:
  HP_TUNING_DIR — рабочая папка (по умолчанию ~/hardpro/workspace/tuning-27b-20261121)
  HP_MODEL_LOG  — журнал llama-сервера для серверных таймингов
  HP_BENCH_PORT — порт сервера (по умолчанию 8084)
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import os

HOME = Path.home()
BASE = f"http://127.0.0.1:{os.environ.get('HP_BENCH_PORT', '8084')}"
MODEL = "Huihui-Qwen3.8-27B-abliterated-Q5_K"
WS = Path(os.environ.get("HP_TUNING_DIR", HOME / "hardpro/workspace/tuning-27b-20261121"))
LOG = Path(os.environ.get("HP_MODEL_LOG", HOME / "hardpro/logs/model-hardpro-abliterated-27b.log"))
OUT_DIR = WS / "results"
CORPUS_DIR = WS / "corpus"

SYS = "Ты аккуратный ассистент по коду для разработчика HARD-PRO. Отвечай по-русски, кратко и по делу."

SHORT_CODE = """Проанализируй функцию и исправь баг:

```python
def moving_average(values, window):
    \"\"\"Среднее по скользящему окну.\"\"\"
    result = []
    total = sum(values[:window])
    result.append(total / window)
    for i in range(window, len(values)):
        total += values[i]
        result.append(total / window)
    return result
```

Функция на каждом шаге должна добавлять новый элемент и убирать элемент,
выпавший из окна. Напиши исправленный вариант целиком и одной строкой объясни,
что было не так."""

SHORT_TOOL_MSG = "Какая сейчас погода в Москве и в Екатеринбурге? Используй инструменты."

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Текущая погода в городе",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "Название города"},
                    "units": {"type": "string", "enum": ["c", "f"], "description": "Единицы"},
                },
                "required": ["city"],
            },
        },
    }
]

# вопросы к дампам кода (проверяем, что модель реально читала ввод)
LONG_Q = {
    "long32": (
        "Это дамп кодовой базы HARD-PRO. Найди в дампе функцию _vram_check "
        "и точно процитируй условие, при котором она возвращает ok=False. "
        "Затем одним предложением: что делает registry.stop с процессом, "
        "который не завершился за таймаут."
    ),
    "long60": (
        "Это дамп кодовой базы HARD-PRO. Найди класс или функцию, которая "
        "заведует перенаправлением запросов LAN-шлюза (gateway), и назови "
        "порт, на котором слушает шлюз по умолчанию, и на какие внутренние "
        "порты он пробрасывает трафик. Кратко, списком."
    ),
}


def http(path: str, payload: dict | None = None, timeout: float = 600) -> tuple[int, bytes]:
    url = BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def tokenize(text: str) -> int:
    st, body = http("/tokenize", {"content": text}, timeout=30)
    if st != 200:
        return -1
    return len(json.loads(body).get("tokens", []))


def log_size() -> int:
    try:
        return LOG.stat().st_size
    except OSError:
        return 0


def parse_log_timings(start: int) -> dict:
    """Достаём последние server-side timings после позиции start (резервный путь,
    основной — поле timings в последнем SSE-чанке)."""
    out = {}
    try:
        with LOG.open("rb") as fh:
            fh.seek(start)
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return out
    pe = re.findall(
        r"prompt eval time =\s*([\d.]+) ms / (\d+) tokens \(\s*[\d.]+ ms per token,\s*([\d.]+) tokens per second\)",
        text)
    ev = re.findall(
        r"eval time =\s*([\d.]+) ms / (\d+) tokens \(\s*[\d.]+ ms per token,\s*([\d.]+) tokens per second\)",
        text)
    if pe:
        ms, n, tps = pe[-1]
        out["log_prefill_ms"] = float(ms)
        out["log_prefill_tokens"] = int(n)
    if ev:
        ms, n, tps = ev[-1]
        out["log_eval_ms"] = float(ms)
        out["log_eval_tokens"] = int(n)
    spec = re.findall(r"draft acceptance = [\d.]+ \(\s*\d+ accepted /\s*\d+ generated\), mean len = [\d.]+", text)
    if spec:
        out["log_spec_last"] = spec[-1]
    return out


class VramWatcher(threading.Thread):
    """Опрос VRAM GPU 1 в фоне: peak used и минимум свободного."""

    def __init__(self, gpu: int = 1):
        super().__init__(daemon=True)
        self.gpu = gpu
        self.peak = 0
        self.min_free = 1 << 30
        self.stop_flag = False

    def run(self):
        while not self.stop_flag:
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.used,memory.total",
                     "--format=csv,noheader,nounits", "-i", str(self.gpu)],
                    capture_output=True, text=True, timeout=5).stdout
                used, total = (int(x) for x in out.strip().split(","))
                self.peak = max(self.peak, used)
                self.min_free = min(self.min_free, total - used)
            except Exception:
                pass
            time.sleep(0.5)


def chat_stream(messages: list, max_tokens: int, seed: int, tools=None, tag_run: str = "",
                *, request_options: dict | None = None) -> dict:
    payload = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.6,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.05,
        "seed": seed,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
    if request_options:
        payload.update(request_options)
    start_pos = log_size()
    vw = VramWatcher()
    vw.start()
    t0 = time.perf_counter()
    ttft = None
    text_parts = []
    reasoning_parts = []
    tool_calls = []
    usage = None
    timings = None
    finish = None
    chunks = 0
    try:
        req = urllib.request.Request(BASE + "/v1/chat/completions",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                if obj.get("usage"):
                    usage = obj["usage"]
                if obj.get("timings"):
                    timings = obj["timings"]
                for ch in obj.get("choices", []):
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
                    delta = ch.get("delta") or {}
                    content = delta.get("content")
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if content or reasoning:
                        now = time.perf_counter()
                        if ttft is None:
                            ttft = now - t0
                        if content:
                            text_parts.append(content)
                        if reasoning:
                            reasoning_parts.append(reasoning)
                    for tc in delta.get("tool_calls") or []:
                        tool_calls.append(tc)
                chunks += 1
        total_s = time.perf_counter() - t0
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "tag_run": tag_run}
    finally:
        vw.stop_flag = True
        vw.join(timeout=2)
    text = "".join(text_parts)
    out = {
        "ttft_s": round(ttft, 3) if ttft is not None else None,
        "total_s": round(total_s, 3),
        "gen_s": round(total_s - (ttft or 0), 3),
        "out_chars": len(text),
        "reasoning_chars": sum(len(x) for x in reasoning_parts),
        "finish_reason": finish,
        "tool_calls_n": len(tool_calls),
        "tool_names": [tc.get("function", {}).get("name") for tc in tool_calls],
        "vram_peak_mib": vw.peak,
        "vram_min_free_mib": vw.min_free,
        "chunks": chunks,
        "usage": usage,
        "timings": timings,
        "text_head": text[:160],
        "text_tail": text[-120:],
    }
    if usage:
        pt = usage.get("prompt_tokens")
        ct = usage.get("completion_tokens")
        if pt and ttft is not None and ttft > 0:
            out["client_prefill_tps"] = round(pt / ttft, 1)
        if ct and ct > 1 and (total_s - (ttft or 0)) > 0:
            out["client_decode_tps"] = round((ct - 1) / (total_s - (ttft or 0)), 1)
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        if cached is not None:
            out["api_cached_tokens"] = cached
    out.update(parse_log_timings(start_pos))
    return out


def build_corpus(target_tokens: int, seed_files: list[str], name: str) -> Path:
    """Собираем дамп из реальных файлов до ~target_tokens, кэш на диске."""
    cache = CORPUS_DIR / f"{name}.txt"
    if cache.exists():
        return cache
    parts = []
    total = 0
    for i, p in enumerate(seed_files):
        try:
            body = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        head = f"\n\n===== ФАЙЛ {i:03d}: {p} =====\n"
        parts.append(head + body)
        total += tokenize(head + body)
        if total >= target_tokens:
            break
    text = "".join(parts)
    # грубая подгонка: режем хвост по объёму, если сильно перелетели
    est = tokenize(text)
    while est > target_tokens * 1.08 and len(text) > 1000:
        text = text[: int(len(text) * target_tokens / est)]
        est = tokenize(text)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    return cache


def corpus_files() -> list[str]:
    """Реальные .py-файлы кодовой базы для длинных случаев (без venv/кешей)."""
    roots = [str(HOME / "hardpro")]
    files: list[str] = []
    for root in roots:
        p = Path(root)
        if p.is_dir():
            for q in sorted(p.rglob("*.py")):
                s = str(q)
                if any(x in s for x in ("/.venv", "/venv/", "/node/", "__pycache__", "/tools/node/")):
                    continue
                files.append(s)
    seen, out = set(), []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="run", help="метка конфигурации (baseline, mtp2, ...)")
    ap.add_argument("--cases", default="short_code,short_tool,long32,long60")
    ap.add_argument("--repeats", type=int, default=2, help="повторов на случай (1-й = fresh)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--max-tokens-short", type=int, default=256)
    ap.add_argument("--max-tokens-long", type=int, default=384)
    ap.add_argument("--probe", action="store_true", help="один крошечный запрос, печать сырого ответа")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{args.tag}.jsonl"

    if args.probe:
        r = chat_stream(
            [{"role": "user", "content": "Скажи ровно: pong"}],
            max_tokens=16, seed=1, tag_run="probe")
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    files = corpus_files()
    corpora = {
        "long32": build_corpus(32000, files, "long32"),
        "long60": build_corpus(60000, files, "long60"),
    }
    sys_msg = {"role": "system", "content": SYS}

    for case in args.cases.split(","):
        for rep in range(1, args.repeats + 1):
            if case == "short_code":
                msgs = [sys_msg, {"role": "user", "content": SHORT_CODE}]
                mt = args.max_tokens_short
                tools = None
            elif case == "short_tool":
                msgs = [sys_msg, {"role": "user", "content": SHORT_TOOL_MSG}]
                mt = args.max_tokens_short
                tools = TOOLS
            elif case in ("long32", "long60"):
                body = corpora[case].read_text(encoding="utf-8")
                msgs = [sys_msg,
                        {"role": "user", "content": body + "\n\n===== ЗАДАНИЕ =====\n"
                                                    + LONG_Q[case]}]
                mt = args.max_tokens_long
                tools = None
            else:
                print(f"unknown case {case}", file=sys.stderr)
                continue
            rec = chat_stream(
                msgs, max_tokens=mt, seed=args.seed,
                tools=tools, tag_run=f"{case}#{rep}")
            rec.update({
                "tag": args.tag, "case": case, "rep": rep,
                "fresh": rep == 1,
            })
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            keys = ("ttft_s", "total_s", "vram_peak_mib", "finish_reason", "error")
            print(f"{args.tag} {case}#{rep}: "
                  + " ".join(f"{k}={rec.get(k)}" for k in keys), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
