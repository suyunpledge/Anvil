import type { IdeApi } from './index'

declare global {
  interface Window {
    ide: IdeApi
  }
}

export {}
