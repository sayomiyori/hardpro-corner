import { createServer } from 'node:http'
import { spawn } from 'node:child_process'
import { strict as assert } from 'node:assert'

const EDMX = `<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="1.0" xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">
<edmx:DataServices xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata" m:DataServiceVersion="3.0">
<Schema Namespace="StandardODATA" xmlns="http://schemas.microsoft.com/ado/2008/09/edm">
<EntityType Name="Catalog_Номенклатура"><Key><PropertyRef Name="Ref_Key"/></Key>
<Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/>
<Property Name="DeletionMark" Type="Edm.Boolean" Nullable="false"/>
<Property Name="Code" Type="Edm.String" Nullable="true"/>
<Property Name="Description" Type="Edm.String" Nullable="true"/>
</EntityType>
<EntityType Name="Document_Отчет"><Key><PropertyRef Name="Ref_Key"/></Key>
<Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/>
<Property Name="Date" Type="Edm.DateTime" Nullable="false"/>
<Property Name="Posted" Type="Edm.Boolean" Nullable="false"/>
</EntityType>
<EntityContainer Name="StandardODATA" m:IsDefaultEntityContainer="true">
<EntitySet Name="Catalog_Номенклатура" EntityType="StandardODATA.Catalog_Номенклатура"/>
<EntitySet Name="Document_Отчет" EntityType="StandardODATA.Document_Отчет"/>
</EntityContainer>
</Schema></edmx:DataServices></edmx:Edmx>`

const ROW = { Ref_Key: '11111111-2222-3333-4444-555555555555', DeletionMark: false, Code: '00042', Description: 'Болт М8' }
const seen = []

const server = createServer(async (req, res) => {
  const chunks = []
  for await (const c of req) chunks.push(c)
  const body = chunks.length ? Buffer.concat(chunks).toString('utf8') : ''
  seen.push({ method: req.method, url: req.url, auth: req.headers.authorization, body })
  const decoded = decodeURIComponent(req.url)
  const json = (code, obj) => {
    res.writeHead(code, { 'Content-Type': 'application/json;charset=utf-8' })
    res.end(obj === undefined ? '' : JSON.stringify(obj))
  }
  if (decoded.startsWith('/base/odata/standard.odata/$metadata')) {
    res.writeHead(200, { 'Content-Type': 'application/xml' }); return res.end(EDMX)
  }
  if (decoded.startsWith('/base/odata/standard.odata/Catalog_Номенклатура') && req.method === 'GET') {
    return json(200, { 'odata.metadata': 'x', value: [ROW] })
  }
  if (decoded.startsWith('/base/odata/standard.odata/Catalog_Номенклатура') && req.method === 'POST') {
    return json(201, { ...ROW, ...JSON.parse(body) })
  }
  if (decoded.startsWith('/base/odata/standard.odata/Catalog_Номенклатура') && req.method === 'PATCH') {
    res.writeHead(204); return res.end()
  }
  if (decoded.startsWith('/base/odata/standard.odata/Catalog_Номенклатура') && req.method === 'DELETE') {
    res.writeHead(204); return res.end()
  }
  if (decoded.startsWith('/base/hs/hardpro/post')) return json(200, { ok: true, posted: true })
  json(404, { error: 'no route' })
})

await new Promise((r) => server.listen(0, '127.0.0.1', r))
const port = server.address().port
const env = {
  ...process.env,
  ONEC_URL: `http://127.0.0.1:${port}/base/odata/standard.odata`,
  ONEC_SERVICE_URL: `http://127.0.0.1:${port}/base/hs/hardpro`,
  ONEC_USER: 'u', ONEC_PASSWORD: 'p', ONEC_MODE: 'admin',
  ONEC_ALLOW: 'Catalog_*,Document_*',
}

const child = spawn(process.execPath, ['src/server.mjs'], { env })
let out = ''
child.stdout.setEncoding('utf8')
const pending = new Map()
child.stdout.on('data', (d) => {
  out += d
  let idx
  while ((idx = out.indexOf('\n')) >= 0) {
    const line = out.slice(0, idx)
    out = out.slice(idx + 1)
    if (!line.trim()) continue
    const msg = JSON.parse(line)
    const fn = pending.get(msg.id)
    if (fn) { pending.delete(msg.id); fn(msg.result ?? msg.error) }
  }
})
const rpc = (id, method, params) => new Promise((resolve) => {
  pending.set(id, resolve)
  child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n')
})
const tools = (name, args) => rpc(Math.ceil(Math.random() * 1e9), 'tools/call', { name, arguments: args })
const text = (r) => r.content?.[0]?.text ?? JSON.stringify(r)

await rpc(1, 'initialize', { protocolVersion: '2025-06-18' })

const meta = await tools('onec_metadata', {})
assert.match(text(meta), /Catalog_Номенклатура/)
assert.match(text(meta), /Document_Отчет/)

const desc = await tools('onec_describe', { entity: 'Catalog_Номенклатура' })
assert.match(text(desc), /DeletionMark/)
assert.match(text(desc), /Болт М8/)

const q = await tools('onec_query', { entity: 'Catalog_Номенклатура', filter: "DeletionMark eq false", top: 5 })
assert.match(text(q), /Болт М8/)

const close = await tools('onec_query', { entity: 'Catalog_Номенклатур' })
assert.match(text(close), /похожие опубликованные/)

const created = await tools('onec_create', { entity: 'Catalog_Номенклатура', data: { Description: 'Гайка', Code: '00043' } })
assert.match(text(created), /ok/)
assert.match(text(created), /11111111-2222/)

const upd = await tools('onec_update', { entity: 'Catalog_Номенклатура', key: ROW.Ref_Key, data: { Description: 'Болт М10' } })
assert.match(text(upd), /updatedFields/)

const del = await tools('onec_delete', { entity: 'Catalog_Номенклатура', key: ROW.Ref_Key })
assert.match(text(del), /deleted/)

const post = await tools('onec_post_document', { document: 'Document_Отчет', ref: ROW.Ref_Key })
assert.match(text(post), /posted/)

const bulk = await tools('onec_bulk_create', { entity: 'Catalog_Номенклатура', items: [{ Description: 'a' }, { Description: 'b' }] })
assert.match(text(bulk), /"created": 2/)

const bad = await tools('onec_create', { entity: 'Catalog_Номенклатура', data: 'nope' })
assert.match(text(bad), /JSON-объектом/)

const patch = seen.find((s) => s.method === 'PATCH')
assert.match(decodeURIComponent(patch.url), /Catalog_Номенклатура\(guid'11111111-2222-3333-4444-555555555555'\)/)
assert.equal(patch.body, '{"Description":"Болт М10"}')
assert.equal(seen[0].auth, 'Basic ' + Buffer.from('u:p').toString('base64'))
const getQ = seen.find((s) => s.method === 'GET' && s.url.includes('$filter'))
assert.match(getQ.url, /\$top=6/)
assert.match(getQ.url, /\$format=json/)

console.log('INTEGRATION_OK', seen.length, 'запросов в mock 1С')
child.kill()
server.close()
process.exit(0)
