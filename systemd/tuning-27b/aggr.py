#!/usr/bin/env python3
"""Сводка результатов bench.py: таблица baseline -> кандидаты по всем тегам.

Использование: python3 aggr.py   (читает $HP_TUNING_DIR/results/*.jsonl)
"""
import glob
import json
import os
import statistics as st
from pathlib import Path

RES = str(Path(os.environ.get(
    "HP_TUNING_DIR", Path.home() / "hardpro/workspace/tuning-27b-20261121")) / "results")


def load(tag):
    rows = []
    p = os.path.join(RES, f"{tag}.jsonl")
    if not os.path.exists(p):
        return rows
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def agg(rows, case, fresh=None):
    sel = [r for r in rows if r["case"] == case and (fresh is None or r.get("fresh") == fresh)]
    if not sel:
        return None
    def med(k, d=None):
        vals = [r.get(k) for r in sel if isinstance(r.get(k), (int, float))]
        return round(st.median(vals), 2) if vals else d
    return {
        "runs": len(sel),
        "ttft": med("ttft_s"),
        "total": med("total_s"),
        "decode_tps": round(st.median([(r.get("timings") or {}).get("predicted_per_second") or 0 for r in sel]), 1),
        "prefill_tps": round(st.median([(r.get("timings") or {}).get("prompt_per_second") or 0 for r in sel]), 1),
        "prompt_n": st.median([(r.get("timings") or {}).get("prompt_n") or 0 for r in sel]),
        "cache_n": st.median([(r.get("timings") or {}).get("cache_n") or 0 for r in sel]),
        "pred_n": st.median([(r.get("timings") or {}).get("predicted_n") or 0 for r in sel]),
        "vram": med("vram_peak_mib"),
        "finish": sel[0].get("finish_reason"),
        "err": sum(1 for r in sel if r.get("error")),
    }


tags = sorted({Path(p).stem for p in glob.glob(os.path.join(RES, "*.jsonl"))})
cases = ["short_code", "short_tool", "long32", "long60"]
print(f"{'case':<14}{'tag':<12}{'fresh':<7}{'ttft':>8}{'total':>9}{'dec_tps':>9}{'pre_tps':>9}"
      f"{'p_in':>7}{'cache':>7}{'vram':>7}  finish")
for tag in tags:
    rows = load(tag)
    for case in cases:
        for fresh in (None,) if case.startswith("short") else (True, False):
            a = agg(rows, case, fresh)
            if not a:
                continue
            lbl = "-" if fresh is None else ("cold" if fresh else "cache")
            print(f"{case:<14}{tag:<12}{lbl:<7}{a['ttft']!s:>8}{a['total']!s:>9}"
                  f"{a['decode_tps']!s:>9}{a['prefill_tps']!s:>9}{int(a['prompt_n']):>7}"
                  f"{int(a['cache_n']):>7}{a['vram']!s:>7}  {a['finish']} err={a['err']}")
