#!/usr/bin/env node
/**
 * Дымовой тест без MCP-клиента: связь, метаданные, выборочное чтение.
 *   node smoke.mjs                       — статус + счётчик объектов
 *   node smoke.mjs Catalog_Номенклатура  — плюс 3 строки и карточка полей
 */
import { loadConfig } from './src/config.mjs'
import { OneCClient } from './src/odata.mjs'

let cfg
try {
  cfg = loadConfig()
} catch (err) {
  console.error('конфиг:', err.message)
  process.exit(2)
}

const client = new OneCClient(cfg)
console.log(`URL:    ${cfg.baseUrl}`)
console.log(`режим:  ${cfg.mode}`)
console.log(`сервис: ${cfg.serviceUrl || '(нет)'}`)

let meta
try {
  meta = await client.metadata({ refresh: true })
} catch (err) {
  console.error(`\nOData недоступен: ${err.toText?.() ?? err.message}`)
  process.exit(1)
}

const names = [...meta.sets.keys()]
console.log(`объектов опубликовано: ${names.length}`)
console.log(`примеры: ${names.slice(0, 8).join(', ') || '(пусто)'}`)
if (!names.length) {
  console.error('OData отвечает, но объектов нет: в 1С не включена публикация объектов')
  process.exit(1)
}

const target = process.argv[2]
if (target) {
  if (!names.includes(target)) {
    const near = names.filter((n) => n.toLowerCase().includes(target.toLowerCase())).slice(0, 8)
    console.error(`\n${target} нет в OData. Похожие: ${near.join(', ') || '(нет)'}`)
    process.exit(1)
  }
  const fields = client.entityFields(meta, target)
  console.log(`\n${target}: полей ${fields.length}`)
  const { rows, truncated } = await client.query({ entity: target, top: 3 })
  console.log(JSON.stringify({ truncated: truncated || undefined, rows }, null, 2).slice(0, 4_000))
}

process.exit(0)
