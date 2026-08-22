from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Optional
from models.entry_leg import EntryLeg
from models.execution_variant import ExecutionVariant


@dataclass
class Position:
    symbol: str
    side: str

    quantity: float

    entry_price: float
    real_entry: float

    tp: float
    sl: float

    entry_ts: int
    signal_price: float
    signal_ts: int
    
    leverage: int = 5
    is_testnet: bool = False

    signal_context: dict | None = None

    plan_max_hold_candles: int = 10
    entry_candle_ts: Optional[int] = None
    candles_in_trade: int = 1

    exit_order_id: Optional[int] = None

    # ========================
    # TRADE EVOLUTION
    # ========================

    current_pnl: float = 0.0

    mae: float | None = None
    mfe: float | None = None

    current_momentum: Optional[str] = None
    current_direction: Optional[str] = None
    current_trend: Optional[str] = None

    momentum_t1: Optional[str] = None
    direction_t1: Optional[str] = None
    pnl_t1: Optional[float] = None
    
    # ========================
    # AGGRESSIVE POST ANALYSIS
    # ========================

    micro_t1: Optional[str] = None
    direction_5m_t1: Optional[str] = None

    reclaimed_ema20_1m: bool = False
    reclaimed_ema34_1m: bool = False
    reclaimed_ema50_1m: bool = False

    lost_ema20_1m: bool = False
    lost_ema34_1m: bool = False
    lost_ema50_1m: bool = False

    dist_ema20_1m_pct: Optional[float] = None
    dist_ema34_1m_pct: Optional[float] = None
    dist_ema50_1m_pct: Optional[float] = None

    max_favorable_pct: Optional[float] = None
    max_adverse_pct: Optional[float] = None

    direction_5m_changed: bool = False
    direction_5m_after_entry: Optional[str] = None
    
    be_moved: bool = False
    
    strategy_mode: Optional[str] = None
    router_reason: Optional[str] = None
    aggregate_position_id: Optional[str] = None
    execution_variant: Optional[ExecutionVariant] = None
    execution_reasons: list[str] = field(default_factory=list)
    entry_legs: list[EntryLeg] = field(default_factory=list)
    position_increased: bool = False
    ever_combined: bool = False
    overlapped_with_other_leg: bool = False

    def __post_init__(self):
        self.execution_variant = ExecutionVariant.parse(self.execution_variant)
        self.signal_context = deepcopy(self.signal_context or {})
        self.execution_reasons = list(self.execution_reasons or [])
        self.entry_legs = [leg if isinstance(leg, EntryLeg) else EntryLeg.from_dict(leg) for leg in (self.entry_legs or [])]
        self.refresh_aggregate_identity()

    def validate_entry_legs(self):
        if not self.entry_legs:
            return
        if not self.aggregate_position_id:
            raise ValueError("Position with entry legs requires aggregate_position_id")
        if {leg.symbol for leg in self.entry_legs} != {self.symbol}:
            raise ValueError("Position contains legs from different symbols")
        if {leg.aggregate_position_id for leg in self.entry_legs} != {self.aggregate_position_id}:
            raise ValueError("Position contains legs from different aggregate positions")
        leg_ids = [leg.leg_id for leg in self.entry_legs]
        keys = [leg.deduplication_key for leg in self.entry_legs]
        if len(leg_ids) != len(set(leg_ids)):
            raise ValueError("Position contains duplicate leg_id values")
        if len(keys) != len(set(keys)):
            raise ValueError("Position contains duplicate entry leg setups")

    def add_entry_leg(self, new_leg: EntryLeg):
        self.validate_entry_legs()
        if new_leg.symbol != self.symbol:
            raise ValueError("EntryLeg symbol does not match Position symbol")
        if new_leg.aggregate_position_id != self.aggregate_position_id:
            raise ValueError("EntryLeg aggregate_position_id does not match Position")
        if any(leg.leg_id == new_leg.leg_id for leg in self.entry_legs):
            raise ValueError("Duplicate leg_id")
        if any(leg.deduplication_key == new_leg.deduplication_key for leg in self.entry_legs):
            raise ValueError("Duplicate entry leg setup")
        open_legs = [leg for leg in self.entry_legs if leg.remaining_quantity > 0]
        if self.entry_legs and not open_legs:
            raise ValueError("Cannot add a leg to a flat aggregate position")
        self.entry_legs.append(new_leg)
        if open_legs:
            self.position_increased = self.ever_combined = True
            self.overlapped_with_other_leg = True
            new_leg.ever_combined = new_leg.overlapped_with_other_leg = True
            for leg in open_legs:
                leg.ever_combined = leg.overlapped_with_other_leg = True
        self.refresh_aggregate_identity()

    def refresh_aggregate_identity(self):
        self.validate_entry_legs()
        variants = {leg.variant for leg in self.entry_legs}
        if {ExecutionVariant.BUCKET_V1, ExecutionVariant.BUCKET_V2}.issubset(variants):
            self.execution_variant = ExecutionVariant.BUCKET_V1_V2
        elif len(variants) == 1:
            self.execution_variant = next(iter(variants))
        if len(self.entry_legs) > 1:
            self.position_increased = self.ever_combined = True
            for leg in self.entry_legs:
                leg.ever_combined = True
        self.execution_reasons = list(dict.fromkeys(leg.execution_reason for leg in self.entry_legs))

    def to_dict(self):
        data = asdict(self)
        data["execution_variant"] = self.execution_variant.value if self.execution_variant else None
        data["entry_legs"] = [leg.to_dict() for leg in self.entry_legs]
        return data

    @classmethod
    def from_dict(cls, data):
        values = deepcopy(data)
        values["entry_legs"] = [EntryLeg.from_dict(x) for x in values.get("entry_legs", [])]
        return cls(**values)
