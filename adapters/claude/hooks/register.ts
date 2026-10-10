// In-session hooks (Claude Code function hooks). Two standing orders the model
// otherwise has to remember, made automatic:
//
//   re-prime  a Bash `cd` into another project attaches that project's brief
//   depcheck  a manifest edit that adds an option recorded as rejected or
//             regretted attaches the matching records
//
// Both answer with tool-result context the model reads and the user does not.
// Claude Code only: the cross-agent behaviour still lives in SKILL.md and the
// SessionStart hook.
import type { EngineInterface, Register } from 'claude-code'

const CLI = 'adapters/claude/skills/precedent/scripts/precedent.py'

// Literal targets only: no $, backticks, parens or quotes, so nothing is evaluated.
const CD_TARGET = /(?:^|[\s;&|(])(?:cd|pushd)\s+(?:--\s+)?([^\s;&|()$`'"]+)/g
const MANIFESTS = new Set(['package.json', 'pyproject.toml', 'pom.xml', 'build.gradle', 'build.gradle.kts',
  'libs.versions.toml', 'Cargo.toml', 'go.mod', 'Gemfile', 'composer.json',
  'dependabot.yml', 'dependabot.yaml', 'renovate.json', 'renovate.json5'])

const basename = (path: string) => path.split(/[\\/]/).at(-1) ?? ''

function isManifest(path: string) {
  const name = basename(path)
  return MANIFESTS.has(name) || name.endsWith('.csproj') || (name.startsWith('requirements') && name.endsWith('.txt'))
}

const REJECTED = `MATCH (d:Decision)-[:REJECTED]->(o:Option), (d)-[:ABOUT]->(t:Topic), (d)-[:IN_PROJECT]->(p:Project)
  WHERE d.status='active' RETURN o.name AS opt, t.name AS topic, p.name AS project, d.title AS title`
const REGRETTED = `MATCH (l:Lesson) RETURN l.option AS opt, l.topic AS topic, l.statement AS why, l.instead AS instead`

type Row = { opt: string; topic: string; project?: string; title?: string; why?: string; instead?: string }

// The CLI sits under the plugin root; accept either reading of "the directory holding plugin.json".
async function cli($: EngineInterface) {
  for (const base of [$.plugin.root, `${$.plugin.root}/..`]) {
    if (await $.fs.exists(`${base}/${CLI}`)) return `${base}/${CLI}`
  }
  return undefined
}

async function run($: EngineInterface, args: string[]) {
  const script = await cli($)
  if (script === undefined) return undefined
  try {
    const r = await $.process.run(['uv', 'run', '--quiet', script, ...args], { timeoutMs: 20_000 })
    return r.exitCode === 0 ? r.stdout : undefined
  } catch {
    return undefined // no uv: stay silent, as the SessionStart hook does
  }
}

async function projectOf($: EngineInterface, dir: string) {
  try {
    const r = await $.process.run(['git', '-C', dir, 'rev-parse', '--show-toplevel'])
    return r.exitCode === 0 ? r.stdout.trim() : undefined
  } catch {
    return undefined
  }
}

// The session snaps back to its own dir after a cd out of it, so the command's last cd target is the truth.
async function landedIn($: EngineInterface, command: string) {
  const target = [...command.matchAll(CD_TARGET)].at(-1)?.[1]
  if (target === undefined) return undefined
  const home = (await $.env.get('HOME')) ?? (await $.env.get('USERPROFILE')) ?? ''
  let path = `${await $.session.cwd()}/${target}`
  if (/^~([\\/]|$)/.test(target)) path = home + target.slice(1)
  else if (/^([\\/]|[A-Za-z]:[\\/])/.test(target)) path = target
  try {
    const st = await $.fs.stat(path, { resolve: true })
    return st.kind === 'dir' ? st.realPath : undefined
  } catch {
    return undefined
  }
}

// Lines in `after` that `before` did not have: what this edit introduced.
function added(before: string, after: string) {
  const had = new Set(before.split('\n'))
  return after.split('\n').filter(l => !had.has(l)).join('\n').toLowerCase()
}

// The option as a whole token, so "go" never matches "google" and "postgres" never "postgresql".
function mentions(text: string, opt: string) {
  const o = opt.toLowerCase()
  if (o.length < 2) return false // "H2" is a real rejected option
  for (let i = text.indexOf(o); i !== -1; i = text.indexOf(o, i + 1)) {
    const pre = text[i - 1], post = text[i + o.length]
    if (!(pre && /[a-z0-9]/.test(pre)) && !(post && /[a-z0-9]/.test(post))) return true
  }
  return false
}

async function rows($: EngineInterface, query: string): Promise<Row[]> {
  const out = await run($, ['cypher', query])
  return out === undefined ? [] : out.split('\n').filter(Boolean).map(line => JSON.parse(line) as Row)
}

async function warnings($: EngineInterface, text: string) {
  if (!text.trim()) return []
  const [rejected, regretted] = await Promise.all([rows($, REJECTED), rows($, REGRETTED)])
  return [
    ...regretted.filter(r => mentions(text, r.opt)).map(r => {
      const instead = r.instead ? ' — now preferred: ' + r.instead : ''
      return `REGRET '${r.opt}' (${r.topic}): ${r.why}${instead}`
    }),
    ...rejected.filter(r => mentions(text, r.opt)).map(r =>
      `REJECTED '${r.opt}' (${r.topic}) in ${r.project}: ${r.title}`),
  ]
}

function report(file: string, found: string[]) {
  return [
    `precedent: this edit to ${file} mentions options you recorded against before.`,
    'These are name matches — judge whether each is really the same choice.',
    ...found.map(f => `  ${f}`),
    "If it is: tell the user and run `precedent.py check --topic <topic> --chose <option>` before keeping it.",
  ].join('\n')
}

export const register: Register = on => {
  // The project the model was last briefed on; the SessionStart hook briefs the start dir.
  let primed: string | undefined

  on('session.start', async ($, e, next) => {
    const started = await next(e)
    primed = await projectOf($, started.cwd)
    return started
  })

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const ran = await next(e)
    if (ran.deny !== undefined) return ran
    const dir = await landedIn($, e.command)
    const project = dir === undefined ? undefined : await projectOf($, dir)
    if (project === undefined || project === primed) return ran

    primed = project
    const brief = (await run($, ['brief', '--project', project, '--only-if-relevant']))?.trim()
    if (!brief) return ran
    $.ui.toast(`precedent: briefed on ${project}`)
    return { ...ran, context: [...(ran.context ?? []), brief] }
  })

  on('tool.call', { tool: 'Edit' }, async ($, e, next) => {
    const ran = await next(e)
    if (ran.deny !== undefined || ran.isError || !isManifest(e.file_path)) return ran
    const found = await warnings($, added(e.old_string, e.new_string))
    if (found.length === 0) return ran
    $.ui.toast(`precedent: ${found.length} past rejection(s) in ${e.file_path}`)
    return { ...ran, context: [...(ran.context ?? []), report(e.file_path, found)] }
  })

  on('tool.call', { tool: 'Write' }, async ($, e, next) => {
    if (!isManifest(e.file_path)) return next(e)
    const existed = await $.fs.exists(e.file_path)
    const before = existed ? String(await $.fs.read(e.file_path)) : ''
    const ran = await next(e)
    if (ran.deny !== undefined || ran.isError) return ran
    // Creating dependabot.yml is the choice itself, whatever its content says.
    const name = existed ? '' : basename(e.file_path) + '\n'
    const found = await warnings($, name + added(before, e.content))
    if (found.length === 0) return ran
    $.ui.toast(`precedent: ${found.length} past rejection(s) in ${e.file_path}`)
    return { ...ran, context: [...(ran.context ?? []), report(e.file_path, found)] }
  })
}
