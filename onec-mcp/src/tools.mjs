/**
 * Инструменты MCP поверх 1С OData.
 * Каждый обработчик возвращает строку (её агент читает как content[0].text).
 */

const MAX_TEXT = 180_000

function json(value) {
  const text = JSON.stringify(value, null, 2) ?? 'null'
  return text.length > MAX_TEXT
    ? `${text.slice(0, MAX_TEXT)}\n… обрезано: сузь $select / $top`
    : text
}

function asTop(args, cfg) {
  const n = Number(args?.top)
  if (!Number.isFinite(n) || n <= 0) return Math.min(50, cfg.maxRows)
  return Math.min(Math.trunc(n), cfg.maxRows)
}

export const TOOLS = [
  {
    name: 'onec_status',
    description:
      'Проверка связи с 1С: URL базы, режим доступа (readonly/write/admin), число опубликованных объектов OData, доступность HTTP-сервиса. Вызывай первым, если не уверен в подключении.',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'onec_metadata',
    description:
      'Список опубликованных в OData объектов 1С (справочники Catalog_*, документы Document_*, регистры сведений InformationRegister_*, AccumulationRegister_*, планы видов характеристик и т.д.). С параметром entity возвращает поля этого объекта. Entity можно фильтровать подстрокой или глобом, например "Catalog_*" или "Номенклатура".',
    inputSchema: {
      type: 'object',
      properties: {
        entity: { type: 'string', description: 'Подстрока или глоб для фильтра имён, пусто — все объекты' },
        fields: { type: 'boolean', description: 'Вернуть полный список полей (тяжело, если объектов много)' },
        refresh: { type: 'boolean', description: 'Перечитать $metadata, минуя кэш 5 минут' },
      },
    },
  },
  {
    name: 'onec_describe',
    description:
      'Карточка объекта: все поля с типами плюс одна реальная строка как пример. Лучший первый шаг перед записью — не угадывать имена полей.',
    inputSchema: {
      type: 'object',
      properties: {
        entity: { type: 'string', description: 'Например Catalog_Номенклатура' },
        sample: { type: 'number', description: 'Сколько строк-примеров вернуть, по умолчанию 1, максимум 5' },
      },
      required: ['entity'],
    },
  },
  {
    name: 'onec_query',
    description:
      "Запрос данных через OData $filter. Язык: 1С OData v3. Примеры: \"DeletionMark eq false\", \"Description like '%болт%'\", \"Date ge datetime'2024-01-01T00:00:00' and Date lt datetime'2025-01-01T00:00:00'\" (даты строго в datetime'...'), \"Ref_Key eq guid'00000000-0000-0000-0000-000000000000'\". Для точного количества добавь inlinecount=true.",
    inputSchema: {
      type: 'object',
      properties: {
        entity: { type: 'string', description: 'Например Document_РеализацияТоваровУслуг' },
        filter: { type: 'string', description: 'Выражение $filter без самого префикса' },
        select: { type: 'array', items: { type: 'string' }, description: 'Поля; Ref_Key возвращай всегда, если планируешь правку' },
        orderby: { type: 'string', description: 'Например Date desc' },
        top: { type: 'number', description: 'Строк, по умолчанию 50' },
        skip: { type: 'number', description: 'Смещение для постраничного чтения' },
        expand: { type: 'array', items: { type: 'string' }, description: 'Развернуть поля-ссылки' },
        inlinecount: { type: 'boolean', description: 'Вернуть общее количество строк' },
      },
      required: ['entity'],
    },
  },
  {
    name: 'onec_get',
    description: 'Одна запись по ключу: Ref_Key (guid) или keyExpr вроде (guid\'...\') / (\'КОД\').',
    inputSchema: {
      type: 'object',
      properties: {
        entity: { type: 'string' },
        key: { type: 'string', description: 'Ref_Key / guid' },
        keyExpr: { type: 'string', description: 'Готовое выражение ключа со скобками, если ключ составной' },
        expand: { type: 'array', items: { type: 'string' } },
      },
      required: ['entity'],
    },
  },
  {
    name: 'onec_create',
    description:
      'Создать объект (POST). Тело — только бизнес-поля: "Description", "Code", "Артикул", ссылки вида {"Ref_Key":"<guid>"} для Catalog_* / "Дата":"2024-01-01T00:00:00". guid, Ref_Key и служебные поля не передавай — 1С назначит сама. dryRun=true возвращает запрос без выполнения.',
    inputSchema: {
      type: 'object',
      properties: {
        entity: { type: 'string' },
        data: { type: 'object', description: 'Поля объекта' },
        dryRun: { type: 'boolean' },
      },
      required: ['entity', 'data'],
    },
  },
  {
    name: 'onec_update',
    description:
      'Изменить поля объекта (PATCH). Передавай только изменяемые поля — остальные не трогаются. Для ссылок {"Ref_Key":"<guid>"}, для дат ISO-строку. dryRun=true возвращает запрос без выполнения.',
    inputSchema: {
      type: 'object',
      properties: {
        entity: { type: 'string' },
        key: { type: 'string', description: 'Ref_Key / guid' },
        keyExpr: { type: 'string' },
        data: { type: 'object' },
        dryRun: { type: 'boolean' },
      },
      required: ['entity', 'data'],
    },
  },
  {
    name: 'onec_delete',
    description: 'Удалить объект по ключу (DELETE). Только ONEC_MODE=admin. Перед вызовом проверь DeletionMark и связи.',
    inputSchema: {
      type: 'object',
      properties: {
        entity: { type: 'string' },
        key: { type: 'string' },
        keyExpr: { type: 'string' },
        dryRun: { type: 'boolean' },
      },
      required: ['entity'],
    },
  },
  {
    name: 'onec_bulk_create',
    description:
      'Пакетное создание строк одного вида (до ONEC_BULK_MAX, по умолчанию 50). Без транзакции: останавливается на первой ошибке либо продолжает при continueOnError=true. Возвращает Ref_Key созданных записей.',
    inputSchema: {
      type: 'object',
      properties: {
        entity: { type: 'string' },
        items: { type: 'array', items: { type: 'object' } },
        continueOnError: { type: 'boolean' },
        dryRun: { type: 'boolean' },
      },
      required: ['entity', 'items'],
    },
  },
  {
    name: 'onec_post_document',
    description:
      'Провести документ (РежимЗаписиДокумента.Проведение) через HTTP-сервис 1С. document — имя из метаданных (Document_РеализацияТоваровУслуг), ref — Ref_Key документа. Требует ONEC_SERVICE_URL.',
    inputSchema: {
      type: 'object',
      properties: {
        document: { type: 'string' },
        ref: { type: 'string', description: 'Ref_Key / guid документа' },
      },
      required: ['document', 'ref'],
    },
  },
  {
    name: 'onec_unpost_document',
    description: 'Отменить проведение документа через HTTP-сервис 1С.',
    inputSchema: {
      type: 'object',
      properties: {
        document: { type: 'string' },
        ref: { type: 'string' },
      },
      required: ['document', 'ref'],
    },
  },
  {
    name: 'onec_service_call',
    description:
      'Произвольный вызов метода HTTP-сервиса 1С (hs/hardpro). Для нестандартных операций: своя логика на стороне 1С. method — имя метода, payload — JSON-объект.',
    inputSchema: {
      type: 'object',
      properties: {
        method: { type: 'string' },
        payload: { type: 'object' },
      },
      required: ['method'],
    },
  },
]

export function createHandlers({ client, cfg }) {
  const requireMode = (level) => {
    if (cfg.mode === 'readonly') {
      throw new Error('режим readonly: запись запрещена, включи ONEC_MODE=write (или admin для удаления)')
    }
    if (level === 'admin' && cfg.mode !== 'admin') {
      throw new Error('удаление требует ONEC_MODE=admin')
    }
  }

  const auditBody = (method, url, body) => json({ dryRun: true, request: { method, url, body } })

  return {
    async onec_status() {
      const info = {
        url: cfg.baseUrl,
        service: cfg.serviceUrl || '(не задан: проведение документов недоступно)',
        mode: cfg.mode,
        allow: cfg.allow.length ? cfg.allow : '(все опубликованные)',
        deny: cfg.deny.length ? cfg.deny : '(нет)',
        maxRows: cfg.maxRows,
        timeoutMs: cfg.timeoutMs,
        audit: cfg.auditPath || '(выключен)',
      }
      try {
        const meta = await client.metadata({ refresh: true })
        info.ok = true
        info.objectsPublished = meta.sets.size
        info.samples = [...meta.sets.keys()].slice(0, 12)
      } catch (err) {
        info.ok = false
        info.error = err instanceof Error ? err.message : String(err)
        info.hint = err?.hint || 'проверь ONEC_URL, логин/пароль и включённый OData в публикации базы'
      }
      return json(info)
    },

    async onec_metadata(args = {}) {
      const meta = await client.metadata({ refresh: Boolean(args.refresh) })
      const needle = String(args.entity ?? '').trim().toLowerCase()
      const glob = needle.includes('*') || needle.includes('?')
      const match = (name) => {
        const low = name.toLowerCase()
        if (!needle) return true
        if (!glob) return low.includes(needle)
        const re = new RegExp(
          `^${needle.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.')}$`,
          'u',
        )
        return re.test(low)
      }
      const names = [...meta.sets.keys()].filter(match).sort()
      const result = { total: meta.sets.size, matched: names.length, objects: names.slice(0, 400) }
      if (names.length > 400) result.note = 'показаны первые 400, сузь фильтр'
      if (args.fields) {
        result.fields = Object.fromEntries(
          names.slice(0, 30).map((name) => [name, client.entityFields(meta, name)]),
        )
      }
      return json(result)
    },

    async onec_describe(args = {}) {
      const entity = await client.ensureEntity(args.entity)
      const meta = await client.metadata()
      const fields = client.entityFields(meta, entity)
      const sampleSize = Math.min(Math.max(Number(args.sample) || 1, 1), 5)
      const sample = await client.query({ entity, top: sampleSize })
      return json({
        entity,
        entityType: meta.sets.get(entity)?.entityType ?? null,
        fieldCount: fields.length,
        fields,
        sampleRows: sample.rows,
        note: fields.length ? undefined : 'поля не описаны в $metadata: читай onec_query с $select',
      })
    },

    async onec_query(args = {}) {
      const entity = await client.ensureEntity(args.entity)
      const res = await client.query({
        entity,
        filter: args.filter,
        select: args.select,
        orderby: args.orderby,
        top: asTop(args, cfg),
        skip: args.skip,
        expand: args.expand,
        inlinecount: args.inlinecount,
      })
      return json({
        entity,
        returned: res.rows.length,
        total: res.total,
        truncated: res.truncated || undefined,
        nextSkip: res.truncated || (res.total !== null && res.total > (Number(args.skip) || 0) + res.rows.length)
          ? (Number(args.skip) || 0) + res.rows.length
          : undefined,
        rows: res.rows,
      })
    },

    async onec_get(args = {}) {
      const entity = await client.ensureEntity(args.entity)
      const path = client.qualify(entity, args.key, args.keyExpr)
      const params = { $format: 'json' }
      if (args.expand) params.$expand = Array.isArray(args.expand) ? args.expand.join(',') : String(args.expand)
      const { json: row } = await client.request(path, { params })
      return json({ entity, key: path, row })
    },

    async onec_create(args = {}) {
      requireMode('write')
      const entity = await client.ensureEntity(args.entity)
      const data = args.data ?? {}
      if (!data || typeof data !== 'object' || Array.isArray(data)) {
        throw new Error('data должен быть JSON-объектом полей')
      }
      if (args.dryRun) {
        return auditBody('POST', client.buildUrl(entity, { $format: 'json' }), data)
      }
      const { status, row } = await client.create(entity, data)
      return json({ ok: true, status, entity, refKey: row?.Ref_Key ?? null, row })
    },

    async onec_update(args = {}) {
      requireMode('write')
      const entity = await client.ensureEntity(args.entity)
      const data = args.data ?? {}
      if (!data || typeof data !== 'object' || Array.isArray(data)) {
        throw new Error('data должен быть JSON-объектом полей')
      }
      const path = client.qualify(entity, args.key, args.keyExpr)
      if (args.dryRun) return auditBody('PATCH', client.buildUrl(path), data)
      const { status, row } = await client.update(entity, args.key, data, args.keyExpr)
      return json({ ok: true, status, entity, updatedFields: Object.keys(data), row })
    },

    async onec_delete(args = {}) {
      requireMode('admin')
      const entity = await client.ensureEntity(args.entity)
      const path = client.qualify(entity, args.key, args.keyExpr)
      if (args.dryRun) return auditBody('DELETE', client.buildUrl(path), null)
      const { status } = await client.remove(entity, args.key, args.keyExpr)
      return json({ ok: true, status, entity, deleted: path })
    },

    async onec_bulk_create(args = {}) {
      requireMode('write')
      const entity = await client.ensureEntity(args.entity)
      const items = Array.isArray(args.items) ? args.items : []
      if (!items.length) throw new Error('items пуст')
      if (items.length > cfg.bulkMax) {
        throw new Error(`items=${items.length}, максимум ${cfg.bulkMax} за вызов: разбей на пачки`)
      }
      if (args.dryRun) {
        return json({
          dryRun: true,
          method: 'POST',
          url: client.buildUrl(entity, { $format: 'json' }),
          count: items.length,
          items,
        })
      }
      const created = []
      const failed = []
      for (const [index, item] of items.entries()) {
        try {
          const { row } = await client.create(entity, item)
          created.push({ index, refKey: row?.Ref_Key ?? null, number: row?.Number ?? row?.Code ?? null })
        } catch (err) {
          failed.push({ index, error: err instanceof Error ? err.message : String(err) })
          if (!args.continueOnError) break
        }
      }
      return json({
        ok: failed.length === 0,
        entity,
        requested: items.length,
        created: created.length,
        failed: failed.length,
        createdRefs: created,
        errors: failed.length ? failed : undefined,
      })
    },

    async onec_post_document(args = {}) {
      requireMode('write')
      const document = String(args.document ?? '')
      if (!/^Document_[\p{L}\p{N}_]+$/u.test(document)) {
        throw new Error('document должен быть именем документа метаданных, например Document_РеализацияТоваровУслуг')
      }
      if (!args.ref) throw new Error('нужен ref (Ref_Key документа)')
      const res = await client.service('post', { document, ref: String(args.ref) })
      return json({ ok: true, ...res })
    },

    async onec_unpost_document(args = {}) {
      requireMode('write')
      const document = String(args.document ?? '')
      if (!/^Document_[\p{L}\p{N}_]+$/u.test(document)) {
        throw new Error('document должен быть именем документа метаданных')
      }
      if (!args.ref) throw new Error('нужен ref (Ref_Key документа)')
      const res = await client.service('unpost', { document, ref: String(args.ref) })
      return json({ ok: true, ...res })
    },

    async onec_service_call(args = {}) {
      requireMode('write')
      const res = await client.service(args.method, args.payload ?? {})
      return json({ ok: true, ...res })
    },
  }
}
