// renderExpression.ts — host-side mirror of the firmware's expression renderer.
//
// This exists for two reasons, and the second is the important one:
//
//   1. The editor needs a live 240x240 preview so a face can be tuned without
//      flashing. Flashing is a multi-minute round trip; iterating on a canvas is
//      instant.
//   2. It is the host-side test for the expression format. Every bug found in
//      the firmware's expression.h or renderer.h so far — 8-bit colours shifted
//      into 5-bit fields, missing channel conversion, silent load failures —
//      could have been caught here without touching the device. Mirroring the
//      semantics means a preview that disagrees with the panel is a bug in one
//      of the two, and both are debuggable on a screen you can see.
//
// Keep this in step with firmware/claude_hud/expression.h and renderer.h. Where
// they differ, the panel is right and this file is wrong — or the reverse, and
// the preview is how you find out.

export type PrimType = 'rect' | 'circle' | 'line' | 'poly' | 'text';
export type Effect = 'none' | 'blink' | 'pulse' | 'shake' | 'spin' | 'fade';

export const EFFECTS: Effect[] = ['none', 'blink', 'pulse', 'shake', 'spin', 'fade'];
export const PRIM_TYPES: PrimType[] = ['rect', 'circle', 'line', 'poly', 'text'];

/** The wire format the firmware parses. Field names match DESIGN.md 5.4. */
export interface Prim {
  type: PrimType;
  color: string;
  // x/y are the rect and text origin, and the line's first endpoint. They are
  // optional because a circle is positioned by cx/cy and a poly by its points,
  // so requiring them forced meaningless zeros onto those types — which is how
  // the built-in face definitions first failed to typecheck.
  x?: number;
  y?: number;
  // rect: w in x2, h in y2 | circle: r in r | line: x2/y2 | text: unused
  w?: number;
  h?: number;
  cx?: number;
  cy?: number;
  r?: number;
  x2?: number;
  y2?: number;
  points?: Array<[number, number]>;
  text?: string;
  size?: number;
  effect?: Effect;
  period_ms?: number;
  on_ms?: number;
  amount?: number;   // pulse/fade: percent, shake: px, spin: degrees

  /**
   * Which graphics-library part contributed this layer.
   *
   * Not sent to the device — the firmware has no concept of a part, it sees a
   * flat layer list, and an unknown field is ignored by its JSON parser. It
   * exists so the editor can show and delete a part as the group it was added
   * as: adding "圆眼" makes one click add three layers, and removing it should
   * not mean hunting those three rows down individually.
   */
  part?: string;
}

export interface Expression {
  schema?: number;
  id?: string;
  name?: string;
  bg?: string;
  layers: Prim[];
}

export const PANEL = { w: 240, h: 240 } as const;
export const MAX_PRIMS = 16;
export const MAX_POINTS = 16;
export const TEXT_MAX_LEN = 24;
export const EXPR_MAX_BYTES = 4096;

// ── colour ───────────────────────────────────────────────────────────────────
function hexToRgb(hex: string): [number, number, number] {
  let h = hex.trim().replace(/^#/, '');
  if (h.length === 3) {
    h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
  }
  if (!/^[0-9a-fA-F]{6}$/.test(h)) return [0, 0, 0];
  const v = parseInt(h, 16);
  return [(v >> 16) & 0xff, (v >> 8) & 0xff, v & 0xff];
}

/** rgb555/565 blend, matching the firmware's blend565(). */
export function blend565(a: string, b: string, t: number): string {
  const [ar, ag, ab] = hexToRgb(a);
  const [br, bgc, bb] = hexToRgb(b);
  // Quantise to the panel's precision first: blending in 8-bit and then
  // truncating is not the same as truncating and then blending.
  const q = (v: number, bits: number) => Math.round((v / 255) * ((1 << bits) - 1));
  const r = Math.round((q(ar, 5) * (255 - t) + q(br, 5) * t) / 255);
  const g = Math.round((q(ag, 6) * (255 - t) + q(bgc, 6) * t) / 255);
  const b2 = Math.round((q(ab, 5) * (255 - t) + q(bb, 5) * t) / 255);
  const to8 = (v: number, bits: number) => Math.round((v / ((1 << bits) - 1)) * 255);
  return `rgb(${to8(r, 5)},${to8(g, 6)},${to8(b2, 5)})`;
}

// ── animation ────────────────────────────────────────────────────────────────
// Mirrors resolvePrim_() in firmware renderer.h. Returns the geometry to draw
// and whether the primitive is visible at this instant.
interface Resolved {
  visible: boolean;
  x: number;
  y: number;
  w: number;
  h: number;
  r: number;
  x2: number;
  y2: number;
  points: Array<[number, number]>;
  color: string;
}

function resolve(prim: Prim, tMs: number, bg: string): Resolved {
  const base: Resolved = {
    visible: true,
    x: prim.x ?? 0,
    y: prim.y ?? 0,
    w: prim.w ?? 0,
    h: prim.h ?? 0,
    r: prim.r ?? 0,
    x2: prim.x2 ?? 0,
    y2: prim.y2 ?? 0,
    points: prim.points ? prim.points.map((p) => [p[0], p[1]] as [number, number]) : [],
    color: prim.color,
  };
  if (prim.type === 'circle') {
    base.x = prim.cx ?? 0;
    base.y = prim.cy ?? 0;
  }

  const effect = prim.effect ?? 'none';
  const period = prim.period_ms ?? 0;
  if (effect === 'none' || period <= 0) return base;

  const phase = tMs % period;

  switch (effect) {
    case 'blink':
      // Visible only for the first on_ms of each period. The firmware's "off"
      // phase means "not drawn", which also clears the previous frame's pixels.
      base.visible = phase < (prim.on_ms ?? 0);
      return base;

    case 'pulse': {
      // Triangle wave, integer-only on the firmware. Rising then falling, so it
      // stays within the declared amount rather than wrapping at the extremes.
      let tri = phase * 2;
      if (tri > period) tri = period * 2 - tri;
      const k = 100 + ((prim.amount ?? 100) - 100) * tri / period;
      if (prim.type === 'rect') {
        const cx = base.x + base.w / 2;
        const cy = base.y + base.h / 2;
        base.w = (base.w * k) / 100;
        base.h = (base.h * k) / 100;
        base.x = cx - base.w / 2;
        base.y = cy - base.h / 2;
      } else if (prim.type === 'circle') {
        base.r = (base.r * k) / 100;   // radius only; centre stays put
      }
      return base;
    }

    case 'shake': {
      // A gaze sweep with dwell at each end, not a vibration.
      //
      // Mirrors FX_SHAKE in firmware renderer.h, breakpoints included. The old
      // version in both places flipped ±amount every 90 ms and ignored
      // period_ms entirely, so the cycle slider did nothing and the layer
      // vibrated at a fixed 90 ms. "Look left, then right" wants to arrive and
      // stay: dwell at each end, spend only the middle travelling.
      const t = phase / period;
      let s: number;
      if (t < 0.30)      s = -1;                                    // look left
      else if (t < 0.50) s = -1 + (2 * (t - 0.30)) / 0.20;          // travel
      else if (t < 0.80) s = 1;                                     // look right
      else               s = 1 - (2 * (t - 0.80)) / 0.20;           // travel back
      base.x += s * (prim.amount ?? 0);
      return base;
    }

    case 'spin': {
      const a = ((tMs * (prim.amount ?? 0)) / period) * (Math.PI / 180);
      const c = Math.cos(a);
      const s = Math.sin(a);
      if (prim.type === 'line') {
        const cx = (base.x + base.x2) / 2;
        const cy = (base.y + base.y2) / 2;
        const dx1 = base.x - cx;
        const dy1 = base.y - cy;
        const dx2 = base.x2 - cx;
        const dy2 = base.y2 - cy;
        base.x = cx + dx1 * c - dy1 * s;
        base.y = cy + dx1 * s + dy1 * c;
        base.x2 = cx + dx2 * c - dy2 * s;
        base.y2 = cy + dx2 * s + dy2 * c;
      } else if (prim.type === 'poly') {
        let cx = 0;
        let cy = 0;
        for (const p of base.points) { cx += p[0]; cy += p[1]; }
        cx /= base.points.length || 1;
        cy /= base.points.length || 1;
        base.points = base.points.map((p) => [
          cx + (p[0] - cx) * c - (p[1] - cy) * s,
          cy + (p[0] - cx) * s + (p[1] - cy) * c,
        ]);
      }
      return base;
    }

    case 'fade': {
      const t = Math.round(((prim.amount ?? 0) * 255) / 100);
      base.color = blend565(prim.color, bg, t);
      return base;
    }

    default:
      return base;
  }
}

// ── drawing ──────────────────────────────────────────────────────────────────
export function drawExpression(
  ctx: CanvasRenderingContext2D,
  expr: Expression,
  tMs: number,
): void {
  const bg = expr.bg ?? '#0A0C10';
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, PANEL.w, PANEL.h);

  for (const prim of expr.layers) {
    const p = resolve(prim, tMs, bg);
    if (!p.visible) continue;
    ctx.fillStyle = p.color;
    ctx.strokeStyle = p.color;

    switch (prim.type) {
      case 'rect':
        ctx.fillRect(p.x, p.y, p.w, p.h);
        break;
      case 'circle':
        ctx.beginPath();
        ctx.arc(p.x, p.y, Math.max(0, p.r), 0, Math.PI * 2);
        ctx.fill();
        break;
      case 'line':
        ctx.beginPath();
        ctx.moveTo(p.x, p.y);
        ctx.lineTo(p.x2, p.y2);
        ctx.lineWidth = 2;   // the firmware's drawLine is effectively 1px; 2 reads better on a preview
        ctx.stroke();
        break;
      case 'poly':
        if (p.points.length >= 2) {
          // Outlines only: the firmware has no fillPolygon, so a preview that
          // filled it would be lying about what the panel does.
          ctx.beginPath();
          ctx.moveTo(p.points[0][0], p.points[0][1]);
          for (let i = 1; i < p.points.length; i++) {
            ctx.lineTo(p.points[i][0], p.points[i][1]);
          }
          ctx.closePath();
          ctx.stroke();
        }
        break;
      case 'text': {
        const size = prim.size ?? 2;
        // The default GFX font is 6x8 per glyph. Match it rather than scaling a
        // system font, or the preview and the panel disagree on layout.
        ctx.font = `${size * 8}px monospace`;
        ctx.textBaseline = 'top';
        ctx.fillText((prim.text ?? '').slice(0, TEXT_MAX_LEN), p.x, p.y);
        break;
      }
    }
  }
}

// ── validation ───────────────────────────────────────────────────────────────
// Enforces the same limits the firmware does, so an upload is never rejected for
// a reason the editor could have shown first.
export function validate(expr: Expression): string[] {
  const problems: string[] = [];
  if (!expr || typeof expr !== 'object') return ['not an object'];
  if (!Array.isArray(expr.layers) || expr.layers.length === 0) {
    problems.push('layers must be a non-empty array');
    return problems;
  }
  if (expr.layers.length > MAX_PRIMS) {
    problems.push(`too many layers: ${expr.layers.length} > ${MAX_PRIMS}`);
  }

  const bytes = new TextEncoder().encode(JSON.stringify(expr)).length;
  if (bytes > EXPR_MAX_BYTES) {
    problems.push(`${bytes} bytes exceeds the ${EXPR_MAX_BYTES} device limit`);
  }

  expr.layers.forEach((l, i) => {
    const at = `layer ${i}`;
    if (!PRIM_TYPES.includes(l.type)) problems.push(`${at}: unknown type '${l.type}'`);
    if (!/^#[0-9a-fA-F]{3,6}$/.test(l.color ?? '')) problems.push(`${at}: bad colour '${l.color}'`);
    if (l.type === 'poly' && (!l.points || l.points.length < 3)) {
      // The firmware rejects a poly with fewer than 3 points.
      problems.push(`${at}: poly needs at least 3 points`);
    }
    if (l.type === 'text' && !(l.text ?? '').length) {
      problems.push(`${at}: empty text`);
    }
    if (l.type === 'circle' && (l.r ?? 0) <= 0) {
      problems.push(`${at}: radius must be positive`);
    }
    if ((l.effect ?? 'none') !== 'none' && (l.period_ms ?? 0) <= 0) {
      problems.push(`${at}: effect needs period_ms > 0`);
    }
  });

  return problems;
}
