"""Model switchboard: profiles, GPU pinning, roles, on-demand llama-server.

Одна карта — одна модель. Каталог `profiles.json` держит раскладку:
у профиля есть список GPU, порт, файл модели и аргументы. Процесс
llama-server поднимается с `CUDA_VISIBLE_DEVICES`, поэтому карта видна
модели целиком и соседи в неё не лезут.

Роли (`agent`, `chat`, `media`, ...) — это ярлык «какая модель сейчас
обслуживает этот вход». Переключение роли пишется в каталог, поднимает
нужную модель и гасит чужие профили, сидящие на тех же картах или порту.

Всё состояние — файлы (`state.json`), доступ через flock: и доска, и
агент-демон меняют раскладку из разных процессов.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(os.environ.get("HP_ROOT", Path.home() / "hardpro"))
MODELS_DIR = Path(os.environ.get("HP_MODELS_DIR", ROOT / "models"))
CATALOG = Path(os.environ.get("HP_MODELS_FILE", MODELS_DIR / "profiles.json"))
STATE = Path(os.environ.get("HP_MODELS_STATE", MODELS_DIR / "state.json"))
LOCK = Path(os.environ.get("HP_MODELS_LOCK", MODELS_DIR / "state.lock"))
LOG_DIR = Path(os.environ.get("HP_MODELS_LOG", ROOT / "logs"))
LLAMA = os.environ.get("HP_LLAMA_BIN", str(ROOT / "bin" / "llama-server"))
VLLM = os.environ.get("HP_VLLM_BIN", str(Path.home() / ".local/share/hardpro/vllm/bin/vllm"))
BOOT_TIMEOUT = int(os.environ.get("HP_MODEL_BOOT_TIMEOUT", "300"))
STOP_TIMEOUT = int(os.environ.get("HP_MODEL_STOP_TIMEOUT", "20"))



class RegistryError(RuntimeError):
    """Каталог или состояние не читаются — работать вслепую нельзя."""


# ---------------------------------------------------------------- files

def _read_json(path: Path, fallback: dict) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return fallback
    except OSError as exc:
        raise RegistryError(f"не читается {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RegistryError(f"битый JSON в {path}: {exc}") from exc
    return data if isinstance(data, dict) else fallback


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise RegistryError(f"не пишется {path}: {exc}") from exc


@contextlib.contextmanager
def _lock() -> Iterator[None]:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a+", encoding="utf-8") as fh:
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except ImportError:  # windows dev machine, no flock
            pass
        try:
            yield
        finally:
            try:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except ImportError:
                pass


# ---------------------------------------------------------------- catalog

def catalog() -> dict:
    data = _read_json(CATALOG, {"profiles": {}, "roles": {}, "defaults": {}, "policy": {}})
    if "profiles" not in data or not isinstance(data["profiles"], dict):
        raise RegistryError(f"в {CATALOG} нет секции profiles")
    data.setdefault("roles", {})
    data.setdefault("defaults", {})
    data.setdefault("policy", {})
    return data


def _profile(cat: dict, model: str) -> dict[str, Any]:
    prof = cat["profiles"].get(model)
    if not isinstance(prof, dict):
        raise RegistryError(f"модель не зарегистрирована: {model}")
    return prof


def _merged(cat: dict, model: str) -> dict[str, Any]:
    """Профиль поверх defaults: что не задано локально — берётся общее."""
    prof = dict(cat["defaults"])
    prof.update(_profile(cat, model))
    prof["id"] = model
    prof.setdefault("backend", "llamacpp")
    prof.setdefault("gpus", [])
    prof.setdefault("args", [])
    prof.setdefault("label", model)
    return prof


def profiles() -> dict[str, dict]:
    cat = catalog()
    return {mid: _merged(cat, mid) for mid in cat["profiles"]}


def roles() -> dict[str, str]:
    return dict(catalog()["roles"])


def model_for_role(role: str) -> str | None:
    return catalog()["roles"].get(role)


def set_role(role: str, model: str) -> dict:
    with _lock():
        cat = catalog()
        _profile(cat, model)
        cat["roles"][role] = model
        _write_json(CATALOG, cat)
    return {"role": role, "model": model}


def upsert_profile(model: str, spec: dict) -> dict:
    if not model or not re.fullmatch(r"[A-Za-z0-9._:/-]+", model):
        raise RegistryError("имя модели: буквы, цифры, . _ : / -")
    clean = {k: v for k, v in spec.items() if k not in ("id",)}
    with _lock():
        cat = catalog()
        current = cat["profiles"].get(model) or {}
        current.update(clean)
        cat["profiles"][model] = current
        _write_json(CATALOG, cat)
    return _merged(catalog(), model)


def drop_profile(model: str) -> dict:
    with _lock():
        cat = catalog()
        if model not in cat["profiles"]:
            raise RegistryError(f"нет профиля {model}")
        for role, bound in list(cat["roles"].items()):
            if bound == model:
                raise RegistryError(f"модель занята ролью {role} — сначала переключи роль")
        cat["profiles"].pop(model)
        _write_json(CATALOG, cat)
    return {"dropped": model}


# ---------------------------------------------------------------- paths

def _expand(value: str | None) -> str | None:
    if not value:
        return None
    return str(Path(value).expanduser())


def base_for(model: str) -> str:
    """URL, куда слать запросы за моделью. Внешние бэкенды — как объявлено."""
    prof = _merged(catalog(), model)
    if prof["backend"] == "external":
        base = prof.get("base")
        if not base:
            raise RegistryError(f"у профиля {model} нет base")
        return str(base).rstrip("/")
    return f"http://127.0.0.1:{int(prof['port'])}"


def token_for(model: str) -> str:
    prof = _merged(catalog(), model)
    env_name = prof.get("token_env")
    return os.environ.get(env_name, "") if env_name else ""


# ---------------------------------------------------------------- state

def _state() -> dict:
    data = _read_json(STATE, {"running": {}})
    running = data.get("running")
    if not isinstance(running, dict):
        data["running"] = {}
    return data


def _alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _prune(data: dict) -> dict:
    """Выкинуть записи о процессах, которых уже нет."""
    live = {mid: rec for mid, rec in data["running"].items()
            if isinstance(rec, dict) and _alive(int(rec.get("pid") or 0))}
    data["running"] = live
    return data


def running() -> dict[str, dict]:
    with _lock():
        data = _prune(_state())
        _write_json(STATE, data)
    return dict(data["running"])


# ---------------------------------------------------------------- gpu

def gpus() -> list[dict]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.used,memory.total,"
                             "utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return []
    rows: list[dict] = []
    for line in out.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) != 6:
            continue
        try:
            rows.append({"index": int(p[0]), "name": p[1], "used_mib": int(p[2]),
                         "total_mib": int(p[3]), "util": int(p[4]), "temp": int(p[5])})
        except ValueError:
            continue
    return rows


def _vram_check(prof: dict) -> dict:
    """Хватает ли памяти на картах профиля. Не знаем — пропускаем (fail open по данным)."""
    need = int(prof.get("vram_mib") or 0)
    if not need:
        return {"ok": True, "checked": False}
    cards = {g["index"]: g for g in gpus()}
    if not cards:
        return {"ok": True, "checked": False}
    reserve = int(catalog()["policy"].get("reserve_mib") or 0)
    for gid in prof["gpus"]:
        card = cards.get(int(gid))
        if card is None:
            return {"ok": False, "checked": True,
                    "error": f"карты {gid} нет: {sorted(cards)}"}
        free = card["total_mib"] - card["used_mib"]
        if free + reserve < need:
            return {"ok": False, "checked": True,
                    "error": f"GPU {gid}: свободно {free} MiB, нужно {need} MiB"}
    return {"ok": True, "checked": True}


# ---------------------------------------------------------------- process

def _http_ok(url: str, token: str = "", timeout: float = 3.0) -> bool:
    """Стучимся без зависимостей: на сервере тот же вызов, но через stdlib."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= int(getattr(resp, "status", 0) or 0) < 400
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _health(port: int, timeout: float = 3.0) -> bool:
    return _http_ok(f"http://127.0.0.1:{int(port)}/health", timeout=timeout)


def online(model: str) -> bool:
    prof = _merged(catalog(), model)
    if prof["backend"] == "external":
        base = str(prof.get("base") or "")
        if not base:
            return False
        return _http_ok(f"{base.rstrip('/')}/v1/models", token_for(model))
    return _health(int(prof["port"]))


def _cmd(prof: dict) -> list[str]:
    if prof["backend"] == "vllm":
        return _cmd_vllm(prof)
    if prof["backend"] != "llamacpp":
        raise RegistryError("внешний бэкенд не запускается локально")
    path = _expand(prof.get("model_path"))
    if not path:
        raise RegistryError(f"у {prof['id']} нет model_path")
    if not Path(path).is_file():
        raise RegistryError(f"нет файла модели: {path}")
    cmd = [LLAMA]
    if prof.get("subcmd"):  # llama.cpp нового формата: `llama serve ...`
        cmd.append(str(prof["subcmd"]))
    cmd += ["--model", path, "--host", "127.0.0.1", "--port", str(int(prof["port"]))]
    ngl = prof.get("n_gpu_layers")
    if ngl is not None:  # null = отдать раскладку автоподбору --fit
        cmd += ["-ngl", str(int(ngl))]
    if prof.get("n_ctx"):
        cmd += ["-c", str(int(prof["n_ctx"]))]
    if prof.get("mmproj"):
        mm = _expand(prof["mmproj"])
        if mm and Path(mm).is_file():
            cmd += ["--mmproj", mm]
    split = prof.get("tensor_split")
    if split:
        cmd += ["--tensor-split", str(split)]
    if prof.get("kv_offload") is False:  # KV остаётся в RAM — режим больших контекстов
        cmd += ["--no-kv-offload"]
    extra = prof.get("args") or []
    if isinstance(extra, list):
        cmd += [str(a) for a in extra]
    return cmd


def _cmd_vllm(prof: dict) -> list[str]:
    """vLLM: каталог/веса, --served-model-name, контекст, утилизация VRAM, ключ."""
    path = _expand(prof.get("model_path"))
    if not path:
        raise RegistryError(f"у {prof['id']} нет model_path")
    p = Path(path)
    if not (p.is_dir() or p.is_file()):
        raise RegistryError(f"нет модели: {path}")
    if not (Path(VLLM).is_file() or shutil.which(VLLM)):
        raise RegistryError(f"нет vllm: {VLLM} (HP_VLLM_BIN)")
    cmd = [VLLM, "serve", path]
    alias = prof.get("alias") or prof.get("served_name") or prof["id"]
    cmd += ["--served-model-name", str(alias),
            "--host", "127.0.0.1", "--port", str(int(prof["port"]))]
    if prof.get("n_ctx"):
        cmd += ["--max-model-len", str(int(prof["n_ctx"]))]
    if prof.get("gpu_util"):
        cmd += ["--gpu-memory-utilization", str(float(prof["gpu_util"]))]
    token = token_for(prof["id"])
    if token:
        cmd += ["--api-key", token]
    extra = prof.get("args") or []
    if isinstance(extra, list):
        cmd += [str(a) for a in extra]
    return cmd


def _logfile(model: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-") or "model"
    return LOG_DIR / f"model-{slug}.log"


def launch(model: str, evict: bool | None = None, wait: bool = True) -> dict:
    """Поднять модель. Возвращает {ok, base, port, gpus, pid, evicted}."""
    cat = catalog()
    prof = _merged(cat, model)
    if prof["backend"] == "external":
        return {"ok": online(model), "model": model, "external": True,
                "base": base_for(model), "error": "" if online(model) else "внешний бэкенд молчит"}

    if evict is None:
        evict = bool(cat["policy"].get("evict", True))

    port = int(prof["port"])
    rec = running().get(model)
    if rec and _health(port):
        return {"ok": True, "model": model, "base": base_for(model), "port": port,
                "gpus": prof["gpus"], "pid": rec.get("pid"), "evicted": [], "cached": True}

    binary = VLLM if prof["backend"] == "vllm" else LLAMA
    bin_name = "vllm" if prof["backend"] == "vllm" else "llama-server"
    bin_env = "HP_VLLM_BIN" if prof["backend"] == "vllm" else "HP_LLAMA_BIN"
    if not Path(binary).is_file() and not shutil.which(binary):
        raise RegistryError(f"нет {bin_name}: {binary} ({bin_env})")

    # сначала гасим соседей по карте/порту, потом меряем свободную память:
    # иначе переключение упирается в VRAM, которую освободит вытеснение
    evicted: list[str] = []
    if evict:
        want_gpus = {int(g) for g in prof["gpus"]}
        for other, orec in running().items():
            if other == model:
                continue
            try:
                oprof = _merged(cat, other)
            except RegistryError:
                continue
            clash = int(oprof.get("port") or -1) == port or \
                want_gpus & {int(g) for g in oprof.get("gpus") or []}
            if clash:
                stop(other)
                evicted.append(other)
        if evicted:
            time.sleep(2.0)

    memory = _vram_check(prof)
    if not memory["ok"]:
        raise RegistryError(memory["error"] +
                            (f" (вытеснено: {', '.join(evicted)})" if evicted else ""))

    cmd = _cmd(prof)
    env = os.environ.copy()
    gpu_ids = prof["gpus"]
    if gpu_ids:
        env["CUDA_VISIBLE_DEVICES"] = ",".join(str(int(g)) for g in gpu_ids)
    env.setdefault("LLAMA_ARG_NO_WEBUI", "1")
    log = _logfile(model)
    with open(log, "ab") as fh:
        fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {model} gpus={gpu_ids} "
                 f"port={port}\n{chr(10).join(cmd)}\n".encode())
        fh.flush()
        try:
            proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=env,
                                    start_new_session=True, cwd=str(ROOT))
        except OSError as exc:
            raise RegistryError(f"не запустился {binary}: {exc}") from exc

    with _lock():
        data = _prune(_state())
        data["running"][model] = {"pid": proc.pid, "port": port,
                                  "gpus": [int(g) for g in gpu_ids],
                                  "started": time.time(), "log": str(log)}
        _write_json(STATE, data)

    if not wait:
        return {"ok": True, "model": model, "base": base_for(model), "port": port,
                "gpus": gpu_ids, "pid": proc.pid, "evicted": evicted, "loading": True}

    deadline = time.time() + int(cat["policy"].get("boot_timeout") or BOOT_TIMEOUT)
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = _tail(log)
            _forget(model)
            raise RegistryError(f"{model} упал при загрузке (код {proc.returncode}). "
                                f"Лог {log}:\n{tail}")
        if _health(port):
            return {"ok": True, "model": model, "base": base_for(model), "port": port,
                    "gpus": gpu_ids, "pid": proc.pid, "evicted": evicted}
        time.sleep(1.5)
    raise RegistryError(f"{model}: не поднялся за {deadline - time.time():.0f} с, лог {log}")


def _forget(model: str) -> None:
    with _lock():
        data = _prune(_state())
        data["running"].pop(model, None)
        _write_json(STATE, data)


def _tail(log: Path, limit: int = 1200) -> str:
    try:
        body = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return body[-limit:]


def stop(model: str) -> dict:
    rec = running().get(model)
    if not rec:
        _forget(model)
        return {"ok": True, "model": model, "stopped": False}
    pid = int(rec.get("pid") or 0)
    if _alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        deadline = time.time() + STOP_TIMEOUT
        while time.time() < deadline and _alive(pid):
            time.sleep(0.3)
        if _alive(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
    _forget(model)
    return {"ok": True, "model": model, "stopped": True, "pid": pid}


def restart(model: str) -> dict:
    """Погасить и поднять заново: применить новые поля профиля к живому процессу."""
    stop(model)
    return launch(model)


def switch(role: str, model: str, start: bool = True, detach: bool = False) -> dict:
    """Привязать роль к модели и (по умолчанию) поднять её на её картах."""
    if not role or not model:
        raise RegistryError("нужны role и model")
    cat = catalog()
    prof = _merged(cat, model)
    # проверку VRAM делает launch — уже после вытеснения соседей по картам
    set_role(role, model)
    out: dict[str, Any] = {"ok": True, "role": role, "model": model, "base": base_for(model),
                           "gpus": prof["gpus"], "port": prof.get("port")}
    if not start:
        return out
    if detach:
        out["started"] = "detached"
        return out
    out.update(launch(model))
    out["role"] = role
    return out


def status() -> dict:
    cat = catalog()
    live = running()
    cards = []
    for mid in cat["profiles"]:
        try:
            prof = _merged(cat, mid)
        except RegistryError:
            continue
        rec = live.get(mid) or {}
        backend = prof["backend"]
        path = _expand(prof.get("model_path"))
        path_ok = bool(path) and (Path(path).is_file() or
                                  (backend == "vllm" and Path(path).is_dir()))
        card = {
            "id": mid,
            "label": prof["label"],
            "backend": backend,
            "gpus": prof.get("gpus") or [],
            "port": prof.get("port"),
            "model_path": path,
            "mmproj": _expand(prof.get("mmproj")),
            "model_exists": path_ok,
            "n_ctx": prof.get("n_ctx"),
            "n_gpu_layers": prof.get("n_gpu_layers"),
            "vram_mib": prof.get("vram_mib"),
            "tensor_split": prof.get("tensor_split"),
            "kv_offload": prof.get("kv_offload", True),
            "args": prof.get("args") or [],
            "autostart": bool(prof.get("autostart")),
            "subcmd": prof.get("subcmd"),
            "token_env": prof.get("token_env"),
            "gpu_util": prof.get("gpu_util"),
            "alias": prof.get("alias") or prof.get("served_name"),
            "note": prof.get("note", ""),
            "roles": [r for r, m in cat["roles"].items() if m == mid],
            "running": bool(rec),
            "pid": rec.get("pid"),
            "base": base_for(mid) if (prof.get("base") or prof.get("port")) else None,
            "online": online(mid),
            "startable": backend in ("llamacpp", "vllm"),
        }
        cards.append(card)
    return {"roles": cat["roles"], "models": cards, "gpu": gpus(),
            "policy": cat["policy"], "llama": LLAMA,
            "llama_found": bool(Path(LLAMA).is_file() or shutil.which(LLAMA))}


def switch_check() -> dict:
    """Проверка каталога для CLI/диагностики: файлы, порты, карты."""
    cat = catalog()
    problems: list[str] = []
    notes: list[str] = []
    seen_ports: dict[int, str] = {}
    seen_gpu: dict[int, str] = {}
    for mid in cat["profiles"]:
        prof = _merged(cat, mid)
        if prof["backend"] == "external":
            continue
        port = int(prof.get("port") or 0)
        if port in seen_ports:
            problems.append(f"порт {port}: {mid} и {seen_ports[port]}")
        seen_ports[port] = mid
        for g in prof["gpus"]:
            if int(g) in seen_gpu:
                notes.append(f"GPU {g} делят {mid} и {seen_gpu[int(g)]} — будут вытеснять друг друга")
            seen_gpu[int(g)] = mid
        path = _expand(prof.get("model_path"))
        p = Path(path) if path else None
        exists = p is not None and (p.is_file() or (prof["backend"] == "vllm" and p.is_dir()))
        if not exists:
            problems.append(f"{mid}: нет модели {path}")
    for role, model in cat["roles"].items():
        if model not in cat["profiles"]:
            problems.append(f"роль {role} смотрит в несуществующую модель {model}")
    return {"ok": not problems, "problems": problems, "notes": notes}
