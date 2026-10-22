import type { Args, On, PromptAutocompleteResult, PromptEditResult } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

const PANE = {
  plugin: 'multi-account-pane',
  component: 'Pane',
  requestId: 'multi-account-pane',
  props: {
    title: 'Claude accounts',
    isFocused: false,
    bodyColumns: 44,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 40 },
    view: {},
  },
} as const

const SURFACES = ['terminal', 'desktop', 'vscode', 'mobile'] as const

const account = (name: string, key: string, text: string, extra: Record<string, unknown> = {}) => ({
  name,
  email: name + '@example.com',
  dir: '~/.claude-' + name,
  key,
  text,
  full_until: null,
  working: 0,
  login: null,
  ...extra,
})

const NOW = 86_400

const DATA = {
  now: NOW,
  accounts: [
    account('main', 'full', 'full until Fri 23 00:59', { full_until: NOW + 3600 }),
    account('work', 'ready', 'ready', { working: 1 }),
    account('team', 'not-signed-in', 'not signed in'),
    account('spare', 'paused', 'paused'),
    account('copy', 'duplicate', 'duplicate of main'),
  ],
  run: {
    id: 'r1',
    task: 'three functions with their tests',
    state: 'working',
    state_text: 'Working.',
    stopping: false,
    report: null,
    pieces: [
      { id: 'add', title: 'Function add', account: 'work', state: 'done', state_text: 'done', minutes: 1, commits: 2, reason: '' },
      { id: 'sub', title: 'Function subtract', account: 'team', state: 'working', state_text: 'working', minutes: null, commits: null, reason: '' },
    ],
  },
  artifacts: {
    total: 2,
    per_account: { main: 1, work: 1 },
    newest: [
      { id: 'A1', url: 'https://example.com/artifact/A1', title: 'Store plan', account: 'main', when_text: 'today 15:12', copy: '/x/index.html', live: false },
      { id: 'A2', url: null, title: 'Sales board', account: 'work', when_text: 'Tue 20 10:00', copy: null, live: true },
    ],
  },
}

// The time the pane prints is this computer's clock, whatever zone the test runs in.
const pad = (n: number) => String(n).padStart(2, '0')
const LOCAL = pad(new Date(NOW * 1000).getHours()) + ':' + pad(new Date(NOW * 1000).getMinutes())

function engine(on: On, opts: { pane?: string; out?: string; exitCode?: number } = {}) {
  const calls: string[][] = []
  const commands: { command: string; args: string }[] = []
  const opened = { n: 0, placed: true }
  on('command.register', () => ({ value: { command: 'accounts-pane' } }))
  on('ui.open', () => {
    opened.n += 1
    return { value: opened.placed ? { isPlaced: true as const } : { isPlaced: false as const, reason: 'narrow' } }
  })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('command.run', (_, e) => {
    commands.push({ command: e.command, args: e.args })
    return {}
  })
  on('process.run', (_, e) => {
    // One named script of the pane's own, never inline code: argv is python3, that script, then the CLI's words.
    if (e.argv[0] !== '/usr/bin/python3' || !e.argv[1].endsWith('/lib/run-multi-account.py')) {
      throw new Error('unexpected command: ' + e.argv.slice(0, 2).join(' '))
    }
    const args = e.argv.slice(2)
    calls.push(args)
    const pane = args[0] === 'pane'
    return {
      value: {
        exitCode: pane ? 0 : (opts.exitCode ?? 0),
        stdout: pane ? (opts.pane ?? JSON.stringify(DATA)) : (opts.out ?? "'work' is paused: it takes no work.\nmore text"),
        stderr: '',
        isStdoutTruncated: false,
        isStderrTruncated: false,
      },
    }
  })
  return { calls, commands, opened }
}

test('the pane shows every account, the run and the artifacts, on every surface', async ($, on) => {
  const clock = mock.clock(on)
  const { calls } = engine(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await clock.settle()

  expect(calls[0]).toEqual(['pane'])
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...PANE, surface })
    const has = (what: string, x: unknown) => expect(x === undefined ? surface + ': missing ' + what : 'ok').toBe('ok')
    expect((await ui.find({ type: 'Text', text: /accounts ready/ }))?.text).toBe('1 of 4 accounts ready · read at ' + LOCAL)
    has('work', await ui.find({ type: 'Text', text: 'work' }))
    has('email', await ui.find({ type: 'Text', text: 'work@example.com' }))
    has('full', await ui.find({ type: 'Text', text: 'Full until Fri 23 00:59' }))
    has('ready', await ui.find({ type: 'Text', text: 'Ready · 1 working' }))
    has('not signed in', await ui.find({ type: 'Text', text: 'Not signed in' }))
    has('paused', await ui.find({ type: 'Text', text: 'Paused' }))
    expect((await ui.find({ type: 'Text', text: /^Duplicates/ }))?.text).toBe('Duplicates (not counted): copy')
    expect(await ui.find({ type: 'Text', text: /copy@example.com/ })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: /%/ })).toBeUndefined() // usage is never measured or shown
    expect(await ui.find({ key: 'pause-copy' })).toBeUndefined()
    has('login-team', await ui.find({ key: 'login-team' }))
    has('resume-spare', await ui.find({ key: 'resume-spare' }))
    expect(await ui.find({ key: 'pause-spare' })).toBeUndefined()
    has('Function subtract', await ui.find({ type: 'Text', text: 'Function subtract' }))
    has('piece done', await ui.find({ type: 'Text', text: /done · 1 min/ }))
    has('1 of 2 done', await ui.find({ type: 'Text', text: '1 of 2 done' }))
    has('stop', await ui.find({ key: 'stop' }))
    has('artifact', await ui.find({ type: 'Text', text: 'Store plan' }))
    has('no copy', await ui.find({ type: 'Text', text: /work · Tue 20 10:00 · no copy/ }))
    has('view-A1', await ui.find({ key: 'view-A1' }))
    has('chat', await ui.find({ type: 'Text', text: 'ready: work' }))
    has('chat-all', await ui.find({ key: 'chat-all' }))
    expect(await ui.find({ key: 'task' })).toBeUndefined()
    await ui.unmount()
  }
})

test('the pane ends with one dim line that links to zoaftware.com', async ($, on) => {
  const clock = mock.clock(on)
  engine(on)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()

  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...PANE, surface })
    const line = await ui.find({ type: 'Text', text: /^Made by Zoaftware/ })
    expect(line?.text).toBe('Made by Zoaftware · zoaftware.com')
    const link = await ui.find({ type: 'Link' })
    expect(link?.text).toBe('zoaftware.com')
    expect(JSON.stringify(link)).toContain('https://zoaftware.com')
    await ui.unmount()
  }
})

test('a button runs its command and shows its first line', async ($, on) => {
  const clock = mock.clock(on)
  const { calls } = engine(on)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.press({ key: 'pause-work' })
  expect(calls).toContainEqual(['pause', 'work'])
  expect((await ui.find({ type: 'Text', text: /is paused/ }))?.text).toBe("'work' is paused: it takes no work.")
  expect(calls[calls.length - 1]).toEqual(['pane'])

  await ui.press({ key: 'view-A1' })
  expect(calls).toContainEqual(['view', 'A1'])
  await ui.press({ key: 'gallery' })
  expect(calls).toContainEqual(['gallery'])
  const before = calls.length
  await ui.press({ key: 'refresh' })
  expect(calls.slice(before)).toEqual([['pane']])
  await ui.unmount()
})

test('connecting and stopping ask first', async ($, on) => {
  const clock = mock.clock(on)
  const { calls } = engine(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await clock.settle()

  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'connect' })
  expect(calls.some(c => c[0] === 'connect')).toBe(false)
  expect(await ui.find({ type: 'Text', text: /^Open a window/ })).toBeDefined()
  await ui.press({ key: 'connect-no' })
  expect(await ui.find({ type: 'Text', text: /^Open a window/ })).toBeUndefined()
  await ui.press({ key: 'connect' })
  await ui.press({ key: 'connect-yes' })
  expect(calls).toContainEqual(['connect'])

  await ui.press({ key: 'stop' })
  expect(calls.some(c => c[0] === 'stop')).toBe(false)
  await ui.press({ key: 'stop-yes' })
  expect(calls).toContainEqual(['stop', 'r1'])
  await ui.unmount()
})

const ABOVE = {
  plugin: 'multi-account-pane',
  component: 'AbovePrompt',
  props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 100, scroll: { offset: 0, bodyRows: 10 }, view: {} },
} as const

function chat(on: On, box = 'fix the login') {
  const sent: { text: string; context: readonly string[] }[] = []
  const filled: { text: string; decorations?: unknown }[] = []
  on('prompt.submit', (_, e) => {
    sent.push({ text: e.text, context: e.context ?? [] })
    return { text: e.text, context: e.context }
  })
  on('prompt.edit', (_, e) => {
    const text = e.text.slice(0, e.start) + e.inputText + e.text.slice(e.end)
    return { text, cursor: e.start + e.inputText.length }
  })
  // The engine's own band, drawn when the plugin passes: an empty row.
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
    const { Box } = $.ui.resolve(e)
    return <Box key="engine" />
  })
  on('prompt.autocomplete', () => ({ suggestions: [{ text: '/clear', label: '/clear' }] }))
  on('prompt.read', () => ({ value: { text: box, cursor: box.length } }))
  on('prompt.fill', (_, e) => {
    filled.push({ text: e.text, decorations: e.decorations })
    return { isFilled: true }
  })
  return { sent, filled }
}

const composer = { kind: 'composer' } as const

// The engine raises these from the prompt box and the pane's frame; the test's `$` types only the calls a plugin makes.
type Raised = {
  prompt: {
    edit: (e: Args<'prompt.edit'>) => Promise<PromptEditResult>
    autocomplete: (e: Args<'prompt.autocomplete'>) => Promise<PromptAutocompleteResult>
  }
}
const raised = ($: unknown) => $ as Raised

test('"all:" and an account name at the start send the task there; anything else stays in this session', async ($, on) => {
  const clock = mock.clock(on)
  engine(on)
  const { sent } = chat(on)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()

  await $.prompt.submit({ text: 'all: add tests to every function', origin: composer, wait: false })
  await $.prompt.submit({ text: 'Work: fix the login', origin: composer, wait: false })
  await $.prompt.submit({ text: 'note: this is for this session', origin: composer, wait: false })
  await $.prompt.submit({ text: 'all: something', origin: { kind: 'plugin', name: 'other' }, wait: false })

  expect(sent.map(x => x.text)).toEqual([
    'all: add tests to every function',
    'Work: fix the login',
    'note: this is for this session',
    'all: something',
  ])
  expect(sent[0]?.context.join('\n')).toContain('skill "multi-account:split", args = the task')
  expect(sent[1]?.context.join('\n')).toContain('"--account work " followed by the task')
  expect(sent[2]?.context).toEqual([])
  expect(sent[3]?.context).toEqual([])

  const empty = await $.prompt.submit({ text: 'all:   ', origin: composer, wait: false })
  expect(empty).toEqual({ drop: 'Type the task after "all:".' })
  expect(sent.length).toBe(4)
})

test('the route word is painted while typing and the band says where Enter sends it', async ($, on) => {
  const clock = mock.clock(on)
  engine(on)
  chat(on)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()

  const r = await raised($).prompt.edit({ origin: composer, text: 'all', cursor: 3, start: 3, end: 3, inputText: ':' })
  expect(r.text).toBe('all:')
  expect(r.decorations).toEqual([{ start: 0, end: 4, color: 'claude', bold: true }])
  const band = await $.ui.mount({ ...ABOVE, surface: 'terminal' })
  expect(await band.find({ type: 'Text', text: '⇉ All accounts' })).toBeDefined()
  expect((await band.find({ type: 'Text', text: /^Enter/ }))?.text).toBe('Enter splits it across work, at the same time')
  await band.unmount()

  const one = await raised($).prompt.edit({ origin: composer, text: 'work: x', cursor: 7, start: 7, end: 7, inputText: 'y' })
  expect(one.decorations).toEqual([{ start: 0, end: 5, color: 'claude', bold: true }])
  const bandWork = await $.ui.mount({ ...ABOVE, surface: 'terminal' })
  expect(await bandWork.find({ type: 'Text', text: '→ work' })).toBeDefined()
  expect(await bandWork.find({ type: 'Text', text: 'Enter sends it whole to this account' })).toBeDefined()
  await bandWork.unmount()

  await raised($).prompt.edit({ origin: composer, text: 'team: x', cursor: 7, start: 7, end: 7, inputText: 'y' })
  const bandTeam = await $.ui.mount({ ...ABOVE, surface: 'terminal' })
  expect(await bandTeam.find({ type: 'Text', text: '→ team' })).toBeDefined()
  expect(await bandTeam.find({ type: 'Text', text: 'Not signed in: it takes no work now' })).toBeDefined()
  await bandTeam.unmount()

  const none = await raised($).prompt.edit({ origin: composer, text: 'hello: x', cursor: 8, start: 8, end: 8, inputText: 'y' })
  expect(none.decorations).toBeUndefined()
})

test('while no surface draws the pane, a button above the chat opens it again', async ($, on) => {
  const clock = mock.clock(on)
  const { opened } = engine(on)
  chat(on)
  opened.placed = false
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await clock.settle()

  for (const surface of ['terminal', 'desktop'] as const) {
    const band = await $.ui.mount({ ...ABOVE, surface })
    expect((await band.find({ type: 'Text', text: /ready ·/ }))?.text).toBe('1 of 4 ready · start with "all:" to split a task')
    const n = opened.n
    await band.press({ key: 'open-pane' })
    expect(opened.n).toBe(n + 1)
    await band.unmount()
  }

  opened.placed = true
  const band = await $.ui.mount({ ...ABOVE, surface: 'desktop' })
  await band.press({ key: 'open-pane' })
  await band.unmount()
  const after = await $.ui.mount({ ...ABOVE, surface: 'desktop' })
  expect(await after.find({ key: 'open-pane' })).toBeUndefined()
  await after.unmount()
})

test('the first word offers "all:" and every account; the pane button writes "all:" in the chat', async ($, on) => {
  const clock = mock.clock(on)
  engine(on)
  const { filled } = chat(on, 'work: fix the login')
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()

  const r = await raised($).prompt.autocomplete({ text: 'sp', cursor: 2, token: 'sp', start: 0 })
  expect(r.suggestions.map(x => x.label)).toEqual(['/clear', 'spare:'])
  const a = await raised($).prompt.autocomplete({ text: 'al', cursor: 2, token: 'al', start: 0 })
  expect(a.suggestions[1]).toEqual({ text: 'all: ', label: 'all:', description: 'split across work' })
  const middle = await raised($).prompt.autocomplete({ text: 'hello al', cursor: 8, token: 'al', start: 6 })
  expect(middle.suggestions.length).toBe(1)

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.press({ key: 'chat-all' })
  expect(filled).toEqual([{ text: 'all: fix the login', decorations: [{ start: 0, end: 4, color: 'claude', bold: true }] }])
  await ui.unmount()
  const band = await $.ui.mount({ ...ABOVE, surface: 'terminal' })
  expect(await band.find({ type: 'Text', text: '⇉ All accounts' })).toBeDefined()
  await band.unmount()
})

test('a failure says so, and the pane reads again every 30 seconds', async ($, on) => {
  const clock = mock.clock(on)
  const { calls } = engine(on, { out: '', exitCode: 1 })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.press({ key: 'pause-work' })
  expect((await ui.find({ type: 'Text', text: /^❌/ }))?.text).toBe('❌ Could not pause it.')

  const before = calls.filter(c => c[0] === 'pane').length
  await clock.advance(30_000)
  expect(calls.filter(c => c[0] === 'pane').length).toBe(before + 1)
  await ui.unmount()
})

test('a plugin that is not installed shows the reason instead of empty rows', async ($, on) => {
  const clock = mock.clock(on)
  engine(on, { pane: '{"error": "the multi-account plugin is not installed"}' })
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await clock.settle()

  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect((await ui.find({ type: 'Text', text: /^❌/ }))?.text).toBe('❌ the multi-account plugin is not installed')
  expect(await ui.find({ type: 'Text', text: /have not been read yet/ })).toBeDefined()
  await ui.unmount()
})
