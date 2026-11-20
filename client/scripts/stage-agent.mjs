import { access, chmod, copyFile, mkdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'

const platform = process.argv[2] || (process.platform === 'win32' ? 'win' : 'linux')
if (!['win', 'linux'].includes(platform)) throw new Error('Expected linux or win')
const filename = `transcriptd-agent${platform === 'win' ? '.exe' : ''}`
const root = fileURLToPath(new URL('..', import.meta.url))
const source = process.env.TRANSCRIPTD_AGENT
  ? resolve(process.env.TRANSCRIPTD_AGENT)
  : resolve(root, '../agent/target/release', filename)
try { await access(source) }
catch { throw new Error(`Build the ${platform} release agent first, or set TRANSCRIPTD_AGENT to its binary. Missing ${source}`) }
const destination = resolve(root, 'bin', filename)
await mkdir(resolve(root, 'bin'), { recursive: true })
await copyFile(source, destination)
if (platform === 'linux') await chmod(destination, 0o755)
console.log(`Staged ${filename} in client/bin`)
