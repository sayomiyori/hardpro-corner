"""Train a PEFT-compatible LoRA against the frozen Qwen3 AWQ deployment weights."""

import argparse
import json
import math
import random
from collections import defaultdict
from pathlib import Path

import torch
from safetensors import safe_open
from safetensors.torch import save_file
from torch import nn
from torch.nn import functional as F
from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer
from vllm.model_executor.layers.quantization.awq_triton import awq_dequantize_triton

from manager_examples import evaluation_examples, training_examples


TARGETS = {"q_proj", "v_proj"}
RANK = 8
ALPHA = 16


class FrozenAWQ(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, qweight, scales, qzeros):
        ctx.save_for_backward(qweight, scales, qzeros)
        return x @ awq_dequantize_triton(qweight, scales, qzeros)

    @staticmethod
    def backward(ctx, grad):
        weight = awq_dequantize_triton(*ctx.saved_tensors)
        return grad @ weight.T, None, None, None


class AWQLinear(nn.Module):
    def __init__(self, qweight, scales, qzeros, *, adapt):
        super().__init__()
        self.register_buffer("qweight", qweight)
        self.register_buffer("scales", scales)
        self.register_buffer("qzeros", qzeros)
        self.adapt = adapt
        if adapt:
            self.lora_A = nn.Parameter(torch.empty(RANK, qweight.shape[0], device="cuda"))
            self.lora_B = nn.Parameter(torch.zeros(scales.shape[1], RANK, device="cuda"))
            nn.init.kaiming_uniform_(self.lora_A, a=math.sqrt(5))

    def forward(self, x):
        result = FrozenAWQ.apply(x, self.qweight, self.scales, self.qzeros)
        if self.adapt:
            a = self.lora_A.to(x.dtype)
            b = self.lora_B.to(x.dtype)
            result = result + F.linear(F.linear(x, a), b) * (ALPHA / RANK)
        return result


def load_model(path):
    config = AutoConfig.from_pretrained(path)
    awq = config.quantization_config
    if config.model_type != "qwen3" or (awq["bits"], awq["version"], awq["group_size"]) != (4, "gemm", 128):
        raise ValueError("Expected Qwen3 AWQ GEMM with 4-bit weights and group size 128")
    del config.quantization_config
    with torch.device("meta"):
        model = AutoModelForCausalLM.from_config(config, dtype=torch.bfloat16, attn_implementation="sdpa")
    model.requires_grad_(False)
    files = json.loads((path / "model.safetensors.index.json").read_text())["weight_map"]
    by_file = defaultdict(list)
    for key, file in files.items():
        by_file[file].append(key)
    quantized = {}
    for file, keys in by_file.items():
        with safe_open(path / file, framework="pt", device=0) as source:
            for key in keys:
                name, _, field = key.rpartition(".")
                value = source.get_tensor(key)
                if field in {"qweight", "scales", "qzeros"}:
                    quantized.setdefault(name, {})[field] = value
                else:
                    module = model.get_submodule(name)
                    setattr(module, field, nn.Parameter(value, requires_grad=False))
    for name, tensors in quantized.items():
        parent, _, field = name.rpartition(".")
        module = model.get_submodule(parent)
        if getattr(module, field).bias is not None:
            raise ValueError(f"Unsupported bias in {name}")
        setattr(module, field, AWQLinear(**tensors, adapt=field in TARGETS))
    # Rotary cache is a non-persistent buffer absent from the checkpoint.
    rotary = model.model.rotary_emb
    with torch.device("cuda"):
        model.model.rotary_emb = type(rotary)(config)
    if any(t.is_meta for t in (*model.parameters(), *model.buffers())):
        raise RuntimeError("Some model tensors were not loaded")
    return model


def prompt(tokenizer, system, question):
    return tokenizer.apply_chat_template(
        [{"role": "system", "content": system}, {"role": "user", "content": question}],
        tokenize=False, add_generation_prompt=True, enable_thinking=False,
    )


def grade(raw, expected):
    try:
        output = json.loads(raw)
    except ValueError:
        return False, False
    valid = isinstance(output, dict) and output.keys() == expected.keys() and all(
        isinstance(value, str) for value in output.values()
    )
    if not valid:
        return False, False
    if expected["action"] == "chat":
        match = output["action"] == "chat" and bool(output["answer"].strip())
    else:
        match = output == expected
    return True, match


@torch.no_grad()
def evaluate(model, tokenizer):
    model.eval()
    checks = evaluation_examples()
    rows = []
    for offset in range(0, len(checks), 7):
        batch = checks[offset:offset + 7]
        encoded = tokenizer([prompt(tokenizer, s, q) for s, q, _ in batch], padding=True,
                            add_special_tokens=False, return_tensors="pt").to("cuda")
        output = model.generate(**encoded, do_sample=False, max_new_tokens=145, use_cache=True,
                                temperature=None, top_p=None, top_k=None)
        replies = tokenizer.batch_decode(output[:, encoded["input_ids"].shape[1]:], skip_special_tokens=True)
        for (_, question, expected), raw in zip(batch, replies, strict=True):
            shape, match = grade(raw, expected)
            rows.append({"question": json.loads(question)["вопрос"], "expected": expected["action"],
                         "valid_json": shape, "match": match, "answer": raw})
        print(f"Оценка: {len(rows)}/{len(checks)}", flush=True)
    return {"exact": sum(row["match"] for row in rows),
            "valid_json": sum(row["valid_json"] for row in rows), "total": len(rows), "rows": rows}


def train(model, tokenizer, steps):
    model.train()
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=1.2e-4)
    samples = training_examples()
    random.Random(711).shuffle(samples)
    for i in range(steps):
        system, question, expected = samples[i % len(samples)]
        prefix = tokenizer.encode(prompt(tokenizer, system, question), add_special_tokens=False)
        suffix = tokenizer.encode(json.dumps(expected, ensure_ascii=False) + "<|im_end|>", add_special_tokens=False)
        tokens = torch.tensor([prefix + suffix], device="cuda")
        output = model(tokens, use_cache=False, logits_to_keep=len(suffix) + 1).logits
        loss = F.cross_entropy(output[0, :-1].float(), tokens[0, -len(suffix):])
        if not torch.isfinite(loss):
            raise RuntimeError("Training loss is not finite")
        loss.backward()
        torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad], 1.0)
        optimizer.step()
        optimizer.zero_grad(set_to_none=True)
        if i == 0 or (i + 1) % 8 == 0:
            print(f"Шаг {i + 1}/{steps}, loss {loss.item():.4f}, "
                  f"пик CUDA {torch.cuda.max_memory_reserved() / (1024 ** 3):.2f} ГиБ", flush=True)
    model.gradient_checkpointing_disable()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--steps", type=int, default=len(training_examples()))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    torch.manual_seed(711)
    tokenizer = AutoTokenizer.from_pretrained(args.model, padding_side="left")
    model = load_model(args.model)
    print("Замороженная AWQ модель загружена", flush=True)
    baseline = evaluate(model, tokenizer)
    (args.output / "baseline.json").write_text(json.dumps(baseline, ensure_ascii=False), encoding="utf-8")
    train(model, tokenizer, args.steps)
    weights = {}
    for name, parameter in model.named_parameters():
        if parameter.requires_grad:
            weights[f"base_model.model.{name}.weight"] = parameter.detach().to(torch.bfloat16).cpu()
    save_file(weights, args.output / "adapter_model.safetensors")
    config = {"base_model_name_or_path": str(args.model), "bias": "none", "inference_mode": True,
              "lora_alpha": ALPHA, "lora_dropout": 0.0, "peft_type": "LORA", "r": RANK,
              "target_modules": sorted(TARGETS), "task_type": "CAUSAL_LM"}
    (args.output / "adapter_config.json").write_text(json.dumps(config), encoding="utf-8")
    candidate = evaluate(model, tokenizer)
    (args.output / "candidate.json").write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
    for name, result in (("baseline", baseline), ("adapter", candidate)):
        print(name, {key: value for key, value in result.items() if key != "rows"}, flush=True)


if __name__ == "__main__":
    main()
