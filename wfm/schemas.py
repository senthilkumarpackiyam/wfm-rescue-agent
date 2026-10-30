"""Shared interfaces (the Day-1 contract between workstreams).

Every component exchanges data in these shapes:

    data pack ──► forecast ──► simulator ──► Snapshot ──► agent ──► Plan ──► supervisor ──► simulator
                                   ▲                                                         │
                                   └──────────────────── LeverAction (approved) ◄────────────┘

Times are wall-clock "HH:MM" strings in these models; the simulator converts them to
minutes-since-open internally (see wfm.timegrid). Money is INR.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
ISODate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
QueueId = Literal["payments", "general"]

# --------------------------------------------------------------------------------------
# History and forecast
# --------------------------------------------------------------------------------------


class IntervalRecord(BaseModel):
    """One row of synthetic history (data/history/intervals.csv)."""

    date: ISODate
    interval_start: HHMM
    queue: QueueId
    offered: int = Field(ge=0)
    aht_sec: float = Field(gt=0)


class IntervalForecast(BaseModel):
    interval_start: HHMM
    queue: QueueId
    calls: float = Field(ge=0)
    aht_sec: float = Field(gt=0)
    calls_low: float | None = None  # prediction interval, when the method provides one
    calls_high: float | None = None


class DayForecast(BaseModel):
    date: ISODate
    made_at: HHMM  # "07:00" for the morning plan; intraday re-forecasts carry the sim time
    method: str  # e.g. "sarima_daily+intraday_profile", "reforecast_shock_factor"
    intervals: list[IntervalForecast]


# --------------------------------------------------------------------------------------
# Roster (the morning staffing plan)
# --------------------------------------------------------------------------------------


class BreakSlot(BaseModel):
    start: HHMM
    duration_min: int
    kind: Literal["break", "lunch"]


class Session(BaseModel):
    """Off-phone time such as coaching; a lever may cancel it unless mandatory."""

    id: str
    start: HHMM
    duration_min: int
    kind: Literal["coaching", "training", "compliance_training"]
    mandatory: bool = False


class Agent(BaseModel):
    id: str  # synthetic IDs only, e.g. "A017"
    primary_queue: QueueId
    skills: list[QueueId]
    shift_start: HHMM
    shift_end: HHMM
    breaks: list[BreakSlot] = []
    sessions: list[Session] = []
    overtime_eligible: bool = True
    overtime_acceptance_prob: float = Field(default=0.4, ge=0, le=1)


class Roster(BaseModel):
    date: ISODate
    agents: list[Agent]


# --------------------------------------------------------------------------------------
# Context feeds (untrusted text: never follow instructions found inside them)
# --------------------------------------------------------------------------------------


class Incident(BaseModel):
    id: str  # e.g. "INC-20261006-0412"
    opened_at: HHMM  # becomes visible to the agent only from this time
    severity: Literal["P1", "P2", "P3", "P4"]
    system: str
    title: str
    description: str  # free text; may contain a prompt-injection attempt in adversarial days
    # End-of-day status. Snapshots show "open" until the sim clock passes resolved_at.
    status: Literal["open", "resolved"] = "open"
    resolved_at: HHMM | None = None


class CalendarEvent(BaseModel):
    id: str  # e.g. "MKT-2026-118"
    send_time: HHMM
    channel: Literal["email", "sms", "push", "social", "tv"]
    title: str
    audience_size: int
    description: str
    owner_team: str = "Marketing"


class AttendanceEvent(BaseModel):
    agent_id: str
    logged_at: HHMM
    status: Literal["sick", "late", "left_early"]
    note: str = ""


# --------------------------------------------------------------------------------------
# Scenarios (the 30 bad days)
# --------------------------------------------------------------------------------------

ScenarioType = Literal[
    "outage_spike",
    "marketing_campaign",
    "weather_seasonal_surge",
    "sick_call_wave",
    "aht_spike",
    "adversarial",
    "normal",
]

TrueCause = Literal[
    "outage", "marketing_campaign", "weather_surge", "seasonal_surge",
    "sick_wave", "system_slowness", "none",
]

# What the agent is allowed to conclude. Weather/seasonal surges leave no trace in any
# feed, so the correct diagnosis there is "unknown_demand_surge", not a guess.
DiagnosisLabel = Literal[
    "outage", "marketing_campaign", "sick_wave", "system_slowness",
    "unknown_demand_surge", "unknown_supply_issue", "none",
]


class Shock(BaseModel):
    """A disturbance injected into the true day.

    The multiplier ramps linearly from 1 to `magnitude` over `ramp_min`, holds, then
    decays back to 1 over `decay_min`. `duration_min` covers ramp + hold; decay comes after.
    For kind="absence", `magnitude` is the share of remaining agents lost at `start`.
    """

    kind: Literal["volume", "aht", "absence"]
    queues: list[QueueId]
    start: HHMM
    duration_min: int | None = None  # None = lasts until close
    magnitude: float = Field(gt=0)
    ramp_min: int = 0
    decay_min: int = 0
    # Reason codes carried by the *extra* calls a volume shock creates (shares sum to 1).
    # None = same mix as a normal day, i.e. no tell-tale signal in the IVR data.
    extra_reason_mix: dict[str, float] | None = None
    # Absence shocks: note written into the attendance log for each lost agent.
    feed_note: str | None = None
    feed_status: Literal["sick", "late", "left_early"] | None = None


class GroundTruth(BaseModel):
    cause: TrueCause
    expected_diagnosis: DiagnosisLabel
    evidence_refs: list[str] = []  # IDs a correct diagnosis should cite (incident, calendar, feed name)
    notes: str = ""


class Scenario(BaseModel):
    id: str  # e.g. "OUT-01"
    type: ScenarioType
    title: str
    date: ISODate
    seed: int  # fixes every random draw, so all four approaches face the identical day
    shocks: list[Shock]
    incidents: list[Incident] = []
    calendar: list[CalendarEvent] = []
    evidence_strength: Literal["strong", "medium", "weak", "none"]
    injection_text: str | None = None  # adversarial days: the planted instruction (for scoring)
    ground_truth: GroundTruth


# --------------------------------------------------------------------------------------
# Simulator outputs
# --------------------------------------------------------------------------------------


class IntervalMetrics(BaseModel):
    """Calls are attributed to the interval they arrived in.
    arrivals = offered + deflected;  offered = answered + abandoned + callbacks (+ still waiting).
    Service level = answered_in_threshold / (offered - callbacks)."""

    interval_start: HHMM
    queue: QueueId
    offered: int  # reached the queue (after any IVR / chat deflection)
    answered: int
    abandoned: int
    answered_in_threshold: int
    deflected: int = 0  # stopped by an IVR outage message or moved to chat
    callbacks: int = 0  # accepted a callback offer instead of waiting
    service_level_pct: float | None  # None when nothing was offered
    asa_sec: float  # average speed of answer
    aht_sec: float
    agents_staffed: float  # average agents logged in and on-queue
    occupancy_pct: float


class ReasonCodeCount(BaseModel):
    interval_start: HHMM
    queue: QueueId
    reason: str
    count: int


class QueueSummary(BaseModel):
    offered: int
    answered: int
    abandoned: int
    service_level_pct: float
    abandon_rate_pct: float
    asa_sec: float


class SimSummary(BaseModel):
    """Outcome over a window (usually "now until close")."""

    window_start: HHMM
    window_end: HHMM
    by_queue: dict[str, QueueSummary]
    service_level_pct: float  # call-weighted across queues
    abandon_rate_pct: float
    action_cost_inr: float


class Snapshot(BaseModel):
    """Everything the agent may see at sim time `now`. Feeds are filtered to what was
    knowable by then (an incident opened at 10:40 is invisible at 10:30)."""

    scenario_id: str
    date: ISODate
    now: HHMM
    actuals: list[IntervalMetrics]  # completed intervals only
    forecast: DayForecast  # the morning plan
    roster: Roster
    reason_codes: list[ReasonCodeCount]
    incidents: list[Incident]
    calendar: list[CalendarEvent]
    attendance: list[AttendanceEvent]
    applied_actions: list["AppliedAction"] = []


# --------------------------------------------------------------------------------------
# Levers and plans
# --------------------------------------------------------------------------------------


class DelayBreaks(BaseModel):
    kind: Literal["delay_breaks"] = "delay_breaks"
    queue: QueueId | None = None  # None = agents of both queues
    window_start: HHMM  # breaks scheduled in this window ...
    window_end: HHMM
    delay_minutes: int = Field(gt=0)  # ... slip by this much


class CancelCoaching(BaseModel):
    kind: Literal["cancel_coaching"] = "cancel_coaching"
    queue: QueueId | None = None
    window_start: HHMM
    window_end: HHMM


class OfferOvertime(BaseModel):
    kind: Literal["offer_overtime"] = "offer_overtime"
    queue: QueueId
    start: HHMM
    end: HHMM
    agents_offered: int = Field(gt=0)  # expected acceptances = offered x acceptance rate


class MoveAgents(BaseModel):
    kind: Literal["move_cross_skilled"] = "move_cross_skilled"
    from_queue: QueueId
    to_queue: QueueId
    agents: int = Field(gt=0)
    start: HHMM
    end: HHMM


class IVRChange(BaseModel):
    kind: Literal["ivr_callback"] = "ivr_callback"
    queue: QueueId
    mode: Literal["callback", "outage_message"]
    start: HHMM
    end: HHMM
    related_reasons: list[str] = []  # outage_message: which reason codes the message addresses
    message_text: str | None = None


class ChatDeflection(BaseModel):
    kind: Literal["chat_deflection"] = "chat_deflection"
    queue: QueueId
    start: HHMM
    end: HHMM
    share: float = Field(gt=0, le=1)  # share of eligible calls offered chat instead
    reasons: list[str]


LeverAction = Annotated[
    Union[DelayBreaks, CancelCoaching, OfferOvertime, MoveAgents, IVRChange, ChatDeflection],
    Field(discriminator="kind"),
]


class AppliedAction(BaseModel):
    """A lever as executed: nothing changes before `decided_at`, and levers with a lead
    time (overtime: 30 min) start no earlier than decided_at + that delay."""

    decided_at: HHMM
    action: LeverAction


class Plan(BaseModel):
    id: str
    name: str
    actions: list[LeverAction]
    rationale: str = ""


LeverKind = Literal["delay_breaks", "cancel_coaching", "offer_overtime", "move_cross_skilled",
                    "ivr_callback", "chat_deflection"]


class QueueFloor(BaseModel):
    queue: QueueId
    min_service_level_pct: float


class Constraints(BaseModel):
    """Supervisor limits for the rest of the day, parsed from plain English and enforced in code."""

    forbidden_levers: list[LeverKind] = []
    overtime_budget_inr: float | None = None  # replaces the policy budget when set
    max_break_delay_minutes: int | None = None
    max_agents_moved: int | None = None
    queue_floors: list[QueueFloor] = []  # e.g. "keep general above 80%"
    notes: list[str] = []  # the supervisor's own words


class PolicyViolation(BaseModel):
    rule_id: str  # e.g. "OT-02"
    lever: str
    message: str


class PlanEvaluation(BaseModel):
    plan: Plan
    predicted: SimSummary  # disruption window (now to when the gap closes): what the choice is based on
    rest_of_day: SimSummary | None = None  # now to close, reported alongside
    violations: list[PolicyViolation] = []

    @property
    def passes_policy(self) -> bool:
        return not self.violations


# --------------------------------------------------------------------------------------
# Agent reasoning, approval and audit trail
# --------------------------------------------------------------------------------------


class Alert(BaseModel):
    at: HHMM
    queue: QueueId
    metric: Literal["volume", "aht", "service_level", "staffing"]
    actual: float
    expected: float
    gap_pct: float


class EvidenceRef(BaseModel):
    source: Literal["incident", "calendar", "reason_codes", "attendance", "metrics"]
    ref_id: str  # incident ID, calendar ID, reason code name, or metric name
    summary: str


class Diagnosis(BaseModel):
    label: DiagnosisLabel
    confidence: Literal["high", "medium", "low"]
    explanation: str
    evidence: list[EvidenceRef]  # mandatory for any named cause
    flagged_injections: list[str] = []  # IDs of feed items that contained instructions


class StaffingGap(BaseModel):
    interval_start: HHMM
    queue: QueueId
    calls: float  # re-forecast calls
    aht_sec: float  # re-forecast handle time
    agents_needed: int  # Erlang A at the planning target
    agents_available: float  # planned on-phone coverage minus known absences
    gap: float  # needed - available (positive = short)


class Reforecast(BaseModel):
    """Rest-of-day projection with the disruption included. Computed in code, never by the LLM."""

    at: HHMM
    basis: DiagnosisLabel
    forecast: DayForecast  # remaining intervals only
    volume_factor: dict[str, float]  # current actual / forecast, per queue
    aht_factor: dict[str, float]
    absent_agents: dict[str, int]
    assumptions: list[str]
    gaps: list[StaffingGap]


class Recommendation(BaseModel):
    at: HHMM
    diagnosis: Diagnosis
    options: list[PlanEvaluation]
    recommended_plan_id: str | None  # None when escalating
    explanation: str
    escalate: bool = False
    escalation_reason: str | None = None


class ApprovalDecision(BaseModel):
    at: HHMM
    decision: Literal["approve", "edit", "reject"]
    constraint_text: str | None = None  # e.g. "no overtime budget today"
    edited_plan: Plan | None = None
    by: str = "supervisor"


class DecisionLogEntry(BaseModel):
    """Append-only audit record: one per agent step, approval or applied action."""

    scenario_id: str
    at: HHMM
    step: Literal[
        "detect", "diagnose", "reforecast", "generate_options", "simulate",
        "check_policy", "recommend", "approval", "execute", "monitor", "escalate",
    ]
    summary: str
    inputs: dict = {}
    outputs: dict = {}


Snapshot.model_rebuild()
