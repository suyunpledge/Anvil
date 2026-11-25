const path = require('node:path')
const fs = require('node:fs/promises')

const packageDir = (name, parent = __dirname) =>
  path.dirname(require.resolve(name + '/package.json', { paths: [parent] }))
const tsx = packageDir('tsx')
const esbuild = packageDir('esbuild', tsx)
const native = packageDir('@esbuild/win32-x64', esbuild)
const tool = (from, name) => ({ from, to: 'runtime/node_modules/' + name, filter: ['**/*', '!node_modules/**/*'] })

module.exports = {
  appId: 'com.anvilide.desktop',
  productName: 'Anvil IDE',
  extraMetadata: { productName: 'Anvil IDE' },
  electronDist: 'node_modules/electron/dist',
  npmRebuild: false,
  directories: { output: 'dist' },
  files: ['out/**/*', 'resources/icon.ico', '!node_modules/**/*'],
  extraResources: [
    tool(tsx, 'tsx'), tool(esbuild, 'esbuild'), tool(native, '@esbuild/win32-x64'),
    tool(packageDir('typescript'), 'typescript'),
    { from: 'node_modules/node-win-x64/bin/node.exe', to: 'runtime/bin/node.exe' },
    { from: 'packaging/bin', to: 'runtime/bin' },
    { from: 'packaging/node-LICENSE.txt', to: 'runtime/node-LICENSE.txt' },
    ...['@xterm/xterm', '@xterm/addon-fit', 'monaco-editor'].map(name => ({
      from: 'node_modules/' + name, to: 'licenses/' + name,
      filter: ['LICENSE*', 'ThirdPartyNotices*']
    }))
  ],
  afterPack: async context => {
    const { rcedit } = await import('rcedit')
    const { productName, version } = context.packager.appInfo
    const executable = path.join(context.appOutDir, productName + '.exe')
    const metadata = {
      icon: path.resolve('resources/icon.ico'),
      'version-string': { FileDescription: productName, ProductName: productName, CompanyName: 'ku-suyun', OriginalFilename: productName + '.exe' },
      'file-version': version, 'product-version': version
    }
    for (let attempt = 0; ; attempt++) {
      try { await rcedit(executable, metadata); break }
      catch (error) {
        if (attempt >= 3 || !String(error).includes('Unable to commit changes')) throw error
        await new Promise(resolve => setTimeout(resolve, 1000 * (attempt + 1)))
      }
    }
    // A locally cached Electron may contain a developer-machine Chromium log.
    await fs.rm(path.join(context.appOutDir, 'debug.log'), { force: true })
  },
  win: { signAndEditExecutable: false, target: [{ target: 'nsis', arch: ['x64'] }], icon: 'resources/icon.ico' },
  nsis: {
    artifactName: 'AnvilIDE-Setup-${version}-win-x64.exe',
    oneClick: true, perMachine: false, runAfterFinish: true,
    createDesktopShortcut: true, createStartMenuShortcut: true,
    include: 'packaging/open-with.nsh', installerLanguages: ['zh_CN', 'en_US']
  }
}

