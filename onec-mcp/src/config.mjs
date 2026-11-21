/**
 * Конфигурация onec-mcp из переменных окружения.
 * Node 20+, без зависимостей.
 */

const MODES = ['readonly', 'write', 'admin']

export function trimSlash(value) {
  return String(value ?? '').trim().replace(/\/+$/, '')
}

function commaList(value) {
  return String(value ?? '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
}

function positiveInt(value, fallback) {
  const n = Number.parseInt(String(value ?? ''), 10)
  return Number.isFinite(n) && n > 0 ? n : fallback
}

function bool(value, fallback) {
  if (value === undefined || value === '') return fallback
  return /^(1|true|yes|on)$/i.test(String(value).trim())
}

/** Глоб-шаблон -> регулярка. ONEC_ALLOW="Catalog_*" / ONEC_DENY="Catalog_Пользователи". */
export function globToRegExp(glob) {
  const escaped = String(glob)
    .replace(/[.+^${}()|[\]\\]/g, '\\$&')
    .replace(/\*/g, '.*')
    .replace(/\?/g, '.')
  return new RegExp(`^${escaped}$`, 'iu')
}

export function loadConfig(env = process.env) {
  const baseUrl = trimSlash(env.ONEC_URL)
  if (!baseUrl) {
    throw new Error(
      'ONEC_URL не задан. Пример: http://1c.local/erp/odata/standard.odata',
    )
  }

  const rawMode = String(env.ONEC_MODE ?? '').trim().toLowerCase()
  let mode = MODES.includes(rawMode) ? rawMode : 'readonly'
  // ONEC_READONLY=1 жёстко режет запись, что бы ни лежало в ONEC_MODE
  if (bool(env.ONEC_READONLY, false)) mode = 'readonly'

  const allow = commaList(env.ONEC_ALLOW)
  const deny = commaList(env.ONEC_DENY)

  return {
    baseUrl,
    serviceUrl: trimSlash(env.ONEC_SERVICE_URL),
    user: String(env.ONEC_USER ?? '').trim(),
    password: String(env.ONEC_PASSWORD ?? ''),
    token: String(env.ONEC_SERVICE_TOKEN ?? '').trim(),
    mode,
    allow,
    deny,
    allowRes: allow.map(globToRegExp),
    denyRes: deny.map(globToRegExp),
    auditPath: String(env.ONEC_AUDIT ?? '').trim(),
    timeoutMs: positiveInt(env.ONEC_TIMEOUT_MS, 30_000),
    serviceTimeoutMs: positiveInt(env.ONEC_SERVICE_TIMEOUT_MS, 120_000),
    maxRows: positiveInt(env.ONEC_MAX_ROWS, 200),
    bulkMax: positiveInt(env.ONEC_BULK_MAX, 50),
    maxFilter: positiveInt(env.ONEC_MAX_FILTER, 4_000),
    metaTtlMs: positiveInt(env.ONEC_META_TTL_MS, 300_000),
  }
}
