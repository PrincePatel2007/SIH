/**
 * SimulatorCanvas.tsx — Live simulation viewer.
 *
 * Phase 9 additions:
 *  - Hover: hovering a train shows a lilac aura over its route tracks (no grey-out).
 *  - Select: clicking a train highlights its route + dims everything else.
 *    Clicking empty space deselects.
 *  - Arrival popup: HTML overlay near station when a train is ≤ ARRIVING_THRESHOLD
 *    sim-minutes from its next scheduled stop.  SHOW_ARRIVALS_FOR_ALL_TRAINS toggles
 *    between all-trains and selected-only.
 *  - SidePanel: shown when a train is selected (right of canvas).
 *  - Dark / light theme via ThemeContext.
 *
 * Visual language:
 *   Hover  → lilac aura (#c4b5fd / #7c3aed) on route tracks.  Nothing else changes.
 *   Select → route tracks bright indigo, non-route elements dimmed to dimOpacity.
 *   Arrival popup → small badge near the destination station.
 */

import { useEffect, useRef, useState, useCallback } from "react";
import type { CSSProperties } from "react";
import { Stage, Layer, Line, Circle, Rect, Text, Group } from "react-konva";
import type { KonvaEventObject } from "konva/lib/Node";
import type { Point, Track } from "../types";
import { densifySpline, samplePolyline, flatPoints } from "../editor/utils/geometry";
import { useTheme } from "../theme";
import SidePanel from "./SidePanel";

// ─────────────────────────────────────────────────────────────────────────────
// Arrival-popup configuration (edit these two constants to tune behaviour)
// ─────────────────────────────────────────────────────────────────────────────

/** Sim-minutes before a scheduled stop at which the "arriving" popup appears. */
const ARRIVING_THRESHOLD_SIM_MINUTES = 2;

/**
 * When true: show arrival popups for ALL active trains.
 * When false: show only for the currently-selected train.
 */
const SHOW_ARRIVALS_FOR_ALL_TRAINS = true;

// ─────────────────────────────────────────────────────────────────────────────
// Snapshot types (mirror _build_tick_snapshot output)
// ─────────────────────────────────────────────────────────────────────────────

interface TrainSnap {
  id: string;
  name: string;
  color: string;
  num_carriages: number;
  priority: number;
  status: string;
  driver_duty_status?: string;
  current_block_id: string | null;
  current_position_in_block: number;
  route: Array<{ node_id: string; segment_id: string | null }>;
  schedule: Record<string, {
    scheduled_arrival:   string | null;
    scheduled_departure: string | null;
    expected_arrival:    string | null;
    actual_arrival:      string | null;
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

interface EventLogEntry {
  timestamp:  string;
  event_type: string;
  train_id:   string | null;
  node_id:    string | null;
  block_id:   string | null;
  detail:     string;
}

interface LayoutGeometry {
  tracks: Array<Track & { geometry: Point[] }>;
  segments: Array<{ id: string; start_node_id: string; end_node_id: string; ordered_block_ids: string[] }>;
  stations: Array<{ id: string; name: string; platform_tracks: string[]; rotation_deg: number; station_type: string }>;
  junctions: Array<{ id: string; connected_segment_ids: string[] }>;
  station_positions:  Record<string, { x: number; y: number }>;
  junction_positions: Record<string, { x: number; y: number }>;
  signal_positions:   Record<string, { x: number; y: number }>;
  train_positions:    Record<string, { x: number; y: number }>;
}

interface SimSnapshot {
  sim_clock: string;
  playing: boolean;
  speed_multiplier: number;
  trains: TrainSnap[];
  blocks: BlockSnap[];
  signals: SignalSnap[];
  weather_cells: WeatherSnap[];
  event_log?: EventLogEntry[];
  layout?: LayoutGeometry;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  error?: any;
}

// ─────────────────────────────────────────────────────────────────────────────
// Legend
// ─────────────────────────────────────────────────────────────────────────────

function Legend() {
  const { theme } = useTheme();
  const items = [
    { color: theme.signalGreen,   label: "Signal: clear",   circle: true },
    { color: theme.signalCaution, label: "Signal: caution", circle: true },
    { color: theme.signalRed,     label: "Signal: stop",    circle: true },
    { color: theme.occupiedStroke,    label: "Block occupied", circle: false },
    { color: theme.maintenanceStroke, label: "Maintenance",    circle: false },
  ];
  return (
    <div style={{ display: "flex", gap: 12, alignItems: "center", marginLeft: "auto", fontSize: 10, color: theme.textSecondary }}>
      {items.map((it) => (
        <span key={it.label} style={{ display: "flex", alignItems: "center", gap: 4 }}>
          <span style={{
            display: "inline-block",
            width: it.circle ? 9 : 12, height: it.circle ? 9 : 7,
            borderRadius: it.circle ? "50%" : 2,
            background: it.color,
            border: `1px solid rgba(128,128,128,0.2)`,
          }} />
          {it.label}
        </span>
      ))}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Props
// ─────────────────────────────────────────────────────────────────────────────

interface Props {
  layoutName: string;
  onClose?: () => void;
}

// ─────────────────────────────────────────────────────────────────────────────
// Main component
// ─────────────────────────────────────────────────────────────────────────────

export default function SimulatorCanvas({ layoutName, onClose }: Props) {
  const { theme } = useTheme();
  const containerRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 1200, h: 700 });
  const [snap, setSnap] = useState<SimSnapshot | null>(null);
  const [layout, setLayout] = useState<LayoutGeometry | null>(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  // Zoom / pan
  const [scale, setScale]   = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });

  // Interaction state
  const [hoveredTrainId, setHoveredTrainId]   = useState<string | null>(null);
  const [selectedTrainId, setSelectedTrainId] = useState<string | null>(null);

  // Spline cache
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

  // ── WebSocket ────────────────────────────────────────────────────────────
  useEffect(() => {
    const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl   = `${wsProto}//${window.location.host}/ws/simulate/${encodeURIComponent(layoutName)}`;
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen  = () => { setConnected(true); setError(null); };
    ws.onerror = () => setError("WebSocket connection failed — is the backend running?");
    ws.onclose = (ev) => {
      setConnected(false);
      if      (ev.code === 4404) setError(`Layout '${layoutName}' not found — save it in the Editor first.`);
      else if (ev.code === 4500) setError("Server error while building the simulation engine.");
      else if (ev.code !== 1000 && ev.code !== 1001)
        setError(`Connection lost (code ${ev.code}${ev.reason ? ": " + ev.reason : ""}).`);
    };
    ws.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data) as SimSnapshot;
        if (data.error) { setError(String(data.error)); return; }
        if (data.layout) setLayout(data.layout);
        setSnap(data);
      } catch { /* ignore parse errors */ }
    };

    return () => { ws.close(); wsRef.current = null; };
  }, [layoutName]);

  // ── Spline pre-computation + auto-fit ───────────────────────────────────
  useEffect(() => {
    if (!layout) return;
    const cache = new Map<string, Point[]>();
    for (const track of layout.tracks) {
      cache.set(track.id, densifySpline(track.geometry, 20));
    }
    splineCache.current = cache;
    fitToContent(layout, cache, size);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layout]);

  function fitToContent(
    lay: LayoutGeometry,
    cache: Map<string, Point[]>,
    viewport: { w: number; h: number },
  ) {
    const xs: number[] = [], ys: number[] = [];
    for (const track of lay.tracks) {
      const pts = cache.get(track.id) ?? track.geometry;
      pts.forEach((p) => { xs.push(p.x); ys.push(p.y); });
    }
    Object.values(lay.station_positions  ?? {}).forEach((p) => { xs.push(p.x); ys.push(p.y); });
    Object.values(lay.junction_positions ?? {}).forEach((p) => { xs.push(p.x); ys.push(p.y); });
    if (!xs.length) return;

    const minX = Math.min(...xs), maxX = Math.max(...xs);
    const minY = Math.min(...ys), maxY = Math.max(...ys);
    const cW = maxX - minX, cH = maxY - minY;
    if (cW === 0 && cH === 0) return;

    const PAD = 60;
    const s = Math.min((viewport.w - 2*PAD) / Math.max(cW, 1), (viewport.h - 2*PAD) / Math.max(cH, 1), 4);
    setScale(s);
    setOffset({ x: (viewport.w - cW*s) / 2 - minX*s, y: (viewport.h - cH*s) / 2 - minY*s });
  }

  // ── Controls ─────────────────────────────────────────────────────────────
  const send = useCallback((msg: object) => {
    if (wsRef.current?.readyState === WebSocket.OPEN)
      wsRef.current.send(JSON.stringify(msg));
  }, []);

  const handlePlay  = () => send({ action: "play" });
  const handlePause = () => send({ action: "pause" });
  const handleSpeed = (x: number) => send({ action: "set_speed", value: x });

  const handleWheel = (e: KonvaEventObject<WheelEvent>) => {
    e.evt.preventDefault();
    const by = 1.1;
    setScale((s) => Math.max(0.1, Math.min(8, e.evt.deltaY < 0 ? s*by : s/by)));
  };

  // ── Lookup helpers ───────────────────────────────────────────────────────
  const blockMap  = new Map<string, BlockSnap>((snap?.blocks  ?? []).map((b) => [b.id, b]));
  const signalMap = new Map<string, SignalSnap>((snap?.signals ?? []).map((s) => [s.id, s]));

  function blocksForTrack(trackId: string): BlockSnap[] {
    if (!layout) return [];
    const track = layout.tracks.find((t) => t.id === trackId);
    if (!track)  return [];
    const seg   = layout.segments.find((s) => s.id === track.segment_id);
    if (!seg)    return [];
    return seg.ordered_block_ids.map((bid) => blockMap.get(bid)).filter(Boolean) as BlockSnap[];
  }

  // ── Route helpers ────────────────────────────────────────────────────────
  /** Returns the Set of segment_ids that form a train's route. */
  function routeSegmentIds(train: TrainSnap): Set<string> {
    const out = new Set<string>();
    for (const hop of train.route) {
      if (hop.segment_id) out.add(hop.segment_id);
    }
    return out;
  }

  /** Returns the Set of node_ids (stations/junctions) on a train's route. */
  function routeNodeIds(train: TrainSnap): Set<string> {
    return new Set(train.route.map((h) => h.node_id));
  }

  /** For a given track, is it on the hovered/selected train's route? */
  function trackOnRoute(track: { segment_id: string }, segIds: Set<string> | null): boolean {
    if (!segIds) return false;
    return segIds.has(track.segment_id);
  }

  // ── Train position interpolation ─────────────────────────────────────────
  function trainPosition(t: TrainSnap): Point | null {
    if (!layout || !t.current_block_id) return null;
    const blk = blockMap.get(t.current_block_id);
    if (!blk) return null;
    const track = layout.tracks.find((tr) => tr.segment_id === blk.segment_id);
    if (!track) return null;
    const seg = layout.segments.find((s) => s.id === blk.segment_id);
    if (!seg) return null;
    const pts = splineCache.current.get(track.id) ?? track.geometry;
    const totalBlocks = seg.ordered_block_ids.length;
    const blkIdx = seg.ordered_block_ids.indexOf(t.current_block_id);
    if (blkIdx === -1) return null;
    const globalT = (blkIdx + t.current_position_in_block) / totalBlocks;
    return samplePolyline(pts, globalT);
  }

  // ── Arrival popup logic ──────────────────────────────────────────────────
  interface ArrivalAlert {
    trainId:    string;
    trainName:  string;
    trainColor: string;
    nodeId:     string;
    etaMins:    number;
    canvasPos:  { x: number; y: number };
  }

  function computeArrivals(): ArrivalAlert[] {
    if (!snap || !layout) return [];
    const clockMs = new Date(snap.sim_clock).getTime();
    const threshold = ARRIVING_THRESHOLD_SIM_MINUTES * 60_000;
    const alerts: ArrivalAlert[] = [];

    const candidates = SHOW_ARRIVALS_FOR_ALL_TRAINS
      ? snap.trains
      : snap.trains.filter((t) => t.id === selectedTrainId);

    for (const train of candidates) {
      if (train.status !== "ACTIVE" && train.status !== "PENDING") continue;
      // Find the next stop: first route hop with a scheduled_arrival and no actual_arrival
      for (const hop of train.route) {
        const entry = train.schedule[hop.node_id];
        if (!entry) continue;
        if (entry.actual_arrival) continue;
        const eta = entry.expected_arrival ?? entry.scheduled_arrival;
        if (!eta) continue;
        const etaMs = new Date(eta).getTime();
        const diffMs = etaMs - clockMs;
        if (diffMs >= 0 && diffMs <= threshold) {
          const stationPos = layout.station_positions[hop.node_id] ?? layout.junction_positions[hop.node_id];
          if (stationPos) {
            alerts.push({
              trainId:    train.id,
              trainName:  train.name,
              trainColor: train.color,
              nodeId:     hop.node_id,
              etaMins:    Math.max(0, Math.round(diffMs / 60_000)),
              canvasPos:  stationPos,
            });
          }
        }
        break; // only first upcoming stop per train
      }
    }
    return alerts;
  }

  /** Convert a canvas point (in Konva space) to screen pixel coordinates. */
  function canvasToScreen(canvasX: number, canvasY: number): { x: number; y: number } {
    return {
      x: canvasX * scale + offset.x,
      y: canvasY * scale + offset.y,
    };
  }

  // ── Weather colour ───────────────────────────────────────────────────────
  function weatherColor(intensity: number): string {
    if (intensity >= 0.8) return "rgba(220,38,38,0.25)";
    if (intensity >= 0.5) return "rgba(234,88,12,0.20)";
    if (intensity >= 0.3) return "rgba(202,138,4,0.18)";
    return theme.weatherLight;
  }

  // ── Derived selection state ──────────────────────────────────────────────
  const selectedTrain = snap?.trains.find((t) => t.id === selectedTrainId) ?? null;
  const hoveredTrain  = snap?.trains.find((t) => t.id === hoveredTrainId)  ?? null;
  const selectedSegIds = selectedTrain ? routeSegmentIds(selectedTrain) : null;
  const selectedNodeIds = selectedTrain ? routeNodeIds(selectedTrain)   : null;
  const hoveredSegIds   = hoveredTrain  ? routeSegmentIds(hoveredTrain) : null;
  const isAnythingSelected = !!selectedTrainId;

  const stationMap = Object.fromEntries(
    (layout?.stations ?? []).map((s) => [s.id, s])
  );

  // ── Styles ───────────────────────────────────────────────────────────────
  const outerStyle: CSSProperties = {
    display:       "flex",
    flexDirection: "column",
    height:        "100%",
    background:    theme.bgCanvas,
    fontFamily:    "'Inter', system-ui, sans-serif",
    color:         theme.textPrimary,
  };

  const topBarStyle: CSSProperties = {
    display:       "flex",
    alignItems:    "center",
    gap:           10,
    padding:       "8px 14px",
    background:    theme.bgSurface,
    borderBottom:  `1px solid ${theme.border}`,
    flexShrink:    0,
    flexWrap:      "wrap",
  };

  const btnStyle = (active?: boolean): CSSProperties => ({
    background:   active ? theme.accent : theme.bgElevated,
    border:       `1px solid ${theme.border}`,
    borderRadius: 6,
    color:        theme.textPrimary,
    cursor:       "pointer",
    fontSize:     12,
    fontWeight:   600,
    padding:      "5px 12px",
    transition:   "background 0.12s",
  });

  const speedBtnStyle = (sel: boolean): CSSProperties => ({ ...btnStyle(sel), padding: "4px 8px", fontSize: 11 });

  const arrivals = computeArrivals();

  // ── Error screen ─────────────────────────────────────────────────────────
  if (error) {
    return (
      <div style={{ ...outerStyle, alignItems: "center", justifyContent: "center" }}>
        <p style={{ color: theme.signalRed, fontSize: 14 }}>⚠ {error}</p>
        {onClose && <button style={btnStyle()} onClick={onClose}>Close</button>}
      </div>
    );
  }

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <div style={outerStyle} data-testid="simulator-canvas">

      {/* Top bar */}
      <div style={topBarStyle}>
        <span style={{ fontSize: 12, color: connected ? theme.signalGreen : theme.signalRed, fontWeight: 600 }}>
          {connected ? "●" : "○"} {connected ? "Live" : "Disconnected"}
        </span>
        {snap && (
          <span style={{ fontSize: 11, color: theme.textMuted }}>
            🕐 {new Date(snap.sim_clock).toLocaleTimeString()} | ×{snap.speed_multiplier}
          </span>
        )}
        <button id="sim-btn-play"  style={btnStyle(snap?.playing)}  onClick={handlePlay}>▶ Play</button>
        <button id="sim-btn-pause" style={btnStyle(!snap?.playing)} onClick={handlePause}>⏸ Pause</button>
        <span style={{ fontSize: 11, color: theme.textMuted }}>Speed:</span>
        {[1, 10, 60, 300].map((x) => (
          <button key={x} id={`sim-speed-${x}`} style={speedBtnStyle(snap?.speed_multiplier === x)}
            onClick={() => handleSpeed(x)}>×{x}</button>
        ))}
        <Legend />
        <button id="sim-btn-fit" style={{ ...btnStyle(), marginLeft: 4 }} title="Fit to viewport"
          onClick={() => layout && fitToContent(layout, splineCache.current, size)}>⊡ Fit</button>
        {onClose && <button id="sim-btn-close" style={{ ...btnStyle(), marginLeft: 4 }} onClick={onClose}>✕</button>}
      </div>

      {/* Main content row: canvas + optional side panel */}
      <div style={{ flex: 1, overflow: "hidden", display: "flex", minHeight: 0 }}>

        {/* Canvas area */}
        <div ref={containerRef} style={{ flex: 1, overflow: "hidden", position: "relative" }}
          id="simulator-canvas-area">
          {!snap && (
            <div style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center",
                          justifyContent: "center", color: theme.textMuted, fontSize: 13 }}>
              Connecting to simulation…
            </div>
          )}
          {snap && layout && (
            <>
              <Stage
                width={size.w} height={size.h}
                scaleX={scale} scaleY={scale}
                x={offset.x}  y={offset.y}
                onWheel={handleWheel}
                draggable
                onDragEnd={(e) => setOffset({ x: e.target.x(), y: e.target.y() })}
                onClick={(e) => {
                  // Click on Stage background → deselect
                  // Guard getStage() — may be absent in test mocks
                  if (e.target?.getStage?.() === e.target) setSelectedTrainId(null);
                }}
                data-testid="konva-stage"
              >
                {/* ── Layer 1: Network (tracks, stations, signals) ──────── */}
                <Layer>

                  {/* Weather overlays */}
                  {snap.weather_cells.map((wc) => (
                    <WeatherOverlay key={wc.id} wc={wc} blockMap={blockMap}
                      layout={layout} color={weatherColor(wc.intensity)}
                      splineCache={splineCache.current} />
                  ))}

                  {/* Tracks */}
                  {layout.tracks.map((track) => {
                    const pts = splineCache.current.get(track.id) ?? track.geometry;
                    const blocks = blocksForTrack(track.id);
                    const seg = layout.segments.find((s) => s.id === track.segment_id);
                    const totalBlocks = seg?.ordered_block_ids.length ?? 1;

                    const onSelectedRoute = trackOnRoute(track, selectedSegIds);
                    const onHoveredRoute  = trackOnRoute(track, hoveredSegIds);

                    // Opacity: dim non-route tracks when something is selected
                    const trackOpacity =
                      !isAnythingSelected ? 1
                      : onSelectedRoute   ? 1
                      : theme.dimOpacity;

                    return (
                      <Group key={track.id} opacity={trackOpacity}>

                        {/* Hover aura — drawn before ballast so it glows beneath */}
                        {onHoveredRoute && !isAnythingSelected && (
                          <Line
                            points={flatPoints(pts)}
                            stroke={theme.auraColor}
                            strokeWidth={18}
                            opacity={theme.auraOpacity}
                            lineCap="round" lineJoin="round"
                            shadowBlur={12}
                            shadowColor={theme.auraColor}
                            shadowOpacity={0.6}
                            listening={false}
                            data-testid={`hover-aura-${track.id}`}
                          />
                        )}

                        {/* Route highlight (selected) */}
                        {onSelectedRoute && (
                          <Line
                            points={flatPoints(pts)}
                            stroke={theme.routeHighlight}
                            strokeWidth={14}
                            opacity={0.35}
                            lineCap="round" lineJoin="round"
                            listening={false}
                          />
                        )}

                        {/* Ballast shadow */}
                        <Line points={flatPoints(pts)} stroke={theme.trackBallast}
                          strokeWidth={8} lineCap="round" lineJoin="round" />

                        {/* Rail */}
                        <Line points={flatPoints(pts)} stroke={theme.trackRail}
                          strokeWidth={3} lineCap="round" lineJoin="round" />

                        {/* Per-block overlays */}
                        {blocks.map((blk, i) => {
                          const t0 = i / totalBlocks;
                          const t1 = (i + 1) / totalBlocks;
                          const midT = (t0 + t1) / 2;
                          const midPt = samplePolyline(pts, midT);
                          const startPt = samplePolyline(pts, t0);
                          const endPt   = samplePolyline(pts, t1);
                          return (
                            <Group key={blk.id}>
                              {blk.occupied_by && (
                                <Line points={[startPt.x, startPt.y, endPt.x, endPt.y]}
                                  stroke={theme.occupiedStroke} strokeWidth={8} lineCap="round" />
                              )}
                              {blk.maintenance_window && (
                                <Line points={[startPt.x, startPt.y, endPt.x, endPt.y]}
                                  stroke={theme.maintenanceStroke} strokeWidth={6} dash={[6,4]} lineCap="round" />
                              )}
                              {blk.speed_restriction && (
                                <>
                                  <Circle x={midPt.x} y={midPt.y} radius={8} fill={theme.signalRed} opacity={0.85} />
                                  <Text x={midPt.x-8} y={midPt.y-5} text={`${blk.speed_restriction}`}
                                    fontSize={7} fill="#fff" fontStyle="bold" />
                                </>
                              )}
                            </Group>
                          );
                        })}
                      </Group>
                    );
                  })}

                  {/* Stations */}
                  {layout.stations.map((sta) => {
                    const pos = layout.station_positions[sta.id];
                    if (!pos) return null;
                    const onRoute = selectedNodeIds?.has(sta.id) ?? true;
                    const stationOpacity = !isAnythingSelected ? 1 : onRoute ? 1 : theme.dimOpacity;
                    const strokeColor    = isAnythingSelected && onRoute ? theme.routeHighlight : theme.stationStroke;
                    return (
                      <Group key={sta.id} opacity={stationOpacity}>
                        <Rect x={pos.x-14} y={pos.y-14} width={28} height={28}
                          fill={theme.stationFill} stroke={strokeColor} strokeWidth={2} cornerRadius={4} />
                        <Text x={pos.x-24} y={pos.y+16} text={sta.name} fontSize={9}
                          fill={theme.textSecondary} align="center" width={48} />
                      </Group>
                    );
                  })}

                  {/* Junctions */}
                  {layout.junctions.map((jct) => {
                    const pos = layout.junction_positions[jct.id];
                    if (!pos) return null;
                    const onRoute = selectedNodeIds?.has(jct.id) ?? true;
                    const jctOpacity = !isAnythingSelected ? 1 : onRoute ? 1 : theme.dimOpacity;
                    return (
                      <Circle key={jct.id} x={pos.x} y={pos.y} radius={8} opacity={jctOpacity}
                        fill={theme.bgCanvas} stroke={theme.junctionStroke} strokeWidth={2} />
                    );
                  })}

                  {/* Signals */}
                  {snap.signals.map((sig) => {
                    const pos = layout.signal_positions[sig.id];
                    if (!pos) return null;
                    // Dim signals on non-route blocks
                    const blk = blockMap.get(sig.block_id);
                    const seg = layout.segments.find((s) => s.id === blk?.segment_id);
                    const onRoute = !isAnythingSelected || (seg && selectedSegIds?.has(seg.id)) || false;
                    const sigOpacity = onRoute ? 1 : theme.dimOpacity;

                    const sigColor = {
                      green:   theme.signalGreen,
                      caution: theme.signalCaution,
                      red:     theme.signalRed,
                    }[sig.state] ?? theme.textSecondary;

                    return (
                      <Group key={sig.id} opacity={sigOpacity} data-testid={`signal-${sig.id}`}>
                        <Circle x={pos.x} y={pos.y} radius={6}
                          fill={sigColor} stroke={theme.bgCanvas} strokeWidth={1.5} />
                        {sig.state === "red" && (
                          <Circle x={pos.x} y={pos.y} radius={9}
                            stroke={theme.signalRed} strokeWidth={1} opacity={0.5} />
                        )}
                      </Group>
                    );
                  })}
                </Layer>

                {/* ── Layer 2: Trains ─────────────────────────────────── */}
                <Layer>
                  {snap.trains.filter((t) => t.current_block_id).map((t) => {
                    const pos = trainPosition(t);
                    if (!pos) return null;
                    const W = Math.max(20, t.num_carriages * 4);
                    const H = 10;
                    const isSelected = t.id === selectedTrainId;
                    const isHovered  = t.id === hoveredTrainId;
                    const trainOpacity =
                      !isAnythingSelected  ? 1
                      : isSelected         ? 1
                      : 0.3;

                    return (
                      <Group
                        key={t.id}
                        x={pos.x} y={pos.y}
                        opacity={trainOpacity}
                        onMouseEnter={() => setHoveredTrainId(t.id)}
                        onMouseLeave={() => setHoveredTrainId(null)}
                        onClick={(e) => { e.cancelBubble = true; setSelectedTrainId(t.id); }}
                        data-testid={`train-${t.id}`}
                      >
                        {/* Hover glow ring */}
                        {isHovered && (
                          <Rect x={-W/2 - 4} y={-H/2 - 4} width={W+8} height={H+8}
                            fill="transparent" stroke={theme.auraColor}
                            strokeWidth={3} cornerRadius={4} opacity={0.8}
                            shadowBlur={8} shadowColor={theme.auraColor} shadowOpacity={0.7}
                          />
                        )}
                        {/* Selected highlight ring */}
                        {isSelected && (
                          <Rect x={-W/2 - 3} y={-H/2 - 3} width={W+6} height={H+6}
                            fill="transparent" stroke={theme.routeHighlight}
                            strokeWidth={2} cornerRadius={4}
                          />
                        )}
                        {/* Train body */}
                        <Rect x={-W/2} y={-H/2} width={W} height={H}
                          fill={t.color} stroke={theme.bgCanvas} strokeWidth={1}
                          cornerRadius={2}
                          opacity={t.status === "HALTED" || t.status === "SIDING" ? 0.6 : 1}
                        />
                        {/* Label */}
                        <Text x={-W/2} y={H/2+2} text={t.name} fontSize={8}
                          fill={theme.textPrimary} width={W} align="center" />
                        {/* Status badge */}
                        {(t.status === "HALTED" || t.status === "SIDING" || t.status === "CANCELLED") && (
                          <Text x={-W/2} y={-H/2-10}
                            text={t.status === "HALTED" ? "⏸" : t.status === "SIDING" ? "⬡" : "✕"}
                            fontSize={8} fill={theme.signalCaution} />
                        )}
                      </Group>
                    );
                  })}
                </Layer>
              </Stage>

              {/* ── Arrival popups (HTML overlay, pointer-events:none) ─── */}
              {arrivals.map((alert) => {
                const screen = canvasToScreen(alert.canvasPos.x, alert.canvasPos.y);
                return (
                  <div
                    key={`${alert.trainId}-${alert.nodeId}`}
                    data-testid={`arrival-popup-${alert.trainId}`}
                    style={{
                      position:      "absolute",
                      left:          screen.x + 18,
                      top:           screen.y - 36,
                      background:    theme.popupBg,
                      border:        `1px solid ${alert.trainColor}`,
                      borderRadius:  8,
                      padding:       "5px 10px",
                      fontSize:      11,
                      color:         theme.popupText,
                      fontWeight:    600,
                      pointerEvents: "none",
                      whiteSpace:    "nowrap",
                      boxShadow:     "0 4px 16px rgba(0,0,0,0.3)",
                      zIndex:        10,
                      display:       "flex",
                      alignItems:    "center",
                      gap:           6,
                    }}
                  >
                    <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%",
                                   background: alert.trainColor }} />
                    🚉 {alert.trainName} arriving
                    {alert.etaMins > 0 ? ` in ${alert.etaMins} min` : " now"}
                  </div>
                );
              })}
            </>
          )}
        </div>

        {/* ── SidePanel ─────────────────────────────────────────────────── */}
        {selectedTrain && snap && (
          <SidePanel
            train={selectedTrain}
            simClock={snap.sim_clock}
            stationMap={stationMap}
            eventLog={snap.event_log ?? []}
            onClose={() => setSelectedTrainId(null)}
          />
        )}
      </div>

      {/* Status bar */}
      {snap && (
        <div style={{ padding: "4px 16px", background: theme.bgSurface, borderTop: `1px solid ${theme.border}`,
                      fontSize: 11, color: theme.textMuted, display: "flex", gap: 16, flexShrink: 0 }}>
          <span>{snap.trains.filter((t) => t.status === "ACTIVE").length} active trains</span>
          <span>{snap.trains.filter((t) => t.status === "COMPLETED").length} completed</span>
          <span>{snap.trains.filter((t) => t.status === "HALTED" || t.status === "SIDING").length} held</span>
          <span>{snap.blocks.filter((b) => b.occupied_by).length} blocks occupied</span>
          {selectedTrainId && (
            <span style={{ color: theme.accent }}>
              ● Selected: {selectedTrain?.name}
            </span>
          )}
          <span style={{ marginLeft: "auto" }}>
            Layout: <b style={{ color: theme.textPrimary }}>{layoutName}</b>
          </span>
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// WeatherOverlay sub-component
// ─────────────────────────────────────────────────────────────────────────────

interface WeatherOverlayProps {
  wc: WeatherSnap;
  blockMap: Map<string, BlockSnap>;
  layout: LayoutGeometry;
  color: string;
  splineCache: Map<string, Point[]>;
}

function WeatherOverlay({ wc, blockMap, layout, color, splineCache }: WeatherOverlayProps) {
  return (
    <>
      {wc.affected_block_ids.map((bid) => {
        const blk   = blockMap.get(bid); if (!blk)  return null;
        const track = layout.tracks.find((t) => t.segment_id === blk.segment_id); if (!track) return null;
        const seg   = layout.segments.find((s) => s.id === blk.segment_id); if (!seg) return null;
        const pts   = splineCache.get(track.id) ?? track.geometry;
        const total = seg.ordered_block_ids.length;
        const idx   = seg.ordered_block_ids.indexOf(bid); if (idx === -1) return null;
        const startPt = samplePolyline(pts, idx / total);
        const endPt   = samplePolyline(pts, (idx + 1) / total);
        return (
          <Line key={bid} points={[startPt.x, startPt.y, endPt.x, endPt.y]}
            stroke={color} strokeWidth={20} lineCap="round" opacity={1} />
        );
      })}
    </>
  );
}
