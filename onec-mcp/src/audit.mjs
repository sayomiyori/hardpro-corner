/**
 * Журнал вызовов: JSONL, одна строка на вызов инструмента.
 * Никогда не бросает наружу — сломанный аудит не должен ломать работу агента.
 */
import { appendFile, mkdir } from 'node:fs/promises'
import { dirname } from 'node:path'

export class Auditor {
  constructor(file) {
    this.file = String(file ?? '')
    this.ready = null
  }

  async write(entry) {
    if (!this.file) return
    try {
      this.ready ??= mkdir(dirname(this.file), { recursive: true })
      await this.ready
      const line = JSON.stringify({ ts: new Date().toISOString(), ...entry })
      await appendFile(this.file, `${line}\n`, 'utf8')
    } catch {
      this.ready = null
    }
  }
}
