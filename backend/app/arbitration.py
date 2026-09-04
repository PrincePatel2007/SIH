"""
arbitration.py — Junction-level priority resolution and ETA propagation.

Architecture contract
---------------------
* This module sits between network.py (structural facts) and engine.py
  (commit layer).  It computes WHAT should happen; engine.py commits it.
* Imports allowed:  models.py, network.py, train.py
* Imports NOT allowed: engine.py  (would create a circular dependency)
* arbitration.py is the ONLY module that calls train.update_expected_arrival()
  and train.propagate_eta_update().
* engine.py is the ONLY module that calls block.occupy() / junction.occupy().
  When a grant is issued here, we set the signal to GREEN but do NOT occupy
  the block — that happens in engine.py when the train physically moves.

4-Aspect Signal Engine
----------------------
Each approach to a junction (or any block entry point) carries one of:

    RED         — next block occupied OR no grant yet for this train.
                  A train MUST NOT enter a RED block.  This is enforced as a
                  hard guard in try_advance(), not just a display value.

    YELLOW      — next block free, but the block after it is occupied.
                  Train decelerates to 40% of its current target speed.

    DOUBLE_YELLOW — next two blocks free, third block occupied.
                  Train decelerates to 70% of its current target speed.

    GREEN       — next 3+ blocks free (or end of route).
                  Train may proceed at full track speed.

Priority ranking at a junction (lower effective_rank = higher priority)
-----------------------------------------------------------------------
    0  — is_max_priority_override == True  (over_duty or ≥12h journey)
    1  — PriorityTier.EXPRESS
    2  — PriorityTier.ORDINARY
    3  — PriorityTier.LOCAL

Ties (same effective_rank) broken by earliest requested_eta.

ETA propagation chain
---------------------
When a train's passage is granted (possibly delayed), it:
  1. Updates its own expected_arrival for this junction.
  2. Recomputes expected_arrival for the NEXT node in its route.
  3. Fires a new request_passage() at the next node's arbiter.
This chains forward automatically through propagate_eta_update().
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Optional

from app.models import SignalStateValue
from app.network import BlockRuntime, JunctionRuntime, NetworkError, NetworkGraph
from app.train import TrainRuntime, travel_time_hours

if TYPE_CHECKING:
    # Avoid circular import at runtime; only used for type hints.
    pass


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SignalViolationError(Exception):
    """
    Raised when a train attempts to advance into a block while its approach
    signal is RED.  This is an invariant violation — the engine must never
    let this happen during normal operation.
    """


class ArbitrationError(Exception):
    """Raised for structural errors in arbiter setup or usage."""


# ---------------------------------------------------------------------------
# 4-Aspect signal
# ---------------------------------------------------------------------------


class SignalAspect(str, Enum):
    """
    Internal 4-aspect signal used for speed control and entry guards.

    Mapped to the frontend-facing SignalStateValue as follows:
        RED           → SignalStateValue.RED
        YELLOW        → SignalStateValue.CAUTION
        DOUBLE_YELLOW → SignalStateValue.CAUTION
        GREEN         → SignalStateValue.GREEN
    """

    RED = "RED"
    YELLOW = "YELLOW"
    DOUBLE_YELLOW = "DOUBLE_YELLOW"
    GREEN = "GREEN"


# Speed factor applied to target speed for each aspect.
ASPECT_SPEED_FACTOR: dict[SignalAspect, float] = {
    SignalAspect.RED: 0.0,           # must not move
    SignalAspect.YELLOW: 0.40,       # 40% of target speed
    SignalAspect.DOUBLE_YELLOW: 0.70,  # 70% of target speed
    SignalAspect.GREEN: 1.0,         # full track speed
}


def aspect_to_signal_state_value(aspect: SignalAspect) -> SignalStateValue:
    """Convert internal 4-aspect to the frontend-contract SignalStateValue."""
    if aspect == SignalAspect.RED:
        return SignalStateValue.RED
    if aspect == SignalAspect.GREEN:
        return SignalStateValue.GREEN
    return SignalStateValue.CAUTION  # YELLOW and DOUBLE_YELLOW both map to caution


# ---------------------------------------------------------------------------
# Block-ahead lookahead — computes the 4-aspect from network state
# ---------------------------------------------------------------------------


def compute_signal_aspect(
    train_id: str,
    next_block_ids: list[str],  # ordered: [immediate next, +1, +2, ...]
    network: NetworkGraph,
    granted_to_train_id: Optional[str],  # which train has a grant for next_block
) -> SignalAspect:
    """
    Compute the 4-aspect signal for a train approaching a series of blocks.

    Parameters
    ----------
    train_id : str
        The requesting train (used to check whether the junction grant
        belongs to this train).
    next_block_ids : list[str]
        Ordered list of upcoming block ids, starting with the very next one.
        May be shorter than 3 if the train is near its destination.
    network : NetworkGraph
        Used to look up block occupancy.
    granted_to_train_id : str | None
        The train that currently holds a grant to enter next_block_ids[0].
        None if no grant has been issued yet.

    Returns
    -------
    SignalAspect
        The aspect to display/enforce for this train.
    """
    if not next_block_ids:
        # No blocks ahead — end of route; GREEN so the train can complete.
        return SignalAspect.GREEN

    # RED condition: next block occupied by a DIFFERENT train, OR no grant
    # for THIS train to the next block.
    first_block = network.get_block(next_block_ids[0])
    if not first_block.is_free:
        return SignalAspect.RED
    if granted_to_train_id != train_id:
        return SignalAspect.RED

    # From here the next block is free and granted.  Look further ahead.
    if len(next_block_ids) < 2:
        return SignalAspect.GREEN

    second_block = network.get_block(next_block_ids[1])
    if not second_block.is_free:
        return SignalAspect.YELLOW  # next free, block after occupied

    if len(next_block_ids) < 3:
        return SignalAspect.GREEN

    third_block = network.get_block(next_block_ids[2])
    if not third_block.is_free:
        return SignalAspect.DOUBLE_YELLOW  # next 2 free, 3rd occupied

    return SignalAspect.GREEN  # 3+ blocks ahead all free


# ---------------------------------------------------------------------------
# Passage request / grant data structures
# ---------------------------------------------------------------------------


@dataclass(order=False)
class PassageRequest:
    """
    A train's request to pass through a junction.

    Stored in JunctionArbiter._pending sorted by effective priority.
    """

    train: TrainRuntime
    requested_eta: datetime
    # Effective rank: 0 = max-override, 1/2/3 = express/ordinary/local
    effective_rank: int = field(init=False, compare=False)
    # Set when network is available for override check
    _rank_set: bool = field(default=False, init=False, repr=False, compare=False)

    def set_rank(self, network: NetworkGraph) -> None:
        if self.train.is_max_priority_override(network):
            self.effective_rank = 0
        else:
            self.effective_rank = int(self.train.priority)
        self._rank_set = True

    def sort_key(self) -> tuple[int, datetime]:
        """Lower tuple = higher priority.  Rank first, then earliest ETA."""
        return (self.effective_rank, self.requested_eta)


@dataclass
class PassageGrant:
    """
    The result of JunctionArbiter.resolve() for one train.
    """

    train_id: str
    junction_id: str
    requested_eta: datetime
    actual_permitted_pass_time: datetime
    wait_duration: timedelta  # actual_permitted_pass_time - requested_eta (>= 0)


# ---------------------------------------------------------------------------
# JunctionArbiter
# ---------------------------------------------------------------------------


class JunctionArbiter:
    """
    Priority resolver for a single junction.

    One JunctionArbiter is created per JunctionRuntime in the network.  The
    simulation engine (or ArbitrationRegistry) holds the mapping
    junction_id → JunctionArbiter.

    Lifecycle
    ---------
    1. A train approaching this junction calls request_passage(train, eta).
    2. resolve() is called (either immediately or on the next tick) to
       re-rank all pending requests and issue grants.
    3. Each granted train then calls propagate_eta_update() to chain the
       update forward to the next node in its route.
    4. When a train physically enters and then exits the junction, the engine
       calls clear_grant(train_id) to remove the grant and allow the next
       pending train to be granted.

    Parameters
    ----------
    junction : JunctionRuntime
        The junction this arbiter manages.
    junction_clearance_minutes : float
        Time (in minutes) added between consecutive grants to ensure the
        previous train has physically cleared the junction before the next
        is allowed in.  Defaults to 2 minutes.
    """

    def __init__(
        self,
        junction: JunctionRuntime,
        junction_clearance_minutes: float = 2.0,
    ) -> None:
        self._junction = junction
        self._clearance = timedelta(minutes=junction_clearance_minutes)
        # Pending requests, not yet granted.
        self._pending: list[PassageRequest] = []
        # Grants issued in this resolution cycle: train_id → PassageGrant.
        self._grants: dict[str, PassageGrant] = {}
        # The time after which the junction is physically clear for the next train.
        self._junction_free_at: Optional[datetime] = None

    @property
    def junction_id(self) -> str:
        return self._junction.id

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    def request_passage(
        self,
        train: TrainRuntime,
        requested_eta: datetime,
        network: NetworkGraph,
    ) -> None:
        """
        Register a train's request to pass through this junction at *requested_eta*.

        If the train already has a pending request, its ETA is updated to the
        new value (re-arbitration will happen on the next resolve() call).
        Immediately calls resolve() to propagate the update.
        """
        # Remove any existing pending request from this train (ETA changed).
        self._pending = [r for r in self._pending if r.train.id != train.id]
        # Also remove any existing grant (the train's ETA has changed; re-grant).
        self._grants.pop(train.id, None)

        req = PassageRequest(train=train, requested_eta=requested_eta)
        req.set_rank(network)
        self._pending.append(req)

        # Resolve immediately on every new request.
        self.resolve(network)

    def resolve(self, network: NetworkGraph) -> list[PassageGrant]:
        """
        Re-rank all pending requests and issue grants.

        Grants are issued in priority order.  Each successive grant is
        separated by *junction_clearance_minutes* to prevent collisions.

        Returns the list of newly issued grants (useful for testing / logging).

        The method is idempotent — calling it multiple times without new
        requests produces the same result.
        """
        if not self._pending:
            return []

        # Sort by (effective_rank, requested_eta) — lowest tuple = first.
        sorted_requests = sorted(self._pending, key=lambda r: r.sort_key())

        new_grants: list[PassageGrant] = []
        # The earliest time the junction is available for the next train.
        slot_start = self._junction_free_at or sorted_requests[0].requested_eta

        for req in sorted_requests:
            # Each train gets the junction no earlier than:
            # a) when the junction is free (slot_start), or
            # b) its own requested_eta.
            grant_time = max(slot_start, req.requested_eta)

            # If a previous grant for this exact train existed with the same
            # time, no update needed — skip re-notifying.
            existing = self._grants.get(req.train.id)
            if (
                existing is not None
                and existing.actual_permitted_pass_time == grant_time
            ):
                slot_start = grant_time + self._clearance
                continue

            grant = PassageGrant(
                train_id=req.train.id,
                junction_id=self.junction_id,
                requested_eta=req.requested_eta,
                actual_permitted_pass_time=grant_time,
                wait_duration=max(timedelta(0), grant_time - req.requested_eta),
            )
            self._grants[req.train.id] = grant
            new_grants.append(grant)

            # The junction becomes free *clearance* after this grant.
            slot_start = grant_time + self._clearance

        return new_grants

    def get_grant(self, train_id: str) -> Optional[PassageGrant]:
        """Return the current grant for *train_id*, or None if not yet granted."""
        return self._grants.get(train_id)

    def clear_grant(self, train_id: str) -> None:
        """
        Called by engine.py when a train has physically cleared the junction.
        Removes the grant and updates _junction_free_at so the next pending
        train can be granted at the correct time.
        """
        grant = self._grants.pop(train_id, None)
        if grant is not None:
            # Junction is free after the clearance window from this grant.
            free_at = grant.actual_permitted_pass_time + self._clearance
            if self._junction_free_at is None or free_at > self._junction_free_at:
                self._junction_free_at = free_at
        # Remove from pending too (train has passed).
        self._pending = [r for r in self._pending if r.train.id != train_id]

    def granted_train_id(self) -> Optional[str]:
        """
        Return the train_id with the earliest actual_permitted_pass_time among
        current grants, i.e. the train that should next physically enter.
        Returns None if no grants are outstanding.
        """
        if not self._grants:
            return None
        return min(
            self._grants,
            key=lambda tid: self._grants[tid].actual_permitted_pass_time,
        )

    # -----------------------------------------------------------------------
    # Signal derivation
    # -----------------------------------------------------------------------

    def signal_aspect_for(
        self,
        train_id: str,
        approaching_from_segment_id: str,
        network: NetworkGraph,
        next_block_ids: list[str],
    ) -> SignalAspect:
        """
        Compute the 4-aspect signal for a train approaching this junction.

        Parameters
        ----------
        train_id : str
        approaching_from_segment_id : str
            The segment the train is coming from (used to look up the signal
            registered on the junction for this approach).
        network : NetworkGraph
        next_block_ids : list[str]
            The next 1–3+ block ids the train would enter after the junction.
        """
        grant = self._grants.get(train_id)
        granted_to = grant.train_id if grant is not None else None
        return compute_signal_aspect(train_id, next_block_ids, network, granted_to)


# ---------------------------------------------------------------------------
# ArbitrationRegistry — engine-level map of junction_id → JunctionArbiter
# ---------------------------------------------------------------------------


class ArbitrationRegistry:
    """
    Holds one JunctionArbiter per junction in the network.

    The engine creates one registry per simulation run and passes it to
    propagate_eta_update() calls.
    """

    def __init__(self, junction_clearance_minutes: float = 2.0) -> None:
        self._arbiters: dict[str, JunctionArbiter] = {}
        self._clearance = junction_clearance_minutes

    def register_junction(self, junction: JunctionRuntime) -> JunctionArbiter:
        """Create and register an arbiter for *junction*."""
        if junction.id in self._arbiters:
            return self._arbiters[junction.id]
        arbiter = JunctionArbiter(junction, self._clearance)
        self._arbiters[junction.id] = arbiter
        return arbiter

    def get_arbiter(self, junction_id: str) -> Optional[JunctionArbiter]:
        return self._arbiters.get(junction_id)

    def require_arbiter(self, junction_id: str) -> JunctionArbiter:
        a = self._arbiters.get(junction_id)
        if a is None:
            raise ArbitrationError(
                f"No arbiter registered for junction {junction_id!r}. "
                "Ensure the engine calls register_junction() before simulation starts."
            )
        return a

    @classmethod
    def from_network(
        cls,
        network: NetworkGraph,
        junction_clearance_minutes: float = 2.0,
    ) -> "ArbitrationRegistry":
        """Convenience: build a registry pre-populated from all network junctions."""
        registry = cls(junction_clearance_minutes)
        for jct in network.all_junctions:
            registry.register_junction(jct)
        return registry


# ---------------------------------------------------------------------------
# ETA propagation — called on the TRAIN, not buried in the arbiter
# ---------------------------------------------------------------------------


def propagate_eta_update(
    train: TrainRuntime,
    current_node_id: str,
    granted_pass_time: datetime,
    network: NetworkGraph,
    registry: ArbitrationRegistry,
    effective_speed_kmh: float,
) -> None:
    """
    After a junction grant is issued to *train* at *current_node_id*, update
    the train's expected_arrival for that node and cascade forward to the next
    node in the route.

    This is the "ask ahead, then update again" chain from the spec:

        grant at node N
          → update expected_arrival[N]
          → recompute ETA to node N+1  (using effective_speed + remaining distance)
          → request_passage at node N+1's arbiter
          → that arbiter resolves, potentially further delaying N+1
          → (recursive cascade stops when the next node has no arbiter,
             i.e. it's a station rather than a junction, or is the final node)

    Parameters
    ----------
    train : TrainRuntime
    current_node_id : str
        The junction (or node) whose grant just fired.
    granted_pass_time : datetime
        The actual_permitted_pass_time from the grant (may be > requested ETA).
    network : NetworkGraph
    registry : ArbitrationRegistry
        The engine's junction arbiter map.
    effective_speed_kmh : float
        The train's current effective speed (accounts for weather/conditions).
        Passed in from the engine/conditions layer; not computed here.
    """
    # Step 1: update expected_arrival at the current node.
    train.update_expected_arrival(current_node_id, granted_pass_time)

    # Step 2: find the next hop in the route.
    route = train.route
    current_idx: Optional[int] = None
    for i, hop in enumerate(route):
        if hop.node_id == current_node_id:
            current_idx = i
            break

    if current_idx is None or current_idx >= len(route) - 1:
        # No next hop — we're at the final destination.
        return

    next_hop = route[current_idx + 1]
    if next_hop.segment_id is None:
        # Structural error — non-origin hop with no segment.
        return

    # Step 3: recompute ETA to next node.
    try:
        dist_km = network.get_segment(next_hop.segment_id).total_length_km
    except NetworkError:
        return  # Segment not in network; skip cascade.

    if effective_speed_kmh <= 0.0:
        return

    travel_h = travel_time_hours(dist_km, effective_speed_kmh)
    next_eta = granted_pass_time + timedelta(hours=travel_h)

    # Step 4: if the next node is a junction, fire a new request there.
    next_arbiter = registry.get_arbiter(next_hop.node_id)
    if next_arbiter is not None:
        next_arbiter.request_passage(train, next_eta, network)
        next_grant = next_arbiter.get_grant(train.id)
        if next_grant is not None:
            # Recursively propagate (the cascade continues).
            propagate_eta_update(
                train=train,
                current_node_id=next_hop.node_id,
                granted_pass_time=next_grant.actual_permitted_pass_time,
                network=network,
                registry=registry,
                effective_speed_kmh=effective_speed_kmh,
            )
    else:
        # Next node is a station (no arbiter) — just update expected_arrival.
        train.update_expected_arrival(next_hop.node_id, next_eta)


# ---------------------------------------------------------------------------
# Advance guard — enforces RED signal as a hard block, not just a display
# ---------------------------------------------------------------------------


def try_advance(
    train: TrainRuntime,
    next_block: BlockRuntime,
    network: NetworkGraph,
    registry: ArbitrationRegistry,
    lookahead_block_ids: list[str],  # [next_block.id, block+1, block+2, ...]
    approaching_junction_id: Optional[str] = None,
) -> SignalAspect:
    """
    Determine whether a train may advance into *next_block*, and return the
    signal aspect controlling entry.

    If the signal is RED, raises SignalViolationError — the engine must not
    move the train.  This is the hard enforcement point for the "never enter a
    RED block" rule.

    Parameters
    ----------
    train : TrainRuntime
        The train requesting to advance.
    next_block : BlockRuntime
        The block the train wants to enter.
    network : NetworkGraph
        For block occupancy queries.
    registry : ArbitrationRegistry
        For checking whether this train has a grant to the junction ahead.
    lookahead_block_ids : list[str]
        [next_block.id, second_block.id, third_block.id, ...] for aspect calc.
        Must start with next_block.id.
    approaching_junction_id : str | None
        If the train is approaching a junction, supply its id here so the
        arbiter's grant can be checked.  None for mid-segment block transitions
        (block-to-block within a segment, no junction arbitration needed).

    Returns
    -------
    SignalAspect
        The computed aspect.  The caller (engine) can use this to set the
        train's target speed (via ASPECT_SPEED_FACTOR).

    Raises
    ------
    SignalViolationError
        If the computed aspect is RED.
    """
    # Determine which train holds the grant for the immediate next block.
    granted_to: Optional[str] = None
    if approaching_junction_id is not None:
        arbiter = registry.get_arbiter(approaching_junction_id)
        if arbiter is not None:
            grant = arbiter.get_grant(train.id)
            granted_to = train.id if grant is not None else None
        else:
            # Junction exists but no arbiter — treat as no grant.
            granted_to = None
    else:
        # Mid-segment block: a train may advance as long as the block is free.
        # No junction arbitration needed — signal is GREEN if block is free.
        if next_block.is_free:
            granted_to = train.id  # implicit "self-grant" for intra-segment moves
        else:
            granted_to = None

    aspect = compute_signal_aspect(
        train_id=train.id,
        next_block_ids=lookahead_block_ids,
        network=network,
        granted_to_train_id=granted_to,
    )

    if aspect == SignalAspect.RED:
        raise SignalViolationError(
            f"Train {train.id!r} ({train.name!r}) attempted to advance into block "
            f"{next_block.id!r} while signal is RED. "
            f"Block occupied_by={next_block.occupied_by!r}, "
            f"grant_held={'yes' if granted_to == train.id else 'no'}, "
            f"junction={approaching_junction_id!r}."
        )

    return aspect
