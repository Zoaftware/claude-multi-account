import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Account, Artifact, Confirm, Data, Piece, Run } from '../types'

const PANE = 'multi-account-pane'
const TITLE = 'Claude accounts'
const COMMAND = 'accounts-pane'
const SITE = 'https://zoaftware.com'
const EVERY_MS = 30_000
const WAIT_MS = 60_000
// From this many columns a piece's or an artifact's details share one row; below it they stack.
const COLUMNS = 64
// A chat message that starts with "all:" goes to every ready account; "work:" goes to that one account.
const ALL = 'all'
const WORD = /^(\s*)([\p{L}\p{N}_.-]+):(?=\s|$)\s*/u

const data = atom({ plugin: 'multi-account-pane', key: 'data' } as const, null)
const loading = atom({ plugin: 'multi-account-pane', key: 'loading' } as const, false)
const note = atom({ plugin: 'multi-account-pane', key: 'note' } as const, '')
const confirm = atom({ plugin: 'multi-account-pane', key: 'confirm' } as const, null)
const isOpen = atom({ plugin: 'multi-account-pane', key: 'open' } as const, false)
const target = atom({ plugin: 'multi-account-pane', key: 'target' } as const, null)

// The pane runs the installed multi-account plugin's own CLI: the newest installed version, found by its number
// (1.0.10 after 1.0.9), whatever config folder holds it.
const LOCATOR = `
import glob, json, os, runpy, sys
def version(p):
    try:
        return tuple(int(x) for x in os.path.basename(os.path.dirname(os.path.dirname(p))).split('.'))
    except ValueError:
        return (-1,)
seen = {}
for p in glob.glob(os.path.expanduser('~/.claude*/plugins/cache/*/multi-account/*/lib/ma.py')):
    seen.setdefault(os.path.realpath(p), p)
found = sorted(seen.values(), key=version)
if not found:
    print(json.dumps({"error": "the multi-account plugin is not installed"}))
    sys.exit(3)
path = found[-1]
sys.argv = [path] + sys.argv[1:]
sys.path.insert(0, os.path.dirname(path))
runpy.run_path(path, run_name='__main__')
`

type Tone = 'success' | 'error' | 'warning' | 'inactive'

const SIGN: Record<Tone, string> = { success: '✓', error: '✗', warning: '●', inactive: '■' }

const PIECE: Record<string, [string, Tone]> = {
  pending: ['○', 'inactive'],
  working: ['●', 'warning'],
  relay: ['↻', 'warning'],
  done: ['✓', 'success'],
  failed: ['✗', 'error'],
  stopped: ['■', 'inactive'],
}

const RUN: Record<string, Tone> = {
  ready: 'success',
  'ready-untested': 'success',
  'test-failed': 'error',
  'finished-with-failures': 'error',
  stopped: 'inactive',
  working: 'warning',
}

let timer: { cancel: () => void } | undefined

async function ma($: EngineInterface, args: string[]) {
  const r = await $.process.run(['/usr/bin/python3', '-c', LOCATOR, ...args], {
    cwd: '/tmp',
    timeoutMs: WAIT_MS,
  })
  return { ok: r.exitCode === 0, out: (r.stdout || '').trim(), err: (r.stderr || '').trim() }
}

function firstLine(text: string) {
  const line = text.split('\n').find(l => l.trim()) ?? ''
  return line.trim()
}

async function load($: EngineInterface) {
  await update($, loading, () => true)
  try {
    const r = await ma($, ['pane'])
    const d = JSON.parse(r.out || '{}') as Data & { error?: string }
    if (!r.ok || d.error || !Array.isArray(d.accounts)) {
      await update($, note, () => '❌ ' + (d.error || firstLine(r.err) || 'Could not read the accounts.'))
    } else {
      await update($, data, () => d)
      await update($, note, n => (n.startsWith('❌ Could not read') ? '' : n))
    }
  } catch (err) {
    await update($, note, () => '❌ Could not read the accounts: ' + String(err))
  }
  await update($, loading, () => false)
}

async function openPane($: EngineInterface) {
  const r = await $.ui.open({ id: PANE, title: TITLE })
  await update($, isOpen, () => r.isPlaced)
  void load($)
}

async function action($: EngineInterface, args: string[], ifFails: string) {
  await update($, note, () => '⏳ One moment…')
  try {
    const r = await ma($, args)
    const line = firstLine(r.out) || firstLine(r.err).replace(/^error:\s*/, '')
    await update($, note, () => (r.ok ? line || '✅ Done.' : '❌ ' + (line || ifFails)))
  } catch (err) {
    await update($, note, () => '❌ ' + ifFails + ' ' + String(err))
  }
  await load($)
}

function usable(d: Data | null) {
  const real = (d?.accounts ?? []).filter(c => c.key !== 'duplicate')
  return { real, ready: real.filter(c => c.key === 'ready') }
}

function names(cs: Account[]) {
  const n = cs.map(c => c.name)
  return n.length < 2 ? n.join('') : n.slice(0, -1).join(', ') + ' and ' + n[n.length - 1]
}

// Where a chat draft goes: "all:" or a connected account's name at its start, painted from `from` to `to`;
// the task itself begins at `length`.
function route(text: string, d: Data | null) {
  const m = WORD.exec(text)
  if (!m) return null
  const [whole, space = '', written = ''] = m
  const word = written.toLowerCase()
  const to = word === ALL ? ALL : usable(d).real.find(c => c.name.toLowerCase() === word)?.name
  return to ? { to, from: space.length, end: space.length + written.length + 1, length: whole.length } : null
}

async function mark($: EngineInterface, to: string | null) {
  if ((await read($, target)) !== to) await update($, target, () => to)
}

function instruction(to: string) {
  const who = to === ALL ? 'every ready connected Claude account, at the same time' : `the one Claude account "${to}"`
  const args = to === ALL ? 'the task' : `"--account ${to} " followed by the task`
  return (
    `The person started this message with "${to}:" in the chat: the multi-account pane's way to send a task to ${who}. ` +
    `Run it now with the Skill tool: skill "multi-account:split", args = ${args}, the task being the message after ` +
    `"${to}:" exactly as written. Do not do the task in this session yourself: this session plans the split and watches it.`
  )
}

async function write($: EngineInterface, to: string) {
  const box = await $.prompt.read()
  const before = route(box.text, await read($, data))
  const text = to + ': ' + (before ? box.text.slice(before.length) : box.text.trimStart())
  const r = await $.prompt.fill({ text, decorations: [{ start: 0, end: to.length + 1, color: 'claude', bold: true }] })
  if (r.isFilled) await mark($, to)
  else await update($, note, () => `👉 Type "${to}:" at the start of your message in the chat.`)
}

function accountTone(key: string): Tone {
  if (key === 'ready') return 'success'
  if (key === 'full') return 'error'
  if (key === 'not-signed-in') return 'warning'
  return 'inactive'
}

function capital(text: string) {
  return text.charAt(0).toUpperCase() + text.slice(1)
}

function accountState(c: Account) {
  let text = capital(c.text)
  if (c.key === 'ready') text = 'Ready'
  else if (c.key === 'not-signed-in') text = 'Not signed in'
  else if (c.key === 'paused') text = 'Paused'
  return c.working ? `${text} · ${c.working} working` : text
}

// A status line may open with a mark (✅, ❌, ⏳); the pane draws its own colored sign instead.
function unmarked(text: string) {
  return text.replace(/^[^\p{L}\p{N}]+/u, '')
}

// The time on this computer's clock.
function clockTime(seconds: number) {
  const d = new Date(seconds * 1000)
  return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0')
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: COMMAND,
      description: 'Open the Claude accounts pane: every account, the last run and every account\'s artifacts',
    })
    void openPane($)
    timer?.cancel()
    timer = $.clock.every(EVERY_MS, () => void load($))

    return next(e)
  })

  on('command.run', { command: COMMAND }, async $ => {
    await openPane($)

    return { text: 'Opened the accounts pane beside the chat.' }
  })

  on('ui.close', async ($, e, next) => {
    if (e.id === PANE) await update($, isOpen, () => false)

    return next(e)
  })

  // The chat itself is the task field: "all:" or an account's name at the start of a message sends it there.
  on('prompt.submit', async ($, e, next) => {
    const own = e.origin.kind === 'composer' || e.origin.kind === 'bridge'
    const k = own && !e.command && !e.isMeta ? route(e.text, await read($, data)) : null
    if (!k) return next(e)
    if (!e.text.slice(k.length).trim()) return { drop: `Type the task after "${k.to}:".` }

    await mark($, null)
    return next({ ...e, context: [...(e.context ?? []), instruction(k.to)] })
  })

  on('prompt.edit', async ($, e, next) => {
    const r = await next(e)
    const k = route(r.text, await read($, data))
    await mark($, k?.to ?? null)
    if (!k) return r

    return { ...r, decorations: [...(r.decorations ?? []), { start: k.from, end: k.end, color: 'claude', bold: true }] }
  })

  on('prompt.change', async ($, e, next) => {
    await mark($, route(e.text, await read($, data))?.to ?? null)

    return next(e)
  })

  // The first word of a message offers "all:" and every account, each with where it would go.
  on('prompt.autocomplete', async ($, e, next) => {
    const r = await next(e)
    if (e.start !== 0 || e.token.length < 2 || e.token.includes(':')) return r

    const { real, ready } = usable(await read($, data))
    const t = e.token.toLowerCase()
    const rows = [
      {
        text: ALL + ': ',
        label: ALL + ':',
        description: ready.length ? 'split across ' + names(ready) : 'split (no account is ready now)',
      },
      ...real.map(c => ({ text: c.name + ': ', label: c.name + ':', description: 'this account only · ' + accountState(c) })),
    ].filter(f => f.label.toLowerCase().startsWith(t))

    return rows.length ? { suggestions: [...r.suggestions, ...rows] } : r
  })

  // Above the prompt: where the draft goes while it starts with a route word, else a way back to a closed pane.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey) return next(e)
    const to = await read($, target)
    if (!to && (await read($, isOpen))) return next(e)

    const { Box, Text, Button } = $.ui.resolve(e)
    const d = await read($, data)
    const { real, ready } = usable(d)

    if (to) {
      const c = real.find(x => x.name === to)
      const ok = to === ALL ? ready.length > 0 : c?.key === 'ready'
      const title = to === ALL ? '⇉ All accounts' : '→ ' + to
      const text =
        to === ALL
          ? ok
            ? `Enter splits it across ${names(ready)}, at the same time`
            : 'No account is ready now'
          : ok && c
            ? 'Enter sends it whole to this account'
            : `${c ? accountState(c) : 'Not found'}: it takes no work now`
      return (
        <Box flexDirection="row" gap={1}>
          <Text color="claude" weight="bold" bold>
            {title}
          </Text>
          <Text dimColor={ok} color={ok ? undefined : 'warning'} wrap="truncate-end">
            {text}
          </Text>
        </Box>
      )
    }

    return (
      <Box flexDirection="row" gap={1}>
        <Button key="open-pane" label="Open the accounts pane" variant="secondary" onPress={() => void openPane($)} />
        {d && (
          <Text dimColor wrap="truncate-end">
            {`${ready.length} of ${real.length} ready · start with "all:" to split a task`}
          </Text>
        )}
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Link } = $.ui.resolve(e)
    const d = await read($, data)
    const busy = await read($, loading)
    const message = await read($, note)
    const asks: Confirm = await read($, confirm)
    const width = Math.max(24, e.props.bodyColumns)
    const terminal = e.surface === 'terminal'
    const columns = terminal && width >= COLUMNS

    const { real, ready } = usable(d)
    const duplicates = (d?.accounts ?? []).filter(c => c.key === 'duplicate')
    const run: Run | null = d?.run ?? null
    const arts = d?.artifacts
    const working = run?.state === 'working'
    const example = ready[0]?.name ?? real[0]?.name ?? 'account2'

    // A section opens with its title, a thin rule and its count on one row (the rule only where cells are fixed).
    const section = (title: string, long: string, short = long) => {
      const summary = title.length + long.length + 6 <= width ? long : short
      const rule = Math.max(2, width - title.length - summary.length - (summary ? 2 : 1))
      return (
        <Box flexDirection="row" gap={1} justifyContent={terminal ? 'flex-start' : 'space-between'}>
          <Text weight="bold" bold>
            {title}
          </Text>
          {terminal && (
            <Text dimColor wrap="truncate">
              {'─'.repeat(rule)}
            </Text>
          )}
          {summary !== '' && (
            <Text dimColor wrap="truncate-end">
              {summary}
            </Text>
          )}
        </Box>
      )
    }

    const accountRow = (c: Account) => {
      const tone = accountTone(c.key)
      const button =
        c.key === 'not-signed-in' ? (
          <Button
            key={'login-' + c.name}
            label="Sign in"
            variant="primary"
            onPress={() => void action($, ['connect', c.name], 'Could not open the sign-in window.')}
          />
        ) : c.key === 'paused' ? (
          <Button
            key={'resume-' + c.name}
            label="Resume"
            variant="secondary"
            onPress={() => void action($, ['resume', c.name], 'Could not resume it.')}
          />
        ) : (
          <Button
            key={'pause-' + c.name}
            label="Pause"
            variant="secondary"
            dimColor
            onPress={() => void action($, ['pause', c.name], 'Could not pause it.')}
          />
        )
      return (
        <Box key={'c-' + c.name} flexDirection="column">
          <Box flexDirection="row" gap={1}>
            <Box flexDirection="row" gap={1} flexGrow={1} flexShrink={1}>
              <Text color={tone}>●</Text>
              <Text weight="bold" bold wrap="truncate-end">
                {c.name}
              </Text>
              {c.email && (
                <Text dimColor wrap="truncate-end">
                  {c.email}
                </Text>
              )}
            </Box>
            {button}
          </Box>
          <Box paddingLeft={2}>
            <Text color={tone === 'inactive' ? 'subtle' : tone} wrap="truncate-end">
              {accountState(c)}
            </Text>
          </Box>
        </Box>
      )
    }

    const pieceRow = (p: Piece) => {
      const [sign, tone] = PIECE[p.state] ?? ['•', 'inactive']
      const end = [unmarked(p.state_text), p.minutes === null ? '' : `${p.minutes} min`].filter(Boolean).join(' · ')
      return (
        <Box key={'p-' + p.id} flexDirection="column" paddingLeft={2}>
          <Box flexDirection="row" gap={1}>
            <Text color={tone}>{sign}</Text>
            <Box flexGrow={1} flexShrink={1}>
              <Text wrap="truncate-end">{p.title}</Text>
            </Box>
            <Text dimColor>{columns ? p.account.padEnd(11) + end.padStart(16) : p.account + ' · ' + end}</Text>
          </Box>
          {p.state === 'failed' && p.reason !== '' && (
            <Box paddingLeft={2}>
              <Text color="error" wrap="wrap">
                {p.reason}
              </Text>
            </Box>
          )}
        </Box>
      )
    }

    const artifactRow = (a: Artifact) => {
      const detail = a.account + ' · ' + a.when_text + (a.copy ? '' : ' · no copy')
      const view = (
        <Button
          key={'view-' + a.id}
          label="View"
          variant="secondary"
          dimColor
          onPress={() => void action($, ['view', a.id], 'Could not open it.')}
        />
      )
      return columns ? (
        <Box key={'a-' + a.id} flexDirection="row" gap={2} paddingLeft={2}>
          <Box flexGrow={1} flexShrink={1}>
            <Text wrap="truncate-end">{a.title}</Text>
          </Box>
          <Text dimColor>{detail.padStart(22)}</Text>
          {view}
        </Box>
      ) : (
        <Box key={'a-' + a.id} flexDirection="row" gap={1} paddingLeft={2}>
          <Box flexDirection="column" flexGrow={1} flexShrink={1}>
            <Text wrap="truncate-end">{a.title}</Text>
            <Text dimColor wrap="truncate-end">
              {detail}
            </Text>
          </Box>
          {view}
        </Box>
      )
    }

    const ask = (text: string, yes: string, yesLabel: string, no: string, noLabel: string, onYes: () => void) => (
      <Box flexDirection="column" paddingLeft={2}>
        <Text color="warning" wrap="wrap">
          {text}
        </Text>
        <Box flexDirection="row" gap={1}>
          <Button
            key={yes}
            label={yesLabel}
            variant="primary"
            onPress={() => {
              void update($, confirm, () => null)
              onYes()
            }}
          />
          <Button key={no} label={noLabel} role="dismiss" onPress={() => void update($, confirm, () => null)} />
        </Box>
      </Box>
    )

    const routeRow = (word: string, what: string) => (
      <Box flexDirection="row" gap={1}>
        <Text color="claude" weight="bold" bold>
          {(word + ':').padEnd(columns ? 12 : 0)}
        </Text>
        <Text dimColor wrap="truncate-end">
          {what}
        </Text>
      </Box>
    )

    const perAccount = arts
      ? Object.entries(arts.per_account)
          .map(([k, n]) => `${k} ${n}`)
          .join(' · ')
      : ''
    const runTone = run ? RUN[run.state] ?? 'inactive' : 'inactive'
    const done = run ? run.pieces.filter(p => p.state === 'done').length : 0
    const readAt = d ? 'read at ' + clockTime(d.now) : ''

    return (
      <Box flexDirection="column" width={width} gap={1}>
        <Box flexDirection="column">
          <Box flexDirection="row" justifyContent="space-between" gap={1}>
            <Text weight="bold" bold color="claude">
              {TITLE}
            </Text>
            <Button
              key="refresh"
              label={busy ? 'Reading…' : 'Refresh'}
              hotkey="r"
              plain
              onPress={() => void load($)}
            />
          </Box>
          <Text dimColor wrap="truncate-end">
            {d
              ? `${ready.length} of ${real.length} accounts ready · ${readAt}`
              : busy
                ? 'Reading the accounts…'
                : 'The accounts have not been read yet.'}
          </Text>
          {message !== '' && (
            <Text wrap="wrap" color={message.startsWith('❌') ? 'error' : message.startsWith('✅') ? 'success' : 'text'}>
              {message}
            </Text>
          )}
        </Box>

        <Box flexDirection="column">
          {section('Accounts', `${real.length} connected`)}
          {d && real.length === 0 && (
            <Box paddingLeft={2}>
              <Text dimColor>No account is connected yet.</Text>
            </Box>
          )}
          <Box flexDirection="column" gap={1} marginTop={1}>
            {real.map(accountRow)}
          </Box>
          {duplicates.length > 0 && (
            <Box paddingLeft={2} marginTop={1}>
              <Text dimColor wrap="wrap">
                {'Duplicates (not counted): ' + duplicates.map(c => c.name).join(', ')}
              </Text>
            </Box>
          )}
          <Box marginTop={1} flexDirection="column">
            {asks === 'connect' ? (
              ask('Open a window to sign in with another account?', 'connect-yes', 'Yes, connect', 'connect-no', 'Cancel', () =>
                void action($, ['connect'], 'Could not prepare the new account.'),
              )
            ) : (
              <Box paddingLeft={2}>
                <Button key="connect" label="Connect another account" hotkey="c" plain onPress={() => void update($, confirm, () => 'connect')} />
              </Box>
            )}
          </Box>
        </Box>

        <Box flexDirection="column">
          {section('Work from the chat', ready.length ? 'ready: ' + names(ready) : 'none ready')}
          <Box flexDirection="column" paddingLeft={2}>
            {routeRow(ALL, 'the task is split across the ready accounts, at the same time')}
            {routeRow(example, 'that one account does the whole task')}
            <Text dimColor wrap="wrap">
              With no route word at the start, the message is for this session, as always.
            </Text>
          </Box>
          <Box marginTop={1} paddingLeft={2}>
            <Button key="chat-all" label='Start a message with "all:"' hotkey="a" plain onPress={() => void write($, ALL)} />
          </Box>
        </Box>

        <Box flexDirection="column">
          {section('Last run', run ? `${done} of ${run.pieces.length} done` : '')}
          {!run && (
            <Box paddingLeft={2}>
              <Text dimColor>No task has been split yet.</Text>
            </Box>
          )}
          {run && (
            <Box flexDirection="column">
              <Box flexDirection="row" gap={1} paddingLeft={2}>
                <Text color={runTone}>{SIGN[runTone]}</Text>
                <Text wrap="wrap">{unmarked(run.state_text) + (run.stopping ? ' Stopping…' : '')}</Text>
              </Box>
              {run.task !== '' && (
                <Box paddingLeft={4} marginBottom={1}>
                  <Text dimColor wrap="wrap">
                    {run.task}
                  </Text>
                </Box>
              )}
              {run.pieces.map(pieceRow)}
              {working && !run.stopping && (
                <Box marginTop={1} flexDirection="column">
                  {asks === 'stop' ? (
                    ask('Stop this run? Finished work is kept.', 'stop-yes', 'Yes, stop', 'stop-no', 'Keep going', () =>
                      void action($, ['stop', run.id], 'Could not stop it.'),
                    )
                  ) : (
                    <Box paddingLeft={2}>
                      <Button key="stop" label="Stop the run" hotkey="s" plain onPress={() => void update($, confirm, () => 'stop')} />
                    </Box>
                  )}
                </Box>
              )}
            </Box>
          )}
        </Box>

        <Box flexDirection="column">
          {section('Artifacts', arts ? `${arts.total} · ${perAccount}` : '', arts ? `${arts.total} in all` : '')}
          {arts && arts.newest.length === 0 && (
            <Box paddingLeft={2}>
              <Text dimColor>No artifact copies saved yet.</Text>
            </Box>
          )}
          {(arts?.newest ?? []).slice(0, 5).map(artifactRow)}
          <Box marginTop={1} paddingLeft={2}>
            <Button
              key="gallery"
              label="See every artifact"
              hotkey="g"
              plain
              onPress={() => void action($, ['gallery'], 'Could not build the page.')}
            />
          </Box>
        </Box>

        <Text dimColor wrap="truncate-end">
          {'Made by Zoaftware · '}
          <Link href={SITE} label="zoaftware.com" />
        </Text>
      </Box>
    )
  })
}
