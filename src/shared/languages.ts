import type { CompilerInfo } from './types'

type Kind = CompilerInfo['kind']
export const languageNames: Record<Kind, string> = {
  gcc: 'C', 'g++': 'C++', python: 'Python', java: 'Java',
  node: 'JavaScript', typescript: 'TypeScript', kotlin: 'Kotlin'
}

const extensions: Record<string, Kind> = {
  c: 'gcc', cpp: 'g++', cc: 'g++', cxx: 'g++', py: 'python', java: 'java',
  js: 'node', mjs: 'node', cjs: 'node', ts: 'typescript', mts: 'typescript',
  cts: 'typescript', tsx: 'typescript', kt: 'kotlin', kts: 'kotlin'
}

export function languageKind(file: string): Kind | null {
  const ext = /\.([a-z0-9]+)$/i.exec(file)?.[1].toLowerCase()
  return ext && Object.hasOwn(extensions, ext) ? extensions[ext] : null
}

