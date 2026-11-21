#!/usr/bin/env python3
"""Мост stdio: принимает UNIX-сокет и проксирует его в stdin/stdout SSH-канала.

Живёт на hardpro-llm. Соединение инициирует Windows (там COM и 1С),
потому что sshd разрешает только локальное проброс-направление.

Сокет: ~/.state/onec-bridge.sock, права 0600, один клиент за раз.
"""
import os
import select
import socket
import sys

SOCK = os.path.expanduser("~/.state/onec-bridge.sock")
CHUNK = 65536


def main():
    os.makedirs(os.path.dirname(SOCK), exist_ok=True)
    try:
        os.unlink(SOCK)
    except FileNotFoundError:
        pass

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o600)
    srv.listen(4)
    sys.stderr.write("onec-bridge pipe: %s\n" % SOCK)
    sys.stderr.flush()

    stdin_fd = 0
    while True:
        conn, _ = srv.accept()
        try:
            while True:
                ready, _, _ = select.select([stdin_fd, conn], [], [], 120)
                if not ready:
                    continue
                if stdin_fd in ready:
                    data = os.read(stdin_fd, CHUNK)
                    if not data:
                        # SSH-канал закрыт: гасим сокет и выходим
                        conn.close()
                        srv.close()
                        return 0
                    conn.sendall(data)
                if conn in ready:
                    data = conn.recv(CHUNK)
                    if not data:
                        break
                    os.write(1, data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
