export type Account = {
  name: string
  email: string | null
  dir: string
  key: 'ready' | 'full' | 'not-signed-in' | 'paused' | 'duplicate' | string
  text: string
  full_until: number | null
  working: number
  login: string | null
}

export type Piece = {
  id: string
  title: string
  account: string
  state: string
  state_text: string
  minutes: number | null
  commits: number | null
  reason: string
}

export type Run = {
  id: string
  task: string
  state: string
  state_text: string
  pieces: Piece[]
  stopping: boolean
  report: string | null
}

export type Artifact = {
  id: string
  url: string | null
  title: string
  account: string
  when_text: string
  copy: string | null
  live: boolean
}

export type Data = {
  now: number
  accounts: Account[]
  run: Run | null
  artifacts: { total: number; per_account: Record<string, number>; newest: Artifact[] }
}

export type Confirm = 'connect' | 'stop' | null

declare module 'claude-code' {
  interface PluginState {
    'multi-account-pane': {
      data: Data | null
      loading: boolean
      note: string
      confirm: Confirm
      /** Whether a surface draws the pane now; the band above the prompt reopens it while false. */
      open: boolean
      /** Where the draft in the chat goes: 'all', one account's name, or null for this session. */
      target: string | null
    }
  }
}
