"""Единая точка входа на порту 8775: маршрутизация по префиксу пути.

  /hub/*    -> доска моделей   (127.0.0.1:9443)
  /agent/*  -> агент           (127.0.0.1:9444)
  остальное -> Open WebUI      (127.0.0.1:8775)

Каждый запрос обслуживается отдельным соединением с бэкендом и закрывает
клиентское соединение после ответа (Connection: close). Так браузер не может
переиспользовать keep-alive-тоннель, выбранный для другого префикса — именно
из-за этого /hub/ попадал в Open WebUI и отдавал {"detail":"Not Found"}.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys

LISTEN_HOST = os.environ.get("GW_HOST", "192.168.88.202")
LISTEN_PORT = int(os.environ.get("GW_PORT", "8775"))
HUB = ("127.0.0.1", int(os.environ.get("GW_HUB_PORT", "9443")))
AGENT = ("127.0.0.1", int(os.environ.get("GW_AGENT_PORT", "9444")))
WEBUI = ("127.0.0.1", int(os.environ.get("GW_WEBUI_PORT", "8775")))
ROUTES = (("/hub", HUB), ("/agent", AGENT))


def route(path: str) -> tuple[tuple[str, int], str]:
    for prefix, backend in ROUTES:
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
            stripped = path[len(prefix):] or "/"
            if not stripped.startswith("/"):
                stripped = "/" + stripped
            return backend, stripped
    return WEBUI, path


def rewrite(header: bytes) -> tuple[bytes, tuple[str, int]] | None:
    try:
        first, _, rest = header.partition(b"\r\n")
        method, target, version = first.decode("latin-1").split(" ", 2)
    except ValueError:
        return None
    if not target.startswith("/"):
        target = "/" + target.split("://", 1)[-1].partition("/")[2]
    path, sep, query = target.partition("?")
    backend, new_path = route(path or "/")
    if sep:
        new_path += "?" + query
    lines = [f"{method} {new_path} {version}".encode("latin-1")]

    # websocket-рукопожатие нельзя трогать: uvicorn отвечает 400, если
    # пара Connection: Upgrade / Upgrade: websocket сломана. Прокидываем
    # такие запросы как есть — дальше шлюз туннелирует байты в обе стороны.
    low = header.lower()
    if b"upgrade: websocket" in low:
        lines += [line for line in rest.split(b"\r\n") if line]
        return b"\r\n".join(lines) + b"\r\n\r\n", backend

    for line in rest.split(b"\r\n"):
        if not line:
            continue
        name = line.split(b":", 1)[0].strip().lower()
        if name in (b"connection", b"keep-alive", b"proxy-connection"):
            continue
        lines.append(line)
    lines.append(b"Connection: close")
    return b"\r\n".join(lines) + b"\r\n\r\n", backend


async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, asyncio.CancelledError, OSError):
        pass
    finally:
        try:
            dst.close()
        except OSError:
            pass


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        header = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
        writer.close()
        return

    prepared = rewrite(header)
    if prepared is None:
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    new_header, backend = prepared

    try:
        up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(*backend), timeout=10)
    except (OSError, asyncio.TimeoutError):
        body = b"backend not running"
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: " +
                     str(len(body)).encode() + b"\r\n\r\n" + body)
        await writer.drain()
        writer.close()
        return

    up_writer.write(new_header)
    await up_writer.drain()
    await asyncio.gather(pump(reader, up_writer), pump(up_reader, writer))
    try:
        writer.close()
    except OSError:
        pass


async def main() -> None:
    server = await asyncio.start_server(handle, LISTEN_HOST, LISTEN_PORT, backlog=512)
    print(f"gateway {LISTEN_HOST}:{LISTEN_PORT} -> hub {HUB[1]}, agent {AGENT[1]}, webui {WEBUI[1]}",
          flush=True)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    async with server:
        await stop.wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
