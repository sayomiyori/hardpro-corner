"""Local deployment settings and public inference recipes."""
import json
from pathlib import Path

CONFIG_FILE = Path.home() / ".config/gpu-manager/settings.json"
VM_DIR = Path.home() / ".local/share/gpu-manager/vm"
SIZES = (16, 64, 128)
UBATCH = (2048, 1536, 256)


def settings():
    return json.loads(CONFIG_FILE.read_bytes())


def recipes(config):
    ports = config["nccl_ports"]
    if len(ports) != 3 or len(set(ports)) != 3 or any(
        type(p) != int or not 1024 <= p <= 65535 for p in ports
    ):
        raise ValueError("Expected three distinct unprivileged ports")
    return {f"Qwen3.8-Flash-Next-{size}K-3GPU-NCCL": (size * 1024, port, ubatch)
            for size, port, ubatch in zip(SIZES, ports, UBATCH)}
