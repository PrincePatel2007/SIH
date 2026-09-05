/**
 * geometry.ts — Pure geometry utilities for the editor canvas.
 *
 * No React, no Konva imports — these are plain functions so they can be
 * unit-tested without a DOM.
 */

import type { Point } from "../../types";

// ---------------------------------------------------------------------------
// Snap-connect
// ---------------------------------------------------------------------------

/** Maximum pixel distance at which two endpoints are snapped together. */
export const SNAP_DISTANCE_PX = 12;

/**
 * Return *target* snapped to *anchor* if it is within SNAP_DISTANCE_PX,
 * otherwise return *target* unchanged.
 */
export function snapPoint(target: Point, anchor: Point): Point {
  const dx = target.x - anchor.x;
  const dy = target.y - anchor.y;
  if (Math.sqrt(dx * dx + dy * dy) <= SNAP_DISTANCE_PX) {
    return { x: anchor.x, y: anchor.y };
  }
  return target;
}

/**
 * Given a dragged point and a list of candidate anchor points, return the
 * nearest anchor within SNAP_DISTANCE_PX, or null if none qualifies.
 */
export function findSnap(
  dragged: Point,
  anchors: Point[]
): Point | null {
  let best: Point | null = null;
  let bestDist = SNAP_DISTANCE_PX + 1;
  for (const a of anchors) {
    const dx = dragged.x - a.x;
    const dy = dragged.y - a.y;
    const d = Math.sqrt(dx * dx + dy * dy);
    if (d <= SNAP_DISTANCE_PX && d < bestDist) {
      best = a;
      bestDist = d;
    }
  }
  return best;
}

// ---------------------------------------------------------------------------
// Catmull-Rom → cubic Bézier conversion
// ---------------------------------------------------------------------------

/**
 * Convert an array of points into Konva-compatible cubic Bézier segment
 * descriptors using a Catmull-Rom parameterization (alpha = 0.5 = centripetal).
 *
 * Returns a flat array suitable for Konva's <Line tension> — actually we
 * return SVG-style [x0,y0, cp1x,cp1y, cp2x,cp2y, x1,y1, ...] per segment
 * for use with Konva's Path 'd' attribute, but since Konva's <Line> with
 * tension handles splines natively we expose a simpler form too.
 *
 * For Konva <Line> usage, just pass the flat point array and set tension=0.5.
 * This function is used when you need the explicit bezier control points,
 * e.g. to compute positions along the curve.
 */
export function catmullRomToBezier(
  pts: Point[],
  alpha = 0.5
): Array<[Point, Point, Point, Point]> {
  if (pts.length < 2) return [];
  // Pad with phantom endpoints so all segments get control points.
  const p = [pts[0], ...pts, pts[pts.length - 1]];
  const segments: Array<[Point, Point, Point, Point]> = [];

  for (let i = 1; i < p.length - 2; i++) {
    const p0 = p[i - 1];
    const p1 = p[i];
    const p2 = p[i + 1];
    const p3 = p[i + 2];

    const t01 = Math.pow(dist(p0, p1), alpha);
    const t12 = Math.pow(dist(p1, p2), alpha);
    const t23 = Math.pow(dist(p2, p3), alpha);

    const m1 = {
      x: (p2.x - p1.x + t12 * ((p1.x - p0.x) / t01 - (p2.x - p0.x) / (t01 + t12))),
      y: (p2.y - p1.y + t12 * ((p1.y - p0.y) / t01 - (p2.y - p0.y) / (t01 + t12))),
    };
    const m2 = {
      x: (p2.x - p1.x + t12 * ((p3.x - p2.x) / t23 - (p3.x - p1.x) / (t12 + t23))),
      y: (p2.y - p1.y + t12 * ((p3.y - p2.y) / t23 - (p3.y - p1.y) / (t12 + t23))),
    };

    const cp1: Point = {
      x: p1.x + m1.x / 3,
      y: p1.y + m1.y / 3,
    };
    const cp2: Point = {
      x: p2.x - m2.x / 3,
      y: p2.y - m2.y / 3,
    };

    segments.push([p1, cp1, cp2, p2]);
  }
  return segments;
}

/**
 * Evaluate a point at parameter t ∈ [0,1] along a cubic Bézier segment.
 * Used for the arrow-head mid-curve position.
 */
export function evalCubicBezier(
  p0: Point,
  cp1: Point,
  cp2: Point,
  p1: Point,
  t: number
): Point {
  const mt = 1 - t;
  return {
    x: mt * mt * mt * p0.x + 3 * mt * mt * t * cp1.x + 3 * mt * t * t * cp2.x + t * t * t * p1.x,
    y: mt * mt * mt * p0.y + 3 * mt * mt * t * cp1.y + 3 * mt * t * t * cp2.y + t * t * t * p1.y,
  };
}

function dist(a: Point, b: Point): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  return Math.sqrt(dx * dx + dy * dy) || 1e-10;
}

// ---------------------------------------------------------------------------
// Flat point array helpers (for Konva <Line points={...}>)
// ---------------------------------------------------------------------------

/** Flatten [{x,y},...] → [x, y, x, y, ...] for Konva. */
export function flatPoints(pts: Point[]): number[] {
  return pts.flatMap((p) => [p.x, p.y]);
}

// ---------------------------------------------------------------------------
// Bounding-box & hit testing (for marquee select)
// ---------------------------------------------------------------------------

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** Axis-aligned bounding box of an array of points. */
export function pointsBBox(pts: Point[]): Rect {
  if (pts.length === 0) return { x: 0, y: 0, w: 0, h: 0 };
  let minX = pts[0].x, maxX = pts[0].x;
  let minY = pts[0].y, maxY = pts[0].y;
  for (const p of pts) {
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.y > maxY) maxY = p.y;
  }
  return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
}

/** Do two axis-aligned rectangles intersect (or touch)? */
export function rectsIntersect(a: Rect, b: Rect): boolean {
  return (
    a.x <= b.x + b.w &&
    a.x + a.w >= b.x &&
    a.y <= b.y + b.h &&
    a.y + a.h >= b.y
  );
}

/** Normalise a rect so that w and h are always ≥ 0. */
export function normaliseRect(x1: number, y1: number, x2: number, y2: number): Rect {
  return {
    x: Math.min(x1, x2),
    y: Math.min(y1, y2),
    w: Math.abs(x2 - x1),
    h: Math.abs(y2 - y1),
  };
}
