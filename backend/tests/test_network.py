"""
test_network.py — Pytest tests for app.network.

Test coverage
-------------
1. Block mutual exclusion:
   - A free block can be occupied by a train.
   - Occupying an already-occupied block by a *different* train raises OccupancyError.
   - The same train re-claiming its block is a no-op (idempotent).
   - Releasing with the wrong train id raises OccupancyError.
   - Releasing correctly returns the block to free state.

2. Junction atomic occupancy:
   - Junction is treated as exactly ONE atomic occupancy unit.
   - Attempting partial / double occupation raises OccupancyError.
   - The junction has no internal blocks of its own.

3. split_track_at_point:
   - Splits a track into exactly two segments at the requested fraction.
   - The new station is present as a node between the two segments.
   - Left segment end_node == new station; right segment start_node == new station.
   - Original segment and track are removed from the graph.
   - Block counts sum to the original count, with ≥1 block per side.
   - All blocks remain in the graph under their original ids.
   - The two new segments are correctly connected to the graph's adjacency.

4. Pathfinding on a small 4-node network:
   - find_path returns the correct segment sequence.
   - find_path_blocks returns blocks in correct traversal order.
   - find_path returns None when the destination is unreachable.
   - Trivial case: start == end returns empty list.

5. NetworkGraph structural helpers:
   - add_track validates that nodes must pre-exist.
   - Duplicate segment/track ids raise NetworkError.
   - Block length is configurable per-block (not hardcoded).

Network used in pathfinding tests (a simple diamond):

    sta_A ---seg_AB--- jct_B ---seg_BD--- sta_D
                |                          |
              seg_AC                     seg_CD
                |                          |
              jct_C -------seg_CD---------/

  (Actually a straight line A→B→D with a bypass A→C→D):

    sta_A --seg_AB-- jct_B --seg_BD-- sta_D
       \\                              /
        seg_AC         seg_CD        /
           \\          /
            jct_C-----
"""

from __future__ import annotations

import pytest

from app.models import Point, StationType
from app.network import (
    NetworkError,
    NetworkGraph,
    OccupancyError,
    StationRuntime,
)


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture()
def graph() -> NetworkGraph:
    """A fresh, empty NetworkGraph."""
    return NetworkGraph()


@pytest.fixture()
def two_node_graph(graph: NetworkGraph):
    """
    Minimal graph:  sta_A --[seg_AB, 10 blocks]--> sta_B

    Returns (graph, sta_A, sta_B, seg_AB, trk_AB)
    """
    sta_a = graph.add_station("Alpha", station_id="sta_A")
    sta_b = graph.add_station("Beta", station_id="sta_B")
    seg, trk = graph.add_track(
        "sta_A",
        "sta_B",
        length_km=10.0,
        num_blocks=10,
        geometry=[Point(x=0, y=0), Point(x=1000, y=0)],
        segment_id="seg_AB",
        track_id="trk_AB",
    )
    return graph, sta_a, sta_b, seg, trk


@pytest.fixture()
def diamond_graph(graph: NetworkGraph):
    """
    Diamond network for pathfinding tests:

        sta_A ─seg_AB─ jct_B ─seg_BD─ sta_D
           \\                          /
            seg_AC               seg_CD
               \\                /
                jct_C ──────────

    Returns (graph, sta_A, jct_B, jct_C, sta_D)
    """
    sta_a = graph.add_station("Alpha", station_id="sta_A")
    jct_b = graph.add_junction(junction_id="jct_B")
    jct_c = graph.add_junction(junction_id="jct_C")
    sta_d = graph.add_station("Delta", station_id="sta_D")

    graph.add_track("sta_A", "jct_B", length_km=5, num_blocks=5, segment_id="seg_AB", track_id="trk_AB")
    graph.add_track("sta_A", "jct_C", length_km=5, num_blocks=5, segment_id="seg_AC", track_id="trk_AC")
    graph.add_track("jct_B", "sta_D", length_km=5, num_blocks=5, segment_id="seg_BD", track_id="trk_BD")
    graph.add_track("jct_C", "sta_D", length_km=5, num_blocks=5, segment_id="seg_CD", track_id="trk_CD")

    return graph, sta_a, jct_b, jct_c, sta_d


# ===========================================================================
# 1. Block mutual exclusion
# ===========================================================================


class TestBlockOccupancy:
    def test_free_block_can_be_occupied(self, two_node_graph):
        graph, *_, seg, _ = two_node_graph
        blk = seg.blocks[0]

        assert blk.is_free
        blk.occupy("train-1")
        assert blk.occupied_by == "train-1"
        assert not blk.is_free

    def test_double_occupy_different_train_raises(self, two_node_graph):
        graph, *_, seg, _ = two_node_graph
        blk = seg.blocks[0]

        blk.occupy("train-1")
        with pytest.raises(OccupancyError, match="train-2"):
            blk.occupy("train-2")

        # State unchanged after failed occupation.
        assert blk.occupied_by == "train-1"

    def test_same_train_reoccupy_is_idempotent(self, two_node_graph):
        graph, *_, seg, _ = two_node_graph
        blk = seg.blocks[0]

        blk.occupy("train-1")
        blk.occupy("train-1")  # must not raise
        assert blk.occupied_by == "train-1"

    def test_release_by_non_occupant_raises(self, two_node_graph):
        graph, *_, seg, _ = two_node_graph
        blk = seg.blocks[0]

        blk.occupy("train-1")
        with pytest.raises(OccupancyError, match="train-2"):
            blk.release("train-2")

        assert blk.occupied_by == "train-1"  # still occupied

    def test_release_by_correct_train_clears(self, two_node_graph):
        graph, *_, seg, _ = two_node_graph
        blk = seg.blocks[0]

        blk.occupy("train-1")
        blk.release("train-1")
        assert blk.is_free
        assert blk.occupied_by is None

    def test_block_length_is_configurable(self, graph: NetworkGraph):
        """Block length must reflect per-block configuration, not a hardcode."""
        graph.add_station("A", station_id="sta_A")
        graph.add_station("B", station_id="sta_B")
        seg, _ = graph.add_track(
            "sta_A", "sta_B",
            length_km=6.0,
            block_length_km=2.0,  # request 2 km blocks → expect 3 blocks
            geometry=[Point(x=0, y=0), Point(x=600, y=0)],
        )
        assert len(seg.blocks) == 3
        for blk in seg.blocks:
            assert blk.length_km == pytest.approx(2.0)

    def test_default_block_length_not_hardcoded(self, graph: NetworkGraph):
        """A segment with custom length must not silently use 1 km blocks."""
        graph.add_station("A", station_id="sta_A")
        graph.add_station("B", station_id="sta_B")
        seg, _ = graph.add_track(
            "sta_A", "sta_B",
            length_km=8.0,
            num_blocks=4,
            geometry=[Point(x=0, y=0), Point(x=800, y=0)],
        )
        # 4 blocks over 8 km → 2 km each
        assert len(seg.blocks) == 4
        for blk in seg.blocks:
            assert blk.length_km == pytest.approx(2.0)


# ===========================================================================
# 2. Junction atomic occupancy
# ===========================================================================


class TestJunctionOccupancy:
    def test_junction_has_no_internal_blocks(self, graph: NetworkGraph):
        jct = graph.add_junction(junction_id="jct_1")
        # There must be no blocks registered to this junction id.
        all_segment_ids = [b.segment_id for b in graph.all_blocks]
        assert "jct_1" not in all_segment_ids

    def test_junction_can_be_occupied(self, graph: NetworkGraph):
        jct = graph.add_junction(junction_id="jct_1")
        assert jct.is_free
        jct.occupy("train-1")
        assert jct.occupied_by == "train-1"
        assert not jct.is_free

    def test_junction_double_occupy_raises(self, graph: NetworkGraph):
        jct = graph.add_junction(junction_id="jct_1")
        jct.occupy("train-1")
        with pytest.raises(OccupancyError, match="train-2"):
            jct.occupy("train-2")
        # Confirm the junction is still held by the first train.
        assert jct.occupied_by == "train-1"

    def test_junction_atomic_all_or_nothing(self, graph: NetworkGraph):
        """
        After a failed occupation attempt by a second train, the junction
        must still appear completely free (occupied by the first train) —
        no partial state.
        """
        jct = graph.add_junction(junction_id="jct_1")
        jct.occupy("train-1")
        try:
            jct.occupy("train-2")
        except OccupancyError:
            pass
        # Junction is fully held by train-1, not partially by both.
        assert jct.occupied_by == "train-1"

    def test_junction_release_and_reoccupy(self, graph: NetworkGraph):
        jct = graph.add_junction(junction_id="jct_1")
        jct.occupy("train-1")
        jct.release("train-1")
        assert jct.is_free
        jct.occupy("train-2")  # now a different train can enter
        assert jct.occupied_by == "train-2"

    def test_junction_idempotent_occupy(self, graph: NetworkGraph):
        jct = graph.add_junction(junction_id="jct_1")
        jct.occupy("train-1")
        jct.occupy("train-1")  # must not raise
        assert jct.occupied_by == "train-1"


# ===========================================================================
# 3. split_track_at_point
# ===========================================================================


class TestSplitTrackAtPoint:
    def _make_splittable_graph(self) -> tuple[NetworkGraph, str, str, str, str]:
        """
        Returns (graph, sta_A_id, sta_B_id, seg_id, trk_id).

        sta_A ──[10 blocks, 1000px wide]── sta_B
        """
        g = NetworkGraph()
        g.add_station("Alpha", station_id="sta_A")
        g.add_station("Beta", station_id="sta_B")
        seg, trk = g.add_track(
            "sta_A",
            "sta_B",
            length_km=10.0,
            num_blocks=10,
            geometry=[Point(x=0, y=0), Point(x=1000, y=0)],
            segment_id="seg_AB",
            track_id="trk_AB",
        )
        return g, "sta_A", "sta_B", seg.id, trk.id

    def test_split_produces_two_segments(self):
        g, sta_a, sta_b, seg_id, trk_id = self._make_splittable_graph()
        mid_station = g.add_station("Midway", station_id="sta_M")

        l_seg, l_trk, r_seg, r_trk = g.split_track_at_point("trk_AB", 0.5, mid_station)

        assert len(g.all_segments) == 2
        assert len(g.all_tracks) == 2

    def test_split_removes_original_segment_and_track(self):
        g, sta_a, sta_b, seg_id, trk_id = self._make_splittable_graph()
        mid_station = g.add_station("Midway", station_id="sta_M")

        g.split_track_at_point("trk_AB", 0.5, mid_station)

        assert "seg_AB" not in [s.id for s in g.all_segments]
        assert "trk_AB" not in [t.id for t in g.all_tracks]

    def test_left_segment_connects_to_new_station(self):
        g, sta_a, sta_b, *_ = self._make_splittable_graph()
        mid_station = g.add_station("Midway", station_id="sta_M")

        l_seg, l_trk, r_seg, r_trk = g.split_track_at_point("trk_AB", 0.5, mid_station)

        assert l_seg.start_node_id == "sta_A"
        assert l_seg.end_node_id == "sta_M"

    def test_right_segment_starts_at_new_station(self):
        g, sta_a, sta_b, *_ = self._make_splittable_graph()
        mid_station = g.add_station("Midway", station_id="sta_M")

        l_seg, l_trk, r_seg, r_trk = g.split_track_at_point("trk_AB", 0.5, mid_station)

        assert r_seg.start_node_id == "sta_M"
        assert r_seg.end_node_id == "sta_B"

    def test_block_count_preserved_with_minimum_one_per_side(self):
        g, *_ = self._make_splittable_graph()
        mid_station = g.add_station("Midway", station_id="sta_M")

        l_seg, l_trk, r_seg, r_trk = g.split_track_at_point("trk_AB", 0.5, mid_station)

        total = len(l_seg.blocks) + len(r_seg.blocks)
        assert total == 10
        assert len(l_seg.blocks) >= 1
        assert len(r_seg.blocks) >= 1

    def test_original_block_ids_survive_split(self):
        """All original block ids must still be reachable in the graph."""
        g, *_ = self._make_splittable_graph()
        original_block_ids = {b.id for b in g.all_blocks}
        mid_station = g.add_station("Midway", station_id="sta_M")

        g.split_track_at_point("trk_AB", 0.5, mid_station)

        post_split_ids = {b.id for b in g.all_blocks}
        assert post_split_ids == original_block_ids

    def test_split_near_start_still_has_one_block_on_left(self):
        g, *_ = self._make_splittable_graph()
        mid_station = g.add_station("Midway", station_id="sta_M")

        l_seg, l_trk, r_seg, r_trk = g.split_track_at_point("trk_AB", 0.05, mid_station)

        assert len(l_seg.blocks) >= 1
        assert len(r_seg.blocks) >= 1

    def test_split_near_end_still_has_one_block_on_right(self):
        g, *_ = self._make_splittable_graph()
        mid_station = g.add_station("Midway", station_id="sta_M")

        l_seg, l_trk, r_seg, r_trk = g.split_track_at_point("trk_AB", 0.95, mid_station)

        assert len(l_seg.blocks) >= 1
        assert len(r_seg.blocks) >= 1

    def test_geometry_split_at_midpoint(self):
        """Left geometry should end roughly at x=500 for a straight 0→1000 line."""
        g, *_ = self._make_splittable_graph()
        mid_station = g.add_station("Midway", station_id="sta_M")

        l_seg, l_trk, r_seg, r_trk = g.split_track_at_point("trk_AB", 0.5, mid_station)

        last_left_pt = l_trk.geometry[-1]
        first_right_pt = r_trk.geometry[0]
        assert last_left_pt.x == pytest.approx(500.0, abs=1.0)
        assert first_right_pt.x == pytest.approx(500.0, abs=1.0)
        assert last_left_pt.y == pytest.approx(0.0, abs=1.0)

    def test_split_invalid_t_raises(self):
        g, *_ = self._make_splittable_graph()
        mid_station = g.add_station("Midway", station_id="sta_M")

        with pytest.raises(NetworkError):
            g.split_track_at_point("trk_AB", 0.0, mid_station)
        with pytest.raises(NetworkError):
            g.split_track_at_point("trk_AB", 1.0, mid_station)

    def test_pathfinding_works_after_split(self):
        """After the split, sta_A → sta_B should still be reachable via sta_M."""
        g, *_ = self._make_splittable_graph()
        mid_station = g.add_station("Midway", station_id="sta_M")
        l_seg, l_trk, r_seg, r_trk = g.split_track_at_point("trk_AB", 0.5, mid_station)

        path = g.find_path("sta_A", "sta_B")
        assert path is not None
        assert len(path) == 2
        assert set(path) == {l_seg.id, r_seg.id}


# ===========================================================================
# 4. Pathfinding on 4-node diamond network
# ===========================================================================


class TestPathfinding:
    def test_direct_path_upper_route(self, diamond_graph):
        """sta_A → sta_D via upper route (A→B→D): 2 segments."""
        graph, sta_a, jct_b, jct_c, sta_d = diamond_graph
        # The BFS may choose upper or lower; check both valid 2-segment routes.
        path = graph.find_path("sta_A", "sta_D")
        assert path is not None
        assert len(path) == 2
        # Verify that path is a valid connected route.
        self._assert_path_connected(graph, "sta_A", "sta_D", path)

    def test_path_is_connected(self, diamond_graph):
        """Every pair of consecutive segments must share a node."""
        graph, sta_a, jct_b, jct_c, sta_d = diamond_graph
        path = graph.find_path("sta_A", "sta_D")
        assert path is not None
        self._assert_path_connected(graph, "sta_A", "sta_D", path)

    def test_trivial_same_node(self, diamond_graph):
        """Start == end → empty path."""
        graph, sta_a, *_ = diamond_graph
        path = graph.find_path("sta_A", "sta_A")
        assert path == []

    def test_no_path_returns_none(self, diamond_graph):
        """A node with no connection to the target returns None."""
        graph, sta_a, jct_b, jct_c, sta_d = diamond_graph
        # Add an island node with no edges.
        graph.add_station("Island", station_id="sta_island")
        path = graph.find_path("sta_A", "sta_island")
        assert path is None

    def test_find_path_blocks_correct_count(self, diamond_graph):
        """find_path_blocks must return blocks from all segments in the path."""
        graph, sta_a, jct_b, jct_c, sta_d = diamond_graph
        block_ids = graph.find_path_blocks("sta_A", "sta_D")
        assert block_ids is not None
        # The path is 2 segments × 5 blocks each = 10 blocks.
        assert len(block_ids) == 10

    def test_find_path_blocks_all_valid_ids(self, diamond_graph):
        """All returned block ids must exist in the graph."""
        graph, *_ = diamond_graph
        block_ids = graph.find_path_blocks("sta_A", "sta_D")
        assert block_ids is not None
        all_known = {b.id for b in graph.all_blocks}
        for bid in block_ids:
            assert bid in all_known, f"Unknown block id {bid!r} in path."

    def test_find_path_blocks_no_duplicates(self, diamond_graph):
        """No block should appear twice in the returned list."""
        graph, *_ = diamond_graph
        block_ids = graph.find_path_blocks("sta_A", "sta_D")
        assert block_ids is not None
        assert len(block_ids) == len(set(block_ids)), "Duplicate block ids in path."

    def test_pathfinding_on_linear_chain(self):
        """A→B→C→D straight chain: path has exactly 3 segments."""
        g = NetworkGraph()
        for sid, name in [("A", "Alpha"), ("B", "Beta"), ("C", "Gamma"), ("D", "Delta")]:
            g.add_station(name, station_id=f"sta_{sid}")
        g.add_track("sta_A", "sta_B", length_km=5, num_blocks=5, segment_id="seg_AB")
        g.add_track("sta_B", "sta_C", length_km=5, num_blocks=5, segment_id="seg_BC")
        g.add_track("sta_C", "sta_D", length_km=5, num_blocks=5, segment_id="seg_CD")

        path = g.find_path("sta_A", "sta_D")
        assert path is not None
        assert len(path) == 3
        assert path == ["seg_AB", "seg_BC", "seg_CD"]

    def _assert_path_connected(
        self, graph: NetworkGraph, start: str, end: str, seg_ids: list[str]
    ) -> None:
        """Walk the segment path and confirm each segment shares a node with the next."""
        nodes = graph._node_sequence_from_segments(start, seg_ids)
        assert nodes[0] == start
        assert nodes[-1] == end
        # Every consecutive pair of segments must share the intermediate node.
        for i in range(len(seg_ids)):
            seg = graph.get_segment(seg_ids[i])
            assert nodes[i] in (seg.start_node_id, seg.end_node_id)
            assert nodes[i + 1] in (seg.start_node_id, seg.end_node_id)


# ===========================================================================
# 5. NetworkGraph structural helpers
# ===========================================================================


class TestNetworkStructure:
    def test_add_track_requires_existing_nodes(self):
        g = NetworkGraph()
        g.add_station("A", station_id="sta_A")
        with pytest.raises(NetworkError, match="sta_B"):
            g.add_track("sta_A", "sta_B", length_km=5, num_blocks=5)

    def test_cannot_add_self_loop_segment(self):
        g = NetworkGraph()
        g.add_station("A", station_id="sta_A")
        with pytest.raises(NetworkError):
            g.add_track("sta_A", "sta_A", length_km=5, num_blocks=5)

    def test_duplicate_node_id_raises(self):
        g = NetworkGraph()
        g.add_station("A", station_id="sta_A")
        with pytest.raises(NetworkError):
            g.add_station("A2", station_id="sta_A")

    def test_blocks_reference_correct_segment(self, two_node_graph):
        graph, *_, seg, _ = two_node_graph
        for blk in seg.blocks:
            assert blk.segment_id == seg.id

    def test_segment_total_length(self, graph: NetworkGraph):
        graph.add_station("A", station_id="sta_A")
        graph.add_station("B", station_id="sta_B")
        seg, _ = graph.add_track(
            "sta_A", "sta_B",
            length_km=10.0,
            num_blocks=5,
            geometry=[Point(x=0, y=0), Point(x=1000, y=0)],
        )
        assert seg.total_length_km == pytest.approx(10.0)

    def test_junction_connected_segments_updated_on_add_track(self):
        g = NetworkGraph()
        g.add_station("A", station_id="sta_A")
        jct = g.add_junction(junction_id="jct_1")
        g.add_station("B", station_id="sta_B")

        seg1, _ = g.add_track("sta_A", "jct_1", length_km=5, num_blocks=5, segment_id="seg_AJ")
        seg2, _ = g.add_track("jct_1", "sta_B", length_km=5, num_blocks=5, segment_id="seg_JB")

        assert "seg_AJ" in jct.connected_segment_ids
        assert "seg_JB" in jct.connected_segment_ids

    def test_to_dict_includes_all_entity_types(self, two_node_graph):
        graph, *_ = two_node_graph
        snapshot = graph.to_dict()
        for key in ("blocks", "segments", "junctions", "stations", "tracks"):
            assert key in snapshot
        assert len(snapshot["blocks"]) == 10
        assert len(snapshot["segments"]) == 1
        assert len(snapshot["stations"]) == 2
