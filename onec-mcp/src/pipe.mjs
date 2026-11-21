#!/usr/bin/env node
/**
 * pipe.mjs — агентская сторона моста к 1С.
 * Соединяет свой stdio с UNIX-сокетом, который держит SSH-канал с Windows.
 * Протокол MCP говорит PowerShell на той стороне; здесь только байты.
 *
 * Сокет: ONEC_PIPE_SOCK (по умолчанию ~/.state/onec-bridge.sock)
 */
import net from 'node:net'
import os from 'node:os'
import path from 'node:path'

const sockPath = process.env.ONEC_PIPE_SOCK
  ? process.env.ONEC_PIPE_SOCK.replace(/^~/, os.homedir())
  : path.join(os.homedir(), '.state', 'onec-bridge.sock')

const RETRY_MS = 1000
const RETRY_LIMIT = 60

function fatal(message) {
  process.stderr.write(`[onec-pipe] ${message}\n`)
  process.exit(1)
}

function connect(attempt = 0) {
  const sock = net.connect({ path: sockPath, allowHalfOpen: true })

  sock.on('connect', () => {
    process.stdin.pipe(sock, { end: true })
    sock.pipe(process.stdout, { end: false })
  })

  sock.on('error', (err) => {
    if (attempt >= RETRY_LIMIT) {
      fatal(`нет моста ${sockPath}: ${err.code || err.message}. Windows-сторона не подключена.`)
    }
    setTimeout(() => connect(attempt + 1), RETRY_MS)
  })

  sock.on('end', () => {
    // Канал закрылся на той стороне — агент увидит конец потока, мост перезапустится
    process.exit(0)
  })

  return sock
}

connect()

process.on('SIGTERM', () => process.exit(0))
process.on('SIGINT', () => process.exit(0))
