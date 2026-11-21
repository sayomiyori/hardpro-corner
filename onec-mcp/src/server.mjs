#!/usr/bin/env node
/**
 * onec-mcp — MCP-сервер (stdio, JSON-RPC 2.0) для 1С:Предприятие 8.
 * Node 20+, один процесс на клиента, без зависимостей.
 *
 * Диагностика идёт в stderr: stdout занят протоколом.
 */
import { createInterface } from 'node:readline'

import { Auditor } from './audit.mjs'
import { loadConfig } from './config.mjs'
import { OneCClient, OneCError } from './odata.mjs'
import { TOOLS, createHandlers } from './tools.mjs'

const PROTOCOL_VERSION = '2025-06-18'
const SERVER_INFO = { name: 'onec-mcp', version: '1.0.0' }

let cfg
try {
  cfg = loadConfig()
} catch (err) {
  process.stderr.write(`[onec-mcp] ${err.message}\n`)
  process.exit(1)
}

const client = new OneCClient(cfg)
const auditor = new Auditor(cfg.auditPath)
const handlers = createHandlers({ client, cfg })

const INSTRUCTIONS = [
  'Доступ к базе 1С через стандартный интерфейс OData.',
  `База: ${cfg.baseUrl}`,
  `Режим: ${cfg.mode}${cfg.mode === 'readonly' ? ' (только чтение)' : ''}.`,
  'Порядок работы: onec_status -> onec_metadata -> onec_describe -> onec_query.',
  'Поля не угадывай: сначала onec_describe, потом пиши.',
  'Ссылки передаются как {"Ref_Key":"<guid>"}, даты — ISO-строкой 2024-01-31T00:00:00,',
  'в $filter дата пишется datetime\'2024-01-31T00:00:00\'.',
  'Проведение документов — только onec_post_document (OData не меняет Posted).',
  'Перед массовыми правками: dryRun=true и проверка одной записи.',
].join(' ')

function send(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`)
}

function reply(id, result) {
  send({ jsonrpc: '2.0', id, result })
}

function replyError(id, code, message, data) {
  const error = { code, message }
  if (data !== undefined) error.data = data
  send({ jsonrpc: '2.0', id, error })
}

function errorText(err) {
  if (err instanceof OneCError) return err.toText()
  if (err instanceof Error) return err.message
  return String(err)
}

function redactArgs(args) {
  if (!args || typeof args !== 'object') return args
  const copy = { ...args }
  for (const key of ['password', 'token', 'secret', 'pass']) {
    if (key in copy) copy[key] = '***'
  }
  const text = JSON.stringify(copy)
  return text.length > 4_000 ? `${text.slice(0, 4_000)}…` : copy
}

async function callTool(id, params) {
  const started = Date.now()
  const name = String(params?.name ?? '')
  const args = params?.arguments && typeof params.arguments === 'object' ? params.arguments : {}
  const handler = handlers[name]
  if (!handler) {
    replyError(id, -32602, `нет инструмента "${name}"`)
    return
  }
  try {
    const text = await handler(args)
    await auditor.write({ tool: name, ok: true, args: redactArgs(args), ms: Date.now() - started })
    reply(id, { content: [{ type: 'text', text }], isError: false })
  } catch (err) {
    const text = errorText(err)
    await auditor.write({
      tool: name,
      ok: false,
      error: text.slice(0, 800),
      args: redactArgs(args),
      ms: Date.now() - started,
    })
    reply(id, { content: [{ type: 'text', text }], isError: true })
  }
}

async function dispatch(msg) {
  const hasId = Object.prototype.hasOwnProperty.call(msg, 'id')
  const id = hasId ? msg.id : null
  const method = String(msg.method ?? '')

  try {
    switch (method) {
      case 'initialize':
        reply(id, {
          protocolVersion: msg.params?.protocolVersion ?? PROTOCOL_VERSION,
          capabilities: { tools: { listChanged: false } },
          serverInfo: SERVER_INFO,
          instructions: INSTRUCTIONS,
        })
        return
      case 'notifications/initialized':
      case 'notifications/cancelled':
      case 'notifications/roots/list_changed':
        return
      case 'ping':
        reply(id, {})
        return
      case 'tools/list':
        reply(id, { tools: TOOLS })
        return
      case 'tools/call':
        await callTool(id, msg.params)
        return
      case 'resources/list':
        reply(id, { resources: [] })
        return
      case 'prompts/list':
        reply(id, { prompts: [] })
        return
      default:
        if (hasId) replyError(id, -32601, `метод не поддержан: ${method}`)
    }
  } catch (err) {
    if (hasId) replyError(id, -32603, errorText(err))
  }
}

const rl = createInterface({ input: process.stdin, crlfDelay: Infinity })
let queue = Promise.resolve()

rl.on('line', (line) => {
  const text = line.trim()
  if (!text) return
  let msg
  try {
    msg = JSON.parse(text)
  } catch {
    replyError(null, -32700, 'не разобрал JSON')
    return
  }
  if (!msg || typeof msg !== 'object' || msg.jsonrpc !== '2.0') {
    replyError(msg?.id ?? null, -32600, 'не JSON-RPC 2.0')
    return
  }
  // Строго последовательно: 1С плохо переносит параллельные записи в один объект
  queue = queue.then(() => dispatch(msg)).catch((err) => {
    process.stderr.write(`[onec-mcp] ${errorText(err)}\n`)
  })
})

rl.on('close', () => {
  // Дать последним вызовам дойти до сети и аудита
  queue.finally(() => process.exit(0))
})

process.on('SIGTERM', () => process.exit(0))
process.on('SIGINT', () => process.exit(0))
process.stderr.write(`[onec-mcp] ${SERVER_INFO.version} -> ${cfg.baseUrl} (${cfg.mode})\n`)
