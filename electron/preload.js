// preload.js — the only bridge between the panel's UI and the machine it runs on.
//
// contextIsolation is on and nodeIntegration is off, so the renderer cannot touch
// the filesystem or spawn processes directly. Everything it needs is exposed
// here, by name, with no generic "do anything" passthrough.
//
// Daemon status is NOT proxied through the main process. The renderer fetches
// http://127.0.0.1:17321/status itself, so the panel shows what the daemon
// actually reports rather than what the main process last believed.

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('claudeHUD', {
  // Daemon process supervision, owned by the main process.
  restartDaemon: () => ipcRenderer.invoke('daemon-restart'),
  installHooks:  () => ipcRenderer.invoke('hooks-install'),
  openExternal:  (url) => ipcRenderer.invoke('open-external', url),

  // Pushed from the main process.
  onDaemonLog:    (cb) => ipcRenderer.on('daemon-log',    (_e, line) => cb(line)),
  onDaemonState:  (cb) => ipcRenderer.on('daemon-state',  (_e, s) => cb(s)),
  onHooksResult:  (cb) => ipcRenderer.on('hooks-result',  (_e, r) => cb(r)),

  // The daemon's own HTTP endpoint. Deliberately named as a constant rather
  // than a fetch wrapper: the renderer already knows how to fetch, and wrapping
  // it here would just hide the URL from whoever debugs it.
  daemonUrl: 'http://127.0.0.1:17321',
});
