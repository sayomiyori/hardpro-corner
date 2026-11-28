"""Run inside the dedicated three-GPU guest. Inputs select a fixed profile."""
import csv
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.request import Request, urlopen

from settings import recipes, settings

CONFIG = settings()
PROFILES = recipes(CONFIG)


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def unit(name):
    return f"gpu-manager-flash-{PROFILES[name][0]}.service"


def main_pid(name):
    return int(output("systemctl", "--user", "show", "-p", "MainPID", "--value", unit(name)) or 0)


def query(kind, fields):
    data = output("nvidia-smi", f"--query-{kind}={fields}", "--format=csv,noheader,nounits")
    return csv.reader(io.StringIO(data), skipinitialspace=True)


def snapshot():
    cards = {uid: {"i": int(i), "used": int(used), "total": int(total)}
             for i, uid, used, total in query("gpu", "index,uuid,memory.used,memory.total")}
    if sorted(c["i"] for c in cards.values()) != [0, 1, 2]:
        raise RuntimeError("Expected three CUDA devices")
    managed = {name: pid for name in PROFILES if (pid := main_pid(name))}
    owners = {name: [] for name in managed}
    for uid, pid, resident in query("compute-apps", "gpu_uuid,pid,used_gpu_memory"):
        name = next((n for n, p in managed.items() if p == int(pid)), None)
        if name is None:
            raise RuntimeError("Unexpected CUDA process in inference VM")
        if int(resident) > 12000:
            owners[name].append(cards[uid]["i"])
    return dict(cards=cards, managed=managed, owners={n: sorted(v) for n,v in owners.items()},
                system=[], vm=True)


def start(name):
    ctx, port, ubatch = PROFILES[name]
    if snapshot()["managed"]:
        raise RuntimeError("Another guest profile is active")
    model = Path(CONFIG["guest_model_path"]).expanduser().resolve(strict=True)
    binary = Path(CONFIG["guest_binary"]).expanduser().resolve(strict=True)
    env = dict(CUDA_DEVICE_ORDER="PCI_BUS_ID", CUDA_VISIBLE_DEVICES="0,1,2",
               LD_LIBRARY_PATH=str(binary.parent), GGML_CUDA_P2P="1",
               GGML_CUDA_ALLREDUCE="nccl", NCCL_P2P_LEVEL="SYS", NCCL_DEBUG="INFO")
    subprocess.run(["systemd-run", "--user", "--collect", "--quiet", "--property=Type=exec",
                    "--unit", unit(name), *(f"--setenv={k}={v}" for k,v in env.items()),
                    "stdbuf", "-oL", str(binary), "-m", str(model), "--alias", name,
                    "--host", "0.0.0.0", "--port", str(port), "--parallel", "1",
                    "--ctx-size", str(ctx), "--gpu-layers", "all", "--tensor-split", "1,1,1",
                    "--split-mode", "tensor", "--fit", "off", "--flash-attn", "on",
                    "--cache-type-k", "f16", "--cache-type-v", "f16", "--batch-size", "2048",
                    "--ubatch-size", str(ubatch), "--threads", "16", "--threads-batch", "32",
                    "--override-tensor", "^per_layer_token_embd[.]weight=CPU",
                    "--offline", "--jinja", "--no-webui", "--metrics", "-lv", "4"], check=True)
    try:
        base = f"http://127.0.0.1:{port}"
        until = time.monotonic() + 360
        while main_pid(name) and time.monotonic() < until:
            try:
                with urlopen(base + "/v1/models", timeout=3) as response:
                    if name in [m["id"] for m in json.load(response)["data"]]:
                        break
            except OSError:
                pass
            time.sleep(2)
        else:
            raise RuntimeError("Guest model did not become ready")
        with urlopen(Request(base + "/completion", json.dumps({
            "prompt": "Two plus two equals", "n_predict": 4, "temperature": 0,
            "ignore_eos": True, "cache_prompt": False
        }).encode(), {"Content-Type": "application/json"}), timeout=45) as response:
            if json.load(response)["timings"]["predicted_n"] != 4:
                raise RuntimeError("Incomplete readiness response")
        if snapshot()["owners"].get(name) != [0, 1, 2]:
            raise RuntimeError("Model is not using all three GPUs")
        invocation = output("systemctl", "--user", "show", "-p", "InvocationID", "--value", unit(name))
        log = output("journalctl", "--user", "--no-pager", "-o", "cat",
                     "_SYSTEMD_INVOCATION_ID=" + invocation)
        edges = set(re.findall(r"(\d)\[\d\] -> (\d)\[\d\] via P2P/direct pointer", log))
        if ({a for a, b in edges if a != b} != {"0", "1", "2"}
                or {b for a, b in edges if a != b} != {"0", "1", "2"}):
            raise RuntimeError("Direct NCCL channels on three GPUs were not confirmed")
    except BaseException:
        subprocess.run(["systemctl", "--user", "stop", unit(name)], check=False)
        raise


if __name__ == "__main__":
    action, *args = sys.argv[1:]
    if action == "status" and not args:
        print(json.dumps(snapshot()))
    elif action == "start" and len(args) == 1 and args[0] in PROFILES:
        start(args[0])
    elif action == "stop" and len(args) == 2 and args[0] in PROFILES:
        if int(args[1]) <= 0 or main_pid(args[0]) != int(args[1]):
            raise RuntimeError("Guest model process changed")
        subprocess.run(["systemctl", "--user", "stop", unit(args[0])], check=True)
    else:
        sys.exit("Unknown profile action")
