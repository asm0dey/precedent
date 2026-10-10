import { expect, test } from 'claude-code/testing'

const ok = (stdout: string, exitCode = 0) => ({ value: { exitCode, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })
const REJ = JSON.stringify({ opt: 'H2', topic: 'database-engine', project: 'calit', title: 'PostgreSQL is the only supported database' })
const LES = JSON.stringify({ opt: 'Dependabot', topic: 'dependency-update-bot', why: 'noisy', instead: 'Renovate' })

// git answers /p/<name> paths as their own top level; precedent answers brief and cypher.
function world(on: any, briefs: string[]) {
  on('fs.exists', (_: unknown, e: { path: string }) => ({ value: !e.path.endsWith('.yml') })) // .yml files are new
  on('fs.stat', (_: unknown, e: { path: string }) => ({ value: { kind: 'dir', size: 0, mtimeMs: 0, isLink: false, realPath: e.path } }))
  on('session.cwd', () => ({ value: '/p/start' }))
  on('env.get', () => ({ value: '/home/u' }))
  on('process.run', (_: unknown, e: { argv: string[] }) => {
    const argv = e.argv
    if (argv[0] === 'git') return argv[2]!.startsWith('/p/') ? ok(`${argv[2]!.replace(/\/sub$/, '')}\n`) : ok('', 128)
    if (argv.includes('cypher')) return ok(argv.at(-1)!.includes('Lesson') ? `${LES}\n` : `${REJ}\n`)
    briefs.push(argv[argv.indexOf('--project') + 1]!)
    return ok(`BRIEF ${argv[argv.indexOf('--project') + 1]}`)
  })
  on('tool.call', { tool: 'Bash' }, () => ({ result: { stdout: '', stderr: '', interrupted: false } }))
  on('tool.call', { tool: 'Edit' }, () => ({ result: {} }))
  on('tool.call', { tool: 'Write' }, () => ({ result: {} }))
}

test('re-prime: briefs once per project a cd lands in, never the start project', async ($, on: any) => {
  const briefs: string[] = []
  world(on, briefs)
  on('session.start', () => ({ cwd: '/p/start' }))
  await $.session.start({ source: 'startup' } as any)
  const bash = (command: string) => $.tool.call({ tool: 'Bash', command } as any)

  expect((await bash('cd sub')).context).toBeUndefined()
  expect((await bash('cd /p/other && ls')).context).toEqual(['BRIEF /p/other'])
  expect((await bash('cd /p/other')).context).toBeUndefined()
  expect((await bash('cd /tmp/not-git')).context).toBeUndefined()
  expect((await bash('echo cdrom; cd $(evil)')).context).toBeUndefined()
  expect(briefs).toEqual(['/p/other'])
})

test('depcheck: flags rejected or regretted options a manifest edit adds, as whole tokens', async ($, on: any) => {
  world(on, [])
  const edit = (file_path: string, new_string: string, old_string = '') =>
    $.tool.call({ tool: 'Edit', file_path, old_string, new_string } as any)

  expect((await edit('/x/pom.xml', '<artifactId>h2</artifactId>')).context?.[0]).toContain("REJECTED 'H2' (database-engine) in calit")
  expect((await edit('/x/pom.xml', '<groupId>com.h2database</groupId>')).context).toBeUndefined()
  expect((await edit('/x/README.md', 'h2')).context).toBeUndefined()
  expect((await edit('/x/pom.xml', '<artifactId>h2</artifactId>', '<artifactId>h2</artifactId>\n')).context).toBeUndefined()

  const bot = (await edit('C:\\x\\.github\\dependabot.yml', 'version: 2 # dependabot')).context?.[0]
  expect(bot).toContain("REGRET 'Dependabot'")
  expect(bot).toContain('now preferred: Renovate')
})

test('depcheck: creating a manifest counts its name, so a new dependabot.yml is flagged', async ($, on: any) => {
  world(on, [])
  on('fs.exists', () => ({ value: false }))
  on('tool.call', { tool: 'Write' }, () => ({ result: {} }))
  const r = await $.tool.call({ tool: 'Write', file_path: '/x/.github/dependabot.yml', content: 'version: 2\n' } as any)
  expect(r.context?.[0]).toContain("REGRET 'Dependabot'")
})

test('depcheck: creating a manifest whose name is the rejected choice warns', async ($, on: any) => {
  world(on, [])
  const write = (file_path: string, content: string) => $.tool.call({ tool: 'Write', file_path, content } as any)
  expect((await write('/x/.github/dependabot.yml', 'version: 2\n')).context?.[0]).toContain("REGRET 'Dependabot'")
  expect((await write('/x/pom.xml', '<project/>\n')).context).toBeUndefined()
})
