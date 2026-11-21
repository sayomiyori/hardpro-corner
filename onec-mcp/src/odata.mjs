/**
 * Клиент стандартного интерфейса OData 1С:Предприятие 8.
 * База: ONEC_URL = http://host/base/odata/standard.odata
 * Node 20+, fetch из stdlib, без зависимостей.
 */

export const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

export class OneCError extends Error {
  constructor(message, info = {}) {
    super(message)
    this.name = 'OneCError'
    this.status = info.status ?? 0
    this.body = info.body ?? ''
    this.url = info.url ?? ''
    this.hint = info.hint ?? ''
  }

  toText() {
    const lines = [this.message]
    if (this.status) lines.push(`HTTP ${this.status}`)
    if (this.hint) lines.push(this.hint)
    if (this.body) lines.push(`тело ответа: ${this.body}`)
    if (this.url) lines.push(`url: ${this.url}`)
    return lines.join('\n')
  }
}

function parseAttrs(tag) {
  const out = {}
  const re = /([A-Za-z_:][\w:.-]*)="([^"]*)"/g
  let m
  while ((m = re.exec(tag))) out[m[1].replace(/^[^:]*:/, '')] = m[2]
  return out
}

function safeJson(text) {
  try {
    return JSON.parse(text)
  } catch {
    return null
  }
}

function hintForStatus(status) {
  if (status === 401) return 'не прошла авторизация: проверь ONEC_USER/ONEC_PASSWORD и права пользователя 1С'
  if (status === 403) return 'пользователю 1С запрещён объект или интерфейс OData'
  if (status === 404) return 'проверь имя объекта и ONEC_URL (публикация OData на веб-сервере)'
  if (status === 400) return '1С отклонила запрос: обычно неверный $filter, тип поля или обязательное поле не заполнено'
  if (status === 405) return 'метод запрещён: для этого объекта OData открыт только на чтение'
  if (status === 500) return 'внутренняя ошибка 1С: смотри журнал регистрации на сервере'
  return ''
}

function nearest(name, keys) {
  const needle = String(name).toLowerCase()
  const scored = keys.map((key) => {
    const hay = key.toLowerCase()
    let score = 0
    if (hay === needle) score = 100
    else if (hay.includes(needle) || needle.includes(hay)) score = 50
    else {
      const head = needle.slice(0, 6)
      if (head.length >= 6 && hay.includes(head)) score = 20
    }
    return { key, score }
  })
  return scored
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, 5)
    .map((item) => item.key)
}

/** Разбор $metadata (EDMX) в сеты и поля. Регекс достаточен: схема плоская. */
export function parseMetadata(xml) {
  const sets = new Map()
  for (const m of String(xml).matchAll(/<EntitySet\b[^>]*>/g)) {
    const attrs = parseAttrs(m[0])
    if (!attrs.Name) continue
    sets.set(attrs.Name, { entityType: String(attrs.EntityType ?? '').split('.').pop() })
  }

  const types = new Map()
  for (const m of String(xml).matchAll(/<EntityType\b[^>]*>([\s\S]*?)<\/EntityType>/g)) {
    const openTag = m[0].slice(0, m[0].indexOf('>') + 1)
    const name = String(parseAttrs(openTag).Name ?? '').split('.').pop()
    if (!name) continue
    const fields = []
    for (const p of m[1].matchAll(/<Property\b[^>]*>/g)) {
      const attrs = parseAttrs(p[0])
      if (!attrs.Name) continue
      fields.push({
        name: attrs.Name,
        type: String(attrs.Type ?? '').replace(/^Edm\./, ''),
        nullable: attrs.Nullable !== 'false',
      })
    }
    types.set(name, fields)
  }

  return { sets, types }
}

export class OneCClient {
  constructor(cfg) {
    this.cfg = cfg
    this._meta = null
    this._metaAt = 0
  }

  headers(extra = {}) {
    const h = { Accept: 'application/json', ...extra }
    if (this.cfg.user) {
      const raw = `${this.cfg.user}:${this.cfg.password}`
      h.Authorization = `Basic ${Buffer.from(raw, 'utf8').toString('base64')}`
    }
    if (this.cfg.token) h['X-Hardpro-Token'] = this.cfg.token
    return h
  }

  buildUrl(path, params) {
    const base = `${this.cfg.baseUrl}/${String(path).replace(/^\/+/, '')}`
    if (!params) return base
    const qs = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined || value === null || value === '') continue
      qs.set(key, String(value))
    }
    const query = qs.toString().replace(/%24/g, '$')
    return query ? `${base}?${query}` : base
  }

  /** Один HTTP-обмен. Единственное место, где ходим в сеть. */
  async send(url, { method = 'GET', body, timeoutMs, accept = 'application/json' } = {}) {
    const hasBody = body !== undefined
    const headers = this.headers(
      hasBody
        ? { Accept: accept, 'Content-Type': 'application/json; charset=utf-8' }
        : { Accept: accept },
    )
    const limit = timeoutMs ?? this.cfg.timeoutMs
    const ac = new AbortController()
    const timer = setTimeout(() => ac.abort(), limit)
    try {
      const res = await fetch(url, {
        method,
        headers,
        body: hasBody ? JSON.stringify(body) : undefined,
        signal: ac.signal,
        redirect: 'follow',
      })
      const text = await res.text()
      if (!res.ok) {
        throw new OneCError(`1С ответила ${res.status} ${res.statusText}`, {
          status: res.status,
          body: text.slice(0, 1_500),
          url,
          hint: hintForStatus(res.status),
        })
      }
      return { status: res.status, text, json: res.status === 204 || !text ? null : safeJson(text) }
    } catch (err) {
      if (err instanceof OneCError) throw err
      if (err?.name === 'AbortError') {
        throw new OneCError(`таймаут ${limit} мс`, { url, hint: 'запрос в 1С не уложился: подними ONEC_TIMEOUT_MS или сузь $filter' })
      }
      const cause = err?.cause?.code || err?.cause?.message || err?.message || String(err)
      throw new OneCError(`сеть недоступна: ${cause}`, { url, hint: 'проверь ONEC_URL, DNS, VPN и публикацию OData' })
    } finally {
      clearTimeout(timer)
    }
  }

  async request(path, opts = {}) {
    return this.send(this.buildUrl(path, opts.params), opts)
  }

  async metadata({ refresh = false } = {}) {
    const fresh = this._meta && Date.now() - this._metaAt < this.cfg.metaTtlMs
    if (fresh && !refresh) return this._meta
    const { text } = await this.request('$metadata', { accept: 'application/xml' })
    this._meta = parseMetadata(text)
    this._metaAt = Date.now()
    return this._meta
  }

  /** Проверка имени объекта: синтаксис, allow/deny, публикация в OData. */
  async ensureEntity(name) {
    const entity = String(name ?? '').trim()
    if (!/^[\p{L}_][\p{L}\p{N}_]*$/u.test(entity)) {
      throw new OneCError(`недопустимое имя объекта: "${name}"`, {
        hint: 'имя как в метаданных: Catalog_Номенклатура, Document_РеализацияТоваровУслуг',
      })
    }
    if (this.cfg.denyRes.some((re) => re.test(entity))) {
      throw new OneCError(`объект ${entity} запрещён в ONEC_DENY`)
    }
    if (this.cfg.allowRes.length && !this.cfg.allowRes.some((re) => re.test(entity))) {
      throw new OneCError(`объект ${entity} не входит в ONEC_ALLOW`, {
        hint: `разрешено: ${this.cfg.allow.join(', ')}`,
      })
    }

    // Метаданные — мягкая проверка: если 1С не отдаёт $metadata, работаем без неё
    let meta
    try {
      meta = await this.metadata()
    } catch (err) {
      if (err instanceof OneCError && (err.status === 401 || err.status === 403)) throw err
      return entity
    }
    if (!meta.sets.has(entity)) {
      const near = nearest(entity, [...meta.sets.keys()])
      throw new OneCError(`объект ${entity} не опубликован в OData`, {
        hint: near.length
          ? `похожие опубликованные: ${near.join(', ')}`
          : 'включи публикацию объекта в настройках OData конфигурации',
      })
    }
    return entity
  }

  /** Сетка ключа: entity(guid'...') либо готовое выражение. */
  qualify(entity, key, keyExpr) {
    if (keyExpr) {
      const expr = String(keyExpr).trim()
      return entity + (expr.startsWith('(') ? expr : `(${expr})`)
    }
    if (!key) {
      throw new OneCError('нужен key (Ref_Key/guid) или keyExpr', {
        hint: "keyExpr пишется целиком с скобками: (guid'...')",
      })
    }
    const k = String(key)
      .trim()
      .replace(/^guid'/i, '')
      .replace(/'$/, '')
      .replace(/^\(/, '')
      .replace(/\)$/, '')
    if (UUID_RE.test(k)) return `${entity}(guid'${k}')`
    return `${entity}('${k.replace(/'/g, "''")}')`
  }

  entityFields(meta, entity) {
    const set = meta.sets.get(entity)
    if (!set) return []
    return meta.types.get(set.entityType) ?? []
  }

  async query({ entity, filter, select, orderby, top, skip, expand, inlinecount }) {
    await this.ensureEntity(entity)
    const limit = Math.max(1, Math.min(Number(top) > 0 ? Number(top) : 50, this.cfg.maxRows))
    const params = { $format: 'json' }
    if (filter) params.$filter = String(filter).slice(0, this.cfg.maxFilter)
    if (select) params.$select = Array.isArray(select) ? select.join(',') : String(select)
    if (orderby) params.$orderby = String(orderby)
    if (expand) params.$expand = Array.isArray(expand) ? expand.join(',') : String(expand)
    if (inlinecount) params.$inlinecount = 'allpages'
    params.$top = limit + 1
    if (Number(skip) > 0) params.$skip = Number(skip)

    const { json, text } = await this.request(entity, { params })
    if (!json || !Array.isArray(json.value)) {
      throw new OneCError('ответ 1С без массива value', { body: text.slice(0, 800) })
    }
    const rows = json.value
    const truncated = rows.length > limit
    return {
      rows: truncated ? rows.slice(0, limit) : rows,
      truncated,
      total: json['odata.count'] ?? json['@odata.count'] ?? null,
      limit,
    }
  }

  async create(entity, data) {
    await this.ensureEntity(entity)
    const { status, json } = await this.request(entity, {
      method: 'POST',
      body: data,
      params: { $format: 'json' },
    })
    return { status, row: json }
  }

  async update(entity, key, data, keyExpr) {
    await this.ensureEntity(entity)
    const { status, json } = await this.request(this.qualify(entity, key, keyExpr), {
      method: 'PATCH',
      body: data,
    })
    return { status, row: json }
  }

  async remove(entity, key, keyExpr) {
    await this.ensureEntity(entity)
    const { status } = await this.request(this.qualify(entity, key, keyExpr), { method: 'DELETE' })
    return { status }
  }

  /** Вызов метода HTTP-сервиса 1С: ONEC_SERVICE_URL/method. */
  async service(method, payload) {
    if (!this.cfg.serviceUrl) {
      throw new OneCError('ONEC_SERVICE_URL не задан', {
        hint: 'проведение документов идёт через HTTP-сервис 1С: ONEC_SERVICE_URL=http://host/base/hs/hardpro',
      })
    }
    const name = String(method ?? '').trim()
    if (!/^[A-Za-z][A-Za-z0-9_]*$/.test(name)) {
      throw new OneCError(`недопустимый метод HTTP-сервиса: "${method}"`)
    }
    const { status, json, text } = await this.send(`${this.cfg.serviceUrl}/${name}`, {
      method: 'POST',
      body: payload ?? {},
      timeoutMs: this.cfg.serviceTimeoutMs,
    })
    if (json === null && text) return { status, raw: text.slice(0, 2_000) }
    return { status, result: json }
  }
}
