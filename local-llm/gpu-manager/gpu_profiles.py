"""Restricted GPU profile runner for a three-GPU Linux host (ordinary user)."""

import contextlib
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import runpy
import select
import signal
import subprocess
import sys
import urllib.error
import time
import urllib.request

from settings import VM_DIR, recipes, settings

CONFIG = settings()
ROOT = Path.home() / ".local/state/gpu-manager"
UNITS = {"manager": CONFIG["manager_unit"], "comfy": CONFIG["comfy_unit"]}
CODER = CONFIG["coder_model"]
NCCL_PORTS = {name: port for name, (_, port, _) in recipes(CONFIG).items()}
NCCL = set(NCCL_PORTS)
FLASH = NCCL | {name.removesuffix("-NCCL") for name in NCCL}
VM_UNIT = "gpu-manager-vm.service"
LABEL = {"manager": "Менеджер", "coder": "Кодер", "comfy": "ComfyUI"}
SLOTS = [slot.upper() for slot in CONFIG["pci_slots"]]
if len(SLOTS) != 3 or len(set(SLOTS)) != 3 or not all(
    re.fullmatch(r"[0-9A-F]{2}:00[.]0", slot) for slot in SLOTS
):
    raise ValueError("Expected three distinct GPU slots in domain 0000")
STOP_ORDER = ["comfy", "coder", "manager", *sorted(FLASH)]
START_ORDER = ["manager", "coder", "comfy", *sorted(FLASH)]


class Refused(Exception):
    pass


def cmd(args, timeout=8):
    return subprocess.check_output(
        args, timeout=timeout, stderr=subprocess.DEVNULL, text=True
    ).strip()


def read_json(path, default=None):
    return json.loads(path.read_bytes()) if path.exists() else default


def call(port, path, *, metrics=False):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/{path}", timeout=4) as r:
            if r.status != 200:
                raise Refused("Сервис ещё не готов.")
            data = r.read(8_000_001)
            if len(data) > 8_000_000:
                raise Refused("Слишком большой ответ при проверке очереди.")
            return data.decode() if metrics else json.loads(data)
    except (OSError, ValueError) as e:
        raise Refused(
            f"Нет подтверждённого ответа от порта {port}, путь /{path}."
        ) from e


def gpu_query(kind, fields):
    out = cmd(
        [
            "nvidia-smi",
            "--query-" + kind + "=" + fields,
            "--format=csv,noheader,nounits",
        ]
    )
    return list(csv.reader(io.StringIO(out), skipinitialspace=True))


def desired(layout, flash):
    if (
        not isinstance(layout, list)
        or len(layout) != 3
        or not all(isinstance(s, str) for s in layout)
    ):
        raise Refused("Нужны три назначения GPU.")
    if layout == ["flash"] * 3 and flash in FLASH:
        return {flash}
    if layout == ["video"] * 3:
        return set()
    result = set()
    for item, expected in zip(layout, ["manager", "coder", "comfy"]):
        if item == expected:
            result.add(item)
        elif item not in ("idle", "reserve", "bench"):
            raise Refused(
                "Нет проверенного рецепта для этого размещения. Менеджер закреплён за GPU 0, кодер за GPU 1, изображения за GPU 2. Flash и резерв под видео занимают три карты."
            )
    return result


def registry():
    return runpy.run_path(str(Path(CONFIG["registry_entry"]).expanduser()))["registry"]


def model_id(name):
    return CODER if name == "coder" else name


def guest(action, *args, timeout=20):
    return cmd([str(VM_DIR / "control"), action, *map(str, args)], timeout)


def exe_matches(proc, binary):
    exe = (proc / "exe").resolve(strict=True)
    binary = Path(binary).resolve(strict=True)
    # llama's app-image launcher execs its neighbouring .llama-app/llama binary.
    return exe == binary or exe == (binary.parent / ".llama-app" / "llama").resolve()


class Host:
    def __init__(self):
        self.reg = registry()
        self.profiles = self.reg.profiles()

    def profile(self, name):
        p = self.profiles.get(model_id(name), {})
        expected = [1] if name == "coder" else [0, 1, 2]
        backend = "external" if name in NCCL else "llamacpp"
        if p.get("backend") != backend or p.get("gpus") != expected:
            raise Refused("Изменилась привязка профиля " + name)
        if name in NCCL:
            port = NCCL_PORTS[name]
            if p.get("port") != port or p.get("base") != f"http://127.0.0.1:{port}" or p.get("alias") != name:
                raise Refused("Изменился адрес или идентификатор гостевой модели.")
        return p

    def validate_pid(self, name, pid):
        p = self.profile(name)
        proc = Path("/proc") / str(pid)
        args = (proc / "cmdline").read_bytes().decode().split(chr(0))

        def option(key):
            return args[args.index(key) + 1] if key in args else ""

        if (
            proc.stat().st_uid != os.getuid()
            or not exe_matches(proc, self.reg.LLAMA)
            or Path(option("--model")).resolve()
            != Path(os.path.expandvars(p["model_path"])).expanduser().resolve()
            or option("--port") != str(p["port"])
        ):
            raise Refused(
                "PID не соответствует зарегистрированным весам и порту: " + name
            )

    def observe(self):
        if ((VM_DIR / "qemu.pid").exists()
                and (VM_DIR / "boot-id").read_text().strip() == Path("/proc/sys/kernel/random/boot_id").read_text().strip()):
            snapshot = json.loads(guest("status"))
            if set(snapshot["cards"]) != set((VM_DIR / "device-uuids").read_text().split()):
                raise Refused("Карты гостевой системы отличаются от переданных хостом.")
            return snapshot
        cards = {}
        for idx, uid, bdf, used, total in gpu_query(
            "gpu", "index,uuid,pci.bus_id,memory.used,memory.total"
        ):
            i = int(idx)
            domain, slot = bdf.upper().split(":", 1)
            if domain not in ("00000000", "0000") or not 0 <= i < 3 or slot != SLOTS[i]:
                raise Refused("Изменился порядок физических GPU.")
            cards[uid] = {"i": i, "used": int(used), "total": int(total)}
        if len(cards) != 3:
            raise Refused("Сервер должен видеть три карты.")
        managed = {}
        for name, unit in UNITS.items():
            props = dict(
                line.split("=", 1)
                for line in cmd(
                    [
                        "systemctl",
                        "--user",
                        "show",
                        unit,
                        "-p",
                        "ActiveState",
                        "-p",
                        "MainPID",
                        "-p",
                        "LoadState",
                    ]
                ).splitlines()
            )
            if props["LoadState"] != "loaded" or props["ActiveState"] not in (
                "active",
                "inactive",
                "failed",
            ):
                raise Refused("Служба в переходном состоянии: " + unit)
            if props["ActiveState"] == "active":
                managed[name] = int(props["MainPID"])
        records = read_json(Path(self.reg.STATE), {}).get("running", {})
        for name in ["coder", *sorted(FLASH)]:
            pid = records.get(model_id(name), {}).get("pid")
            if type(pid) == int and Path(f"/proc/{pid}").exists():
                self.validate_pid(name, pid)
                managed[name] = pid
        owners = {name: set() for name in managed}
        system = []
        for uid, pid_str, exe, used in gpu_query(
            "compute-apps", "gpu_uuid,pid,process_name,used_gpu_memory"
        ):
            i = cards[uid]["i"]
            pid = int(pid_str)
            if (
                exe == CONFIG.get("display_executable")
                and CONFIG.get("display_unit")
                in Path(f"/proc/{pid}/cgroup").read_text().strip().split("/")
            ):
                system.append([i, pid, Path(exe).name, int(used)])
                continue
            cgroup = Path(f"/proc/{pid}/cgroup").read_text()
            owner = next(
                (
                    k
                    for k in managed
                    if managed[k] == pid
                    or (k in UNITS and UNITS[k] in cgroup.strip().split("/"))
                ),
                None,
            )
            if owner is None:
                raise Refused(
                    f"GPU {i} занята сторонним процессом {Path(exe).name} (PID {pid})."
                )
            owners[owner].add(i)
        return {
            "managed": managed,
            "owners": {k: sorted(v) for k, v in owners.items()},
            "cards": cards,
            "system": system,
        }

    def idle(self, name):
        if name == "comfy":
            q = call(CONFIG["comfy_port"], "queue")
            if any(
                not isinstance(q.get(k), list) or q[k]
                for k in ("queue_running", "queue_pending")
            ):
                raise Refused("В ComfyUI есть выполняющиеся или ожидающие задания.")
            return
        if name == "manager":
            text = call(CONFIG["manager_port"], "metrics", metrics=True)
            keys = ("vllm:num_requests_running", "vllm:num_requests_waiting")
        else:
            port = self.profile(name)["port"]
            slots = call(port, "slots")
            if (
                not isinstance(slots, list)
                or not slots
                or any(s.get("is_processing") is not False for s in slots)
            ):
                raise Refused("Есть активный запрос: " + name)
            try:
                text = call(port, "metrics", metrics=True)
            except Refused as exc:
                if not isinstance(
                    exc.__cause__, urllib.error.HTTPError
                ) or exc.__cause__.code not in (404, 501):
                    raise
                # With the optional metrics exporter disabled, a deferred HTTP request
                # still owns a connection to the listener. Require idle slots AND no clients.
                exc.__cause__.close()
                for _ in range(5):
                    states = [
                        line.split()[0]
                        for line in cmd(
                            ["ss", "-Htan", f"( sport = :{port} )"]
                        ).splitlines()
                    ]
                    if "LISTEN" in states and all(
                        v in ("LISTEN", "TIME-WAIT") for v in states
                    ):
                        return
                    time.sleep(0.15)
                raise Refused("У модели остаётся клиентское соединение: " + name)
            keys = ("llamacpp:requests_processing", "llamacpp:requests_deferred")
        for key in keys:
            values = re.findall(
                r"^" + re.escape(key) + r"(?:\{[^\n]*?\})?\s+([^\s]+)", text, re.M
            )
            if not values or any(float(v) != 0 for v in values):
                raise Refused(
                    "Очередь занята или её состояние неизвестно: " + name + " / " + key
                )

    def stop(self, name, pid):
        self.idle(name)
        if name in NCCL:
            guest("stop", name, pid, timeout=100)
        elif name in UNITS:
            if cmd(
                ["systemctl", "--user", "show", "-p", "MainPID", "--value", UNITS[name]]
            ) != str(pid):
                raise Refused("Процесс службы уже сменился.")
            cmd(["systemctl", "--user", "stop", UNITS[name]], 90)
        else:
            fd = os.pidfd_open(pid)
            try:
                self.validate_pid(name, pid)
                signal.pidfd_send_signal(fd, signal.SIGTERM)
                poll = select.poll()
                poll.register(fd, select.POLLIN)
                if not poll.poll(30000):
                    raise Refused("Процесс не завершился: " + name)
            finally:
                os.close(fd)

    def ready(self, name):
        if name == "comfy":
            call(CONFIG["comfy_port"], "system_stats")
        else:
            call(
                CONFIG["manager_port"] if name == "manager" else self.profile(name)["port"],
                "health",
                metrics=True,
            )
        s = self.observe()
        expected = {"manager": [0], "coder": [1], "comfy": [2]}.get(name, [0, 1, 2])
        if s["owners"].get(name) != expected:
            raise Refused("Пока не подтверждено размещение на GPU: " + name)

    def start(self, name):
        if name in NCCL:
            cmd(["systemctl", "--user", "start", VM_UNIT], 640)
            guest("start", name, timeout=460)
        elif name in UNITS:
            cmd(["systemctl", "--user", "start", UNITS[name]], 120)
        else:
            p = self.profile(name)
            cards = {v["i"]: v for v in self.observe()["cards"].values()}
            for i in p["gpus"]:
                if cards[i]["total"] - cards[i]["used"] < int(p["vram_mib"]) + 512:
                    raise Refused(f"Недостаточно свободной памяти на GPU {i}.")
            # Keep inference in its own cgroup after the switching job finishes.
            cmd(
                [
                    "systemd-run",
                    "--user",
                    "--collect",
                    "--quiet",
                    "--property=Type=exec",
                    "--unit=gpu-manager-inference-" + name,
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--serve",
                    name,
                ]
            )
        due = time.monotonic() + 240
        while True:
            try:
                self.ready(name)
                return
            except (Refused, OSError):
                if time.monotonic() >= due:
                    raise Refused("Истекло ожидание готовности: " + name)
                time.sleep(2)


def ordered(items, order):
    return [n for n in order if n in items]


def signature(s, target):
    canonical = [
        s["managed"],
        s["owners"],
        sorted((v["i"], k) for k, v in s["cards"].items()),
        sorted(target),
    ]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


def check(host, target):
    s = host.observe()
    current = set(s["managed"])
    # Check known endpoints even for a no-op: report whether the destination is healthy.
    for n in target & current:
        host.ready(n)
    for n in target - current:
        if n not in UNITS:
            p = host.profile(n)
            if not Path(os.path.expandvars(p["model_path"])).expanduser().is_file():
                raise Refused("Не найдены веса: " + n)
    if target != current:
        for n in current:
            host.idle(n)
    return s


def transition(host, target, record, expected=None):
    s = check(host, target)
    if expected and fingerprint(host, s, target) != expected:
        raise Refused("План устарел перед первой остановкой.")
    for n in ordered(set(s["managed"]) - target, STOP_ORDER):
        record("Остановка " + LABEL.get(n, n))
        host.stop(n, s["managed"][n])
        # Reinspect before another change, including external owners.
        s = host.observe()
        if n in s["managed"]:
            raise Refused("Процесс продолжает работать: " + n)
    if s.get("vm") and not target & NCCL:
        record("Возврат трёх GPU хосту")
        cmd(["systemctl", "--user", "stop", VM_UNIT], 240)
        s = host.observe()
    for n in ordered(target - set(s["managed"]), START_ORDER):
        record("Запуск " + LABEL.get(n, n))
        host.start(n)
    final = host.observe()
    if set(final["managed"]) != target:
        raise Refused("Итоговый набор процессов отличается от выбранного.")
    for n in target:
        host.ready(n)
    occupied = {i for gpus in final["owners"].values() for i in gpus}
    for card in final["cards"].values():
        if card["i"] not in occupied and card["used"] > 1024:
            raise Refused("На освобождённой карте осталось больше 1 ГиБ видеопамяти.")
    return final


def read_job():
    path = ROOT / "job.json"
    return json.loads(path.read_bytes()) if path.is_file() else {"status": "idle"}


def save(path, data):
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


@contextlib.contextmanager
def exclusive():
    import fcntl

    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (ROOT / "lock").open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise Refused("Уже выполняется операция управления GPU.") from e
        yield


def safe_error(exc):
    if isinstance(exc, Refused):
        return str(exc)
    if isinstance(exc, subprocess.TimeoutExpired):
        return "Истекло время ожидания службы."
    return "Проверка службы завершилась ошибкой (" + type(exc).__name__ + ")."


def fingerprint(host, s, target):
    current = set(s["managed"])
    roles = host.reg.catalog()["roles"]
    return hashlib.sha256(
        (
            signature(s, target)
            + json.dumps(roles, sort_keys=True)
            + json.dumps(
                {k: host.profile(k) for k in current | target if k not in UNITS},
                sort_keys=True,
            )
        ).encode()
    ).hexdigest()


def plan(host, target):
    s = check(host, target)
    current = set(s["managed"])
    digest = fingerprint(host, s, target)
    return {
        "signature": digest,
        "stop": [LABEL.get(n, n) for n in STOP_ORDER if n in current - target],
        "start": [LABEL.get(n, n) for n in START_ORDER if n in target - current],
        "system": s["system"],
        "before": sorted(current),
    }


def job_note(job, text):
    job["updated_at"] = time.time()
    job["step"] = text
    job.setdefault("steps", []).append(text)
    save(ROOT / "job.json", job)


def activate_routes(host, target, job):
    # Persist exact bindings before putting Flash behind any working role.
    backup_path = ROOT / "roles.json"
    roles = host.reg.catalog()["roles"]
    original = json.loads(backup_path.read_bytes()) if backup_path.exists() else {}
    flash = next(iter(target & FLASH), None)
    if flash:
        for role, model in roles.items():
            if model in (CODER, CONFIG["manager_model"]) or model in FLASH:
                original.setdefault(role, model)
        save(backup_path, original)
    for role, old in original.items():
        now = host.reg.catalog()["roles"].get(role)
        if now != old and now not in FLASH:
            continue
        wanted = flash or old
        if now != wanted:
            host.reg.set_role(role, wanted)
    if not flash and backup_path.exists():
        backup_path.unlink()


def execute(jobid):
    # The transient unit can start just before the dispatcher releases its lock.
    import fcntl

    with (ROOT / "lock").open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        job = read_job()
        if job.get("id") != jobid or job["status"] != "queued":
            return
        run_job(job)


def run_job(job):
    target = set(job["target"])
    try:
        host = Host()
        first = plan(host, target)
        if first["signature"] != job["signature"]:
            raise Refused(
                "Состав процессов или конфигурация изменились после подтверждения."
            )
    except Exception as exc:
        job["status"] = "blocked"
        job_note(job, safe_error(exc))
        return
    job["status"] = "running"
    job["before"] = first["before"]
    job_note(job, "Состояние и очереди проверены")
    touched = False

    def record_step(message):
        nonlocal touched
        job_note(job, message)
        touched = True

    try:
        final = transition(host, target, record_step, expected=first["signature"])
        touched = True
        activate_routes(host, target, job)
        job["memory"] = {str(c["i"]): c["used"] for c in final["cards"].values()}
        job["status"] = "done"
        job_note(job, "Профиль применён и проверен")
    except Exception as exc:
        job["error"] = safe_error(exc)
        if not touched:
            job["status"] = "blocked"
            job_note(job, "Переключение отменено до изменения конфигурации")
            return
        job_note(
            job, "Не удалось применить профиль. Восстанавливаем исходное состояние."
        )
        try:
            transition(host, set(first["before"]), lambda t: job_note(job, t))
            activate_routes(host, set(first["before"]), job)
            job["status"] = "rolled_back"
            job_note(job, "Исходное размещение восстановлено и проверено")
        except Exception as recovery:
            job["status"] = "needs_recovery"
            job_note(job, "Требуется восстановление: " + safe_error(recovery))


def visible_job():
    job = read_job()
    if job["status"] in ("queued", "running"):
        state = subprocess.run(
            [
                "systemctl",
                "--user",
                "is-active",
                "gpu-manager-job-" + job["id"] + ".service",
            ],
            capture_output=True,
            text=True,
            timeout=4,
        )
        if state.stdout.strip() not in ("active", "activating", "deactivating"):
            job = dict(
                job,
                status="interrupted",
                step="Исполнитель остановлен. Проверьте размещение и примените рабочий профиль.",
            )
    return job


def handle(request):
    mode = request["mode"]
    if mode == "status":
        return visible_job()
    layout = request.get("layout")
    target = desired(layout, request.get("flash"))
    if mode == "plan":
        data = plan(Host(), target)
        return {k: data[k] for k in ("signature", "start", "stop", "system")}
    if mode != "apply":
        raise Refused("Неизвестная операция.")
    from uuid import UUID

    jobid = str(UUID(request["id"]))
    with exclusive():
        old = read_job()
        archived = ROOT / ("job-" + jobid + ".json")
        if archived.is_file():
            return json.loads(archived.read_bytes())
        if old.get("id") == jobid:
            return visible_job()
        if visible_job()["status"] in ("queued", "running"):
            raise Refused("Идёт смена профиля.")
        proposal = plan(Host(), target)
        if proposal["signature"] != request.get("signature"):
            raise Refused(
                "План устарел. Проверьте размещение и повторите переключение."
            )
        job = {
            "id": jobid,
            "created_at": time.time(),
            "layout": layout,
            "target": sorted(target),
            "signature": proposal["signature"],
            "before": proposal["before"],
            "status": "queued",
            "step": "Запуск исполнителя",
            "steps": [],
        }
        if old.get("id"):
            save(ROOT / ("job-" + old["id"] + ".json"), visible_job())
        save(ROOT / "job.json", job)
        try:
            cmd(
                [
                    "systemd-run",
                    "--user",
                    "--quiet",
                    "--collect",
                    "--unit=gpu-manager-job-" + jobid,
                    sys.executable,
                    str(Path(__file__).resolve()),
                    jobid,
                ]
            )
        except Exception:
            job["status"] = "blocked"
            job_note(job, "Не удалось запустить пользовательскую службу.")
            raise
        return job


def serve(name):
    if name not in FLASH and name != "coder":
        raise Refused("Неизвестная модель.")
    reg = registry()
    result = reg.launch(model_id(name), evict=False, wait=False)
    if not result.get("loading") or not result.get("pid"):
        raise Refused("Модель не запустилась.")
    fd = os.pidfd_open(result["pid"])
    try:
        poll = select.poll()
        poll.register(fd, select.POLLIN)
        poll.poll()
    finally:
        os.close(fd)


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--serve":
    serve(sys.argv[2])
elif __name__ == "__main__" and sys.argv[1:] == ["request"]:
    try:
        print(json.dumps(handle(json.load(sys.stdin)), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"error": safe_error(exc)}, ensure_ascii=False))
        sys.exit(1)
elif __name__ == "__main__" and len(sys.argv) == 2:
    execute(sys.argv[1])
