/**
 * EditorCanvas.tsx — Main Konva canvas (Phase 7).
 *
 * Tool modes: track | station | junction | signal | select | train
 *
 * Phase 7 additions:
 *  - "train" tool: click to place train, then click node sequence to build route.
 *    Enter commits route; Escape cancels.
 *  - Route overlay: dashed polyline through committed hops, red flash on invalid hop.
 *  - All existing jank fixes from Phase 6 carried forward.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Stage, Layer, Line, Circle, Rect, Arrow, Group, Text } from "react-konva";
import type Konva from "konva";
import {
  useEditorStore,
  type JunctionWithPos,
  type StationWithPos,
  type SignalWithPos,
  type TrainWithPos,
} from "./store";
import type { Point } from "../types";
import {
  flatPoints,
  normaliseRect,
  rectsIntersect,
  pointsBBox,
  SNAP_DISTANCE_PX,
  findSnap,
} from "./utils/geometry";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const JUNCTION_RADIUS_PX      = 14;
const STATION_WIDTH_PX        = 44;
const STATION_HEIGHT_PX       = 22;
const SIGNAL_RADIUS_PX        = 8;
const PLATFORM_LINE_LENGTH_PX = 32;
const GRID_SIZE_PX            = 20;
const TRAIN_RADIUS_PX         = 12;
const DBLCLICK_GUARD_MS       = 220;

const SIGNAL_COLORS: Record<string, string> = {
  green:   "#22c55e",
  red:     "#ef4444",
  caution: "#f59e0b",
};

const TOOL_CURSOR: Record<ToolMode, string> = {
  track:    "crosshair",
  station:  "cell",
  junction: "copy",
  signal:   "pointer",
  select:   "default",
  train:    "crosshair",
};

export type ToolMode = "track" | "station" | "junction" | "signal" | "select" | "train";

interface Props {
  tool: ToolMode;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function EditorCanvas({ tool }: Props) {
  const { state, dispatch } = useEditorStore();
  const stageRef     = useRef<Konva.Stage>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [stageSize, setStageSize] = useState({ width: 800, height: 600 });

  // Track drawing state
  const [draftPoints, setDraftPoints] = useState<Point[]>([]);
  const [cursorPos,   setCursorPos]   = useState<Point | null>(null);

  // Double-click guard
  const lastClickTime = useRef<number>(0);

  // "invalid hop" flash — briefly highlight when a disconnected node is clicked
  const [invalidFlash, setInvalidFlash] = useState(false);

  // Marquee
  const [marquee, setMarquee] = useState<{ start: Point; current: Point } | null>(null);

  // Resize observer
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const e = entries[0];
      if (e) setStageSize({ width: e.contentRect.width, height: e.contentRect.height });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Flash effect when route hop is invalid
  useEffect(() => {
    if (state.routeBuilding?.invalidLastHop) {
      setInvalidFlash(true);
      const t = setTimeout(() => setInvalidFlash(false), 700);
      return () => clearTimeout(t);
    }
  }, [state.routeBuilding?.invalidLastHop]);

  // Keyboard shortcuts
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (document.activeElement as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;

      if (e.key === "Escape") {
        setDraftPoints([]);
        setCursorPos(null);
        dispatch({ type: "CANCEL_ROUTE_BUILD" });
      }
      if (e.key === "Enter") {
        // Commit draft track
        setDraftPoints((pts) => {
          if (pts.length >= 2) dispatch({ type: "ADD_TRACK", points: pts });
          return [];
        });
        setCursorPos(null);
        // Commit route build
        dispatch({ type: "FINISH_ROUTE_BUILD" });
      }
      if (e.key === "Delete" || e.key === "Backspace") {
        state.selection.forEach((id) => dispatch({ type: "REMOVE_ELEMENT", id }));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.selection, dispatch]);

  // Collect track endpoints for snap
  const allEndpoints: Point[] = [];
  for (const t of state.tracks) {
    if (t.geometry.length > 0) allEndpoints.push(t.geometry[0]);
    if (t.geometry.length > 1) allEndpoints.push(t.geometry[t.geometry.length - 1]);
  }

  const getPos = useCallback((): Point | null => {
    const stage = stageRef.current;
    if (!stage) return null;
    const ptr = stage.getPointerPosition();
    return ptr ? { x: ptr.x, y: ptr.y } : null;
  }, []);

  // ---------------------------------------------------------------------------
  // Helpers — node position lookup for route rendering
  // ---------------------------------------------------------------------------
  const getNodePos = useCallback((nodeId: string): Point | null => {
    const sta = state.stations.find((s) => s.id === nodeId) as StationWithPos | undefined;
    if (sta?._pos) return sta._pos;
    const jct = state.junctions.find((j) => j.id === nodeId) as JunctionWithPos | undefined;
    if (jct?._pos) return jct._pos;
    return null;
  }, [state.stations, state.junctions]);

  // ---------------------------------------------------------------------------
  // Stage events
  // ---------------------------------------------------------------------------

  const handleStageMouseMove = useCallback(() => {
    const pos = getPos();
    if (!pos) return;
    if (tool === "track" && draftPoints.length > 0) {
      setCursorPos(findSnap(pos, allEndpoints) ?? pos);
    } else {
      setCursorPos(pos);
    }
    if (marquee) setMarquee((m) => m ? { ...m, current: pos } : null);
  }, [tool, draftPoints, allEndpoints, marquee, getPos]);

  const handleStageClick = useCallback(
    (e: Konva.KonvaEventObject<MouseEvent>) => {
      if (e.target !== e.currentTarget) return;
      const pos = getPos();
      if (!pos) return;

      if (tool === "track") {
        const now = Date.now();
        if (now - lastClickTime.current < DBLCLICK_GUARD_MS) return;
        lastClickTime.current = now;
        setDraftPoints((pts) => [...pts, findSnap(pos, allEndpoints) ?? pos]);
      } else if (tool === "station") {
        dispatch({ type: "ADD_STATION", position: pos });
      } else if (tool === "junction") {
        dispatch({ type: "ADD_JUNCTION", position: pos });
      } else if (tool === "signal") {
        dispatch({
          type: "ADD_SIGNAL",
          position: pos,
          blockId: `block-at-${Math.round(pos.x)}-${Math.round(pos.y)}`,
        });
      } else if (tool === "train") {
        // Place a new train at this position and start building its route.
        const id = `trn-${Date.now().toString(36)}`;
        dispatch({ type: "ADD_TRAIN", position: pos });
        // START_ROUTE_BUILD requires a real node — clicking empty canvas doesn't give one.
        // We dispatch ADD_TRAIN which returns via state; the user then clicks a node.
        // (Route building starts when user clicks a station/junction node below.)
      } else if (tool === "select") {
        dispatch({ type: "CLEAR_SELECTION" });
      }
    },
    [tool, allEndpoints, dispatch, getPos]
  );

  const handleStageDblClick = useCallback(
    (e: Konva.KonvaEventObject<MouseEvent>) => {
      if (e.target !== e.currentTarget) return;
      if (tool !== "track") return;
      setDraftPoints((pts) => {
        if (pts.length >= 2) dispatch({ type: "ADD_TRACK", points: pts });
        return [];
      });
      setCursorPos(null);
      lastClickTime.current = 0;
    },
    [tool, dispatch]
  );

  const handleStageMouseDown = useCallback(
    (e: Konva.KonvaEventObject<MouseEvent>) => {
      if (tool !== "select") return;
      if (e.target !== e.currentTarget) return;
      const pos = getPos();
      if (!pos) return;
      setMarquee({ start: pos, current: pos });
    },
    [tool, getPos]
  );

  const handleStageMouseUp = useCallback(() => {
    if (!marquee) return;
    const rect = normaliseRect(marquee.start.x, marquee.start.y, marquee.current.x, marquee.current.y);
    const selected: string[] = [];
    for (const track of state.tracks) {
      if (rectsIntersect(pointsBBox(track.geometry), rect)) selected.push(track.id);
    }
    for (const sta of state.stations) {
      const pos = (sta as StationWithPos)._pos;
      const bbox = { x: pos.x - STATION_WIDTH_PX / 2, y: pos.y - STATION_HEIGHT_PX / 2, w: STATION_WIDTH_PX, h: STATION_HEIGHT_PX };
      if (rectsIntersect(bbox, rect)) selected.push(sta.id);
    }
    for (const jct of state.junctions) {
      const pos = (jct as JunctionWithPos)._pos;
      const bbox = { x: pos.x - JUNCTION_RADIUS_PX, y: pos.y - JUNCTION_RADIUS_PX, w: JUNCTION_RADIUS_PX * 2, h: JUNCTION_RADIUS_PX * 2 };
      if (rectsIntersect(bbox, rect)) selected.push(jct.id);
    }
    for (const sig of state.signals) {
      const pos = (sig as SignalWithPos)._pos;
      if (!pos) continue;
      const bbox = { x: pos.x - SIGNAL_RADIUS_PX, y: pos.y - SIGNAL_RADIUS_PX, w: SIGNAL_RADIUS_PX * 2, h: SIGNAL_RADIUS_PX * 2 };
      if (rectsIntersect(bbox, rect)) selected.push(sig.id);
    }
    if (selected.length > 0) dispatch({ type: "SET_SELECTION", ids: selected });
    else dispatch({ type: "CLEAR_SELECTION" });
    setMarquee(null);
  }, [marquee, state, dispatch]);

  // ---------------------------------------------------------------------------
  // Track point drag
  // ---------------------------------------------------------------------------
  const handlePointDragMove = useCallback(
    (trackId: string, pointIndex: number, e: Konva.KonvaEventObject<DragEvent>) => {
      const node = e.target;
      dispatch({ type: "UPDATE_TRACK_POINT", trackId, pointIndex, point: { x: node.x(), y: node.y() } });
    },
    [dispatch]
  );

  const handlePointDragEnd = useCallback(
    (trackId: string, pointIndex: number, e: Konva.KonvaEventObject<DragEvent>) => {
      const node = e.target;
      dispatch({ type: "FINISH_TRACK_POINT_DRAG", trackId, pointIndex, point: { x: node.x(), y: node.y() } });
    },
    [dispatch]
  );

  // ---------------------------------------------------------------------------
  // Node click — handles route building when train tool is active
  // ---------------------------------------------------------------------------
  const handleNodeClick = useCallback(
    (nodeId: string) => {
      if (tool === "train" && state.routeBuilding) {
        dispatch({ type: "ADD_ROUTE_HOP", nodeId });
      } else {
        dispatch({ type: "TOGGLE_SELECTION", id: nodeId });
      }
    },
    [tool, state.routeBuilding, dispatch]
  );

  // ---------------------------------------------------------------------------
  // Render helpers
  // ---------------------------------------------------------------------------
  const marqueeRect = marquee
    ? normaliseRect(marquee.start.x, marquee.start.y, marquee.current.x, marquee.current.y)
    : null;
  const isSelected = (id: string) => state.selection.has(id);

  return (
    <div
      ref={containerRef}
      style={{ width: "100%", height: "100%", cursor: TOOL_CURSOR[tool] }}
      data-testid="editor-canvas-container"
    >
      <Stage
        ref={stageRef}
        width={stageSize.width}
        height={stageSize.height}
        id="editor-stage"
        onMouseMove={handleStageMouseMove}
        onClick={handleStageClick}
        onDblClick={handleStageDblClick}
        onMouseDown={handleStageMouseDown}
        onMouseUp={handleStageMouseUp}
      >
        {/* ── Grid ──────────────────────────────────────────────────────── */}
        <Layer listening={false}>
          {Array.from({ length: Math.ceil(stageSize.width / GRID_SIZE_PX) + 1 }, (_, i) => (
            <Line key={`gv-${i}`} points={[i * GRID_SIZE_PX, 0, i * GRID_SIZE_PX, stageSize.height]} stroke="#1e293b" strokeWidth={0.5} opacity={0.4} />
          ))}
          {Array.from({ length: Math.ceil(stageSize.height / GRID_SIZE_PX) + 1 }, (_, i) => (
            <Line key={`gh-${i}`} points={[0, i * GRID_SIZE_PX, stageSize.width, i * GRID_SIZE_PX]} stroke="#1e293b" strokeWidth={0.5} opacity={0.4} />
          ))}
        </Layer>

        {/* ── Main content ──────────────────────────────────────────────── */}
        <Layer>

          {/* Tracks */}
          {state.tracks.map((track) => {
            const pts      = flatPoints(track.geometry);
            const selected = isSelected(track.id);
            const isForward = track.directionality === "one_way_forward";
            const isReverse = track.directionality === "one_way_reverse";
            const midIdx = Math.floor(track.geometry.length / 2);
            const prevPt = track.geometry[Math.max(0, midIdx - 1)];
            const nextPt = track.geometry[Math.min(track.geometry.length - 1, midIdx + 1)];
            return (
              <Group key={track.id}>
                {selected && <Line points={pts} stroke="#6366f1" strokeWidth={10} lineCap="round" lineJoin="round" tension={0.5} opacity={0.35} listening={false} />}
                <Line points={pts} stroke={selected ? "#818cf8" : "#94a3b8"} strokeWidth={3} lineCap="round" lineJoin="round" tension={0.5}
                  onClick={() => dispatch({ type: "TOGGLE_SELECTION", id: track.id })} />
                {(isForward || isReverse) && prevPt && nextPt && (
                  <Arrow points={isForward ? [prevPt.x, prevPt.y, nextPt.x, nextPt.y] : [nextPt.x, nextPt.y, prevPt.x, prevPt.y]}
                    pointerLength={10} pointerWidth={8} fill="#f59e0b" stroke="#f59e0b" strokeWidth={1.5} listening={false} />
                )}
                {track.geometry.map((pt, i) => (
                  <Circle key={i} x={pt.x} y={pt.y}
                    radius={i === 0 || i === track.geometry.length - 1 ? 6 : 4}
                    fill={i === 0 || i === track.geometry.length - 1 ? "#38bdf8" : "#64748b"}
                    stroke="#0f172a" strokeWidth={1} draggable
                    onDragMove={(e) => handlePointDragMove(track.id, i, e)}
                    onDragEnd={(e) => handlePointDragEnd(track.id, i, e)} />
                ))}
              </Group>
            );
          })}

          {/* Route overlays — rendered behind nodes */}
          {state.trains.map((train) => {
            const route = train.route;
            if (route.length < 2) return null;
            const pts: number[] = [];
            for (const hop of route) {
              const pos = getNodePos(hop.node_id);
              if (pos) { pts.push(pos.x, pos.y); }
            }
            if (pts.length < 4) return null;
            return (
              <Line key={`route-${train.id}`}
                points={pts}
                stroke={(train as TrainWithPos).color ?? "#3b82f6"}
                strokeWidth={2.5}
                lineCap="round"
                lineJoin="round"
                opacity={0.5}
                listening={false}
              />
            );
          })}

          {/* In-progress route overlay */}
          {state.routeBuilding && (() => {
            const rb = state.routeBuilding;
            const pts: number[] = [];
            for (const hop of rb.hops) {
              const pos = getNodePos(hop.node_id);
              if (pos) { pts.push(pos.x, pos.y); }
            }
            return pts.length >= 4 ? (
              <Line
                points={pts}
                stroke={invalidFlash ? "#ef4444" : "#f59e0b"}
                strokeWidth={2}
                dash={[6, 4]}
                lineCap="round"
                listening={false}
              />
            ) : null;
          })()}

          {/* Stations */}
          {state.stations.map((sta) => {
            const pos      = (sta as StationWithPos)._pos;
            const selected = isSelected(sta.id);
            const isRouteBuilding = tool === "train" && !!state.routeBuilding;
            const fillColor =
              sta.station_type === "terminus"           ? "#7c3aed"
              : sta.station_type === "junction_station" ? "#0891b2"
              : "#0f766e";
            return (
              <Group key={sta.id} x={pos.x} y={pos.y} rotation={sta.rotation_deg} draggable
                onClick={() => handleNodeClick(sta.id)}
                onDragEnd={(e) => dispatch({ type: "MOVE_STATION", id: sta.id, position: { x: e.target.x(), y: e.target.y() } })}
              >
                {selected && <Rect x={-STATION_WIDTH_PX / 2 - 4} y={-STATION_HEIGHT_PX / 2 - 4} width={STATION_WIDTH_PX + 8} height={STATION_HEIGHT_PX + 8} fill="#6366f1" opacity={0.25} cornerRadius={6} />}
                {isRouteBuilding && <Rect x={-STATION_WIDTH_PX / 2 - 6} y={-STATION_HEIGHT_PX / 2 - 6} width={STATION_WIDTH_PX + 12} height={STATION_HEIGHT_PX + 12} fill="transparent" stroke="#f59e0b" strokeWidth={2} cornerRadius={8} dash={[4, 3]} />}
                <Rect x={-STATION_WIDTH_PX / 2} y={-STATION_HEIGHT_PX / 2} width={STATION_WIDTH_PX} height={STATION_HEIGHT_PX} fill={fillColor} cornerRadius={4} stroke={selected ? "#818cf8" : "#1e293b"} strokeWidth={selected ? 2 : 1} />
                {Array.from({ length: Math.max(1, Math.min(sta.platform_tracks.length || 1, 4)) }, (_, i) => (
                  <Line key={i} points={[-PLATFORM_LINE_LENGTH_PX / 2, -STATION_HEIGHT_PX / 2 - 6 - i * 5, PLATFORM_LINE_LENGTH_PX / 2, -STATION_HEIGHT_PX / 2 - 6 - i * 5]}
                    stroke="#cbd5e1" strokeWidth={2} lineCap="round" listening={false} />
                ))}
                <Text text={sta.name} fontSize={9} fill="#f8fafc" width={STATION_WIDTH_PX} align="center" x={-STATION_WIDTH_PX / 2} y={-5} listening={false} />
              </Group>
            );
          })}

          {/* Junctions */}
          {state.junctions.map((jct) => {
            const pos      = (jct as JunctionWithPos)._pos;
            const selected = isSelected(jct.id);
            const isRouteBuilding = tool === "train" && !!state.routeBuilding;
            return (
              <Group key={jct.id} x={pos.x} y={pos.y} draggable
                onClick={() => handleNodeClick(jct.id)}
                onDragEnd={(e) => dispatch({ type: "MOVE_JUNCTION", id: jct.id, position: { x: e.target.x(), y: e.target.y() } })}
              >
                {selected && <Circle radius={JUNCTION_RADIUS_PX + 5} fill="#6366f1" opacity={0.25} />}
                {isRouteBuilding && <Circle radius={JUNCTION_RADIUS_PX + 7} fill="transparent" stroke="#f59e0b" strokeWidth={2} dash={[4, 3]} />}
                <Circle radius={JUNCTION_RADIUS_PX} fill="#1e293b" stroke={selected ? "#818cf8" : "#f59e0b"} strokeWidth={selected ? 3 : 2} />
                <Text text="⬡" fontSize={12} fill="#f59e0b" align="center" verticalAlign="middle" width={JUNCTION_RADIUS_PX * 2} height={JUNCTION_RADIUS_PX * 2} x={-JUNCTION_RADIUS_PX} y={-JUNCTION_RADIUS_PX} listening={false} />
              </Group>
            );
          })}

          {/* Signals */}
          {state.signals.map((sig) => {
            const pos      = (sig as SignalWithPos)._pos;
            const color    = SIGNAL_COLORS[sig.state] ?? "#94a3b8";
            const selected = isSelected(sig.id);
            if (!pos) return null;
            return (
              <Group key={sig.id} x={pos.x} y={pos.y} draggable
                onClick={() => dispatch({ type: "TOGGLE_SELECTION", id: sig.id })}
                onDragEnd={(e) => dispatch({ type: "MOVE_SIGNAL", id: sig.id, position: { x: e.target.x(), y: e.target.y() } })}
              >
                {selected && <Circle radius={SIGNAL_RADIUS_PX + 4} fill="#6366f1" opacity={0.3} />}
                <Line points={[0, 0, 0, SIGNAL_RADIUS_PX * 2.5]} stroke="#64748b" strokeWidth={1.5} listening={false} />
                <Circle radius={SIGNAL_RADIUS_PX} fill={color} stroke="#0f172a" strokeWidth={1.5} />
              </Group>
            );
          })}

          {/* Trains */}
          {state.trains.map((train) => {
            const pos      = (train as TrainWithPos)._pos;
            const selected = isSelected(train.id);
            const color    = (train as TrainWithPos).color ?? "#3b82f6";
            const isBuilding = state.routeBuilding?.trainId === train.id;
            return (
              <Group key={train.id} x={pos.x} y={pos.y} draggable
                onClick={() => {
                  if (tool === "train" && !state.routeBuilding) {
                    dispatch({ type: "START_ROUTE_BUILD", trainId: train.id, originNodeId: train.route[0]?.node_id ?? "" });
                  } else {
                    dispatch({ type: "TOGGLE_SELECTION", id: train.id });
                  }
                }}
                onDragEnd={(e) => dispatch({ type: "MOVE_TRAIN", id: train.id, position: { x: e.target.x(), y: e.target.y() } })}
              >
                {selected && <Circle radius={TRAIN_RADIUS_PX + 5} fill="#6366f1" opacity={0.25} />}
                {isBuilding && <Circle radius={TRAIN_RADIUS_PX + 7} fill="transparent" stroke="#f59e0b" strokeWidth={2} />}
                <Circle radius={TRAIN_RADIUS_PX} fill={color} stroke="#0f172a" strokeWidth={1.5} />
                <Text text="🚂" fontSize={10} align="center" verticalAlign="middle" width={TRAIN_RADIUS_PX * 2} height={TRAIN_RADIUS_PX * 2} x={-TRAIN_RADIUS_PX} y={-TRAIN_RADIUS_PX} listening={false} />
              </Group>
            );
          })}

          {/* Draft track */}
          {draftPoints.length > 0 && (
            <Group listening={false}>
              <Line points={flatPoints(cursorPos ? [...draftPoints, cursorPos] : draftPoints)} stroke="#38bdf8" strokeWidth={2} lineCap="round" lineJoin="round" tension={0.5} dash={[6, 4]} />
              {draftPoints.map((pt, i) => <Circle key={i} x={pt.x} y={pt.y} radius={4} fill="#38bdf8" opacity={0.8} />)}
              {cursorPos && <Circle x={cursorPos.x} y={cursorPos.y} radius={4} fill="#f59e0b" opacity={0.9} />}
            </Group>
          )}

          {/* Snap indicator */}
          {tool === "track" && cursorPos && (() => {
            const snap = findSnap(cursorPos, allEndpoints);
            return snap ? <Circle x={snap.x} y={snap.y} radius={SNAP_DISTANCE_PX} stroke="#22c55e" strokeWidth={1.5} dash={[3, 3]} fill="transparent" listening={false} /> : null;
          })()}

          {/* Marquee */}
          {marqueeRect && (
            <Rect x={marqueeRect.x} y={marqueeRect.y} width={marqueeRect.w} height={marqueeRect.h} fill="#6366f1" opacity={0.12} stroke="#818cf8" strokeWidth={1} dash={[4, 3]} listening={false} />
          )}
        </Layer>
      </Stage>
    </div>
  );
}
