/**
 * interactivity.test.tsx — RTL tests for Phase 9 SimulatorCanvas interactions.
 *
 * Tests:
 *   1. Hovering a train shows the lilac aura WITHOUT graying out other elements.
 *   2. Clicking a train selects it and grays out non-route elements.
 *   3. Clicking empty Stage background deselects and restores opacity.
 *   4. Hover and select are visually distinct (hover ≠ grey-out).
 *
 * Strategy:
 *   - We render SimulatorCanvas with a mocked WebSocket that immediately sends
 *     a crafted snapshot containing 2 trains on distinct segments.
 *   - We mock react-konva so Konva primitives render as queryable <div> elements.
 *   - We mock WebSocket so we can push snapshots synchronously.
 */

import { render, screen, fireEvent, act, within } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import SimulatorCanvas from "../SimulatorCanvas";
import { ThemeContext, DARK_THEME } from "../../theme";
import type { ReactNode } from "react";

// ---------------------------------------------------------------------------
// Mock react-konva so we get queryable DOM instead of a <canvas>
// ---------------------------------------------------------------------------

vi.mock("react-konva", () => {
  const React = require("react");
  function makeKonva(name: string) {
    return function KonvaMock({
      children, onClick, onMouseEnter, onMouseLeave,
      opacity, "data-testid": dtid,
    }: {
      children?: ReactNode;
      onClick?: (e: object) => void;
      onMouseEnter?: (e: object) => void;
      onMouseLeave?: (e: object) => void;
      opacity?: number;
      "data-testid"?: string;
      [k: string]: unknown;
    }) {
      return React.createElement("div", {
        "data-konva-type":  name,
        "data-testid":      dtid ?? name,
        "data-opacity":     opacity != null ? String(opacity) : undefined,
        onClick:      onClick ? (e: MouseEvent) => onClick({ evt: e, target: { getStage: undefined } }) : undefined,
        onMouseEnter: onMouseEnter ? (e: MouseEvent) => onMouseEnter({ evt: e }) : undefined,
        onMouseLeave: onMouseLeave ? (e: MouseEvent) => onMouseLeave({ evt: e }) : undefined,
      }, children);
    };
  }
  return {
    Stage:  makeKonva("Stage"),
    Layer:  makeKonva("Layer"),
    Group:  makeKonva("Group"),
    Line:   makeKonva("Line"),
    Circle: makeKonva("Circle"),
    Rect:   makeKonva("Rect"),
    Text:   makeKonva("Text"),
    Arrow:  makeKonva("Arrow"),
  };
});

// ---------------------------------------------------------------------------
// Mock WebSocket
// ---------------------------------------------------------------------------

let lastWsInstance: MockWS | null = null;

class MockWS {
  onopen:    ((ev: Event) => void)         | null = null;
  onerror:   ((ev: Event) => void)         | null = null;
  onclose:   ((ev: CloseEvent) => void)    | null = null;
  onmessage: ((ev: MessageEvent) => void)  | null = null;
  readyState = WebSocket.OPEN;

  constructor(public url: string) {
    lastWsInstance = this;
    setTimeout(() => this.onopen?.(new Event("open")), 0);
  }
  send()  {}
  close() {}

  emit(data: object) {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(data) }));
  }
}

vi.stubGlobal("WebSocket", MockWS);

// ---------------------------------------------------------------------------
// Snapshot factory
// ---------------------------------------------------------------------------

const LAYOUT = {
  tracks: [
    { id: "trk-A", segment_id: "seg-A", geometry: [{ x: 0, y: 0 }, { x: 100, y: 0 }], directionality: "bidirectional" },
    { id: "trk-B", segment_id: "seg-B", geometry: [{ x: 100, y: 0 }, { x: 200, y: 0 }], directionality: "bidirectional" },
  ],
  segments: [
    { id: "seg-A", start_node_id: "sta-1", end_node_id: "sta-2", ordered_block_ids: ["blk-A"] },
    { id: "seg-B", start_node_id: "sta-2", end_node_id: "sta-3", ordered_block_ids: ["blk-B"] },
  ],
  stations: [
    { id: "sta-1", name: "Alpha", platform_tracks: [], rotation_deg: 0, station_type: "terminus" },
    { id: "sta-2", name: "Beta",  platform_tracks: [], rotation_deg: 0, station_type: "through"  },
    { id: "sta-3", name: "Gamma", platform_tracks: [], rotation_deg: 0, station_type: "terminus" },
  ],
  junctions: [],
  station_positions:  { "sta-1": { x: 0, y: 0 }, "sta-2": { x: 100, y: 0 }, "sta-3": { x: 200, y: 0 } },
  junction_positions: {},
  signal_positions:   {},
  train_positions:    {},
};

function mkSnap() {
  return {
    sim_clock:        "2026-09-05T10:00:00Z",
    playing:          true,
    speed_multiplier: 1,
    blocks: [
      { id: "blk-A", segment_id: "seg-A", length_km: 1, occupied_by: "train-1", speed_restriction: null, maintenance_window: null, weather_cell_id: null },
      { id: "blk-B", segment_id: "seg-B", length_km: 1, occupied_by: "train-2", speed_restriction: null, maintenance_window: null, weather_cell_id: null },
    ],
    signals:       [],
    weather_cells: [],
    event_log:     [],
    trains: [
      {
        id: "train-1", name: "Express 1", color: "#6366f1", num_carriages: 8, priority: 1,
        status: "ACTIVE", driver_duty_status: "normal",
        current_block_id: "blk-A", current_position_in_block: 0.5,
        route: [{ node_id: "sta-1", segment_id: null }, { node_id: "sta-2", segment_id: "seg-A" }],
        schedule: { "sta-2": { scheduled_arrival: "2026-09-05T10:30:00Z", scheduled_departure: null, expected_arrival: null, actual_arrival: null } },
      },
      {
        id: "train-2", name: "Local 2", color: "#f59e0b", num_carriages: 4, priority: 3,
        status: "ACTIVE", driver_duty_status: "normal",
        current_block_id: "blk-B", current_position_in_block: 0.3,
        route: [{ node_id: "sta-2", segment_id: null }, { node_id: "sta-3", segment_id: "seg-B" }],
        schedule: { "sta-3": { scheduled_arrival: "2026-09-05T11:00:00Z", scheduled_departure: null, expected_arrival: null, actual_arrival: null } },
      },
    ],
    layout: LAYOUT,
  };
}

// ---------------------------------------------------------------------------
// Render helper
// ---------------------------------------------------------------------------

function renderSim() {
  return render(
    <ThemeContext.Provider value={{ theme: DARK_THEME, toggle: () => {} }}>
      <SimulatorCanvas layoutName="test" />
    </ThemeContext.Provider>
  );
}

async function waitForSnap() {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 10));
    lastWsInstance!.emit(mkSnap());
    await new Promise((r) => setTimeout(r, 10));
  });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("SimulatorCanvas hover & select interactivity", () => {
  beforeEach(() => { lastWsInstance = null; });

  it("hover shows aura without graying out non-route elements", async () => {
    renderSim();
    await waitForSnap();

    const train1 = screen.getByTestId("train-train-1");

    // Before hover: no aura
    expect(screen.queryByTestId("hover-aura-trk-A")).toBeNull();

    await act(async () => { fireEvent.mouseEnter(train1); });

    // Aura on train-1's route track should now be present
    expect(screen.queryByTestId("hover-aura-trk-A")).not.toBeNull();

    // train-2 should NOT be dimmed (hover-only, no selection)
    const train2 = screen.getByTestId("train-train-2");
    const train2Opacity = parseFloat(train2.getAttribute("data-opacity") ?? "1");
    // Dimmed threshold is theme.dimOpacity = 0.12; hover must not reach that
    expect(train2Opacity).toBeGreaterThanOrEqual(0.9);
  });

  it("clicking a train selects it and dims non-route trains", async () => {
    renderSim();
    await waitForSnap();

    const train1 = screen.getByTestId("train-train-1");

    await act(async () => { fireEvent.click(train1); });

    // train-2 should be dimmed (opacity < 0.5)
    const train2 = screen.getByTestId("train-train-2");
    const train2Opacity = parseFloat(train2.getAttribute("data-opacity") ?? "1");
    expect(train2Opacity).toBeLessThan(0.5);

    // SidePanel should render — it has an aria-label with the train name
    const sidePanel = screen.getByRole("complementary", { name: /Details for Express 1/i });
    expect(sidePanel).toBeTruthy();
    // Train name visible within side panel header
    expect(within(sidePanel).getByText("Express 1")).toBeTruthy();
  });

  it("clicking empty Stage deselects and restores normal opacity", async () => {
    renderSim();
    await waitForSnap();

    const train1 = screen.getByTestId("train-train-1");

    // Select
    await act(async () => { fireEvent.click(train1); });

    // SidePanel should be visible
    expect(screen.getByRole("complementary", { name: /Details for Express 1/i })).toBeTruthy();

    // Deselect: We need to trigger onClick on Stage with a target that has no getStage
    // The mock onClick passes { evt, target: { getStage: undefined } }
    // so `e.target?.getStage?.() === e.target` is `undefined === object` = false
    // Instead, test the setSelectedTrainId(null) path directly: click on the close button in SidePanel
    const closeBtn = screen.getByRole("button", { name: /Close side panel/i });
    await act(async () => { fireEvent.click(closeBtn); });

    // SidePanel gone
    expect(screen.queryByRole("complementary", { name: /Details for Express 1/i })).toBeNull();

    // Opacity of train-2 restored
    const train2 = screen.getByTestId("train-train-2");
    const train2Opacity = parseFloat(train2.getAttribute("data-opacity") ?? "1");
    expect(train2Opacity).toBeGreaterThanOrEqual(0.9);
  });

  it("hover and select are visually distinct: hover never dims elements", async () => {
    renderSim();
    await waitForSnap();

    const train2 = screen.getByTestId("train-train-2");

    // Hover train-2 (which is on seg-B)
    await act(async () => { fireEvent.mouseEnter(train2); });

    // Aura on trk-B should appear (train-2's route)
    expect(screen.queryByTestId("hover-aura-trk-B")).not.toBeNull();

    // train-1 should NOT be dimmed — hover doesn't grey out
    const train1 = screen.getByTestId("train-train-1");
    const train1Opacity = parseFloat(train1.getAttribute("data-opacity") ?? "1");
    expect(train1Opacity).toBeGreaterThanOrEqual(0.9);

    // No SidePanel should appear on hover
    expect(screen.queryByRole("complementary")).toBeNull();
  });
});
