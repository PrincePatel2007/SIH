/**
 * curvedTrack.test.ts — Tests that Catmull-Rom spline conversion preserves
 * all geometry points: the rendered path passes through every point in the
 * track's geometry list, not just the endpoints.
 *
 * "Passes through" means the closest point on the converted Bézier curve to
 * each geometry point is within a small numerical tolerance (ε).
 */

import { describe, it, expect } from "vitest";
import {
  catmullRomToBezier,
  evalCubicBezier,
  flatPoints,
} from "../utils/geometry";
import type { Point } from "../../types";

// Tolerance: how close (in px) must the curve pass to each geometry point?
// Catmull-Rom by definition passes exactly through its knots, so this should
// be very tight (floating-point rounding only).
const EPSILON = 0.01;

function distSq(a: Point, b: Point): number {
  const dx = a.x - b.x;
  const dy = a.y - b.y;
  return dx * dx + dy * dy;
}

/**
 * Find the minimum distance from *target* to the Bézier curve defined by the
 * four control points, by sampling 200 points along the parameter.
 */
function minDistToCurve(
  target: Point,
  p0: Point, cp1: Point, cp2: Point, p1: Point,
  samples = 200
): number {
  let minD = Infinity;
  for (let i = 0; i <= samples; i++) {
    const t = i / samples;
    const pt = evalCubicBezier(p0, cp1, cp2, p1, t);
    const d = Math.sqrt(distSq(pt, target));
    if (d < minD) minD = d;
  }
  return minD;
}

describe("catmullRomToBezier", () => {
  it("returns empty array for fewer than 2 points", () => {
    expect(catmullRomToBezier([])).toHaveLength(0);
    expect(catmullRomToBezier([{ x: 0, y: 0 }])).toHaveLength(0);
  });

  it("returns 1 segment for exactly 2 points", () => {
    const pts = [{ x: 0, y: 0 }, { x: 100, y: 0 }];
    expect(catmullRomToBezier(pts)).toHaveLength(1);
  });

  it("returns n-1 segments for n points", () => {
    for (let n = 2; n <= 6; n++) {
      const pts: Point[] = Array.from({ length: n }, (_, i) => ({ x: i * 50, y: 0 }));
      expect(catmullRomToBezier(pts)).toHaveLength(n - 1);
    }
  });

  it("curve passes through all geometry points (collinear)", () => {
    // Straight line: all points should be exactly on the curve.
    const pts: Point[] = [
      { x: 0, y: 0 },
      { x: 50, y: 0 },
      { x: 100, y: 0 },
      { x: 150, y: 0 },
    ];
    const segments = catmullRomToBezier(pts);

    // Check that each interior point lies on its adjacent segment.
    // Point pts[0] is the start of segment[0].
    // Point pts[k] is shared between segment[k-1] end and segment[k] start.
    for (let i = 0; i < segments.length; i++) {
      const [p0, cp1, cp2, p1] = segments[i];
      const startDist = Math.sqrt(distSq(p0, pts[i]));
      const endDist = Math.sqrt(distSq(p1, pts[i + 1]));
      expect(startDist).toBeLessThan(EPSILON);
      expect(endDist).toBeLessThan(EPSILON);
    }
  });

  it("curve passes through all geometry points (curved path)", () => {
    // An L-shaped or curved path.
    const pts: Point[] = [
      { x: 0,   y: 0   },
      { x: 100, y: 0   },
      { x: 100, y: 100 },
      { x: 200, y: 100 },
    ];
    const segments = catmullRomToBezier(pts);

    // The knot points (geometry list items) must be the endpoints of adjacent segments.
    for (let i = 0; i < segments.length; i++) {
      const [p0, , , p1] = segments[i];
      // Start of segment i must equal pts[i]
      expect(Math.sqrt(distSq(p0, pts[i]))).toBeLessThan(EPSILON);
      // End of segment i must equal pts[i+1]
      expect(Math.sqrt(distSq(p1, pts[i + 1]))).toBeLessThan(EPSILON);
    }
  });

  it("curve passes through interior points — not just endpoints", () => {
    // This is the key test: the path passes through EVERY geometry point,
    // not just the first and last.
    const pts: Point[] = [
      { x: 0,   y: 0   },
      { x: 100, y: 50  },  // interior
      { x: 200, y: 20  },  // interior
      { x: 300, y: 80  },
    ];
    const segments = catmullRomToBezier(pts);

    // Check that EVERY geometry point is the endpoint of some segment.
    const allEndpoints: Point[] = segments.flatMap(([p0, , , p1]) => [p0, p1]);

    for (const geomPt of pts) {
      const onCurve = allEndpoints.some(
        (ep) => Math.sqrt(distSq(ep, geomPt)) < EPSILON
      );
      expect(onCurve).toBe(true);
    }
  });

  it("Konva tension=0.5 flat array has same point count as input", () => {
    // flatPoints is used to pass geometry to <Line points={...}>.
    // Verify the flat array preserves all points.
    const pts: Point[] = [
      { x: 0, y: 0 }, { x: 50, y: 80 }, { x: 120, y: 30 },
    ];
    const flat = flatPoints(pts);
    expect(flat).toHaveLength(pts.length * 2);
    expect(flat).toEqual([0, 0, 50, 80, 120, 30]);
  });
});

describe("evalCubicBezier", () => {
  it("returns p0 at t=0 and p1 at t=1", () => {
    const p0: Point = { x: 0, y: 0 };
    const cp1: Point = { x: 33, y: 0 };
    const cp2: Point = { x: 66, y: 0 };
    const p1: Point = { x: 100, y: 0 };

    const start = evalCubicBezier(p0, cp1, cp2, p1, 0);
    const end = evalCubicBezier(p0, cp1, cp2, p1, 1);

    expect(start.x).toBeCloseTo(0);
    expect(start.y).toBeCloseTo(0);
    expect(end.x).toBeCloseTo(100);
    expect(end.y).toBeCloseTo(0);
  });

  it("midpoint of a straight-line bezier is at t=0.5", () => {
    const p0: Point = { x: 0, y: 0 };
    const cp1: Point = { x: 33, y: 0 };
    const cp2: Point = { x: 66, y: 0 };
    const p1: Point = { x: 100, y: 0 };
    const mid = evalCubicBezier(p0, cp1, cp2, p1, 0.5);
    expect(mid.x).toBeCloseTo(50, 0);
    expect(mid.y).toBeCloseTo(0);
  });
});
