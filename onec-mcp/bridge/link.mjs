/**
 * link.mjs — Windows-сторона моста.
 * Держит две вещи склеенными: MCP-сервер на PowerShell (COM к 1С)
 * и SSH-канал на hardpro-llm, где python раздаёт UNIX-сокет.
 *
 *   powershell onec-mcp.ps1  <->  ssh  <->  python onec-bridge-pipe.py
 *
 * Падение любой стороны лечится перезапуском пары через 5 секунд.
 */
import { spawn } from 'node:child_process'

const ROOT = 'E:\\Work\\Tools\\onec-bridge'
const REMOTE = 'hardpro-llm'
const REMOTE_CMD = 'python3 ~/.local/bin/onec-bridge-pipe.py'

const BRIDGE = ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', `${ROOT}\\onec-mcp.ps1`]
const SSH = [
  '-T',
  '-o', 'BatchMode=yes',
  '-o', 'ServerAliveInterval=20',
  '-o', 'ServerAliveCountMax=3',
  '-o', 'ConnectTimeout=15',
  REMOTE,
  REMOTE_CMD,
]

let stopping = false
let backoffMs = 5000

function relay(from, to) {
  from.on('data', (chunk) => {
    if (!to.stdin.writable) return
    if (!to.stdin.write(chunk)) from.pause()
  })
  to.stdin.on('drain', () => from.resume())
  from.on('end', () => to.stdin.end())
  from.on('error', () => {})
}

function startOnce() {
  const startedAt = Date.now()
  const ps = spawn('powershell.exe', BRIDGE, { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true })
  const ssh = spawn('ssh.exe', SSH, { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true })

  relay(ps.stdout, ssh)
  relay(ssh.stdout, ps)

  ps.stderr.pipe(process.stderr)
  ssh.stderr.pipe(process.stderr)

  const finish = (reason) => {
    if (stopping) return
    // Минутная стабильность сбрасывает паузу, иначе пауза растёт до минуты:
    // нечего плодить COM-сессии, пока сервер лежит
    const stable = Date.now() - startedAt > 60_000
    const delay = stable ? 5000 : backoffMs
    backoffMs = stable ? 5000 : Math.min(backoffMs * 2, 60_000)
    console.error(`[link] ${reason}, перезапуск через ${Math.round(delay / 1000)} с`)
    try { ps.kill() } catch { }
    try { ssh.kill() } catch { }
    setTimeout(startOnce, delay)
  }

  ps.on('exit', (code) => finish(`powershell вышел с ${code}`))
  ssh.on('exit', (code) => finish(`ssh вышел с ${code}`))
  ps.on('error', (err) => finish(`powershell не стартовал: ${err.message}`))
  ssh.on('error', (err) => finish(`ssh не стартовал: ${err.message}`))
}

process.on('SIGINT', () => { stopping = true; process.exit(0) })
process.on('SIGTERM', () => { stopping = true; process.exit(0) })

console.error('[link] onec bridge link up')
startOnce()
