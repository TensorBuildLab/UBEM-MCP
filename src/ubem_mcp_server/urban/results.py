"""
Read simulation results from EnergyPlus SQLite output (Output:SQLite).
"""

import sqlite3
from pathlib import Path
from typing import Dict, Optional

# Fuel columns of the "End Uses" table -> short keys.
_FUELS = {"Electricity": "electricity", "Natural Gas": "natural_gas", "District Cooling": "district_cooling",
          "District Heating Water": "district_heating", "District Heating Steam": "district_heating_steam"}
_KWH_TO_MJ = 3.6
_GJ_TO_MJ = 1000.0


def find_sql(output_directory: str) -> Optional[str]:
    hits = sorted(Path(output_directory).glob("*.sql"))
    return str(hits[0]) if hits else None


def annual_end_uses(sql_path: str) -> Dict[str, object]:
    """Annual site energy by fuel and end use, and the floor areas, from the
    AnnualBuildingUtilityPerformanceSummary report.

    Returns {"end_uses_mj": {fuel: {end_use: MJ}}, "total_mj", "conditioned_area_m2", "total_area_m2"}.
    """
    con = sqlite3.connect(f"file:{sql_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT RowName, ColumnName, Units, Value FROM TabularDataWithStrings "
            "WHERE ReportName='AnnualBuildingUtilityPerformanceSummary' AND TableName='End Uses'"
        ).fetchall()
        area_rows = con.execute(
            "SELECT RowName, Value FROM TabularDataWithStrings "
            "WHERE ReportName='AnnualBuildingUtilityPerformanceSummary' AND TableName='Building Area' "
            "AND ColumnName='Area'"
        ).fetchall()
    finally:
        con.close()

    end_uses: Dict[str, Dict[str, float]] = {}
    for row, col, units, value in rows:
        fuel = _FUELS.get(col)
        if fuel is None or row.startswith("Total"):
            continue
        try:
            x = float(value)
        except (TypeError, ValueError):
            continue
        if x == 0:
            continue
        scale = _KWH_TO_MJ if units.lower() == "kwh" else _GJ_TO_MJ if units.lower() == "gj" else 1.0
        end_uses.setdefault(fuel, {})[row.strip().lower().replace(" ", "_")] = x * scale

    areas = {r.strip(): float(v) for r, v in area_rows}
    return {
        "end_uses_mj": end_uses,
        "total_mj": sum(sum(d.values()) for d in end_uses.values()),
        "conditioned_area_m2": areas.get("Net Conditioned Building Area"),
        "total_area_m2": areas.get("Total Building Area"),
    }
