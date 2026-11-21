"""HARD-PRO LAN hub: model board, live speed bench, service links.

Runs behind LAN bind + HTTP Basic. No nginx needed: TLS terminated here.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

try:  # каталог моделей рядом с доской, пакет общий с агентом
    from models import api as models_api
except ImportError:
    models_api = None  # type: ignore[assignment]

ROOT = Path.home() / "hardpro"
HUB = ROOT / "hub"
WORKSPACE = ROOT / "workspace"
DB = HUB / "bench.sqlite"
HUB_USER = os.environ.get("HUB_USER", "hardpro")
HUB_PASS = os.environ.get("HUB_PASS", "")
CATALOG_TOKEN = os.environ.get("CATALOG_MANAGER_TOKEN", "")

TARGETS = [
    {
        "id": "coding",
        "title": "Qwen3.8 Flash Next",
        "kind": "llama.cpp",
        "base": "http://127.0.0.1:8081",
        "model": "Qwen3.8-Flash-Next",
        "note": "агентная разработка",
        "bench": True,
    },
    {
        "id": "media",
        "title": "HARD-PRO Медиа",
        "kind": "llama.cpp",
        "base": "http://127.0.0.1:8083",
        "model": "hardpro-media-vision",
        "note": "27B + mmproj",
        "bench": True,
    },
    {
        "id": "manager",
        "title": "hardpro-manager",
        "kind": "catalog RAG",
        "base": "http://127.0.0.1:8767/manager",
        "model": "hardpro-manager",
        "note": "каталог 1С, без LLM-скорости",
        "bench": False,
    },
    {
        "id": "ollama",
        "title": "ollama",
        "kind": "ollama",
        "base": "http://127.0.0.1:11434",
        "model": "qwen3:0.6b",
        "note": "служебная, embed/тесты",
        "bench": True,
    },
]

LINKS = [
    {"title": "Open WebUI", "url": "/", "note": "чат, аккаунты менеджеров"},
    {"title": "Агент-разработка", "url": "/agent/", "note": "терминал, файлы, браузер, MCP"},
]

BENCH_PROMPT = (
    "Напиши на Python функцию, которая читает текстовый файл построчно, "
    "отбрасывает пустые строки и возвращает список кортежей (номер, строка). "
    "Добавь обработку ошибок ввода-вывода и docstring."
)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
security = HTTPBasic(auto_error=False)


def auth(cred: HTTPBasicCredentials | None = Depends(security)) -> str:
    if not HUB_PASS:
        raise HTTPException(503, "hub password not configured")
    if cred is None:
        raise HTTPException(401, "auth required", headers={"WWW-Authenticate": "Basic"})
    ok_user = hmac.compare_digest(cred.username.encode(), HUB_USER.encode())
    ok_pass = hmac.compare_digest(cred.password.encode(), HUB_PASS.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(401, "bad credentials", headers={"WWW-Authenticate": "Basic"})
    return cred.username


def db() -> sqlite3.Connection:
    HUB.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.execute(
        """CREATE TABLE IF NOT EXISTS runs(
            ts REAL, target TEXT, model TEXT, prompt_tps REAL, gen_tps REAL,
            ttft_ms REAL, gen_tokens INTEGER, ok INTEGER, err TEXT)"""
    )
    con.commit()
    return con


def gpu() -> list[dict]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) == 5:
            rows.append({"index": p[0], "name": p[1], "used": p[2], "total": p[3], "util": p[4]})
    return rows


def workspace_stats() -> dict:
    idx = WORKSPACE / "index.sqlite"
    if not idx.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{idx}?mode=ro", uri=True, timeout=5)
        out = {}
        for table in ("projects", "sessions", "skills", "mcp_servers"):
            try:
                out[table] = con.execute(f"select count(*) from {table}").fetchone()[0]
            except sqlite3.Error:
                out[table] = 0
        con.close()
        return out
    except sqlite3.Error:
        return {}


async def probe(target: dict) -> dict:
    info = {"id": target["id"], "title": target["title"], "kind": target["kind"],
            "model": target["model"], "note": target["note"], "online": False, "models": [],
            "bench": bool(target["bench"])}
    headers = {"Authorization": f"Bearer {CATALOG_TOKEN}"} if target["id"] == "manager" else {}
    try:
        async with httpx.AsyncClient(timeout=6) as cli:
            r = await cli.get(f"{target['base']}/v1/models", headers=headers)
            if r.status_code == 200:
                info["online"] = True
                info["models"] = [m.get("id") or m.get("name") for m in r.json().get("data", [])]
            if target["kind"] == "llama.cpp":
                p = await cli.get(f"{target['base']}/props")
                if p.status_code == 200:
                    props = p.json()
                    gen = props.get("default_generation_settings", {})
                    info["ctx"] = gen.get("n_ctx")
                    info["model_path"] = props.get("model_path", "")
                    info["slots"] = props.get("total_slots")
    except (httpx.HTTPError, ValueError):
        pass
    con = db()
    row = con.execute(
        "select ts, prompt_tps, gen_tps, ttft_ms, gen_tokens, ok from runs "
        "where target=? and ok=1 order by ts desc limit 1", (target["id"],)
    ).fetchone()
    con.close()
    if row:
        info["last"] = {"ts": row[0], "prompt_tps": row[1], "gen_tps": row[2],
                        "ttft_ms": row[3], "gen_tokens": row[4]}
    return info


async def bench(target: dict) -> dict:
    payload = {
        "model": target["model"],
        "messages": [{"role": "user", "content": BENCH_PROMPT}],
        "max_tokens": 160,
        "temperature": 0.0,
        "stream": True,
    }
    started = time.perf_counter()
    ttft = None
    chunks = 0
    text = 0
    prompt_tps = None
    err = ""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=240)) as cli:
            async with cli.stream("POST", f"{target['base']}/v1/chat/completions",
                                  json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread())[:200].decode("utf-8", "replace")
                    raise RuntimeError(f"HTTP {resp.status_code}: {body}")
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue
                    tim = obj.get("timings") or {}
                    if tim.get("prompt_per_second"):
                        prompt_tps = round(float(tim["prompt_per_second"]), 2)
                    for ch in obj.get("choices", []):
                        d = ch.get("delta") or {}
                        piece = (d.get("content") or "") + (d.get("reasoning_content") or "")
                        if piece:
                            if ttft is None:
                                ttft = (time.perf_counter() - started) * 1000
                            text += len(piece)
                            chunks += 1
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        err = f"{type(exc).__name__}: {exc}"[:300]
    elapsed = time.perf_counter() - started
    gen_tps = round(chunks / elapsed, 2) if chunks and elapsed > 0 else None
    if ttft and elapsed > (ttft / 1000):
        gen_tps = round(chunks / (elapsed - ttft / 1000), 2)
    con = db()
    con.execute(
        "insert into runs values(?,?,?,?,?,?,?,?,?)",
        (time.time(), target["id"], target["model"], prompt_tps, gen_tps,
         ttft, chunks, 0 if err else 1, err or None),
    )
    con.commit()
    con.close()
    return {"target": target["id"], "ok": not err, "error": err, "prompt_tps": prompt_tps,
            "gen_tps": gen_tps, "ttft_ms": round(ttft, 1) if ttft else None,
            "gen_tokens": chunks, "chars": text, "wall_s": round(elapsed, 2)}


def history(target_id: str, limit: int = 12) -> list[dict]:
    con = db()
    rows = con.execute(
        "select ts, prompt_tps, gen_tps, ttft_ms, gen_tokens, ok from runs "
        "where target=? order by ts desc limit ?", (target_id, limit)
    ).fetchall()
    con.close()
    return [{"ts": r[0], "prompt_tps": r[1], "gen_tps": r[2],
             "ttft_ms": r[3], "gen_tokens": r[4], "ok": bool(r[5])} for r in rows]


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "ts": time.time()}


@app.get("/api/state")
async def api_state(_: str = Depends(auth)) -> JSONResponse:
    cards = [await probe(t) for t in TARGETS]
    return JSONResponse({"cards": cards, "gpu": gpu(), "links": LINKS,
                         "workspace": workspace_stats(), "ts": time.time()})


@app.post("/api/bench/{target_id}")
async def api_bench(target_id: str, _: str = Depends(auth)) -> JSONResponse:
    target = next((t for t in TARGETS if t["id"] == target_id), None)
    if target is None or not target["bench"]:
        raise HTTPException(404, "unknown target")
    return JSONResponse(await bench(target))


@app.post("/api/bench")
async def api_bench_all(_: str = Depends(auth)) -> JSONResponse:
    out = []
    for t in TARGETS:
        if t["bench"]:
            out.append(await bench(t))
    return JSONResponse({"runs": out})


if models_api is not None:
    app.include_router(models_api.router, dependencies=[Depends(auth)])


@app.get("/")
async def index(_: str = Depends(auth)) -> HTMLResponse:
    cards = [await probe(t) for t in TARGETS]
    hist = {t["id"]: history(t["id"]) for t in TARGETS}
    stats = workspace_stats()
    payload = json.dumps({"cards": cards, "gpu": gpu(), "links": LINKS,
                          "workspace": stats, "hist": hist}, ensure_ascii=False)
    return HTMLResponse(PAGE.replace("__STATE__", payload))


@app.get("/app.css")
async def css() -> HTMLResponse:
    path = Path(__file__).with_name("style.css")
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        body = STYLE
    return HTMLResponse(body, media_type="text/css")


PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<base href="/hub/">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HARD-PRO — модели</title><link rel="stylesheet" href="app.css"></head>
<body>
<header>
  <div class="brand"><span class="logo">HP</span> HARD-PRO <b>LAN</b></div>
  <div class="sub">модели, карты, замеры — только своя сеть</div>
  <nav><a href="/hub/">доска</a><a href="/agent/">агент</a><a href="/">чат</a></nav>
</header>
<main>
<section class="panel">
  <div class="phead"><h2>Модели и карты</h2>
    <div class="tools"><button id="checkbtn" class="ghost">проверить каталог</button>
    <button id="refresh" class="ghost">обновить</button><span id="mmsg" class="dim"></span></div></div>
  <div id="gpus" class="gpus"></div>
  <div id="roles" class="roles"></div>
  <div id="models" class="mgrid"></div>
  <div class="addrow"><button id="addmodel" class="ghost">＋ новый профиль</button><span id="checkmsg" class="dim"></span></div>
</section>
<section class="panel"><h2>Скорость</h2>
<div class="row"><button id="bench">Прогнать замер по всем</button><span id="benchmsg" class="dim"></span></div>
<div id="hist" class="hist"></div></section>
<section class="panel"><h2>Железо</h2><div id="gpu" class="gpu"></div>
<div id="ws" class="ws"></div></section>
<section class="panel"><h2>Входы</h2><div id="links" class="links"></div></section>
</main>
<footer>harness: llama.cpp · каталог моделей · agentd · каталог 1С</footer>
<script>
const S = __STATE__;
const $ = (x) => document.getElementById(x);
const esc = (v) => String(v == null ? '' : v).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let M = null;

async function call(path, body, method){
  const opt = {method: method || 'POST', headers: {'Content-Type': 'application/json'}};
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt);
  let d = {};
  try { d = await r.json(); } catch(e){}
  if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
  return d;
}

function mmsg(text, bad){
  const el = $('mmsg');
  el.textContent = text || '';
  el.classList.toggle('bad', !!bad);
}

async function loadModels(){
  try { M = await call('api/models', undefined, 'GET'); renderSwitch(); mmsg(''); }
  catch(e){ $('models').innerHTML = '<div class="dim">каталог не отвечает: '+esc(e.message||e)+'</div>'; }
}

function gpuName(i){
  const g = (M.gpu||[]).find(x => x.index === i);
  return g ? g.name.replace('NVIDIA ','') : 'GPU ' + i;
}

function renderSwitch(){
  if (!M) return;
  renderGpus();
  renderRoles();
  renderProfiles();
}

function renderGpus(){
  $('gpus').innerHTML = (M.gpu||[]).map(g => {
    const pinned = (M.models||[]).filter(m => (m.gpus||[]).includes(g.index));
    const pct = Math.max(0, Math.min(100, Math.round(g.used_mib / g.total_mib * 100)));
    return `<div class="gpucard">
      <div class="gttl"><b>GPU ${g.index}</b> ${esc(gpuName(g.index))} <span class="chip ${pct>5?'ok':''}">${pct}%</span></div>
      <div class="bar"><i style="width:${pct}%"></i></div>
      <div class="ginfo">${g.used_mib} / ${g.total_mib} MiB · ${g.util}% · ${g.temp}°C</div>
      <div class="gpins">${pinned.map(m => `<span class="pin ${m.online?'on':''}">${esc(m.label)}</span>`).join('') || '<span class="dim">пусто</span>'}</div>
    </div>`;
  }).join('') || '<div class="dim">nvidia-smi недоступен</div>';
}

function modelOptions(current){
  return (M.models||[]).map(m =>
    `<option value="${esc(m.id)}"${m.id===current?' selected':''}>${esc(m.label)}${(m.gpus||[]).length?' · GPU '+m.gpus.join(','):''}${m.online?' ●':''}</option>`).join('');
}

function renderRoles(){
  const rows = Object.entries(M.roles||{}).map(([role, model]) =>
    `<div class="rrow"><b>${esc(role)}</b>
      <select data-role="${esc(role)}">${modelOptions(model)}</select>
      <button class="primary" data-goswitch="${esc(role)}">переключить</button></div>`).join('') || '';
  $('roles').innerHTML = rows + `
    <div class="rrow"><b>＋</b>
      <input id="newrole" placeholder="имя роли">
      <select id="newrolemodel">${modelOptions('')}</select>
      <button class="ghost" id="addrole">добавить</button></div>`;

  document.querySelectorAll('button[data-goswitch]').forEach(btn => {
    btn.onclick = async () => {
      const role = btn.getAttribute('data-goswitch');
      const model = document.querySelector(`select[data-role="${role}"]`).value;
      btn.disabled = true;
      mmsg(role + ': поднимаю ' + model + '…');
      try {
        const d = await call('api/models/switch', {role, model, start: true});
        mmsg(role + ' → ' + model + ' · GPU ' + (d.gpus||[]).join(',') + ' · порт ' + d.port +
          (d.evicted && d.evicted.length ? ' · вытеснено: ' + d.evicted.join(', ') : ''));
      } catch(e){ mmsg('ошибка: ' + (e.message||e), true); }
      btn.disabled = false;
      loadModels();
    };
  });
  $('addrole').onclick = async () => {
    const role = $('newrole').value.trim(), model = $('newrolemodel').value;
    if (!role) return mmsg('имя роли пустое', true);
    try { await call('api/models/role', {role, model}); mmsg('роль ' + role + ' → ' + model); }
    catch(e){ return mmsg('ошибка: ' + (e.message||e), true); }
    loadModels();
  };
}

function collectCard(card, m){
  const int = (v, dflt) => { const n = parseInt(v, 10); return isNaN(n) ? dflt : n; };
  const spec = {
    label: card.querySelector('.f-label').value.trim() || m.id,
    backend: m.backend,
    note: card.querySelector('.f-note').value,
  };
  if (m.backend === 'llamacpp'){
    spec.port = int(card.querySelector('.f-port').value, m.port || 0);
    spec.n_ctx = card.querySelector('.f-ctx').value === '' ? null : int(card.querySelector('.f-ctx').value, m.n_ctx);
    spec.n_gpu_layers = card.querySelector('.f-ngl').value === '' ? null : int(card.querySelector('.f-ngl').value, m.n_gpu_layers);
    spec.vram_mib = int(card.querySelector('.f-vram').value, 0);
    const ts = card.querySelector('.f-tsplit');
    if (ts) spec.tensor_split = ts.value.trim() || null;
    const kv = card.querySelector('.f-kvoff');
    if (kv) spec.kv_offload = kv.checked;
    spec.autostart = card.querySelector('.f-auto').checked;
    spec.model_path = card.querySelector('.f-path').value.trim();
    const mm = card.querySelector('.f-mmproj');
    if (mm) spec.mmproj = mm.value.trim();
    spec.gpus = [...card.querySelectorAll('input[data-gpu]:checked')].map(x => parseInt(x.dataset.gpu, 10));
    spec.args = card.querySelector('.f-args').value.trim().split(/\\s+/).filter(Boolean);
    if (m.subcmd) spec.subcmd = m.subcmd;
  } else {
    spec.base = card.querySelector('.f-base').value.trim();
    spec.token_env = card.querySelector('.f-token').value.trim();
    spec.gpus = [];
    spec.vram_mib = 0;
  }
  return spec;
}

function renderProfiles(){
  $('models').innerHTML = (M.models||[]).map(m => {
    const status = m.online ? '<span class="chip ok">онлайн</span>'
      : (m.running ? '<span class="chip warn">жив</span>' : '<span class="chip off">выключена</span>');
    const rolesChips = (m.roles||[]).map(r => `<span class="pin on">${esc(r)}</span>`).join('')
      || '<span class="dim">роль не назначена</span>';
    const gpuBoxes = (M.gpu||[]).map(g =>
      `<label class="gbox"><input type="checkbox" data-gpu="${g.index}" ${(m.gpus||[]).includes(g.index)?'checked':''}> GPU ${g.index}</label>`).join('')
      || '<span class="dim">карты не определены</span>';
    const existsWarn = m.backend === 'llamacpp' && m.model_exists === false ? '<span class="bad">нет файла модели</span>' : '';
    const llama = m.backend === 'llamacpp';
    return `<article class="mcard ${m.online?'on':(m.running?'load':'off')}" data-id="${esc(m.id)}">
      <div class="mttl"><div class="mtop"><input class="f-label" value="${esc(m.label)}">${status}</div>
        <div class="mid">${esc(m.id)} · ${esc(m.backend)} ${existsWarn}</div></div>
      ${llama ? `
        <div class="frow"><span class="flab">карты</span>${gpuBoxes}</div>
        <div class="fgrid">
          <label>порт<input class="f-port" type="number" value="${m.port||''}"></label>
          <label>контекст<input class="f-ctx" type="number" value="${m.n_ctx||''}" placeholder="0 — из модели"></label>
          <label>слоёв на GPU<input class="f-ngl" value="${m.n_gpu_layers==null?'':m.n_gpu_layers}" placeholder="пусто = auto (--fit)"></label>
          <label>VRAM, MiB<input class="f-vram" type="number" value="${m.vram_mib||0}"></label>
        </div>
        <div class="fgrid">
          <label>tensor-split<input class="f-tsplit" value="${esc(m.tensor_split||'')}" placeholder="напр. 3,1 — доли карт"></label>
          <label class="chkbox"><input type="checkbox" class="f-kvoff" ${m.kv_offload!==false?'checked':''}> KV-кэш в VRAM</label>
        </div>
        <div class="hint">контекст: 16–32k на одной карте · 100k+ — обе карты («слоёв» пусто = --fit) или сними «KV-кэш в VRAM», кэш уйдёт в RAM · после правки: сохранить → перезапустить</div>
        <label class="wide">файл модели<input class="f-path" value="${esc(m.model_path||'')}" spellcheck="false"></label>
        <label class="wide">mmproj<input class="f-mmproj" value="${esc(m.mmproj||'')}" spellcheck="false"></label>
        <div class="frow"><span class="flab">флаги</span><textarea class="f-args" rows="2" spellcheck="false">${esc((m.args||[]).join(' '))}</textarea></div>
        <label class="chk"><input type="checkbox" class="f-auto" ${m.autostart?'checked':''}> запускать при старте сервера</label>
      ` : `
        <div class="frow"><span class="flab">карты</span>${(m.gpus||[]).length
          ? (m.gpus||[]).map(g=>`<span class="pin">GPU ${esc(g)}</span>`).join('')
          : '<span class="dim">—</span>'}</div>
        <div class="fgrid">
          <label class="wide">base<input class="f-base" value="${esc(m.base||'')}" spellcheck="false"></label>
          <label>token_env<input class="f-token" value="${esc(m.token_env||'')}" spellcheck="false"></label>
        </div>`}
      <label class="wide">заметка<input class="f-note" value="${esc(m.note||'')}"></label>
      <div class="mroles">${rolesChips}</div>
      <div class="mbtns">
        <button class="primary" data-save="${esc(m.id)}">сохранить</button>
        ${llama ? (m.running
          ? `<button class="ghost" data-restart="${esc(m.id)}">перезапустить</button>
             <button class="ghost" data-stop="${esc(m.id)}">погасить</button>`
          : `<button class="primary2" data-start="${esc(m.id)}">поднять</button>`) : ''}
        <button class="ghost danger" data-del="${esc(m.id)}">удалить</button>
      </div>
    </article>`;
  }).join('');

  document.querySelectorAll('.mcard').forEach(card => {
    const id = card.getAttribute('data-id');
    const m = (M.models||[]).find(x => x.id === id);
    const busy = async (btn, fn) => {
      btn.disabled = true;
      mmsg(id + ': …');
      try { const d = await fn(); mmsg(id + ': ok'); loadModels(); return d; }
      catch(e){ mmsg(id + ': ' + (e.message||e), true); }
      finally { btn.disabled = false; }
    };
    const save = card.querySelector('[data-save]');
    if (save) save.onclick = () => busy(save, async () => {
      await call('api/models/profile', {model: id, spec: collectCard(card, m)});
      if (m.running) mmsg(id + ': сохранено — перезапусти, чтобы применить');
    });
    const start = card.querySelector('[data-start]');
    if (start) start.onclick = () => busy(start, async () => {
      const d = await call('api/models/launch', {model: id});
      mmsg(id + ': поднята на GPU ' + (d.gpus||[]).join(',') + ' · порт ' + d.port);
    });
    const stop = card.querySelector('[data-stop]');
    if (stop) stop.onclick = () => busy(stop, async () => {
      const d = await call('api/models/stop', {model: id});
      mmsg(id + ': погашена');
    });
    const restart = card.querySelector('[data-restart]');
    if (restart) restart.onclick = () => busy(restart, async () => {
      const d = await call('api/models/restart', {model: id});
      mmsg(id + ': перезапущена на GPU ' + (d.gpus||[]).join(',') + ' · порт ' + d.port);
    });
    const del = card.querySelector('[data-del]');
    if (del) del.onclick = () => busy(del, async () => {
      if (!confirm('Удалить профиль ' + id + ' из каталога?')) return;
      await call('api/models/profile/' + encodeURIComponent(id), undefined, 'DELETE');
      mmsg(id + ': профиль удалён');
    });
  });
}

function cards(){
  $('cards') && ($('cards').innerHTML = (S.cards||[]).map(c => `
    <article class="card ${c.online?'on':'off'}">
      <div class="ttl"><i class="dot"></i>${esc(c.title)}</div>
      <div class="kind">${esc(c.kind)}${c.ctx? ' · ctx '+c.ctx : ''}</div>
      <div class="model">${esc(c.model)}</div>
      <div class="note">${esc(c.note||'')}</div>
      <div class="metrics">
        <div><b>${c.last?fmt(c.last.gen_tps):'—'}</b><span>tok/s</span></div>
        <div><b>${c.last?fmt(c.last.ttft_ms,0):'—'}</b><span>ttft, мс</span></div>
        <div><b>${c.last?fmt(c.last.prompt_tps):'—'}</b><span>prompt t/s</span></div>
      </div>
      ${c.online && c.bench ? `<a class="go" href="api/bench/${c.id}" onclick="return false">замер</a>`:''}
    </article>`).join(''));
  document.querySelectorAll('.go').forEach(el => {
    el.onclick = async () => {
      const id = el.getAttribute('href').split('/').pop();
      el.textContent='…';
      const r = await fetch('api/bench/'+id,{method:'POST'});
      const d = await r.json();
      el.textContent = d.ok ? fmt(d.gen_tps)+' tok/s' : 'ошибка';
      setTimeout(()=>location.reload(), 800);
    };
  });
  $('gpu').innerHTML = (S.gpu||[]).map(g =>
    `<div class="gpucard"><b>GPU ${g.index}</b> ${esc(g.name)}<div class="bar"><i style="width:${Math.round(g.used/g.total*100)}%"></i></div>
     <span>${g.used}/${g.total} MiB · ${g.util}%</span></div>`).join('');
  $('ws').innerHTML = S.workspace && Object.keys(S.workspace).length
    ? Object.entries(S.workspace).map(([k,v])=>`<span class="chip">${esc(k)}: <b>${v}</b></span>`).join('')
    : '<span class="chip dim">workspace не импортирован</span>';
  $('links').innerHTML = (S.links||[]).map(l =>
    `<a class="link" href="${l.url}" target="_blank"><b>${esc(l.title)}</b><span>${esc(l.note)}</span></a>`).join('');
  $('hist').innerHTML = Object.entries(S.hist||{}).map(([id, rows]) => {
    if(!rows.length) return '';
    const max = Math.max(...rows.map(r=>r.gen_tps||0), 1);
    return `<div class="hrow"><div class="hname">${esc(id)}</div><div class="bars">`+
      rows.slice().reverse().map(r=>`<i class="${r.ok?'':'bad'}" title="${new Date(r.ts*1000).toLocaleString()}"
        style="height:${Math.max(3, Math.round((r.gen_tps||0)/max*46))}px"></i>`).join('')+
      `</div><div class="hlen">${fmt(rows[0].gen_tps)} tok/s</div></div>`;
  }).join('') || '<div class="dim">замеров ещё нет</div>';
}

const fmt = (v, d=1) => (v===null||v===undefined) ? '—' : Number(v).toFixed(d);
cards();

$('refresh').onclick = () => loadModels();
$('checkbtn').onclick = async (e) => {
  e.target.disabled = true;
  $('checkmsg').textContent = 'проверяю…';
  try {
    const d = await call('api/models/check', undefined, 'GET');
    $('checkmsg').textContent = d.ok ? 'каталог в порядке'
      : ('проблемы: ' + (d.problems||[]).join(' | ')) + ((d.notes||[]).length ? ' · заметки: ' + d.notes.join(' | ') : '');
    $('checkmsg').classList.toggle('bad', !d.ok);
  } catch(err){ $('checkmsg').textContent = 'ошибка: ' + (err.message||err); }
  e.target.disabled = false;
};
$('addmodel').onclick = async () => {
  const id = prompt('Имя модели (id):', 'new-model');
  if (!id) return;
  const ports = (M.models||[]).map(m => m.port || 0);
  const nextPort = Math.max(8090, ...ports) + 1;
  try {
    await call('api/models/profile', {model: id, spec: {label: id, backend: 'llamacpp',
      gpus: [], port: nextPort, n_ctx: 16384, vram_mib: 0, args: ['--jinja'], note: 'новый профиль'}});
    mmsg('профиль ' + id + ' создан — заполни карту, файл и флаги');
    loadModels();
  } catch(e){ mmsg('ошибка: ' + (e.message||e), true); }
};

$('bench').onclick = async (e) => {
  e.target.disabled = true;
  $('benchmsg').textContent = 'идёт замер…';
  const r = await fetch('api/bench',{method:'POST'}); const d = await r.json();
  $('benchmsg').textContent = (d.runs||[]).map(x=>x.target+': '+(x.ok?fmt(x.gen_tps)+' tok/s':'ошибка')).join(' · ');
  setTimeout(()=>location.reload(), 1200);
};

loadModels();
setInterval(loadModels, 30000);
</script></body></html>
"""

STYLE = """
:root{
  --bg:#f6f4f2;--pnl:#ffffff;--pnl2:#faf8f7;--line:#e7e0dc;--line2:#d9cfc9;
  --tx:#191b20;--dm:#6f7480;--ac:#d0342c;--ac2:#a3211b;--ok:#177245;
  --warn:#b7791f;--no:#c0392b;--mono:ui-monospace,"JetBrains Mono",Consolas,monospace;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--tx);
  font:15px/1.55 -apple-system,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
header{position:sticky;top:0;z-index:5;padding:14px 28px;border-bottom:1px solid var(--line);
  background:#fff;display:flex;flex-wrap:wrap;gap:14px;align-items:baseline}
.brand{font-size:19px;font-weight:700;display:flex;align-items:center;gap:10px}
.logo{background:var(--ac);color:#fff;border-radius:8px;padding:2px 9px;font-size:14px;letter-spacing:1px}
.brand b{color:var(--ac)}
.sub{color:var(--dm);font-size:12.5px}
nav{margin-left:auto;display:flex;gap:6px}
nav a{color:var(--dm);text-decoration:none;font-size:13px;padding:5px 12px;border-radius:8px;border:1px solid var(--line)}
nav a:hover{color:var(--ac);border-color:var(--ac)}
main{padding:18px 36px 48px}
.panel{background:var(--pnl);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin-bottom:16px;
  box-shadow:0 1px 2px rgba(20,20,20,.04)}
.panel h2{font-size:11.5px;text-transform:uppercase;letter-spacing:1.1px;color:var(--dm);margin:0;font-weight:700}
.phead{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
.phead .tools{margin-left:auto;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.row{display:flex;gap:14px;align-items:center;flex-wrap:wrap}
button{background:#fff;color:var(--tx);border:1px solid var(--line2);padding:8px 15px;border-radius:9px;
  cursor:pointer;font-size:13px;transition:border-color .15s,background .15s,color .15s}
button:hover{border-color:var(--ac);color:var(--ac)}
button:disabled{opacity:.5;cursor:default}
button.primary{background:var(--ac);border-color:var(--ac);color:#fff}
button.primary:hover{background:var(--ac2);color:#fff}
button.primary2{background:#fff;border-color:var(--ac);color:var(--ac)}
button.primary2:hover{background:#fdf0ef}
button.danger:hover{background:#fdf0ef;color:var(--no);border-color:var(--no)}
.chip{font-size:11.5px;border:1px solid var(--line2);border-radius:20px;padding:2px 10px;
  color:var(--dm);font-family:var(--mono)}
.chip.ok{color:var(--ok);border-color:#bfe0cf;background:#f2faf6}
.chip.warn{color:var(--warn);border-color:#ead9b9;background:#fdf8ee}
.chip.off{color:var(--no);border-color:#eec8c4;background:#fdf1f0}
.chip.dim{opacity:.7}
.pin{font-size:11.5px;border:1px solid var(--line2);border-radius:20px;padding:2px 10px;color:var(--dm)}
.pin.on{color:var(--ac);border-color:#eec8c4;background:#fdf1f0}
.dim{color:var(--dm);font-size:12.5px}
.bad{color:var(--no);font-weight:600}
#mmsg,#checkmsg{font-size:12.5px}
#mmsg.bad,#checkmsg.bad{color:var(--no)}
.gpus{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px;margin-bottom:16px}
.gpucard{background:var(--pnl2);border:1px solid var(--line);border-radius:12px;padding:12px 14px;font-size:12.5px}
.gttl{display:flex;gap:8px;align-items:center;margin-bottom:7px}
.gttl b{font-size:13px}
.bar{height:6px;background:#efe9e6;border-radius:4px;margin:6px 0;overflow:hidden}
.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--ac),var(--ac2))}
.ginfo{color:var(--dm);font-family:var(--mono);font-size:11.5px}
.gpins{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.roles{display:flex;flex-direction:column;gap:8px;margin-bottom:18px;padding:12px;
  background:var(--pnl2);border:1px solid var(--line);border-radius:12px}
.rrow{display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:12.5px}
.rrow b{color:var(--ac);font-family:var(--mono);min-width:86px}
.rrow select{background:#fff;color:var(--tx);border:1px solid var(--line2);border-radius:8px;
  padding:6px 10px;font-size:12.5px;flex:1;min-width:220px;max-width:560px}
.rrow input{background:#fff;border:1px solid var(--line2);border-radius:8px;padding:6px 10px;font-size:12.5px}
.mgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:14px}
.mcard{position:relative;background:var(--pnl2);border:1px solid var(--line);border-radius:14px;
  padding:14px 16px;display:flex;flex-direction:column;gap:9px;overflow:hidden}
.mcard::before{content:"";position:absolute;inset:0 auto 0 0;width:3px;background:var(--line2)}
.mcard.on::before{background:var(--ok)}
.mcard.load::before{background:var(--warn)}
.mcard.off::before{background:var(--no)}
.mtop{display:flex;gap:10px;align-items:center;justify-content:space-between}
.mttl .mid{font-family:var(--mono);font-size:11.5px;color:var(--dm);margin-top:3px;word-break:break-all}
input,textarea,select{font-family:inherit;color:var(--tx);background:#fff;border:1px solid var(--line2);
  border-radius:8px;padding:6px 10px;font-size:13px}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--ac);box-shadow:0 0 0 3px rgba(208,52,44,.09)}
.f-label{font-weight:650;font-size:14.5px;flex:1;min-width:0}
.frow{display:flex;gap:8px;align-items:flex-start;flex-wrap:wrap}
.flab{font-size:11.5px;color:var(--dm);text-transform:uppercase;letter-spacing:.6px;padding-top:8px;min-width:52px}
.fgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px}
.fgrid label,.wide{display:flex;flex-direction:column;gap:4px;font-size:11.5px;color:var(--dm)}
.wide input,.wide textarea{width:100%}
.chkbox{display:flex;gap:8px;align-items:center;font-size:12.5px;color:var(--tx);padding-top:4px}
.chkbox input{accent-color:var(--ac)}
.hint{font-size:11.5px;color:var(--dm);line-height:1.45}
.gbox{display:inline-flex;gap:6px;align-items:center;font-size:12.5px;color:var(--tx);
  border:1px solid var(--line2);border-radius:8px;padding:5px 10px;cursor:pointer}
.gbox input{accent-color:var(--ac);margin:0}
.chk{display:flex;gap:8px;align-items:center;font-size:12.5px;color:var(--tx)}
.chk input{accent-color:var(--ac)}
.mroles{display:flex;gap:6px;flex-wrap:wrap}
.mbtns{display:flex;gap:8px;flex-wrap:wrap;margin-top:2px}
.addrow{display:flex;gap:12px;align-items:center;margin-top:14px;flex-wrap:wrap}
.card{position:relative;background:var(--pnl);border:1px solid var(--line);border-radius:14px;
  padding:15px 16px 14px;display:flex;flex-direction:column;gap:4px;overflow:hidden}
.card::before{content:"";position:absolute;inset:0 auto 0 0;width:3px;background:var(--no)}
.card.on{border-color:#d9e8e0}
.card.on::before{background:var(--ok)}
.card.off{opacity:.7}
.card:hover{border-color:var(--line2)}
.ttl{font-weight:650;font-size:15.5px;display:flex;align-items:center;gap:8px}
.dot{width:7px;height:7px;border-radius:50%;background:var(--no);flex:none}
.card.on .dot{background:var(--ok);box-shadow:0 0 0 3px rgba(23,114,69,.12)}
.kind{font-size:11.5px;color:var(--dm);text-transform:uppercase;letter-spacing:.6px}
.model{font-family:var(--mono);font-size:12.5px;color:#5d4a48;word-break:break-all}
.note{font-size:12.5px;color:var(--dm);min-height:19px}
.metrics{display:flex;gap:14px;margin-top:8px;font-family:var(--mono)}
.metrics div{display:flex;flex-direction:column;gap:1px}
.metrics b{font-size:15px;font-weight:600}
.metrics span{font-size:10.5px;color:var(--dm);text-transform:uppercase;letter-spacing:.5px}
.go{align-self:flex-start;margin-top:10px;font-size:12px;color:var(--ac);border:1px solid var(--line2);
  padding:4px 12px;border-radius:8px;cursor:pointer;text-decoration:none;user-select:none}
.go:hover{border-color:var(--ac);background:#fdf0ef}
#benchmsg{font-size:13px;color:var(--dm)}
.hist{display:flex;flex-direction:column;gap:9px;margin-top:16px}
.hrow{display:grid;grid-template-columns:110px 1fr 96px;gap:12px;align-items:end;font-size:12px}
.hname{color:var(--dm);font-family:var(--mono);padding-bottom:3px}
.hlen{text-align:right;font-family:var(--mono)}
.bars{display:flex;gap:4px;align-items:flex-end;height:50px}
.bars i{display:block;width:12px;border-radius:3px 3px 0 0;background:linear-gradient(180deg,var(--ac),var(--ac2));opacity:.85}
.bars i.bad{background:linear-gradient(180deg,var(--no),#7a2b22)}
.gpu{display:flex;gap:14px;flex-wrap:wrap}
.gpu .gpucard{min-width:240px;flex:1}
.ws{margin-top:14px;display:flex;gap:8px;flex-wrap:wrap}
.links{display:flex;gap:12px;flex-wrap:wrap}
.link{display:flex;flex-direction:column;gap:3px;background:var(--pnl2);border:1px solid var(--line);
  border-radius:11px;padding:11px 15px;text-decoration:none;color:var(--tx);min-width:210px;flex:1;
  transition:border-color .15s,transform .15s}
.link:hover{border-color:var(--ac);transform:translateY(-1px)}
.link b{font-size:13.5px}
.link span{font-size:12px;color:var(--dm)}
footer{padding:4px 28px 30px;color:#9aa0a8;font-size:11.5px;font-family:var(--mono);letter-spacing:.3px}
@media (max-width:720px){
  main,header{padding-left:14px;padding-right:14px}
  .mgrid{grid-template-columns:1fr}
  .hrow{grid-template-columns:78px 1fr 72px}
}
"""
