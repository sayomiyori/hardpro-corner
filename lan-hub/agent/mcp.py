"""Minimal MCP stdio client: JSON-RPC 2.0 over a child process pipe.

Sync on purpose; the agent calls it through asyncio.to_thread.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-06-18"


class McpError(RuntimeError):
    pass


class McpServer:
    def __init__(self, name: str, command: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None, cwd: str | None = None,
                 url: str | None = None) -> None:
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.cwd = cwd
        self.url = url
        self.proc: subprocess.Popen[str] | None = None
        self._q: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._id = 0
        self._tools: list[dict[str, Any]] = []
        self._err: str = ""

    # ---------- transport ----------
    def start(self) -> None:
        if self.url:
            raise McpError("http MCP transport не подключён: используйте stdio")
        environ = dict(os.environ)
        environ.update({k: str(v) for k, v in self.env.items()})
        try:
            self.proc = subprocess.Popen(
                [self.command, *self.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1, env=environ,
                cwd=self.cwd or str(Path.home()),
            )
        except OSError as exc:
            raise McpError(f"{self.name}: {exc}") from exc
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._stderr, args=(self.proc,), daemon=True).start()

    def _reader(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._q.put(json.loads(line))
            except ValueError:
                continue
        self._q.put(None)

    def _stderr(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            self._err = (self._err + line)[-2000:]

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    # ---------- rpc ----------
    def _send(self, payload: dict[str, Any]) -> None:
        if not self.proc or self.proc.poll() is not None or self.proc.stdin is None:
            raise McpError(f"{self.name}: процесс не запущен {self._err[-300:]}")
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _await(self, rid: int, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise McpError(f"{self.name}: таймаут ответа")
            try:
                msg = self._q.get(timeout=left)
            except queue.Empty:
                raise McpError(f"{self.name}: таймаут ответа") from None
            if msg is None:
                raise McpError(f"{self.name}: процесс закрыл stdout {self._err[-300:]}")
            if msg.get("id") == rid:
                if "error" in msg:
                    raise McpError(f"{self.name}: {msg['error'].get('message', 'rpc error')}")
                return msg.get("result") or {}

    def request(self, method: str, params: dict[str, Any] | None = None,
                timeout: float = 60) -> dict[str, Any]:
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        return self._await(rid, timeout)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # ---------- lifecycle ----------
    def initialize(self) -> list[dict[str, Any]]:
        if not self.proc:
            self.start()
        self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}, "roots": {"listChanged": False}},
            "clientInfo": {"name": "hardpro-agent", "version": "1.0"},
        }, timeout=30)
        self.notify("notifications/initialized")
        listed = self.request("tools/list", {}, timeout=30)
        self._tools = listed.get("tools", []) or []
        return self._tools

    def call(self, tool: str, arguments: dict[str, Any], timeout: float = 120) -> dict[str, Any]:
        res = self.request("tools/call", {"name": tool, "arguments": arguments}, timeout=timeout)
        parts: list[str] = []
        for item in res.get("content", []) or []:
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif item.get("type") == "resource":
                parts.append(json.dumps(item.get("resource", {}), ensure_ascii=False)[:4000])
        text = "\n".join(p for p in parts if p).strip()
        return {"ok": not res.get("isError"), "content": text[:20000] or "(пусто)"}


def load_servers(path: Path) -> list[McpServer]:
    if not path.is_file():
        return []
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return []
    servers: list[McpServer] = []
    for name, spec in (cfg.get("servers") or {}).items():
        if spec.get("disabled"):
            continue
        command = spec.get("command")
        if not command:
            continue
        servers.append(McpServer(
            name=name, command=command, args=list(spec.get("args") or []),
            env=dict(spec.get("env") or {}), cwd=spec.get("cwd"),
            url=spec.get("url"),
        ))
    return servers
