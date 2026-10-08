// builtinFaces.ts — the six compiled-in faces, mirrored for preview.
//
// The firmware's authoritative definitions live in buildFaces() inside
// claude_hud.ino. This is a copy so the UI can show what each state looks like
// before the user replaces it — otherwise "which face am I about to overwrite?"
// has no answer except triggering the hook and squinting at the panel.
//
// A copy is a liability, so it comes with a tripwire: PRIM_COUNTS below is what
// the firmware builds, and the editor checks it against this file. A mismatch is
// shown as a warning in the UI rather than silently rendering the wrong thing.
//
// The values match firmware/claude_hud/claude_hud.ino exactly. If you change one,
// change both, and update PRIM_COUNTS.

import { Expression } from './renderExpression';

export const BUILTIN_FACES: Record<string, Expression> = {
  idle: {
    schema: 1,
    id: 'builtin-idle',
    name: '内置 · 空闲',
    bg: '#DA1100',
    layers: [
      { type: 'rect', color: '#000000', x: 62, y: 70, w: 28, h: 46 },
      { type: 'rect', color: '#000000', x: 150, y: 70, w: 28, h: 46 },
      { type: 'poly', color: '#000000',
        points: [[96, 156], [120, 166], [144, 156]] },
    ],
  },

  thinking: {
    schema: 1,
    id: 'builtin-thinking',
    name: '内置 · 思考',
    bg: '#DA1100',
    layers: [
      { type: 'rect', color: '#000000', x: 62, y: 70, w: 28, h: 46 },
      { type: 'rect', color: '#000000', x: 150, y: 88, w: 28, h: 6 },
      { type: 'circle', color: '#5A5856', cx: 186, cy: 30, r: 3,
        effect: 'blink', period_ms: 1500, on_ms: 500 },
      { type: 'circle', color: '#5A5856', cx: 198, cy: 30, r: 4,
        effect: 'blink', period_ms: 1500, on_ms: 500 },
      { type: 'circle', color: '#5A5856', cx: 210, cy: 30, r: 5,
        effect: 'blink', period_ms: 1500, on_ms: 500 },
    ],
  },

  tool_start: {
    schema: 1,
    id: 'builtin-tool_start',
    name: '内置 · 调用工具',
    bg: '#DA1100',
    layers: [
      { type: 'poly', color: '#000000',
        points: [[62, 66], [90, 93], [62, 120], [62, 66]] },
      { type: 'poly', color: '#000000',
        points: [[178, 66], [150, 93], [178, 120], [178, 66]] },
      { type: 'rect', color: '#5A5856', x: 198, y: 40, w: 4, h: 26 },
      { type: 'rect', color: '#5A5856', x: 188, y: 34, w: 24, h: 9,
        effect: 'pulse', period_ms: 900, amount: 140 },
    ],
  },

  tool_end: {
    schema: 1,
    id: 'builtin-tool_end',
    name: '内置 · 工具完成',
    bg: '#DA1100',
    layers: [
      { type: 'rect', color: '#000000', x: 62, y: 70, w: 28, h: 46 },
      { type: 'rect', color: '#000000', x: 150, y: 70, w: 28, h: 46 },
      { type: 'poly', color: '#50DC82',
        points: [[100, 156], [108, 164], [144, 140], [162, 152]] },
    ],
  },

  waiting: {
    schema: 1,
    id: 'builtin-waiting',
    name: '内置 · 等待输入',
    bg: '#DA1100',
    layers: [
      { type: 'rect', color: '#000000', x: 62, y: 88, w: 28, h: 6 },
      { type: 'rect', color: '#000000', x: 150, y: 88, w: 28, h: 6 },
      { type: 'line', color: '#50A0FF', x: 120, y: 160, x2: 140, y2: 180,
        effect: 'spin', period_ms: 1200, amount: 360 },
    ],
  },

  error: {
    schema: 1,
    id: 'builtin-error',
    name: '内置 · 错误',
    bg: '#DA1100',
    layers: [
      { type: 'line', color: '#FF5050', x: 60, y: 66, x2: 92, y2: 118 },
      { type: 'line', color: '#FF5050', x: 92, y: 66, x2: 60, y2: 118 },
      { type: 'line', color: '#FF5050', x: 148, y: 66, x2: 180, y2: 118 },
      { type: 'line', color: '#FF5050', x: 180, y: 66, x2: 148, y2: 118 },
      { type: 'text', color: '#FF5050', x: 70, y: 140, size: 2, text: 'ERROR' },
    ],
  },

  offline: {
    schema: 1,
    id: 'builtin-offline',
    name: '内置 · 主机离线',
    bg: '#DA1100',
    layers: [
      { type: 'rect', color: '#000000', x: 62, y: 88, w: 28, h: 5 },
      { type: 'rect', color: '#000000', x: 150, y: 88, w: 28, h: 5 },
      { type: 'text', color: '#FFFFFF', x: 58, y: 118, size: 2,
        text: 'WAITING FOR PC' },
    ],
  },
};

/**
 * Layer counts as the firmware builds them. A drift between this and the file
 * above — or between this file and claude_hud.ino — means the preview is lying,
 * and the editor surfaces that instead of showing a confident wrong picture.
 */
export const BUILTIN_PRIM_COUNTS: Record<string, number> = {
  idle: 3,
  thinking: 5,
  tool_start: 4,
  tool_end: 3,
  waiting: 3,
  error: 5,
  offline: 3,
};

/** States in the order the firmware enumerates them. */
export const BUILTIN_ORDER = [
  'idle', 'thinking', 'tool_start', 'tool_end',
  'waiting', 'error', 'offline',
] as const;

// Names of faces whose preview does not match its declared prim count.
export function builtinDrift(): string[] {
  const problems: string[] = [];
  for (const [state, expected] of Object.entries(BUILTIN_PRIM_COUNTS)) {
    const face = BUILTIN_FACES[state];
    if (!face) {
      problems.push(`${state}: missing from BUILTIN_FACES`);
      continue;
    }
    if (face.layers.length !== expected) {
      problems.push(`${state}: ${face.layers.length} layers, expected ${expected}`);
    }
  }
  return problems;
}
