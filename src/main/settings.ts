import { app } from 'electron'
import fs from 'node:fs'
import path from 'node:path'

export interface Settings {
  recentFolders: string[]
  recentFiles: string[]
  lastFolder: string | null
  lastFile: string | null
  anvilRepo: string
  anvilPort: number
}

const DEFAULT_ANVIL_REPO = 'C:\\Users\\匡溯昀\\Desktop\\本地工作流\\Anvil'

function defaults(): Settings {
  return {
    recentFolders: [],
    recentFiles: [],
    lastFolder: null,
    lastFile: null,
    anvilRepo: process.env.ANVIL_REPO || DEFAULT_ANVIL_REPO,
    anvilPort: 8099
  }
}

function file(): string {
  return path.join(app.getPath('userData'), 'settings.json')
}

export function loadSettings(): Settings {
  try {
    const raw = fs.readFileSync(file(), 'utf-8')
    const parsed = JSON.parse(raw) as Partial<Settings>
    return { ...defaults(), ...parsed }
  } catch {
    return defaults()
  }
}

export function saveSettings(s: Settings): void {
  fs.mkdirSync(path.dirname(file()), { recursive: true })
  fs.writeFileSync(file(), JSON.stringify(s, null, 2), 'utf-8')
}

export function rememberFolder(folder: string): Settings {
  const s = loadSettings()
  s.recentFolders = [folder, ...s.recentFolders.filter((f) => f !== folder)].slice(0, 8)
  s.lastFolder = folder
  saveSettings(s)
  return s
}

export function rememberFile(file: string): Settings {
  const s = loadSettings()
  s.recentFiles = [file, ...s.recentFiles.filter((f) => f !== file)].slice(0, 8)
  s.lastFile = file
  saveSettings(s)
  return s
}
