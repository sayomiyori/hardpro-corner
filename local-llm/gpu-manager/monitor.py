"""Report live GPU telemetry from the system that currently owns the cards."""
import csv
import io
import json
import math
import os
from pathlib import Path
import subprocess

from settings import VM_DIR

def number(value):
    try:
        n = float(value)
        return int(n) if n.is_integer() else n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None

vm_dir = VM_DIR
guest_gpu = ((vm_dir / "qemu.pid").exists() and (vm_dir / "boot-id").is_file()
             and (vm_dir / "boot-id").read_text().strip() == Path("/proc/sys/kernel/random/boot_id").read_text().strip())

def query(kind, fields):
    command = ["nvidia-smi", "--query-" + kind + "=" + fields, "--format=csv,noheader,nounits"]
    if guest_gpu:
        command.insert(0, str(vm_dir / "guest"))
    result = subprocess.run(
        command, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=4,
        encoding="utf-8", errors="replace",
    )
    return csv.reader(io.StringIO(result.stdout), skipinitialspace=True)

fields = ("index,uuid,pci.bus_id,name,memory.used,memory.total,temperature.gpu,"
          "power.draw,power.limit,utilization.gpu,fan.speed")
metrics = ("used_mib", "total_mib", "temperature_c", "power_w", "limit_w", "util_pct", "fan_pct")
by_uuid = {}
for index, uuid, pci, name, used, total, temp, draw, limit, util, fan, low, high in query("gpu", fields + ",power.min_limit,power.max_limit"):
    gpu = dict(index=number(index), uuid=uuid, pci_bus_id=pci, name=name, processes=[], min_w=number(low), max_w=number(high))
    if guest_gpu:
        gpu["name"] += " (VM)"
    gpu.update(zip(metrics, map(number, (used, total, temp, draw, limit, util, fan))))
    by_uuid[uuid] = gpu

for uuid, name, pid, used in query("compute-apps", "gpu_uuid,process_name,pid,used_gpu_memory"):
    if uuid in by_uuid:
        by_uuid[uuid]["processes"].append(
            dict(name=os.path.basename(name), pid=number(pid), used_mib=number(used))
        )

print(json.dumps(list(by_uuid.values()), ensure_ascii=False))
