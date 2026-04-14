"""analyze_attr_csv.py — Analyze the attrs CSV produced by the OUPES Mega HA plugin.

Usage:
    python analyze_attr_csv.py                          # auto-find CSV in same folder
    python analyze_attr_csv.py path/to/attrs.csv        # explicit path

The CSV is written by the HA integration when "Debug: log all attribute values"
is enabled in the integration options.  Each row is one attr observation:
  timestamp, attr, attr_hex, value, known, slot, soc, grid_w, note

Sections produced:
  1 — Unknown attrs (never seen in our protocol map)
  2 — Attr-78 deep-dive (mystery / voltage / runtime by slot)
  3 — Full value-range summary for every attr
  4 — Low-SoC regime (SoC ≤ 5%) — new or changed values vs normal
  5 — Grid-correlated attrs (non-zero on grid, zero off grid)
  6 — Attr-78 mystery values in detail (if any)
"""

import csv
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Known attr names (mirrors protocol.py) ────────────────────────────────────
ATTR_NAMES: dict[int, str] = {
    1:   "AC Output (bool)",
    2:   "DC Output (bool)",
    3:   "Battery SoC %",
    4:   "AC Output Power W",
    5:   "Unknown (mirrors AC W)",
    6:   "DC 12V Output W",
    7:   "USB-C Output W",
    8:   "USB-A Output W",
    9:   "Unknown",
    21:  "Total Input Power W",
    22:  "Grid Input Power W",
    23:  "Solar Input Power W",
    30:  "Remaining Runtime min",
    32:  "Main Unit Temperature F/10",
    51:  "Connected Expansion Battery Count",
    53:  "B2 Input Power W",
    54:  "B2 Output Power W",
    84:  "AC Output Control (bool)",
    105: "AC Inverter Protection (bool)",
    78:  "Ext Battery Runtime/Voltage (mux)",
    79:  "Ext Battery SoC %",
    80:  "Ext Battery Temperature F/10",
    101: "Ext Battery Slot Index",
}
EXT_ATTRS = {78, 79, 80, 101}
KNOWN_ATTRS = set(ATTR_NAMES)

ATTR78_RUNTIME_MAX = 6000
ATTR78_MV_MIN      = 44000
ATTR78_MV_MAX      = 58500
LOW_SOC_THRESHOLD  = 5


# ── Data structures ───────────────────────────────────────────────────────────

class Row:
    __slots__ = ("ts", "attr", "value", "known", "slot", "soc", "grid_w", "note")
    def __init__(self, r: dict) -> None:
        self.ts     = r["timestamp"]
        self.attr   = int(r["attr"])
        self.value  = int(r["value"])
        self.known  = r["known"] == "yes"
        self.slot   = int(r["slot"]) if r["slot"] else None
        self.soc    = int(r["soc"]) if r["soc"] else -1
        self.grid_w = int(r["grid_w"]) if r["grid_w"] else 0
        self.note   = r["note"]


def load_csv(path: Path) -> list[Row]:
    rows: list[Row] = []
    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                rows.append(Row(r))
            except (ValueError, KeyError):
                continue
    return rows


def find_csv() -> Path:
    here = Path(__file__).parent
    candidates = sorted(here.glob("oupes_mega_ble_*_attrs.csv"))
    if not candidates:
        print("ERROR: No oupes_mega_ble_*_attrs.csv file found in", here)
        sys.exit(1)
    if len(candidates) > 1:
        print(f"Multiple CSV files found — using: {candidates[-1].name}")
    return candidates[-1]


# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_range(vals: list[int], n: int = 20) -> str:
    if not vals:
        return "(none)"
    u = sorted(set(vals))
    s = f"min={u[0]}  max={u[-1]}  n={len(vals)}  unique={u[:n]}"
    if len(u) > n:
        s += f" … ({len(u)} distinct)"
    return s


def attr_label(attr: int) -> str:
    name = ATTR_NAMES.get(attr, "UNKNOWN")
    return f"Attr {attr:3d} (0x{attr:02x})  [{name}]"


# ── Sections ──────────────────────────────────────────────────────────────────

def section_unknown_attrs(rows: list[Row]) -> None:
    print("=" * 70)
    print("SECTION 1 — Unknown attrs (not in protocol map)")
    print("=" * 70)

    unknown: dict[int, list[int]] = defaultdict(list)
    for r in rows:
        if not r.known and r.attr not in KNOWN_ATTRS:
            unknown[r.attr].append(r.value)

    if not unknown:
        print("\n  No unknown attrs seen yet.")
        return

    print(f"\n  {len(unknown)} unknown attr(s) observed:\n")
    for attr in sorted(unknown):
        vals = unknown[attr]
        print(f"  {attr_label(attr)}")
        print(f"    {fmt_range(vals)}")
        print()


def section_attr78(rows: list[Row]) -> None:
    print()
    print("=" * 70)
    print("SECTION 2 — Attr-78 deep-dive (runtime / voltage / mystery)")
    print("=" * 70)

    attr78 = [r for r in rows if r.attr == 78]
    if not attr78:
        print("\n  No attr-78 observations in this CSV yet.")
        return

    runtime = [r for r in attr78 if r.value <= ATTR78_RUNTIME_MAX]
    voltage = [r for r in attr78 if ATTR78_MV_MIN <= r.value <= ATTR78_MV_MAX]
    mystery = [r for r in attr78 if ATTR78_RUNTIME_MAX < r.value < ATTR78_MV_MIN]
    other   = [r for r in attr78 if r.value > ATTR78_MV_MAX]

    print(f"\n  Total attr-78 obs : {len(attr78)}")
    print(f"  Runtime  (≤{ATTR78_RUNTIME_MAX})        : {len(runtime)}")
    print(f"  Voltage  ({ATTR78_MV_MIN}–{ATTR78_MV_MAX})  : {len(voltage)}")
    print(f"  Mystery  ({ATTR78_RUNTIME_MAX+1}–{ATTR78_MV_MIN-1})     : {len(mystery)}")
    if other:
        print(f"  >MV_MAX  (>{ATTR78_MV_MAX})       : {len(other)}")

    # Voltage readings
    if voltage:
        print(f"\n  ★ Battery voltage readings ({len(voltage)} samples):")
        slots = sorted({r.slot for r in voltage})
        for slot in slots:
            sv = [r for r in voltage if r.slot == slot]
            print(f"\n    Slot {slot}:")
            print(f"    {'Timestamp':<26}  {'mV':>8}  {'Volts':>8}  {'SoC%':>5}  {'Grid W':>7}")
            print(f"    {'-'*26}  {'-'*8}  {'-'*8}  {'-'*5}  {'-'*7}")
            for r in sv:
                soc_s = f"{r.soc}%" if r.soc >= 0 else "  ?"
                print(f"    {r.ts:<26}  {r.value:>8}  {r.value/1000:>8.3f}  {soc_s:>5}  {r.grid_w:>7}")
    else:
        print("\n  No voltage readings captured yet.")

    # Mystery readings
    if mystery:
        print(f"\n  ⚠ Mystery values ({len(mystery)} samples — range {ATTR78_RUNTIME_MAX+1}–{ATTR78_MV_MIN-1}):")
        slots = sorted({r.slot for r in mystery})
        for slot in slots:
            sm = [r for r in mystery if r.slot == slot]
            unique_vals = sorted(set(r.value for r in sm))
            print(f"\n    Slot {slot}: {len(sm)} obs,  unique values: {unique_vals[:30]}")
            print(f"    {'Timestamp':<26}  {'Value':>8}  {'SoC%':>5}  {'Grid W':>7}")
            print(f"    {'-'*26}  {'-'*8}  {'-'*5}  {'-'*7}")
            for r in sm[:30]:
                soc_s = f"{r.soc}%" if r.soc >= 0 else "  ?"
                print(f"    {r.ts:<26}  {r.value:>8}  {soc_s:>5}  {r.grid_w:>7}")
            if len(sm) > 30:
                print(f"    … {len(sm)-30} more rows not shown")


def section_value_ranges(rows: list[Row]) -> None:
    print()
    print("=" * 70)
    print("SECTION 3 — Full value-range summary for all attrs")
    print("=" * 70)

    by_attr: dict[int, list[int]] = defaultdict(list)
    for r in rows:
        by_attr[r.attr].append(r.value)

    print()
    for attr in sorted(by_attr):
        vals = by_attr[attr]
        label = attr_label(attr)
        print(f"  {label}")
        print(f"    {fmt_range(vals)}")


def section_low_soc(rows: list[Row]) -> None:
    print()
    print("=" * 70)
    print(f"SECTION 4 — Low-SoC regime (SoC ≤ {LOW_SOC_THRESHOLD}%)")
    print("=" * 70)

    rows_with_soc = [r for r in rows if r.soc >= 0]
    low  = [r for r in rows_with_soc if r.soc <= LOW_SOC_THRESHOLD]
    norm = [r for r in rows_with_soc if r.soc  > LOW_SOC_THRESHOLD]

    print(f"\n  Observations with SoC known: {len(rows_with_soc)}")
    print(f"  At SoC ≤ {LOW_SOC_THRESHOLD}%: {len(low)}")
    print(f"  At SoC  > {LOW_SOC_THRESHOLD}%: {len(norm)}")

    if not low:
        print("\n  No low-SoC data in this CSV yet.")
        return

    low_by_attr:  dict[int, list[int]] = defaultdict(list)
    norm_by_attr: dict[int, list[int]] = defaultdict(list)
    for r in low:
        low_by_attr[r.attr].append(r.value)
    for r in norm:
        norm_by_attr[r.attr].append(r.value)

    only_low = set(low_by_attr) - set(norm_by_attr)
    if only_low:
        print(f"\n  ★ Attrs seen ONLY at low SoC: {sorted(only_low)}")
        for attr in sorted(only_low):
            print(f"    {attr_label(attr)}")
            print(f"      {fmt_range(low_by_attr[attr])}")
    else:
        print("\n  No attrs exclusive to low SoC.")

    print("\n  Attrs with NEW values at low SoC vs normal:")
    found_any = False
    for attr in sorted(set(low_by_attr) & set(norm_by_attr)):
        new_vals = set(low_by_attr[attr]) - set(norm_by_attr[attr])
        if new_vals:
            found_any = True
            print(f"\n    {attr_label(attr)}")
            print(f"      Normal:         {fmt_range(norm_by_attr[attr])}")
            print(f"      Low SoC:        {fmt_range(low_by_attr[attr])}")
            print(f"      New at low SoC: {sorted(new_vals)}")
    if not found_any:
        print("    None so far.")


def section_grid_correlated(rows: list[Row]) -> None:
    print()
    print("=" * 70)
    print("SECTION 5 — Grid-correlated attrs (high on grid, low off grid)")
    print("=" * 70)

    has_grid_on  = any(r.grid_w > 0 for r in rows)
    has_grid_off = any(r.grid_w == 0 for r in rows)
    if not (has_grid_on and has_grid_off):
        print("\n  Need captures with grid both ON and OFF for correlation.")
        return

    by_attr_on:  dict[int, list[int]] = defaultdict(list)
    by_attr_off: dict[int, list[int]] = defaultdict(list)
    for r in rows:
        if r.grid_w > 0:
            by_attr_on[r.attr].append(r.value)
        else:
            by_attr_off[r.attr].append(r.value)

    candidates = []
    for attr in sorted(set(by_attr_on) & set(by_attr_off)):
        on_vals  = by_attr_on[attr]
        off_vals = by_attr_off[attr]
        frac_on_nz  = sum(1 for v in on_vals  if v > 0) / len(on_vals)
        frac_off_nz = sum(1 for v in off_vals if v > 0) / len(off_vals)
        if frac_on_nz >= 0.7 and frac_off_nz <= 0.3:
            candidates.append((attr, on_vals, off_vals, frac_on_nz, frac_off_nz))

    print()
    if not candidates:
        print("  No strongly grid-correlated attrs found yet.")
    else:
        for attr, on_v, off_v, fon, foff in candidates:
            print(f"  {attr_label(attr)}")
            print(f"    Grid ON  ({len(on_v):4d} obs): {fon*100:.0f}% non-zero  {fmt_range(on_v)}")
            print(f"    Grid OFF ({len(off_v):4d} obs): {foff*100:.0f}% non-zero  {fmt_range(off_v)}")
            print()


def section_mystery_detail(rows: list[Row]) -> None:
    mystery = [r for r in rows if r.attr == 78
               and ATTR78_RUNTIME_MAX < r.value < ATTR78_MV_MIN]
    if not mystery:
        return

    print()
    print("=" * 70)
    print("SECTION 6 — Attr-78 mystery values: SoC and grid correlation")
    print("=" * 70)

    vals = [r.value for r in mystery]
    soc_pairs  = [(r.soc, r.value) for r in mystery if r.soc >= 0]
    grid_pairs = [(r.grid_w, r.value) for r in mystery]

    print(f"\n  {len(mystery)} mystery observations")
    print(f"  Value range: {fmt_range(vals)}")

    if soc_pairs:
        by_soc: dict[int, list[int]] = defaultdict(list)
        for soc, val in soc_pairs:
            by_soc[soc].append(val)
        print(f"\n  By SoC bucket:")
        for soc in sorted(by_soc):
            print(f"    SoC={soc}%: {fmt_range(by_soc[soc])}")

    grid_on  = [v for g, v in grid_pairs if g > 0]
    grid_off = [v for g, v in grid_pairs if g == 0]
    if grid_on or grid_off:
        print(f"\n  When grid ON  ({len(grid_on)} obs): {fmt_range(grid_on)}")
        print(f"  When grid OFF ({len(grid_off)} obs): {fmt_range(grid_off)}")

    # Check if mystery values correlate with Slot 1 SoC (attr 79 in ext_batteries)
    # by looking at nearby rows with the same timestamp prefix
    print("\n  Checking if mystery value tracks SoC linearly...")
    pairs = sorted(soc_pairs)
    if len(pairs) >= 2:
        lo_soc, lo_val = pairs[0]
        hi_soc, hi_val = pairs[-1]
        if hi_soc > lo_soc:
            direction = "increases with SoC" if hi_val > lo_val else "decreases with SoC"
            print(f"    SoC {lo_soc}% → val={lo_val},  SoC {hi_soc}% → val={hi_val}  ({direction})")
        else:
            print(f"    Only one SoC value ({lo_soc}%) — can't determine direction yet")
    else:
        print("    Not enough SoC-tagged mystery rows yet.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
        if not path.exists():
            print(f"ERROR: File not found: {path}")
            sys.exit(1)
    else:
        path = find_csv()

    rows = load_csv(path)
    print(f"Loaded {len(rows):,} rows from {path.name}")
    if not rows:
        print("No data rows yet — let the HA plugin collect more data and re-run.")
        return

    ts_first = rows[0].ts
    ts_last  = rows[-1].ts
    print(f"Time range: {ts_first}  →  {ts_last}\n")

    section_unknown_attrs(rows)
    section_attr78(rows)
    section_value_ranges(rows)
    section_low_soc(rows)
    section_grid_correlated(rows)
    section_mystery_detail(rows)


if __name__ == "__main__":
    main()
