"""Generate a synthetic CNC plant dataset with planted fault scenarios.

Why synthetic: real shop-floor data is confidential, and planting known faults
gives us ground truth, so the eval can check whether the agent finds the
*actual* root cause instead of a plausible-sounding one.

Planted scenarios (the ground truth the eval is built on):

  S1  M03  Coolant pump filter PM planned 2026-07-10 was deferred for a rush
           order and only done on 2026-08-12. The clogging filter drives
           CL-302 (coolant pressure low), then SP-101 (spindle overheat).
           Both drop back to baseline after the fix.
  S2  M05, M06  Purchasing switched APKT1604 inserts from Kento to ToolMax on
           2026-07-20. TL-201 (tool wear) roughly triples, TL-202 rises.
           M08 is the same model (HMC-630) but stays on Kento: the control.
           Still unresolved at the end of the data.
  S3  M01  Intermittent AX-402 (X-axis following error) from July. Operators
           and techs reset it four times; on 2026-08-19 a worn encoder cable
           (rubbing on the chip conveyor) is found and replaced. Stops after.
  S4  Site A  2026-07-28 ~14:05 utility voltage sag: PW-701 on M01-M06 within
           minutes, AX-401 on two machines at restart. Site B unaffected.
           Not a machine fault.

Run:  python -m triage.generate_data
"""
from __future__ import annotations

import csv
import math
import random
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import config

SEED = 7
START = date(2026, 6, 1)
END = date(2026, 9, 1)  # exclusive

MACHINES = [
    # machine_id, model, site, line, install_year
    ("M01", "VMC-850", "Site A", "L1", 2019),
    ("M02", "VMC-850", "Site A", "L1", 2019),
    ("M03", "VMC-1100", "Site A", "L1", 2021),
    ("M04", "VMC-850", "Site A", "L2", 2020),
    ("M05", "HMC-630", "Site A", "L2", 2022),
    ("M06", "HMC-630", "Site A", "L2", 2022),
    ("M07", "VMC-1100", "Site B", "L3", 2021),
    ("M08", "HMC-630", "Site B", "L3", 2023),
]
MODEL_OF = {m[0]: m[1] for m in MACHINES}
SITE_OF = {m[0]: m[2] for m in MACHINES}
TECHS = ["Chen", "Lin", "Wang", "Huang", "Liu"]

ALARM_CODES = [
    dict(code="SP-101", name="Spindle overheat", name_zh="主軸過熱", severity="high",
         description="Spindle housing temperature exceeded the limit. Machine feed hold until temperature drops.",
         likely_causes="Insufficient coolant flow to spindle cooling jacket; spindle bearing wear; lubrication failure; sustained high load.",
         recommended_checks="Check coolant pressure and flow; inspect coolant pump filter; check spindle lubrication; review load history."),
    dict(code="SP-102", name="Spindle load high", name_zh="主軸負載過高", severity="medium",
         description="Spindle load exceeded 120% of rated for more than 5 s.",
         likely_causes="Aggressive cutting parameters; dull or chipped tool; wrong tool offset.",
         recommended_checks="Review cutting parameters; inspect tool condition."),
    dict(code="TL-201", name="Tool wear limit reached", name_zh="刀具磨耗達上限", severity="low",
         description="Tool life counter or wear monitor reached the configured limit; tool change required.",
         likely_causes="Normal tool life consumption; insert grade or quality change; cutting parameters too aggressive; poor coolant delivery.",
         recommended_checks="Compare tool life against baseline; check insert batch/supplier; check cutting parameters."),
    dict(code="TL-202", name="Tool breakage detected", name_zh="刀具斷裂", severity="high",
         description="Tool breakage detected by load monitor or tool-setter.",
         likely_causes="Insert chipping progressing to breakage; interrupted cut; wrong parameters; insert quality.",
         recommended_checks="Inspect workpiece; inspect broken insert edge; check insert batch."),
    dict(code="CL-301", name="Coolant level low", name_zh="切削液液位低", severity="low",
         description="Coolant tank level below minimum sensor.",
         likely_causes="Normal consumption; leak; evaporation.",
         recommended_checks="Top up coolant; check for leaks."),
    dict(code="CL-302", name="Coolant pressure low", name_zh="切削液壓力低", severity="medium",
         description="Coolant supply pressure below 3.0 bar while coolant is commanded on.",
         likely_causes="Clogged pump suction filter; pump wear; leak in supply line; low tank level.",
         recommended_checks="Check tank level; inspect and clean pump suction filter; check pump and lines."),
    dict(code="AX-401", name="Servo overload", name_zh="伺服過載", severity="high",
         description="Axis servo motor current exceeded overload threshold.",
         likely_causes="Mechanical binding; chips on way covers; collision; restart after power loss.",
         recommended_checks="Check axis for binding and chips; re-home axis; check servo load history."),
    dict(code="AX-402", name="Axis following error", name_zh="軸跟隨誤差過大", severity="high",
         description="Difference between commanded and actual axis position exceeded tolerance.",
         likely_causes="Encoder signal problem (damaged cable, loose connector); servo tuning; mechanical binding.",
         recommended_checks="Inspect encoder cable and connector along its full route; compare servo parameters to backup; check for binding."),
    dict(code="LU-501", name="Lubrication pressure low", name_zh="潤滑壓力低", severity="medium",
         description="Way lubrication pressure below threshold.",
         likely_causes="Lube tank low; blocked line; pump fault.",
         recommended_checks="Refill tank; check lines and pump."),
    dict(code="DR-601", name="Door interlock open", name_zh="安全門開啟", severity="low",
         description="Operator door opened during cycle; cycle stopped by interlock.",
         likely_causes="Operator opened door; interlock switch misaligned.",
         recommended_checks="Operator practice; check interlock switch alignment if no one opened the door."),
    dict(code="PW-701", name="Supply voltage fluctuation", name_zh="電源電壓異常", severity="high",
         description="Main supply voltage out of range; drives disabled.",
         likely_causes="Utility grid disturbance; site power distribution issue; loose main terminal.",
         recommended_checks="Check whether other machines alarmed at the same time; check facilities records before troubleshooting the machine."),
    dict(code="EM-901", name="Emergency stop", name_zh="緊急停止", severity="high",
         description="Emergency stop button pressed.",
         likely_causes="Operator action.",
         recommended_checks="Confirm reason with operator."),
]

# Background alarm rates, events per machine per day.
BASE_RATES = {
    "SP-101": 0.05, "SP-102": 0.15, "TL-201": 0.40, "TL-202": 0.03,
    "CL-301": 0.08, "CL-302": 0.04, "AX-401": 0.03, "AX-402": 0.03,
    "LU-501": 0.05, "DR-601": 0.30, "PW-701": 0.003, "EM-901": 0.05,
}
DURATION = {  # minutes (lo, hi)
    "SP-101": (15, 45), "SP-102": (1, 5), "TL-201": (3, 10), "TL-202": (10, 30),
    "CL-301": (5, 15), "CL-302": (5, 20), "AX-401": (5, 15), "AX-402": (2, 8),
    "LU-501": (5, 15), "DR-601": (0.5, 2), "PW-701": (10, 20), "EM-901": (1, 5),
}
RESET_BY = {
    "SP-101": [("auto", 3), ("technician", 1)], "SP-102": [("auto", 1)],
    "TL-201": [("operator", 1)], "TL-202": [("operator", 1), ("technician", 1)],
    "CL-301": [("operator", 1)], "CL-302": [("operator", 2), ("technician", 1)],
    "AX-401": [("operator", 1), ("technician", 1)], "AX-402": [("operator", 3), ("technician", 1)],
    "LU-501": [("operator", 1)], "DR-601": [("auto", 1)], "PW-701": [("technician", 1)],
    "EM-901": [("operator", 1)],
}

S1_CL_START, S1_SP_START, S1_FIX = date(2026, 7, 12), date(2026, 7, 18), date(2026, 8, 12)
S2_SWITCH = date(2026, 7, 20)
S3_DAYS = [date(2026, 7, d) for d in (1, 3, 7, 10, 15, 18, 22, 26, 29)] + \
          [date(2026, 8, d) for d in (2, 5, 8, 12, 15, 18)]
S3_FIX = date(2026, 8, 19)
S4_TIME = datetime(2026, 7, 28, 14, 5)

CM_TEMPLATES = {
    "TL-202": ["Broken insert replaced, checked workpiece surface OK.", "刀片斷裂，已更換刀片，工件檢查OK",
               "TL-202 insert broke mid-cut, replaced + re-zeroed tool offset."],
    "CL-301": ["Coolant level low, topped up ~20L.", "切削液液位低，已補充", "Topped up coolant, checked for leaks - none."],
    "LU-501": ["Way lube tank refilled.", "導軌潤滑油補充完成", "Lube pressure low, refilled tank, pressure OK."],
    "SP-102": ["Spindle load high on roughing pass, reduced feed 10%.", "主軸負載高，粗加工進給調降"],
    "AX-401": ["Servo overload {axis} axis, cleaned chips off way covers, reset OK.", "{axis}軸伺服過載，清除導軌屑，復歸OK"],
    "SP-101": ["Spindle overheat, allowed to cool ~20 min, restart OK.", "主軸過熱，冷卻後重新啟動正常",
               "SP-101 again, let spindle cool down and resumed."],
    "CL-302": ["Coolant pressure low alarm, reset and resumed.", "冷卻液壓力低，復歸後正常"],
    "AX-402": ["Following error {axis} axis, reset OK.", "{axis}軸跟隨誤差，復歸後正常"],
}
CM_PROB = {"TL-202": 0.6, "CL-301": 0.3, "LU-501": 0.4, "SP-102": 0.05, "AX-401": 0.5,
           "SP-101": 0.3, "CL-302": 0.3, "AX-402": 0.4}
CM_COMPONENT = {"TL-202": "cutting tools", "CL-301": "coolant system", "LU-501": "way lube system",
                "SP-102": "spindle", "AX-401": "axis drive", "SP-101": "spindle", "CL-302": "coolant system",
                "AX-402": "axis drive"}

PM_COMPONENTS = [("coolant pump filter", 30), ("way lube system", 45), ("spindle bearing inspection", 90)]
PM_TEMPLATES = {
    "coolant pump filter": ["PM: coolant pump filter cleaned, pressure {p} bar OK.", "PM 冷卻泵濾網清潔完成，壓力 {p} bar 正常",
                            "Coolant filter PM done. Light sludge, cleaned. {p} bar."],
    "way lube system": ["PM: way lube system checked, oil level OK, lines clear.", "導軌潤滑系統保養完成，油路正常"],
    "spindle bearing inspection": ["PM: spindle bearing inspection, vibration within limits, temp rise normal.",
                                   "主軸軸承點檢完成，溫升正常"],
}

MANUAL_GUIDES = {
    "coolant_system.md": """# Coolant system maintenance guide

## How the coolant system works
The coolant pump draws from the tank through a suction filter and supplies both the cutting nozzles and the spindle cooling jacket. Nominal supply pressure is 4.0-4.5 bar.

## Low coolant pressure
A clogged suction filter is the most common cause of low coolant pressure (CL-302). Chips and sludge accumulate faster during high-volume aluminium and cast-iron jobs. Reduced flow to the spindle cooling jacket raises spindle temperature and can lead to spindle overheat alarms (SP-101).

## Preventive maintenance
Clean the pump suction filter every 30 days. Heavier chip load may require a shorter interval. Deferring this PM is a known cause of pressure and temperature problems.
""",
    "cutting_tools.md": """# Cutting tool and insert guidelines

## Tool life baseline
Each machine keeps a tool life baseline per insert type. A sustained rise in TL-201 (wear limit) frequency means inserts are reaching the wear limit sooner than baseline.

## Changing insert supplier or grade
Different suppliers and grades vary in edge toughness and wear resistance. Validate a new insert on sample parts before rollout. When wear alarms rise after a change, compare against machines of the same model still using the previous insert.

## Chipping and breakage
Edge chipping often precedes breakage (TL-202). Inspect broken inserts and record the batch.
""",
    "axis_drive.md": """# Axis drive and encoder troubleshooting

## Following error (AX-402)
Intermittent following errors that clear on reset often indicate an encoder signal problem: a damaged cable, a loose connector, or a cable routed near moving parts such as the chip conveyor or way covers.

## Repeated resets
If the same axis alarm recurs after resets, do not keep resetting. Inspect the encoder cable along its full route, compare servo parameters to the backup and check for mechanical binding.

## Servo overload (AX-401)
Commonly caused by chips on the way covers, binding, or restarting after a power loss before re-homing.
""",
    "power_quality.md": """# Power quality events

## Supply voltage fluctuation (PW-701)
When several machines raise PW-701 within minutes of each other, the cause is almost always on the supply side (utility grid or site distribution), not in the machine.

## What to do
Check facilities records and other machines first. After the event, re-home axes before resuming; restarting without re-homing can trigger servo overload alarms.
""",
}


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _weighted(rng: random.Random, options):
    names, weights = zip(*options)
    return rng.choices(names, weights=weights, k=1)[0]


def _days():
    d = START
    while d < END:
        yield d
        d += timedelta(days=1)


def _at(day: date, rng: random.Random, h0: int = 0, h1: int = 24) -> datetime:
    return datetime.combine(day, time()) + timedelta(seconds=rng.randrange(h0 * 3600, h1 * 3600))


def _fmt(ts: datetime | None) -> str | None:
    return ts.strftime("%Y-%m-%d %H:%M:%S") if ts else None


def _rate(mid: str, code: str, day: date) -> float:
    r = BASE_RATES[code]
    if mid == "M03" and day < S1_FIX:  # S1
        if code == "CL-302" and day >= S1_CL_START:
            r = 0.3 + 1.2 * (day - S1_CL_START).days / (S1_FIX - S1_CL_START).days
        if code == "SP-101" and day >= S1_SP_START:
            r = 0.2 + 1.0 * (day - S1_SP_START).days / (S1_FIX - S1_SP_START).days
    if mid in ("M05", "M06") and day >= S2_SWITCH:  # S2
        r = {"TL-201": 1.3, "TL-202": 0.15}.get(code, r)
    if mid == "M01" and code == "AX-402" and day >= date(2026, 7, 1):
        r = 0.0  # S3 alarms are placed explicitly; none after the cable fix
    return r


def _axis(rng, mid):
    return rng.choice(["X", "Y", "Z", "B"] if MODEL_OF[mid].startswith("HMC") else ["X", "Y", "Z"])


def generate(seed: int = SEED) -> dict[str, list[dict]]:
    rng = random.Random(seed)
    events: list[dict] = []

    def add_event(mid, ts, code, axis=None, reset_by=None, scripted=False):
        lo, hi = DURATION[code]
        events.append(dict(machine_id=mid, ts=ts, alarm_code=code,
                           axis=axis if code.startswith("AX") else None,
                           duration_min=round(rng.uniform(lo, hi), 1),
                           reset_by=reset_by or _weighted(rng, RESET_BY[code]), scripted=scripted))

    # Background + rate-modulated scenarios (S1, S2)
    for day in _days():
        for mid in MODEL_OF:
            for code in BASE_RATES:
                for _ in range(_poisson(rng, _rate(mid, code, day))):
                    add_event(mid, _at(day, rng), code, axis=_axis(rng, mid))

    # S3: M01 intermittent X-axis following error
    for day in S3_DAYS:
        for _ in range(rng.randint(1, 3)):
            add_event("M01", _at(day, rng), "AX-402", axis="X", reset_by="operator", scripted=True)

    # S4: Site A voltage sag
    for mid in ("M01", "M02", "M03", "M04", "M05", "M06"):
        add_event(mid, S4_TIME + timedelta(seconds=rng.randint(0, 90)), "PW-701", reset_by="technician", scripted=True)
    add_event("M02", datetime(2026, 7, 28, 14, 22, 10), "AX-401", axis="Z", reset_by="technician", scripted=True)
    add_event("M05", datetime(2026, 7, 28, 14, 24, 40), "AX-401", axis="B", reset_by="technician", scripted=True)

    events.sort(key=lambda e: (e["ts"], e["machine_id"]))
    alarms = []
    for i, e in enumerate(events, 1):
        alarms.append(dict(alarm_id=f"A{i:05d}", machine_id=e["machine_id"], ts=_fmt(e["ts"]),
                           alarm_code=e["alarm_code"],
                           severity=next(c["severity"] for c in ALARM_CODES if c["code"] == e["alarm_code"]),
                           axis=e["axis"], duration_min=e["duration_min"], reset_by=e["reset_by"],
                           shift="day" if 8 <= e["ts"].hour < 20 else "night"))

    work_orders: list[dict] = []

    def add_wo(mid, created, wo_type, component, notes, closed=None, tech=None):
        work_orders.append(dict(machine_id=mid, created_ts=created, closed_ts=closed, wo_type=wo_type,
                                component=component, technician=tech or rng.choice(TECHS), notes=notes))

    # Corrective WOs for a fraction of unscripted alarms (mostly symptom treatment, like real sites)
    for e in events:
        code = e["alarm_code"]
        if e["scripted"] or code not in CM_PROB or rng.random() > CM_PROB[code]:
            continue
        created = e["ts"] + timedelta(minutes=rng.randint(5, 40))
        add_wo(e["machine_id"], created, "CM", CM_COMPONENT[code],
               rng.choice(CM_TEMPLATES[code]).format(axis=e["axis"] or "X"),
               closed=created + timedelta(minutes=rng.randint(20, 90)))

    # PM plan (next PM is re-based on the actual done date, as most CMMS do)
    pm_plan = []
    for mid in MODEL_OF:
        for comp, interval in PM_COMPONENTS:
            if mid == "M03" and comp == "coolant pump filter":
                planned = date(2026, 6, 10)
            else:
                planned = START + timedelta(days=rng.randrange(0, min(interval, 40)))
            while True:
                if planned >= END:
                    pm_plan.append(dict(machine_id=mid, component=comp, interval_days=interval,
                                        planned_date=planned.isoformat(), done_date=None, status="scheduled"))
                    break
                if mid == "M03" and comp == "coolant pump filter" and planned == date(2026, 6, 10):
                    done = planned  # on time, so the next one lands exactly on 7/10
                elif mid == "M03" and comp == "coolant pump filter" and planned == date(2026, 7, 10):
                    done = S1_FIX  # S1: deferred PM
                elif rng.random() < 0.05:
                    done = planned + timedelta(days=rng.randint(4, 6))  # ordinary, harmless lateness
                else:
                    done = planned + timedelta(days=rng.choice([-1, 0, 0, 0, 1, 2]))
                if done >= END:
                    done = END - timedelta(days=1)
                status = "done" if (done - planned).days <= 3 else "done_late"
                pm_plan.append(dict(machine_id=mid, component=comp, interval_days=interval,
                                    planned_date=planned.isoformat(), done_date=done.isoformat(), status=status))
                if mid == "M03" and comp == "coolant pump filter" and done == S1_FIX:
                    note = "PM done late (planned 7/10, deferred). Filter was severely clogged - see CM same day."
                else:
                    note = rng.choice(PM_TEMPLATES[comp]).format(p=f"{rng.uniform(4.0, 4.5):.1f}")
                created = _at(done, rng, 8, 17)
                add_wo(mid, created, "PM", comp, note, closed=created + timedelta(minutes=rng.randint(30, 120)))
                planned = done + timedelta(days=interval)
    pm_plan.sort(key=lambda p: (p["planned_date"], p["machine_id"]))
    for i, p in enumerate(pm_plan, 1):
        p["plan_id"] = f"PM-{i:04d}"

    # Scripted work orders
    dt = datetime
    scripted = [
        # S1
        ("M03", dt(2026, 7, 10, 9, 15), "NOTE", "coolant pump filter", "Lin",
         "Coolant pump filter PM deferred - rush order (Q3 housing batch), line cannot stop. Will reschedule next week."),
        ("M03", dt(2026, 7, 24, 10, 40), "NOTE", "coolant pump filter", "Lin",
         "Coolant filter PM still pending. 冷卻液壓力偶爾偏低 (CL-302 a few times/day), monitoring."),
        ("M03", dt(2026, 8, 2, 15, 20), "CM", "spindle", "Wang",
         "SP-101 spindle overheat repeated this week. Let spindle cool, restart OK. Suggest checking coolant flow to spindle."),
        ("M03", dt(2026, 8, 12, 9, 0), "CM", "coolant pump filter", "Chen",
         "Repeated SP-101 + CL-302. Opened coolant pump: suction filter heavily clogged with chips and sludge "
         "(濾網嚴重堵塞). Replaced filter, flushed tank. Coolant pressure back to 4.3 bar (was ~2.1)."),
        # S2
        ("M05", dt(2026, 7, 19, 16, 0), "NOTE", "cutting tools", "Huang",
         "Purchasing notice: APKT1604 face mill inserts switched from Kento to ToolMax from 7/20 (cost-down). "
         "Applies to L2 HMC machines."),
        ("M05", dt(2026, 7, 24, 11, 10), "CM", "cutting tools", "Huang",
         "TL-201 frequent. Inserts reaching wear limit at roughly half the usual life. New ToolMax batch TM-2607?"),
        ("M06", dt(2026, 7, 29, 14, 30), "CM", "cutting tools", "Liu",
         "刀片崩角 (insert edge chipping) after ~40 parts, previously ~90 parts per edge."),
        ("M05", dt(2026, 8, 6, 10, 5), "CM", "cutting tools", "Huang",
         "TL-202 tool breakage, broken insert replaced. Chipping on cutting edge again."),
        ("M06", dt(2026, 8, 21, 13, 45), "NOTE", "cutting tools", "Liu",
         "Tool cost per part up on L2 despite cheaper inserts. Asked purchasing for Kento samples to compare."),
        # S3
        ("M01", dt(2026, 7, 3, 16, 10), "CM", "axis drive", "Wang", "X axis following error (AX-402), reset OK."),
        ("M01", dt(2026, 7, 15, 22, 30), "CM", "axis drive", "Lin", "AX-402 X axis again. Reset, ran test program OK."),
        ("M01", dt(2026, 7, 29, 9, 50), "CM", "axis drive", "Wang", "X軸跟隨誤差又出現，復歸後正常，持續觀察"),
        ("M01", dt(2026, 8, 8, 14, 0), "CM", "axis drive", "Chen",
         "AX-402 X recurring. Checked servo params vs backup - no change. Checked gib/way for binding - OK."),
        ("M01", dt(2026, 8, 19, 10, 30), "CM", "axis drive", "Chen",
         "Root cause found: X-axis encoder cable jacket worn through where it rubs against chip conveyor housing "
         "(intermittent signal). Replaced encoder cable, added cable guard and re-routed. Test run 2h OK."),
        # S4
        ("M02", dt(2026, 7, 28, 14, 40), "NOTE", "power", "Chen",
         "Power dip ~14:05. All Site A machines (L1+L2) alarmed PW-701 together. Facilities: utility grid voltage sag, "
         "廠務確認為外部電網問題. Restarted, re-homed axes, no damage found."),
        ("M05", dt(2026, 7, 28, 14, 50), "CM", "axis drive", "Huang", "AX-401 after power dip restart, re-homed B axis, OK."),
    ]
    for mid, created, wo_type, comp, tech, notes in scripted:
        closed = None if wo_type == "NOTE" else created + timedelta(minutes=rng.randint(30, 150))
        add_wo(mid, created, wo_type, comp, notes, closed=closed, tech=tech)

    work_orders.sort(key=lambda w: (w["created_ts"], w["machine_id"]))
    for i, w in enumerate(work_orders, 1):
        w["wo_id"] = f"WO-{260000 + i}"
        w["created_ts"], w["closed_ts"] = _fmt(w["created_ts"]), _fmt(w["closed_ts"])

    # Insert changes (S2 lives here)
    tool_changes = []
    for mid, model, *_ in MACHINES:
        tool_type = "APKT1604 face mill insert" if model.startswith("HMC") else "SEKT1204 face mill insert"
        day = START + timedelta(days=rng.randint(0, 2))
        while day < END:
            switched = mid in ("M05", "M06") and day >= S2_SWITCH
            supplier = "ToolMax" if switched else "Kento"
            batch = f"{'TM' if switched else 'KT'}-26{day.month:02d}"
            tool_changes.append(dict(machine_id=mid, ts=_fmt(_at(day, rng, 6, 22)), tool_type=tool_type,
                                     supplier=supplier, batch=batch))
            day += timedelta(days=rng.randint(1, 2) if switched else rng.randint(2, 4))
    tool_changes.sort(key=lambda t: (t["ts"], t["machine_id"]))
    for i, t in enumerate(tool_changes, 1):
        t["change_id"] = f"TC-{i:04d}"

    machines = [dict(machine_id=m, model=mo, site=s, line=l, install_year=y) for m, mo, s, l, y in MACHINES]
    return dict(machines=machines, alarm_codes=[dict(c) for c in ALARM_CODES], alarms=alarms,
                work_orders=work_orders, pm_plan=pm_plan, tool_changes=tool_changes)


SCHEMA_SQL = """
CREATE TABLE machines (machine_id TEXT PRIMARY KEY, model TEXT, site TEXT, line TEXT, install_year INTEGER);
CREATE TABLE alarm_codes (code TEXT PRIMARY KEY, name TEXT, name_zh TEXT, severity TEXT, description TEXT,
    likely_causes TEXT, recommended_checks TEXT);
CREATE TABLE alarms (alarm_id TEXT PRIMARY KEY, machine_id TEXT, ts TEXT, alarm_code TEXT, severity TEXT,
    axis TEXT, duration_min REAL, reset_by TEXT, shift TEXT);
CREATE TABLE work_orders (wo_id TEXT PRIMARY KEY, machine_id TEXT, created_ts TEXT, closed_ts TEXT, wo_type TEXT,
    component TEXT, technician TEXT, notes TEXT);
CREATE TABLE pm_plan (plan_id TEXT PRIMARY KEY, machine_id TEXT, component TEXT, interval_days INTEGER,
    planned_date TEXT, done_date TEXT, status TEXT);
CREATE TABLE tool_changes (change_id TEXT PRIMARY KEY, machine_id TEXT, ts TEXT, tool_type TEXT, supplier TEXT, batch TEXT);
CREATE INDEX idx_alarms_machine_ts ON alarms(machine_id, ts);
CREATE INDEX idx_alarms_code ON alarms(alarm_code);
CREATE INDEX idx_wo_machine ON work_orders(machine_id, created_ts);
"""
COLUMNS = {
    "machines": ["machine_id", "model", "site", "line", "install_year"],
    "alarm_codes": ["code", "name", "name_zh", "severity", "description", "likely_causes", "recommended_checks"],
    "alarms": ["alarm_id", "machine_id", "ts", "alarm_code", "severity", "axis", "duration_min", "reset_by", "shift"],
    "work_orders": ["wo_id", "machine_id", "created_ts", "closed_ts", "wo_type", "component", "technician", "notes"],
    "pm_plan": ["plan_id", "machine_id", "component", "interval_days", "planned_date", "done_date", "status"],
    "tool_changes": ["change_id", "machine_id", "ts", "tool_type", "supplier", "batch"],
}


def _alarm_code_manual(codes: list[dict]) -> str:
    parts = ["# Alarm code reference\n"]
    for c in codes:
        parts.append(f"## {c['code']} {c['name']} ({c['name_zh']})\n"
                     f"Severity: {c['severity']}. {c['description']}\n"
                     f"Likely causes: {c['likely_causes']}\n"
                     f"Recommended checks: {c['recommended_checks']}\n")
    return "\n".join(parts)


def write_all(data: dict[str, list[dict]], data_dir: Path = config.DATA_DIR) -> Path:
    raw, manual, db_path = data_dir / "raw", data_dir / "manual", data_dir / "plant.db"
    raw.mkdir(parents=True, exist_ok=True)
    manual.mkdir(parents=True, exist_ok=True)
    for table, cols in COLUMNS.items():
        with open(raw / f"{table}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows({c: r[c] for c in cols} for r in data[table])
    (manual / "alarm_codes.md").write_text(_alarm_code_manual(data["alarm_codes"]), encoding="utf-8")
    for name, text in MANUAL_GUIDES.items():
        (manual / name).write_text(text, encoding="utf-8")

    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA_SQL)
    for table, cols in COLUMNS.items():
        con.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        [tuple(r[c] for c in cols) for r in data[table]])
    con.commit()
    con.close()

    print(f"LINE 474 : ",db_path)

    return db_path


def main() -> None:
    data = generate()
    # print(data)
    path = write_all(data)
    print(f"Wrote {path}")
    for table in COLUMNS:
        print(f"  {table:<13} {len(data[table]):>6} rows")
        print(data[table])
        break

if __name__ == "__main__":
    main()
