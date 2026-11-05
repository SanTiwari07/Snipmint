// Copies the production build into the Python package so the wheel ships the UI.
import { cpSync, existsSync, mkdirSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const dist = resolve(here, '..', 'dist')
const target = resolve(here, '..', '..', 'snipmint', 'server', 'static')
if (!existsSync(join(dist, 'index.html'))) throw new Error('Run `npm run build` first.')
mkdirSync(target, { recursive: true })
for (const entry of readdirSync(target)) rmSync(join(target, entry), { recursive: true, force: true })
cpSync(dist, target, { recursive: true })
writeFileSync(join(target, '.gitkeep'), '')
console.log(`Copied UI to ${target}`)
