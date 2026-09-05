/**
 * SimulatorCanvas.tsx — Live simulation viewer.
 *
 * Connects to /ws/simulate/{name}, renders the saved network read-only,
 * animates trains along actual track curves (Catmull-Rom spline sampled by
 * current_position_in_block), and shows live signal / occupancy / weather /
 * maintenance / speed-restriction overlays.
 *
 * Visual language (consistent across all overlays):
 *   - Occupied block      → amber background stripe on that block's track segment
 *   - Speed-capped block  → 🔴 speed-limit icon at block midpoint
 *   - Maintenance block   → cyan hatched stripe
 *   - Weather cell        → translucent coloured region over affected blocks
 *   - Signal green        → #22c55e dot
 *   - Signal caution      → #f59e0b dot  (amber — distinct from both red and green)
 *   - Signal red          → #ef4444 dot
 *   - Train               → coloured rectangle sized by num_carriages; label with name
 *
 * Reuses geometry.ts catmullRomToBezier / densifySpline / samplePolyline for
 * train position interpolation — NOT a straight-line lerp.
 */

import { useEffect, useRef, useState, useCallback } from "react";
import type { CSSProperties } from "react";
import { Stage, Layer, Line, Circle, Rect, Text, Group, Arrow } from "react-konva";
import type { KonvaEventObject } from "konva/lib/Node";
import type { Point, Track, Segment, Station, Junction, SignalState, Block, WeatherCell } from "../types";
import { densifySpline, samplePolyline, flatPoints } from "../editor/utils/geometry";

// ---------------------------------------------------------------------------
// WebSocket snapshot types (mirror _build_tick_snapshot output)
// ---------------------------------------------------------------------------

interface TrainSnap {
  id: string;
  name: string;
  color: string;
  num_carriages: number;
  priority: number;
  status: string;  // PENDING | ACTIVE | SIDING | COMPLETED | CANCELLED | HALTED
  current_block_id: string | null;
  current_position_in_block: number;
  route: Array<{ node_id: string; segment_id: string | null }>;
  schedule: Record<string, {
    scheduled_arrival: string | null;
    scheduled_departure: string | null;
    expected_arrival: string | null;
    actual_arrival: string | null;
  }>;
}

interface BlockSnap {
  id: string;
  segment_id: string;
  length_km: number;
  occupied_by: string | null;
  speed_restriction: number | null;
  maintenance_window: { start_iso: string; end_iso: string } | null;
  weather_cell_id: string | null;
}

interface SignalSnap {
  id: string;
  block_id: string;
  state: "green" | "caution" | "red";
  controlled_by_junction_id: string | null;
}

interface WeatherSnap {
  id: string;
  intensity: number;
  affected_block_ids: string[];
}

interface LayoutGeometry {
  tracks: Array<Track & { geometry: Point[] }>;
  segments: Array<{ id: string; start_node_id: string; end_node_id: string; ordered_block_ids: string[] }>;
  stations: Array<{ id: string; name: string; platform_tracks: string[]; rotation_deg: number; station_type: string }>;
  junctions: Array<{ id: string; connected_segment_ids: string[] }>;
  station_positions: Record<string, { x: number; y: number }>;
  junction_positions: Record<string, { x: number; y: number }>;
  signal_positions: Record<string, { x: number; y: number }>;
  train_positions: Record<string, { x: number; y: number }>;
}

interface SimSnapshot {
  sim_clock: string;
  playing: boolean;
  speed_multiplier: number;
  trains: TrainSnap[];
  blocks: BlockSnap[];
  signals: SignalSnap[];
  weather_cells: WeatherSnap[];
  layout?: LayoutGeometry;
}

// ---------------------------------------------------------------------------
// Signal colour constants
// ---------------------------------------------------------------------------

const SIGNAL_COLOR = {
  green:   "#22c55e",
  caution: "#f59e0b",  // amber — clearly distinct from red and green
  red:     "#ef4444",
} as const;

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const outerStyle: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  height: "100%",
  background: "#0f172a",
  fontFamily: "'Inter', system-ui, sans-serif",
  color: "#e2e8f0",
};

const topBarStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 12,
  padding: "8px 16px",
  background: "#0f172a",
  borderBottom: "1px solid #1e293b",
  flexShrink: 0,
};

const canvasWrapStyle: CSSProperties = {
  flex: 1,
  overflow: "hidden",
  position: "relative",
};

const btnStyle = (active?: boolean): CSSProperties => ({
  background: active ? "#6366f1" : "#1e293b",
  border: "1px solid #334155",
  borderRadius: 6,
  color: "#e2e8f0",
  cursor: "pointer",
  fontSize: 12,
  fontWeight: 600,
  padding: "5px 12px",
  transition: "background 0.12s",
});

const speedBtnStyle = (selected: boolean): CSSProperties => ({
  ...btnStyle(selected),
  padding: "4px 8px",
  fontSize: 11,
});

// ---------------------------------------------------------------------------
// Legend
// ---------------------------------------------------------------------------

function Legend() {
  const items: Array<{ color: string; label: string; shape?: "circle" | "rect" }> = [
    { color: SIGNAL_COLOR.green, label: "Signal: clear", shape: "circle" },
    { color: SIGNAL_COLOR.caution, label: "Signal: caution", shape: "circle" },
    { color: SIGNAL_COLOR.red, label: "Signal: stop", shape: "circle" },
    { color: "rgba(251,191,36,0.25)", label: "Block occupied", shape: "rect" },
    { color: "rgba(6,182,212,0.3)", label: "Maintenance", shape: "rect" },
    { color: "rgba(99,102,241,0.2)", label: "Weather cell", shape: "rect" },
  ];
  return (
    <div style={{ display: "flex", gap: 14, alignItems: "center", marginLeft: "auto", fontSize: 11, color: "#94a3b8" }}>
      {items.map((it) => (
        <span key={it.label} style={{ display: "flex", alignItems: "center", gap: 4 }}>
          <span style={{
            display: "inline-block",
            width: it.shape === "circle" ? 10 : 14,
            height: it.shape === "circle" ? 10 : 8,
            borderRadius: it.shape === "circle" ? "50%" : 2,
            background: it.color,
            border: "1px solid rgba(255,255,255,0.1)",
          }} />
          {it.label}
        </span>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

interface Props {
  layoutName: string;
  onClose?: () => void;
}

export default function SimulatorCanvas({ layoutName, onClose }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 1200, h: 700 });
  const [snap, setSnap] = useState<SimSnapshot | null>(null);
  const [layout, setLayout] = useState<LayoutGeometry | null>(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  // Zoom/pan state
  const [scale, setScale] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });

  // Precomputed densified splines for each track (avoid recomputing every frame)
  const splineCache = useRef<Map<string, Point[]>>(new Map());

  // ── Resize observer ──────────────────────────────────────────────────────
  useEffect(() => {
    if (!containerRef.current) return;
    const ro = new ResizeObserver((entries) => {
      const e = entries[0];
      setSize({ w: e.contentRect.width, h: e.contentRect.height });
    });
    ro.observe(containerRef.current);
    return () => ro.disconnect();
  }, []);

  // ── WebSocket connection ─────────────────────────────────────────────────
  useEffect(() => {
    const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${wsProto}//${window.location.host}/ws/simulate/${encodeURIComponent(layoutName)}`;
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen  = () => { setConnected(true); setError(null); };
    ws.onerror = () => setError("WebSocket connection failed — is the backend running?");
    ws.onclose = (ev) => {
      setConnected(false);
      // Only show an error if we didn't intentionally close it ourselves.
      if (ev.code === 4404) setError(`Layout '${layoutName}' not found — save it in the Editor first.`);
      else if (ev.code === 4500) setError("Server error while building the simulation engine.");
      else if (ev.code !== 1000 && ev.code !== 1001) {
        setError(`Connection lost (code ${ev.code}${ev.reason ? ": " + ev.reason : ""}).`);
      }
    };
    ws.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data) as SimSnapshot;
        if (data.error) {
          setError(String(data.error));
          return;
        }
        if (data.layout) {
          setLayout(data.layout);
        }
        setSnap(data);
      } catch {
        // ignore parse errors
      }
    };

    return () => {
      ws.close();
      wsRef.current = null;
    };
  }, [layoutName]);

  // ── Precompute splines + auto-fit when layout arrives ───────────────────
  useEffect(() => {
    if (!layout) return;
    const cache = new Map<string, Point[]>();
    for (const track of layout.tracks) {
      cache.set(track.id, densifySpline(track.geometry, 20));
    }
    splineCache.current = cache;

    // Auto-fit: compute bounding box of all track geometry points + station positions.
    fitToContent(layout, cache, size);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layout]);

  /** Compute bounding box of all visible geometry and set scale/offset to fit viewport. */
  function fitToContent(
    lay: LayoutGeometry,
    cache: Map<string, Point[]>,
    viewport: { w: number; h: number },
  ) {
    const xs: number[] = [];
    const ys: number[] = [];

    // Track geometry
    for (const track of lay.tracks) {
      const pts = cache.get(track.id) ?? track.geometry;
      for (const p of pts) {
        xs.push(p.x);
        ys.push(p.y);
      }
    }
    // Station and junction positions
    for (const pos of Object.values(lay.station_positions ?? {})) {
      xs.push((pos as { x: number; y: number }).x);
      ys.push((pos as { x: number; y: number }).y);
    }
    for (const pos of Object.values(lay.junction_positions ?? {})) {
      xs.push((pos as { x: number; y: number }).x);
      ys.push((pos as { x: number; y: number }).y);
    }

    if (xs.length === 0) return; // No geometry yet.

    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    const contentW = maxX - minX;
    const contentH = maxY - minY;

    if (contentW === 0 && contentH === 0) return; // All points are the same.

    const PADDING = 60; // pixels of margin around the content
    const newScale = Math.min(
      (viewport.w - 2 * PADDING) / Math.max(contentW, 1),
      (viewport.h - 2 * PADDING) / Math.max(contentH, 1),
      4, // cap zoom-in
    );
    // Centre the content
    const newOffsetX = (viewport.w - contentW * newScale) / 2 - minX * newScale;
    const newOffsetY = (viewport.h - contentH * newScale) / 2 - minY * newScale;

    setScale(newScale);
    setOffset({ x: newOffsetX, y: newOffsetY });
  }

  // ── Control helpers ──────────────────────────────────────────────────────
  const send = useCallback((msg: object) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(msg));
    }
  }, []);

  const handlePlay  = () => send({ action: "play" });
  const handlePause = () => send({ action: "pause" });
  const handleSpeed = (x: number) => send({ action: "set_speed", value: x });

  // ── Wheel zoom ───────────────────────────────────────────────────────────
  const handleWheel = (e: KonvaEventObject<WheelEvent>) => {
    e.evt.preventDefault();
    const scaleBy = 1.1;
    const newScale = e.evt.deltaY < 0 ? scale * scaleBy : scale / scaleBy;
    setScale(Math.max(0.1, Math.min(8, newScale)));
  };

  // ── Derived lookup maps (from snapshot) ─────────────────────────────────
  const blockMap = new Map<string, BlockSnap>((snap?.blocks ?? []).map((b) => [b.id, b]));
  const signalMap = new Map<string, SignalSnap>((snap?.signals ?? []).map((s) => [s.id, s]));

  // Build a map: track_id → blocks in order (from the segment's ordered_block_ids)
  // so we can split the track geometry per block.
  function blocksForTrack(trackId: string): BlockSnap[] {
    if (!layout) return [];
    const track = layout.tracks.find((t) => t.id === trackId);
    if (!track) return [];
    const seg = layout.segments.find((s) => s.id === track.segment_id);
    if (!seg) return [];
    return seg.ordered_block_ids.map((bid) => blockMap.get(bid)).filter(Boolean) as BlockSnap[];
  }

  // ── Train position interpolation ────────────────────────────────────────
  /**
   * Given a train snapshot, return the canvas position using the actual
   * track spline — not a straight-line lerp between block endpoints.
   *
   * Algorithm:
   * 1. Find which track corresponds to the train's current_block_id via segment_id.
   * 2. Get the ordered block list for that segment.
   * 3. Determine this block's fractional range along the full track arc length.
   * 4. Within that range, sample at current_position_in_block.
   */
  function trainPosition(t: TrainSnap): Point | null {
    if (!layout || !t.current_block_id) return null;
    const blk = blockMap.get(t.current_block_id);
    if (!blk) return null;

    // Find the track for this segment
    const track = layout.tracks.find((tr) => tr.segment_id === blk.segment_id);
    if (!track) return null;

    const seg = layout.segments.find((s) => s.id === blk.segment_id);
    if (!seg) return null;

    // Get the densified spline
    const spline = splineCache.current.get(track.id);
    const pts = spline ?? track.geometry;

    const totalBlocks = seg.ordered_block_ids.length;
    const blkIdx = seg.ordered_block_ids.indexOf(t.current_block_id);
    if (blkIdx === -1) return null;

    // Each block occupies 1/totalBlocks of the arc length.
    const blockFrac = 1 / totalBlocks;
    const globalT = (blkIdx + t.current_position_in_block) * blockFrac;

    return samplePolyline(pts, globalT);
  }

  // ── Platform offset for trains at same station ───────────────────────────
  function platformOffset(trainId: string, nodeId: string): number {
    if (!layout || !snap) return 0;
    const sta = layout.stations.find((s) => s.id === nodeId);
    if (!sta) return 0;
    const platformIdx = sta.platform_tracks.indexOf(trainId); // id coincidentally matches
    // Find all ACTIVE trains at this station
    const trainsHere = snap.trains.filter((t) => {
      if (t.status !== "ACTIVE" && t.status !== "HALTED") return false;
      // Check if last node is this station
      const lastHop = t.route.filter((h) => h.node_id === nodeId);
      return lastHop.length > 0;
    });
    const idx = trainsHere.findIndex((t) => t.id === trainId);
    return idx * 20; // 20px vertical offset per simultaneous train
  }

  // ── Weather intensity → colour ───────────────────────────────────────────
  function weatherColor(intensity: number): string {
    if (intensity >= 0.8) return "rgba(220,38,38,0.25)";    // severe — red tint
    if (intensity >= 0.5) return "rgba(234,88,12,0.20)";    // heavy — orange
    if (intensity >= 0.3) return "rgba(202,138,4,0.18)";    // moderate — amber
    return "rgba(99,102,241,0.12)";                          // light — indigo
  }

  // ── Render ───────────────────────────────────────────────────────────────

  if (error) {
    return (
      <div style={{ ...outerStyle, alignItems: "center", justifyContent: "center" }}>
        <p style={{ color: "#ef4444", fontSize: 14 }}>⚠ {error}</p>
        {onClose && <button style={btnStyle()} onClick={onClose}>Close</button>}
      </div>
    );
  }

  return (
    <div style={outerStyle}>
      {/* ── Top bar ─────────────────────────────────────────────────────── */}
      <div style={topBarStyle}>
        <span style={{ fontSize: 12, color: connected ? "#22c55e" : "#ef4444", fontWeight: 600 }}>
          {connected ? "●" : "○"} {connected ? "Live" : "Disconnected"}
        </span>
        {snap && (
          <span style={{ fontSize: 11, color: "#64748b" }}>
            🕐 {new Date(snap.sim_clock).toLocaleTimeString()} | ×{snap.speed_multiplier}
          </span>
        )}
        <button id="sim-btn-play"  style={btnStyle(snap?.playing)} onClick={handlePlay}>▶ Play</button>
        <button id="sim-btn-pause" style={btnStyle(!snap?.playing)} onClick={handlePause}>⏸ Pause</button>
        <span style={{ fontSize: 11, color: "#475569" }}>Speed:</span>
        {[1, 10, 60, 300].map((x) => (
          <button key={x} id={`sim-speed-${x}`} style={speedBtnStyle(snap?.speed_multiplier === x)}
            onClick={() => handleSpeed(x)}>×{x}</button>
        ))}
        <Legend />
        <button
          id="sim-btn-fit"
          style={{ ...btnStyle(), marginLeft: 4 }}
          title="Fit network to viewport"
          onClick={() => layout && fitToContent(layout, splineCache.current, size)}
        >
          ⊡ Fit
        </button>
        {onClose && (
          <button id="sim-btn-close" style={{ ...btnStyle(), marginLeft: 4 }} onClick={onClose}>✕</button>
        )}
      </div>

      {/* ── Canvas ──────────────────────────────────────────────────────── */}
      <div ref={containerRef} style={canvasWrapStyle} id="simulator-canvas-area">
        {!snap && (
          <div style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center",
                        justifyContent: "center", color: "#475569", fontSize: 13 }}>
            Connecting to simulation…
          </div>
        )}
        {snap && layout && (
          <Stage
            width={size.w}
            height={size.h}
            scaleX={scale}
            scaleY={scale}
            x={offset.x}
            y={offset.y}
            onWheel={handleWheel}
            draggable
            onDragEnd={(e) => setOffset({ x: e.target.x(), y: e.target.y() })}
          >
            {/* Layer 1: tracks + block overlays + weather */}
            <Layer>
              {/* Weather cell overlays — drawn first (behind everything) */}
              {snap.weather_cells.map((wc) => (
                <WeatherOverlay
                  key={wc.id}
                  wc={wc}
                  blockMap={blockMap}
                  layout={layout}
                  color={weatherColor(wc.intensity)}
                  splineCache={splineCache.current}
                />
              ))}

              {/* Tracks with per-block occupancy / maintenance / speed overlays */}
              {layout.tracks.map((track) => {
                const spline = splineCache.current.get(track.id);
                const pts = spline ?? track.geometry;
                const blocks = blocksForTrack(track.id);
                const seg = layout.segments.find((s) => s.id === track.segment_id);
                const totalBlocks = seg?.ordered_block_ids.length ?? 1;

                return (
                  <Group key={track.id}>
                    {/* Track ballast shadow (wider, darker) */}
                    <Line
                      points={flatPoints(pts)}
                      stroke="#1e3a5f"
                      strokeWidth={8}
                      tension={0}
                      lineCap="round"
                      lineJoin="round"
                    />
                    {/* Rail line (bright steel grey so it's clearly visible) */}
                    <Line
                      points={flatPoints(pts)}
                      stroke="#94a3b8"
                      strokeWidth={3}
                      tension={0}
                      lineCap="round"
                      lineJoin="round"
                    />

                    {/* Per-block overlays */}
                    {blocks.map((blk, i) => {
                      const t0 = i / totalBlocks;
                      const t1 = (i + 1) / totalBlocks;
                      const midT = (t0 + t1) / 2;
                      const midPt = samplePolyline(pts, midT);

                      const isOccupied = blk.occupied_by !== null;
                      const isMaintenance = blk.maintenance_window !== null;
                      const isSpeedCapped = blk.speed_restriction !== null;

                      // Block start/end points for the stripe
                      const startPt = samplePolyline(pts, t0);
                      const endPt   = samplePolyline(pts, t1);

                      return (
                        <Group key={blk.id}>
                          {/* Occupied block → amber stroke overlay */}
                          {isOccupied && (
                            <Line
                              points={[startPt.x, startPt.y, endPt.x, endPt.y]}
                              stroke="rgba(251,191,36,0.6)"
                              strokeWidth={8}
                              lineCap="round"
                            />
                          )}
                          {/* Maintenance → cyan dashed overlay */}
                          {isMaintenance && (
                            <Line
                              points={[startPt.x, startPt.y, endPt.x, endPt.y]}
                              stroke="rgba(6,182,212,0.7)"
                              strokeWidth={6}
                              dash={[6, 4]}
                              lineCap="round"
                            />
                          )}
                          {/* Speed cap → small red circle marker at midpoint */}
                          {isSpeedCapped && (
                            <>
                              <Circle x={midPt.x} y={midPt.y} radius={8} fill="#ef4444" opacity={0.85} />
                              <Text
                                x={midPt.x - 8} y={midPt.y - 5}
                                text={`${blk.speed_restriction}`}
                                fontSize={7} fill="#fff" fontStyle="bold"
                              />
                            </>
                          )}
                        </Group>
                      );
                    })}
                  </Group>
                );
              })}

              {/* Station nodes */}
              {layout.stations.map((sta) => {
                const pos = layout.station_positions[sta.id];
                if (!pos) return null;
                return (
                  <Group key={sta.id}>
                    <Rect
                      x={pos.x - 14} y={pos.y - 14}
                      width={28} height={28}
                      fill="#1e293b" stroke="#6366f1"
                      strokeWidth={2} cornerRadius={4}
                    />
                    <Text
                      x={pos.x - 24} y={pos.y + 16}
                      text={sta.name} fontSize={9}
                      fill="#94a3b8" align="center" width={48}
                    />
                  </Group>
                );
              })}

              {/* Junction nodes */}
              {layout.junctions.map((jct) => {
                const pos = layout.junction_positions[jct.id];
                if (!pos) return null;
                return (
                  <Circle key={jct.id} x={pos.x} y={pos.y} radius={8}
                    fill="#0f172a" stroke="#f59e0b" strokeWidth={2} />
                );
              })}

              {/* Live signal indicators */}
              {snap.signals.map((sig) => {
                const pos = layout.signal_positions[sig.id];
                if (!pos) return null;
                return (
                  <Group key={sig.id}>
                    <Circle
                      x={pos.x} y={pos.y} radius={6}
                      fill={SIGNAL_COLOR[sig.state] ?? "#94a3b8"}
                      stroke="#0f172a" strokeWidth={1.5}
                    />
                    {/* Pulsing ring for red signals */}
                    {sig.state === "red" && (
                      <Circle x={pos.x} y={pos.y} radius={9}
                        stroke="#ef4444" strokeWidth={1} opacity={0.5} />
                    )}
                  </Group>
                );
              })}
            </Layer>

            {/* Layer 2: Trains */}
            <Layer>
              {snap.trains.filter((t) => t.current_block_id).map((t) => {
                const pos = trainPosition(t);
                if (!pos) return null;

                const W = Math.max(20, t.num_carriages * 4);
                const H = 10;

                return (
                  <Group key={t.id} x={pos.x} y={pos.y}>
                    {/* Train body */}
                    <Rect
                      x={-W / 2} y={-H / 2}
                      width={W} height={H}
                      fill={t.color}
                      stroke="#0f172a" strokeWidth={1}
                      cornerRadius={2}
                      opacity={t.status === "HALTED" || t.status === "SIDING" ? 0.6 : 1}
                    />
                    {/* Label */}
                    <Text
                      x={-W / 2} y={H / 2 + 2}
                      text={t.name}
                      fontSize={8}
                      fill="#e2e8f0"
                      width={W}
                      align="center"
                    />
                    {/* Status badge */}
                    {(t.status === "HALTED" || t.status === "SIDING" || t.status === "CANCELLED") && (
                      <Text
                        x={-W / 2} y={-H / 2 - 10}
                        text={t.status === "HALTED" ? "⏸" : t.status === "SIDING" ? "⬡" : "✕"}
                        fontSize={8} fill="#f59e0b"
                      />
                    )}
                  </Group>
                );
              })}
            </Layer>
          </Stage>
        )}
      </div>

      {/* ── Status bar ──────────────────────────────────────────────────── */}
      {snap && (
        <div style={{ padding: "4px 16px", background: "#0f172a", borderTop: "1px solid #1e293b",
                      fontSize: 11, color: "#475569", display: "flex", gap: 16, flexShrink: 0 }}>
          <span>{snap.trains.filter((t) => t.status === "ACTIVE").length} active trains</span>
          <span>{snap.trains.filter((t) => t.status === "COMPLETED").length} completed</span>
          <span>{snap.trains.filter((t) => t.status === "HALTED" || t.status === "SIDING").length} held</span>
          <span>{snap.blocks.filter((b) => b.occupied_by).length} blocks occupied</span>
          <span style={{ marginLeft: "auto" }}>Layout: <b style={{ color: "#e2e8f0" }}>{layoutName}</b></span>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// WeatherOverlay sub-component — translucent region over affected blocks
// ---------------------------------------------------------------------------

interface WeatherOverlayProps {
  wc: WeatherSnap;
  blockMap: Map<string, BlockSnap>;
  layout: LayoutGeometry;
  color: string;
  splineCache: Map<string, Point[]>;
}

function WeatherOverlay({ wc, blockMap, layout, color, splineCache }: WeatherOverlayProps) {
  // For each affected block, draw a wide highlighted stroke over its track span.
  return (
    <>
      {wc.affected_block_ids.map((bid) => {
        const blk = blockMap.get(bid);
        if (!blk) return null;
        const track = layout.tracks.find((t) => t.segment_id === blk.segment_id);
        if (!track) return null;
        const seg = layout.segments.find((s) => s.id === blk.segment_id);
        if (!seg) return null;
        const spline = splineCache.get(track.id);
        const pts = spline ?? track.geometry;
        const totalBlocks = seg.ordered_block_ids.length;
        const blkIdx = seg.ordered_block_ids.indexOf(bid);
        if (blkIdx === -1) return null;
        const t0 = blkIdx / totalBlocks;
        const t1 = (blkIdx + 1) / totalBlocks;
        const startPt = samplePolyline(pts, t0);
        const endPt   = samplePolyline(pts, t1);
        return (
          <Line
            key={bid}
            points={[startPt.x, startPt.y, endPt.x, endPt.y]}
            stroke={color}
            strokeWidth={20}
            lineCap="round"
            opacity={1}
          />
        );
      })}
    </>
  );
}
