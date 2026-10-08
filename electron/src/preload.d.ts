// preload.d.ts — the type contract for what preload.js exposes on `window`.
//
// Without this, every reference to window.claudeHUD is an implicit `any` that
// compiles (esbuild does not typecheck) and then fails at runtime the moment a
// method is renamed in preload.js. Keeping the declaration next to the code
// that consumes it makes that a compile error instead.
//
// Keep this in sync with electron/preload.js. The two files are deliberately
// the only places that know this shape.

interface ClaudeHudBridge {
  // Daemon process supervision, owned by the main process.
  restartDaemon: () => Promise<void>;
  installHooks: () => Promise<void>;
  openExternal: (url: string) => Promise<void>;

  // Pushed from the main process. Each returns an unsubscribe function.
  onDaemonLog: (cb: (line: string) => void) => () => void;
  onDaemonState: (cb: (state: DaemonState) => void) => () => void;
  onHooksResult: (cb: (result: { ok: boolean; output: string }) => void) => () => void;

  // The daemon's own HTTP base URL. Fetched directly, not proxied.
  daemonUrl: string;
}

interface DaemonState {
  state: string;
  restarts: number;
  startedAt: number | null;
}

interface Window {
  claudeHUD: ClaudeHudBridge;
}
