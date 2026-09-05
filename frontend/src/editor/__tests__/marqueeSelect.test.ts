/**
 * marqueeSelect.test.ts — Tests that marquee selection correctly identifies
 * only elements whose bounding box intersects the drag rectangle.
 *
 * Tests are against pure geometry utilities — no Konva/canvas needed.
 */

import { describe, it, expect } from "vitest";
import {
  rectsIntersect,
  normaliseRect,
  pointsBBox,
  type Rect,
} from "../utils/geometry";
import type { Point } from "../../types";

describe("normaliseRect", () => {
  it("produces positive w/h regardless of drag direction", () => {
    // Dragged bottom-right to top-left
    const r = normaliseRect(200, 200, 50, 50);
    expect(r).toEqual({ x: 50, y: 50, w: 150, h: 150 });
  });

  it("handles top-left to bottom-right (normal drag direction)", () => {
    const r = normaliseRect(10, 20, 110, 80);
    expect(r).toEqual({ x: 10, y: 20, w: 100, h: 60 });
  });

  it("handles zero-size marquee (click without drag)", () => {
    const r = normaliseRect(50, 50, 50, 50);
    expect(r).toEqual({ x: 50, y: 50, w: 0, h: 0 });
  });
});

describe("rectsIntersect", () => {
  const marquee: Rect = { x: 100, y: 100, w: 200, h: 150 };

  it("returns true for a rect fully inside the marquee", () => {
    const inside: Rect = { x: 120, y: 120, w: 60, h: 40 };
    expect(rectsIntersect(marquee, inside)).toBe(true);
  });

  it("returns true for a rect that partially overlaps", () => {
    const partial: Rect = { x: 250, y: 120, w: 100, h: 60 }; // overlaps right edge
    expect(rectsIntersect(marquee, partial)).toBe(true);
  });

  it("returns true for a rect that fully contains the marquee", () => {
    const container: Rect = { x: 0, y: 0, w: 600, h: 600 };
    expect(rectsIntersect(marquee, container)).toBe(true);
  });

  it("returns false for a rect entirely to the left", () => {
    const left: Rect = { x: 10, y: 120, w: 80, h: 40 };
    expect(rectsIntersect(marquee, left)).toBe(false);
  });

  it("returns false for a rect entirely to the right", () => {
    const right: Rect = { x: 320, y: 120, w: 50, h: 40 };
    expect(rectsIntersect(marquee, right)).toBe(false);
  });

  it("returns false for a rect above", () => {
    const above: Rect = { x: 120, y: 10, w: 60, h: 80 };
    expect(rectsIntersect(marquee, above)).toBe(false);
  });

  it("returns false for a rect below", () => {
    const below: Rect = { x: 120, y: 260, w: 60, h: 40 };
    expect(rectsIntersect(marquee, below)).toBe(false);
  });

  it("returns true for touching edges (boundary = intersecting)", () => {
    // rect touching the right edge of marquee exactly
    const touching: Rect = { x: 300, y: 100, w: 50, h: 50 };
    expect(rectsIntersect(marquee, touching)).toBe(true);
  });
});

describe("pointsBBox", () => {
  it("returns zero rect for empty array", () => {
    expect(pointsBBox([])).toEqual({ x: 0, y: 0, w: 0, h: 0 });
  });

  it("returns zero-size rect for a single point", () => {
    expect(pointsBBox([{ x: 50, y: 80 }])).toEqual({ x: 50, y: 80, w: 0, h: 0 });
  });

  it("computes correct bbox for a diagonal track", () => {
    const pts: Point[] = [{ x: 10, y: 20 }, { x: 100, y: 80 }, { x: 50, y: 150 }];
    const bbox = pointsBBox(pts);
    expect(bbox).toEqual({ x: 10, y: 20, w: 90, h: 130 });
  });
});

// ── Integration: marquee selects correct subset ────────────────────────────

describe("marquee selects only elements inside the drag rectangle", () => {
  const marquee: Rect = { x: 100, y: 100, w: 200, h: 200 };

  const trackInside: Point[]  = [{ x: 120, y: 120 }, { x: 180, y: 180 }];
  const trackOutside: Point[] = [{ x: 10, y: 10 }, { x: 80, y: 80 }];
  const trackPartial: Point[] = [{ x: 80, y: 150 }, { x: 150, y: 150 }]; // straddles left edge

  it("selects track wholly inside the marquee", () => {
    expect(rectsIntersect(marquee, pointsBBox(trackInside))).toBe(true);
  });

  it("does NOT select track wholly outside the marquee", () => {
    expect(rectsIntersect(marquee, pointsBBox(trackOutside))).toBe(false);
  });

  it("selects track that partially overlaps the marquee", () => {
    expect(rectsIntersect(marquee, pointsBBox(trackPartial))).toBe(true);
  });

  it("correctly partitions a mixed set of tracks", () => {
    const tracks = [
      { id: "t1", pts: trackInside },
      { id: "t2", pts: trackOutside },
      { id: "t3", pts: trackPartial },
    ];
    const selected = tracks
      .filter((t) => rectsIntersect(marquee, pointsBBox(t.pts)))
      .map((t) => t.id);

    expect(selected).toContain("t1");
    expect(selected).not.toContain("t2");
    expect(selected).toContain("t3");
    expect(selected).toHaveLength(2);
  });
});
