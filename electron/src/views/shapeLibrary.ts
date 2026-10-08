// shapeLibrary.ts — pick a part, or pick an animation, instead of hand-building
// both out of coordinates.
//
// The workbench used to offer only primitives: to make a face blink you had to
// know that it needs a background-coloured rectangle as an eyelid, that its
// effect is "blink", and that on_ms must be shorter than period_ms. That is a
// lot of knowledge for "I want it to blink".
//
// So the two things people actually choose are now data:
//
//   PARTS      a group of layers that together make one recognisable thing —
//              an eye, an eyelid, a mouth, a blush. One click adds the group.
//   ANIMATIONS a partial layer patch that applies an effect and its tuned
//              defaults. One click animates the selected layer.
//
// Both are pure data with no knowledge of React or the daemon, so they can be
// extended without touching the editor — which is the point.
//
// Keep in step with firmware/claude_hud/config.h: MAX_PRIMS is the ceiling the
// device enforces by silently truncating, so PARTS must stay well under it.

import { Prim, PrimType } from '../render/renderExpression';

/** The device's hard ceiling. Exceeded layers are dropped without an error. */
export const MAX_PRIMS = 16;

/** The frame payload ceiling, and the practical warning line below it. */
export const MAX_BYTES = 4096;
export const BYTE_WARN = 3400;

/**
 * How many simultaneously-animating layers the panel can afford.
 *
 * Not a firmware limit — a physics one. Every animated layer repaints its
 * bounding box each frame, and one full-screen repaint at 40 MHz SPI is about
 * 23 ms against a 33 ms frame budget. Past roughly six moving layers the
 * renderer starts taking time the BLE stack needs, which shows up as a laggy
 * face and a slow state change rather than an error.
 */
export const ANIM_BUDGET = 6;

// ── parts ────────────────────────────────────────────────────────────────────

export interface Part {
  id: string;
  /** Grouping label in the panel. */
  group: '五官' | '装饰' | '文字';
  name: string;
  /** Shown as the button's subtitle, so the choice is not guesswork. */
  hint: string;
  /**
   * The layers this part adds. Written as a function of the background colour
   * so an eyelid or a highlight can match whatever the face is sitting on —
   * a hardcoded "#DA1100" here would be wrong the moment the user changes it.
   */
  build: (bg: string) => Array<Omit<Prim, 'type'> & { type: PrimType }>;
}

const eyeLayers = (bg: string, x: number, y: number, w: number, h: number,
                   round: boolean) => {
  const layers: Array<Omit<Prim, 'type'> & { type: PrimType }> = [];
  if (round) {
    layers.push({ type: 'circle', cx: x + w / 2, cy: y + h / 2, r: w / 2,
                  color: '#000000' });
    // A highlight reads as a glossy eye at this size and costs one layer.
    layers.push({ type: 'circle', cx: x + w / 2 + 6, cy: y + h / 2 - 6, r: 4,
                  color: '#FFFFFF' });
  } else {
    layers.push({ type: 'rect', x, y, w, h, color: '#000000' });
  }
  // The lid covers the eye's whole box, in the background colour, and is drawn
  // last — so when it is down it covers the iris and the highlight too. That is
  // what makes it read as an eye closing rather than a band cut out of the
  // middle of a rectangle, and it is also the difference between this and
  // putting blink on the eye itself: the eye's own blink would leave the pupil
  // and highlight floating on the background.
  layers.push({ type: 'rect', x, y, w, h, color: bg,
                effect: 'blink', period_ms: 4600, on_ms: 170 });
  return layers;
};

export const PARTS: Part[] = [
  {
    id: 'eye-round',
    group: '五官',
    name: '圆眼',
    hint: '含高光，自带眨眼眼皮',
    build: (bg) => eyeLayers(bg, 58, 66, 34, 52, true),
  },
  {
    id: 'eye-square',
    group: '五官',
    name: '方眼',
    hint: '现在的款式，补一个眼皮',
    build: (bg) => eyeLayers(bg, 58, 66, 30, 52, false),
  },
  {
    id: 'eye-spinner',
    group: '五官',
    name: '旋转眼',
    hint: '进度条式绕圈，已带眨眼',
    build: (bg) => [
      { type: 'rect', x: 58, y: 66, w: 34, h: 52, color: '#000000' },
      // A short bar across the eye, spinning: reads as a progress indicator.
      { type: 'line', x: 58, y: 92, x2: 92, y2: 92, color: '#5AC8FA',
        effect: 'spin', period_ms: 1100, amount: 360 },
      // Second bar, offset in phase, so the rotation is visible while it turns.
      { type: 'line', x: 58, y: 92, x2: 92, y2: 92, color: '#FFD60A',
        effect: 'spin', period_ms: 1100, amount: 360 },
      // Same full-coverage lid as the other eyes: the bar and the highlight are
      // inside the box, so a band would slice through them.
      { type: 'rect', x: 58, y: 66, w: 34, h: 52, color: bg,
        effect: 'blink', period_ms: 4600, on_ms: 170 },
    ],
  },
  {
    id: 'mouth-smile',
    group: '五官',
    name: '微笑',
    hint: '两段折线，静态',
    build: () => [
      { type: 'poly', color: '#000000',
        points: [[92, 156], [120, 170], [148, 156]] },
    ],
  },
  {
    id: 'mouth-open',
    group: '五官',
    name: '张嘴',
    hint: '椭圆形，可加呼吸',
    build: () => [
      { type: 'circle', cx: 120, cy: 168, r: 16, color: '#000000' },
    ],
  },
  {
    id: 'mouth-flat',
    group: '五官',
    name: '一字嘴',
    hint: '平静 / 无语',
    build: () => [
      { type: 'line', x: 96, y: 168, x2: 144, y2: 168, color: '#000000' },
    ],
  },
  {
    id: 'blush',
    group: '五官',
    name: '腮红',
    hint: '两条浅色块',
    build: () => [
      { type: 'rect', x: 44, y: 118, w: 22, h: 8, color: '#FF6B6B' },
      { type: 'rect', x: 174, y: 118, w: 22, h: 8, color: '#FF6B6B' },
    ],
  },
  {
    id: 'sweat',
    group: '装饰',
    name: '汗滴',
    hint: '尴尬 / 出错',
    build: () => [
      { type: 'poly', color: '#5AC8FA',
        points: [[196, 44], [206, 62], [196, 74], [186, 62]] },
    ],
  },
  {
    id: 'sparkle',
    group: '装饰',
    name: '星星',
    hint: '闪光，可加闪烁',
    build: () => [
      { type: 'poly', color: '#FFD60A',
        points: [[60, 40], [66, 54], [80, 56], [69, 66], [72, 80],
                 [60, 72], [48, 80], [51, 66], [40, 56], [54, 54]] },
    ],
  },
  {
    id: 'bubble',
    group: '装饰',
    name: '对话气泡',
    hint: '留白配文字用',
    build: () => [
      { type: 'rect', x: 150, y: 96, w: 78, h: 46, color: '#FFFFFF' },
      { type: 'poly', color: '#FFFFFF',
        points: [[158, 140], [172, 140], [160, 154]] },
    ],
  },
  {
    id: 'dots',
    group: '装饰',
    name: '省略号',
    hint: '三点，自带闪烁，可调周期',
    build: () => [
      { type: 'circle', cx: 186, cy: 46, r: 5, color: '#5A5856',
        effect: 'blink', period_ms: 1500, on_ms: 500 },
      { type: 'circle', cx: 198, cy: 46, r: 5, color: '#5A5856',
        effect: 'blink', period_ms: 1500, on_ms: 500 },
      { type: 'circle', cx: 210, cy: 46, r: 5, color: '#5A5856',
        effect: 'blink', period_ms: 1500, on_ms: 500 },
    ],
  },
  {
    id: 'text-ascii',
    group: '文字',
    name: 'ASCII 文字',
    hint: '面板无中文字库，仅英文数字',
    build: () => [
      { type: 'text', x: 70, y: 200, size: 2, text: 'HI', color: '#FFFFFF' },
    ],
  },
];

// ── animations ───────────────────────────────────────────────────────────────

export interface Animation {
  id: string;
  /** Grouping label; matches the way people describe the effect. */
  group: '常用' | '位移' | '缩放' | '显隐';
  name: string;
  /** What it will actually look like, so the pick is not a coin flip. */
  hint: string;
  /** The fields to merge into the selected layer. */
  apply: Partial<Prim>;
}

export const ANIMATIONS: Animation[] = [
  { id: 'none', group: '常用', name: '静止', hint: '去掉动画',
    apply: { effect: 'none', period_ms: 0, on_ms: 0, amount: 100 } },

  { id: 'blink', group: '常用', name: '眨眼', hint: '4.6s 一次，盖 170ms',
    apply: { effect: 'blink', period_ms: 4600, on_ms: 170, amount: 100 } },
  // 快眨 and 追逐闪烁 used to sit here too. They were removed, not merged:
  // all three were the same FX_BLINK with different period/on_ms numbers — the
  // firmware has no per-layer phase, so "chase" was three layers blinking in
  // unison — and clicking any of the three looked identical on the panel.
  // 爆闪 stays: its 600ms cycle is visibly a different effect, not a re-tune.

  // Amounts are in pixels and are deliberately small: a pupil that slides out of
  // its eye reads as a bug, not as looking around. The cap is roughly
  // (eye width - pupil width) / 2.
  { id: 'gaze', group: '位移', name: '左右看', hint: '约 1.2s 一个来回，幅度 7px',
    apply: { effect: 'shake', period_ms: 1200, amount: 7 } },
  { id: 'gaze-slow', group: '位移', name: '慢扫视', hint: '更耐看，2.2s 一个来回',
    apply: { effect: 'shake', period_ms: 2200, amount: 6 } },
  { id: 'dart', group: '位移', name: '快速瞥视', hint: '紧张 / 警觉',
    apply: { effect: 'shake', period_ms: 400, amount: 5 } },

  { id: 'spin-360', group: '缩放', name: '旋转 360°', hint: '一圈一周期',
    apply: { effect: 'spin', period_ms: 1400, amount: 360 } },
  { id: 'spin-180', group: '缩放', name: '旋转 180°', hint: '来回摆',
    apply: { effect: 'spin', period_ms: 1400, amount: 180 } },
  { id: 'spin-slow', group: '缩放', name: '慢转', hint: '待机时的悠闲',
    apply: { effect: 'spin', period_ms: 2600, amount: 360 } },

  { id: 'breathe', group: '缩放', name: '呼吸', hint: '放大到 130%',
    apply: { effect: 'pulse', period_ms: 1200, amount: 130 } },
  { id: 'shrink', group: '缩放', name: '缩小', hint: '缩到 70%',
    apply: { effect: 'pulse', period_ms: 1200, amount: 70 } },
  { id: 'throb', group: '缩放', name: '心跳', hint: '快速胀缩',
    apply: { effect: 'pulse', period_ms: 620, amount: 145 } },

  { id: 'fade', group: '显隐', name: '淡入淡出', hint: '80% 混合',
    apply: { effect: 'fade', period_ms: 1600, amount: 80 } },
  { id: 'strobe', group: '显隐', name: '爆闪', hint: '0.6s 一亮一灭',
    apply: { effect: 'blink', period_ms: 600, on_ms: 300, amount: 100 } },
];

/** Presets, grouped for the panel. */
export const ANIM_GROUPS = ['常用', '位移', '缩放', '显隐'] as const;

export const PART_GROUPS = ['五官', '装饰', '文字'] as const;
