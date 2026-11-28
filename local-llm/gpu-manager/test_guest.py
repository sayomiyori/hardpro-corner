import io
import json
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import settings


class GuestTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).parent
        self.config = json.loads((self.base / "settings.example.json").read_bytes())

    def launch(self, name, log):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "model"
            fixture.touch()
            config = dict(self.config, guest_binary=str(fixture), guest_model_path=str(fixture))
            with patch.object(settings, "settings", return_value=config):
                start = runpy.run_path(str(self.base / "guest-model.py"))["start"]
            commands = Mock()
            replies = [io.BytesIO(json.dumps(value).encode()) for value in (
                {"data": [{"id": name}]}, {"timings": {"predicted_n": 4}}
            )]
            with patch.dict(start.__globals__, {
                "subprocess": commands, "urlopen": Mock(side_effect=replies),
                "main_pid": Mock(return_value=42), "output": Mock(side_effect=["invocation", log]),
                "snapshot": Mock(side_effect=[{"managed": {}}, {"owners": {name: [0, 1, 2]}}])
            }):
                start(name)
        return commands.run.call_args_list[0].args[0]

    def test_three_guest_command_lines(self):
        ring = "\n".join(f"{i}[{i}] -> {(i+1)%3}[{(i+1)%3}] via P2P/direct pointer" for i in range(3))
        for size, microbatch in ((16, 2048), (64, 1536), (128, 256)):
            with self.subTest(size=size):
                args = self.launch(f"Qwen3.8-Flash-Next-{size}K-3GPU-NCCL", ring)
                for flag, value in (("--ctx-size", size*1024), ("--ubatch-size", microbatch),
                                    ("--split-mode", "tensor"), ("--tensor-split", "1,1,1")):
                    self.assertEqual(args[args.index(flag)+1], str(value))
                self.assertIn("--setenv=GGML_CUDA_ALLREDUCE=nccl", args)
                self.assertIn("--setenv=GGML_CUDA_P2P=1", args)

    def test_missing_direct_channels_refuses_readiness(self):
        with self.assertRaisesRegex(RuntimeError, "Direct NCCL"):
            self.launch("Qwen3.8-Flash-Next-16K-3GPU-NCCL", "0[0] -> 1[1] via SHM")

    def test_register_three_profiles_in_registry(self):
        register = runpy.run_path(str(self.base / "register-profiles.py"))["register"]
        registry = Mock()
        with patch.object(settings, "settings", return_value=self.config), patch.object(
            runpy, "run_path", return_value={"registry": registry}
        ):
            register()
        specs = [call.args for call in registry.upsert_profile.call_args_list]
        self.assertEqual([p["n_ctx"] for _, p in specs], [16384, 65536, 131072])
        for name, spec in specs:
            self.assertEqual(name, spec["alias"])
            self.assertEqual(spec["backend"], "external")
            self.assertEqual(spec["gpus"], [0, 1, 2])
            self.assertFalse(spec["autostart"])
