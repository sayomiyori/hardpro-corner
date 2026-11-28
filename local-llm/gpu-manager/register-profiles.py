"""Register the three loopback endpoints served by the managed inference guest."""
import runpy
from pathlib import Path


def register():
    from settings import recipes, settings

    config = settings()
    registry = runpy.run_path(str(Path(config["registry_entry"]).expanduser()))["registry"]
    for name, (ctx, port, _) in recipes(config).items():
        size = ctx // 1024
        registry.upsert_profile(name, {
            "label": f"Flash Next {size}K · NCCL + PCIe P2P",
            "backend": "external", "port": port, "base": f"http://127.0.0.1:{port}",
            "alias": name, "gpus": [0, 1, 2], "model_path": config["model_path"],
            "n_ctx": size * 1024, "tensor_split": "1,1,1", "autostart": False,
            "note": "Запуск и остановка через профили GPU в панели управления.",
        })


if __name__ == "__main__":
    register()
