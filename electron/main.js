// main.js — Electron main process for the Claude HUD control panel.
//
// Responsibilities, in order of importance:
//
//   1. Own the daemon. The panel is only a view; the daemon holds the BLE link,
//      so it must outlive the window. Closing the window hides to tray and
//      leaves the daemon running, because a HUD that stops when you close a
//      window is not a HUD.
//   2. Keep the daemon alive. It restarts on unexpected exit, with backoff, so
//      a crash becomes a brief blip rather than a dead panel.
//   3. Report honestly. The renderer reads the daemon's own /status endpoint
//      rather than anything the main process believes, so there is one source
//      of truth and no state to drift.
//
// The renderer talks to the daemon over plain localhost HTTP/WS. Nothing is
// proxied through the main process: that would add a failure mode for no gain.

const { app, BrowserWindow, Tray, Menu, ipcMain, shell, nativeImage } = require('electron');
const path = require('path');
const http = require('http');
const fs = require('fs');
const { spawn } = require('child_process');

// Resolved and validated before anything uses it. Failing loudly here beats
// starting with undefined paths: every spawn below would silently do nothing,
// and the panel would look perfectly healthy while the daemon never launched.
//
// build.json sits at the project root, but this app lives in a sibling
// directory (electron/ next to the project), so walking up from __dirname is
// not enough. Search the app's own directory, its siblings, then upward — that
// tolerates the project being renamed, which a hardcoded '..\代码' would not.
function findProjectRoot(startDir) {
  const candidates = [startDir, path.join(startDir, '..')];
  try {
    for (const entry of fs.readdirSync(path.join(startDir, '..'))) {
      candidates.push(path.join(startDir, '..', entry));
    }
  } catch { /* unreadable parent: the upward walk below still runs */ }

  let dir = startDir;
  for (let i = 0; i < 5; i++) {
    candidates.push(dir);
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }

  for (const candidate of candidates) {
    try {
      if (fs.existsSync(path.join(candidate, 'build.json'))) return candidate;
    } catch { /* skip an unreadable candidate */ }
  }
  return null;
}

// Resolved and validated only in development. A packaged app has no build.json
// and must not need one: everything it would have configured is baked into the
// frozen daemon, which resolves its own paths at runtime (see
// daemon/hud_daemon/paths.py). Failing loudly here beats starting with
// undefined paths — but only in development, where build.json genuinely exists.
const PROJECT_ROOT = app.isPackaged ? null : findProjectRoot(__dirname);
if (!PROJECT_ROOT && !app.isPackaged) {
  throw new Error(`build.json not found near ${__dirname} — the project may have moved`);
}

const BUILD = PROJECT_ROOT
  ? JSON.parse(fs.readFileSync(path.join(PROJECT_ROOT, 'build.json'), 'utf-8'))
  : null;

// Where `python -m hud_daemon` runs from. Null when packaged, because a packaged
// app has no source tree — it spawns a frozen exe instead (see daemonLaunch).
// Declared here rather than inside daemonLaunch() because the start-at-logon
// path reads it too, and a variable used in two places that is defined in one
// is how the daemon silently fails to start.
const DAEMON_DIR = PROJECT_ROOT ? path.join(PROJECT_ROOT, 'daemon') : null;

// ── where the daemon binary lives ─────────────────────────────────────────────
//
// Two shapes, and they want different things:
//
//   development  the daemon is `python -m hud_daemon` in the source tree, and
//                build.json tells us which interpreter and which shim to use.
//
//   packaged     neither exists. build.json is a developer file with one
//                machine's paths in it, and the recipient has no Python. The
//                daemon is a frozen exe sitting in process.resourcesPath/bin,
//                plus the hook shim beside it.
//
// The old code ran findProjectRoot() unconditionally and threw when build.json
// was missing. In a packaged app it always is, so the app could not start at
// all — it failed loudly, which at least was visible.
//
// isPackaged is the discriminator, not a filesystem guess: app.isPackaged is
// Electron's own statement about how this launch came about.
function daemonLaunch() {
  if (app.isPackaged) {
    // resourcesPath/bin/hud_daemon, laid out by electron-builder's
    // extraResources ("from": "../dist/hud_daemon", "to": "bin").
    const exe = path.join(
      process.resourcesPath,
      'bin',
      process.platform === 'win32' ? 'hud_daemon.exe' : 'hud_daemon',
    );
    if (!fs.existsSync(exe)) {
      // Not a crash-on-purpose: a missing sidecar is a packaging bug the user
      // can do nothing about, so say so plainly rather than throwing.
      log(`frozen daemon missing at ${exe} — the build is incomplete`);
    }
    return { exe, args: ['--debug'], cwd: path.dirname(exe), python: null };
  }

  if (!PROJECT_ROOT) {
    throw new Error(
      `build.json not found near ${__dirname} and the app is not packaged`);
  }
  return {
    python: BUILD.python_exe,
    args: ['-m', 'hud_daemon', '--debug'],
    cwd: DAEMON_DIR,
  };
}

// The daemon owns the hook command, not this process. It used to be computed
// here from build.json, which meant two places had to agree on the same
// quoting rule — and they only noticed when they drifted, because the failure
// is a hook that silently never runs. paths.hookshim_command() on the Python
// side is now the single source, so this function is gone.
//
// The Electron side only ever triggers the daemon to (re)install; see
// repairHooks() below.

const STATUS_URL = 'http://127.0.0.1:17321/status';
const DAEMON_PORT = 17321;

// How often to check the daemon is still answering. Two seconds is slow enough
// that a busy event loop cannot make it look like a crash, and fast enough that
// a dead daemon is back within a few seconds of dying.
const WATCHDOG_MS = 2000;

const RESTART_BACKOFF_MS = [1000, 2000, 5000, 10000, 30000];

let mainWindow = null;
let tray = null;
let daemonProc = null;
// True when we found a daemon already running and did not start it ourselves.
// In that case we must not kill it on quit, and we must not try to restart it.
let adopted = false;
let daemonState = 'stopped';   // stopped | starting | running | crashed
let daemonRestarts = 0;
let daemonStartedAt = null;
let backoffIndex = 0;
let restartTimer = null;
let quitting = false;
// Watchdog state, so the supervision loop survives a restart and more than one
// spawnDaemon() call cannot leave two loops polling the same port.
let watchdogTimer = null;
let watchdogMisses = 0;

// Is another daemon already listening? The panel is often started while the
// user still has a daemon running from a terminal, and spawning a second one
// would make both fight over the UDP port and restart-loop forever.
function probeDaemon() {
  return new Promise((resolve) => {
    const req = http.get(STATUS_URL, { timeout: 1200 }, (res) => {
      res.resume();
      res.on('end', () => resolve(res.statusCode === 200));
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
  });
}

// ── daemon supervision ───────────────────────────────────────────────────────
// The daemon is detached and unref'd, so this process gets no exit event for it:
// a daemon that dies looks exactly like one that never started. Polling the port
// is the only signal available, which makes this loop the app's sole supervision.
//
// It deliberately lives here and not inside spawnDaemon(). Placing it in the
// spawn path meant an adopted daemon — one already running when the app started,
// from logon or from a terminal — skipped it entirely, because adopting returns
// early. A daemon that died afterwards was then never noticed and never
// restarted, and the panel sat on "daemon 没连上" with nothing explaining why.
// Supervision is an app-level concern, so it is started once, from whenReady,
// whether or not this process spawned anything.
//
// Three consecutive misses before acting: a single miss is far more likely to be
// a busy event loop than a dead process, and restarting a healthy daemon drops
// the BLE link for no reason.
function startWatchdog() {
  if (watchdogTimer) return;          // one loop, however many times this runs
  watchdogMisses = 0;
  let wasUp = false;

  watchdogTimer = setInterval(async () => {
    if (quitting) return;
    let up = false;
    try {
      const res = await fetch(`http://127.0.0.1:${DAEMON_PORT}/status`,
                              { signal: AbortSignal.timeout(1200) });
      up = res.ok;
    } catch { /* not answering */ }

    if (up) {
      watchdogMisses = 0;
      if (!wasUp) {
        log('daemon is answering on 17321');
        wasUp = true;
      }
      if (daemonState !== 'running') {
        daemonState = 'running';
        daemonStartedAt = daemonStartedAt ?? Date.now();
        backoffIndex = 0;
        broadcastDaemonState();
      }
      return;
    }

    watchdogMisses += 1;
    if (wasUp) {
      wasUp = false;
      log(`daemon stopped answering (miss ${watchdogMisses})`);
    }
    if (daemonState !== 'crashed') {
      daemonState = 'crashed';
      broadcastDaemonState();
    }
    if (watchdogMisses >= 3) {
      watchdogMisses = 0;      // restart, then grant the same grace period
      log('daemon is not coming back; restarting it');
      daemonProc = null;
      adopted = false;         // it is gone, so there is nothing left to adopt
      spawnDaemon();
    }
  }, WATCHDOG_MS);
}

async function spawnDaemon() {
  if (daemonProc) return;

  // Adopt an existing daemon rather than competing with it. The manual terminal
  // daemon is the one the user is watching, and killing it from here would be
  // far more confusing than leaving it alone.
  if (await probeDaemon()) {
    log('an existing daemon answered on 17321; adopting it instead of spawning one');
    daemonState = 'running';
    daemonStartedAt = null;
    adopted = true;
    broadcastDaemonState();
    return;
  }

  const launch = daemonLaunch();
  if (!launch) {
    log('cannot start the daemon: no launch configuration for this shape');
    daemonState = 'crashed';
    broadcastDaemonState();
    return;
  }

  log(`spawning daemon: ${launch.exe || launch.python} (cwd ${launch.cwd})`);
  daemonState = 'starting';
  broadcastDaemonState();

  // Detached: the daemon outlives this window. That is the point of a HUD — it
  // reports while the panel is closed — but it also means this process can no
  // longer stop it, so the tray's 退出 writes a sentinel file instead of
  // relying on a process handle.
  //
  // stdio ignored: an owned pipe would make the child's lifetime depend on
  // this parent closing it.
  const cmdArgs = launch.python ? [launch.python, ...launch.args] : [launch.exe];
  daemonProc = spawn(cmdArgs[0], cmdArgs.slice(1), {
    cwd: launch.cwd,
    stdio: 'ignore',
    detached: true,
    windowsHide: true,
  });
  daemonProc.unref();
  daemonProc.on('error', (err) => {
    log(`daemon spawn failed: ${err.message}`);
    daemonState = 'crashed';
    scheduleRestart();
  });

  // No watchdog here. It is started once from whenReady (see startWatchdog),
  // because the adopt path above returns before this point and would otherwise
  // leave an adopted daemon unsupervised.

  let stdoutTail = '';
  if (daemonProc.stdout) {
    daemonProc.stdout.on('data', (chunk) => {
      stdoutTail += chunk.toString();
      const lines = stdoutTail.split('\n');
      stdoutTail = lines.pop() ?? '';
      for (const line of lines) {
        if (line.trim()) send('daemon-log', line);
      }
    });
  }

  if (daemonProc.stderr) {
    daemonProc.stderr.on('data', (chunk) => {
      for (const line of chunk.toString().split('\n')) {
        if (line.trim()) send('daemon-log', `[stderr] ${line}`);
      }
    });
  }

  daemonProc.on('error', (err) => {
    log(`daemon spawn failed: ${err.message}`);
    daemonState = 'crashed';
    scheduleRestart();
  });

  daemonProc.on('exit', (code, signal) => {
    daemonProc = null;
    if (quitting) {
      daemonState = 'stopped';
    } else if (!adopted) {
      daemonState = 'crashed';
      daemonRestarts += 1;
      log(`daemon exited (code=${code} signal=${signal}); will restart`);
      scheduleRestart();
    }
    broadcastDaemonState();
  });
}

function scheduleRestart() {
  if (restartTimer || quitting) return;
  const delay = RESTART_BACKOFF_MS[Math.min(backoffIndex, RESTART_BACKOFF_MS.length - 1)];
  backoffIndex += 1;
  log(`daemon restart in ${delay}ms`);
  restartTimer = setTimeout(() => { restartTimer = null; spawnDaemon(); }, delay);
}

// ── stopping a detached daemon ───────────────────────────────────────────────
//
// A detached process is no longer a child, so there is no handle to signal. The
// daemon polls for this file and shuts itself down when it appears, which is the
// only way to stop it that works the same whether the UI started it or it came
// up at logon by itself.
//
// Two rules that make it safe, both learned the hard way:
//
//   1. Only write it when a daemon is actually listening. An app that quits with
//      no daemon running used to leave the file behind, and the next daemon to
//      start would exit immediately — a HUD that looks permanently dead, with
//      nothing in any log to say why.
//   2. The daemon ignores a request older than a minute, so even a file that
//      slips through cannot brick a later start. Both sides guard the same
//      failure because only one of them is enough.
function stopSentinel() {
  const base = process.env.LOCALAPPDATA
    || path.join(process.env.APPDATA || process.env.USERPROFILE || '.', '.local');
  return path.join(base, 'ClaudeHUD', 'stop');
}

async function requestDaemonStop() {
  // Nothing listening means nothing to stop, and writing the file anyway is
  // exactly how a stale request gets left to bite the next start.
  if (!(await probeDaemon())) return false;

  try {
    fs.mkdirSync(path.dirname(stopSentinel()), { recursive: true });
    fs.writeFileSync(stopSentinel(), String(Date.now()), 'utf8');
    return true;
  } catch (err) {
    log(`could not write the stop sentinel: ${err.message}`);
    return false;
  }
}

async function stopDaemon() {
  if (restartTimer) { clearTimeout(restartTimer); restartTimer = null; }
  // The watchdog's whole job is to restart a daemon that is not answering, so
  // leaving it running across an intentional stop would immediately undo the
  // stop. This is what makes 退出 actually quit.
  if (watchdogTimer) { clearInterval(watchdogTimer); watchdogTimer = null; }
  // An adopted daemon was started by someone else — the terminal the user is
  // watching, or logon before this app ran. Stopping it from here would kill
  // something the user did not ask to close.
  if (adopted) return;
  // Async because it probes the port first: writing a stop request with no
  // daemon to read it is how a stale one gets left behind.
  if (!(await requestDaemonStop())) return;
  daemonState = 'stopped';
  broadcastDaemonState();
}

// A restart needs the daemon gone before the replacement starts, and the
// sentinel is polled, so wait for the port to actually go quiet rather than
// guessing at it. Two new daemons fighting over the BLE link is a worse failure
// than a restart that takes an extra second.
async function restartDaemon() {
  await stopDaemon();
  const deadline = Date.now() + 8000;
  while (Date.now() < deadline) {
    try {
      const res = await fetch('http://127.0.0.1:17321/status',
                              { signal: AbortSignal.timeout(800) });
      if (res.ok) { await new Promise((r) => setTimeout(r, 400)); continue; }
    } catch { return spawnDaemon(); }
  }
  log('daemon did not stop in time; starting a second one');
  spawnDaemon();
}

// ── start at logon, independent of this window ───────────────────────────────
//
// Registered through Electron's own login-item API rather than by writing a
// registry Run key: it handles the per-user vs per-machine distinction, and its
// path is the installed app's executable, which does not move on update.
//
// The daemon's own detach + this entry are what make "close the panel and it
// keeps working" true. Both are needed: the entry starts it at next logon, the
// detach keeps it alive when this window closes before then.
function ensureDaemonAutostart() {
  try {
    const settings = app.getLoginItemSettings();
    if (!settings.openAtLogin) {
      app.setLoginItemSettings({ openAtLogin: true });
      log('registered for start at logon');
    }
  } catch (err) {
    // Not fatal: the daemon can still be started by hand from the tray.
    log(`could not register start at logon: ${err.message}`);
  }
}

// ── window ───────────────────────────────────────────────────────────────────
function createWindow() {
  mainWindow = new BrowserWindow({
    // Wider and taller than the old status-only panel: the editor needs a
    // canvas, an inspector and a library side by side, and squeezing them into
    // 460px made every control two rows tall.
    width: 860,
    height: 720,
    minWidth: 720,
    minHeight: 620,
    show: false,
    autoHideMenuBar: true,
    icon: appIcon(),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadFile(path.join(__dirname, 'dist', 'index.html'));
  mainWindow.once('ready-to-show', () => mainWindow.show());

  mainWindow.on('close', (e) => {
    // Hide rather than quit: the panel is a view of a service that keeps running.
    if (!quitting) { e.preventDefault(); mainWindow.hide(); }
  });
  mainWindow.on('closed', () => { mainWindow = null; });
}

// The tray and window icon. .ico on Windows, .png elsewhere — and the file has
// to exist, because a Tray built from an empty image renders nothing to
// right-click, which silently removes the only route to "显示面板" and "退出".
// Referencing a file that is not in assets/ produced exactly that, with only a
// WARNING in the log to show for it.
function appIcon() {
  const ico = path.join(__dirname, 'assets', 'icon.ico');
  if (process.platform === 'win32' && fs.existsSync(ico)) return ico;
  const png = path.join(__dirname, 'assets', 'icon.png');
  if (fs.existsSync(png)) return png;
  // Last resort: the icns ships in assets/ and Electron will take it on macOS.
  const icns = path.join(__dirname, 'assets', 'icon.icns');
  if (fs.existsSync(icns)) return icns;
  return '';
}

// ── tray ─────────────────────────────────────────────────────────────────────
function createTray() {
  const iconPath = appIcon();
  // A missing icon must not take the tray down: the tray is the only way back
  // to the panel once the window is hidden, so an empty image is better than no
  // tray at all.
  const raw = iconPath ? nativeImage.createFromPath(iconPath) : nativeImage.createEmpty();
  const icon = raw.isEmpty() ? nativeImage.createEmpty() : raw.resize({ width: 16, height: 16 });
  if (icon.isEmpty()) {
    log('tray icon unavailable; the tray will be blank but still present');
  }
  tray = new Tray(icon);
  tray.setToolTip('Claude HUD');
  // ── daemon at logon, independent of this window ────────────────────────────
  //
  // The daemon used to be a child of this process, which made a HUD that stops
  // when you close a window: closing the panel killed the BLE link, and a HUD
  // whose whole job is to sit there reporting did nothing whenever it was not
  // looked at.
  //
  // So the process is detached and started at logon, and this window attaches to
  // it rather than owning it. Two details that matter:
  //
  //   detached: true  +  stdio: 'ignore'
  //        Without both, the daemon is still parented to this process and dies
  //        with it — Node's subprocess bookkeeping holds it either way.
  //   The registry Run key, not the portable path
  //        Registered once at install-time-fixed location (%LOCALAPPDATA%\...)
  //        so an app update under a new directory does not orphan the entry.
  //
  // A process the user did not start and cannot see must still be stoppable:
  // the tray's 退出 writes a sentinel file first, and the daemon notices it and
  // exits on its own. Killing it from here is the alternative, but a detached
  // process is no longer a child and there is no reliable handle to kill.
  ensureDaemonAutostart();

  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '显示面板', click: () => mainWindow?.show() },
    { type: 'separator' },
    { label: '重启 daemon', click: () => { restartDaemon(); } },
    { label: '安装 / 修复 hook', click: () => repairHooks() },
    { type: 'separator' },
    { label: '开机自启', checked: app.getLoginItemSettings().openAtLogin, type: 'checkbox',
      click: (item) => app.setLoginItemSettings({ openAtLogin: item.checked }) },
    { type: 'separator' },
    { label: '退出', click: () => {
        // awaiting before quit(): the stop request is written to disk, and a
        // process that exits before the write lands leaves the daemon running.
        quitting = true;
        void stopDaemon().finally(() => app.quit());
      } },
  ]));
  tray.on('double-click', () => mainWindow?.show());
}

// ── hook / watcher helpers ───────────────────────────────────────────────────
// The daemon owns the hook injector and its watcher (see hud_daemon/__main__.py),
// so there is deliberately no second watcher here. An earlier revision spawned
// one from the tray, which meant two processes polling the same settings.json
// and both trying to inject: each saw the other's write as an external change,
// so the file was rewritten far more often than anything had actually changed.
//
// Repair used to be `python -m hud_daemon.settings_watch --once`, a fresh
// interpreter per button press. That is a non-starter once packaged: the
// recipient has no Python at all, and the daemon that is already running owns
// the injector anyway. It is an HTTP call now, over the same localhost API the
// renderer uses.
function repairHooks() {
  const url = `http://127.0.0.1:${DAEMON_PORT}/hooks/repair`;
  const req = http.request(url, { method: 'POST', timeout: 8000 }, (res) => {
    let body = '';
    res.on('data', (c) => { body += c.toString(); });
    res.on('end', () => {
      let ok = res.statusCode === 200;
      let output = body;
      try {
        const parsed = JSON.parse(body);
        ok = parsed.ok !== false && res.statusCode === 200;
        output = parsed.changed
          ? 'hook 已重新注入'
          : 'hook 已就位，无需改动';
      } catch { /* leave body as output */ }
      send('hooks-result', { ok, output });
    });
  });
  req.on('error', (e) => send('hooks-result',
    { ok: false, output: `daemon 未运行，无法修复 hook: ${e.message}` }));
  req.on('timeout', () => { req.destroy(); });
  req.end();
}

// ── renderer bridge ──────────────────────────────────────────────────────────
function send(channel, payload) {
  mainWindow?.webContents.send(channel, payload);
}

function broadcastDaemonState() {
  send('daemon-state', { state: daemonState, restarts: daemonRestarts, startedAt: daemonStartedAt });
}

function log(msg) {
  console.log(`[main] ${msg}`);
  send('daemon-log', `[main] ${msg}`);
}

ipcMain.handle('hooks-install', () => repairHooks());
// restartDaemon() already waits for the port to go quiet before starting the
// replacement. The old inline version stopped the daemon and blindly spawned a
// new one 500 ms later, which occasionally overlapped the two.
ipcMain.handle('daemon-restart', () => { void restartDaemon(); });
ipcMain.handle('open-external', (_e, url) => shell.openExternal(url));

// ── lifecycle ────────────────────────────────────────────────────────────────
app.whenReady().then(() => {
  app.setLoginItemSettings({ openAtLogin: true });   // default on, per the user
  createTray();
  createWindow();
  spawnDaemon();
  // Supervision is independent of the spawn: it has to cover the adopted case
  // just as much as the spawned one, which is the whole reason it lives out here.
  startWatchdog();
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});

app.on('before-quit', () => { quitting = true; });
app.on('window-all-closed', () => { /* stay resident: the tray owns the app */ });

// Stop the daemon on the way out — but only if one is actually running.
// requestDaemonStop() probes the port first, so this cannot leave a stop request
// behind for a daemon that never existed; that leftover used to make every later
// daemon exit on start-up, which is the "HUD is permanently dead" bug.
//
// It cannot be awaited: Electron does not wait for quit handlers, and a daemon
// that is still shutting down when the process dies loses its BLE link to the OS
// rather than to its own cleanup.
app.on('quit', () => { void stopDaemon(); });

// A second instance would fight the first for the BLE link through its daemon.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => { if (mainWindow) mainWindow.show(); });
}
