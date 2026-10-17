"""
Plot the outputs of validate_archetypes.py and benchmark_scaling.py.

Usage:
    uv run python scripts/plot_benchmarks.py --validation validation/2B validation/5A ... \
        --scaling benchmark/ --out figures/
"""

import argparse
import csv
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

END_USE_GROUPS = {
    "heating": "Heating", "cooling": "Cooling", "fans": "Fans & pumps", "pumps": "Fans & pumps",
    "heat_rejection": "Fans & pumps", "humidification": "Fans & pumps", "heat_recovery": "Fans & pumps",
    "interior_lighting": "Lighting", "exterior_lighting": "Lighting",
    "interior_equipment": "Equipment", "exterior_equipment": "Equipment",
    "water_systems": "Water heating", "refrigeration": "Refrigeration",
}
GROUP_ORDER = ["Heating", "Cooling", "Fans & pumps", "Lighting", "Equipment", "Water heating", "Refrigeration"]
VINTAGE_MARKERS = {"pre1980": "o", "1980_2004": "s", "post2004": "^"}


def _grouped(end_uses):
    out = dict.fromkeys(GROUP_ORDER, 0.0)
    for fuel in (end_uses or {}).values():
        for k, v in fuel.items():
            out[END_USE_GROUPS.get(k, "Equipment")] += v
    return out


def load_validation(dirs):
    rows = []
    for d in dirs:
        data = json.loads((Path(d) / "validation.json").read_text())
        rows += [r for r in data["cases"] if "error" not in r and r.get("ref_eui_no_refrigeration")]
    return rows


def parity(rows, out: Path):
    zones = sorted({r["climate_zone"] for r in rows})
    colors = dict(zip(zones, plt.cm.tab10.colors))
    fig, ax = plt.subplots(figsize=(5.2, 5))
    for r in rows:
        ax.scatter(r["ref_eui_no_refrigeration"], r["sim_eui_no_refrigeration"], s=26,
                   marker=VINTAGE_MARKERS.get(r["vintage"], "o"), color=colors[r["climate_zone"]],
                   edgecolor="k", linewidth=0.3, alpha=0.85)
    hi = max(max(r["ref_eui_no_refrigeration"], r["sim_eui_no_refrigeration"]) for r in rows) * 1.05
    ax.plot([0, hi], [0, hi], "k-", lw=0.8)
    for f in (0.8, 1.2):
        ax.plot([0, hi], [0, hi * f], "k--", lw=0.5, alpha=0.6)
    ax.set_xlim(0, hi)
    ax.set_ylim(0, hi)
    ax.set_xlabel("DOE reference site EUI (MJ/m², excl. refrigeration)")
    ax.set_ylabel("UBEM-MCP site EUI (MJ/m², excl. refrigeration)")
    handles = [plt.Line2D([], [], marker="o", ls="", color=colors[z], label=z) for z in zones]
    handles += [plt.Line2D([], [], marker=m, ls="", color="gray", label=v) for v, m in VINTAGE_MARKERS.items()]
    ax.legend(handles=handles, fontsize=7, loc="upper left", frameon=False)
    ax.set_title("Archetype models vs. DOE reference results (±20% dashed)", fontsize=9)
    fig.tight_layout()
    fig.savefig(out / "validation_parity.png", dpi=200)
    plt.close(fig)


def end_use_bars(rows, out: Path, climate_zone: str, vintage: str = "post2004"):
    sel = sorted((r for r in rows if r["climate_zone"] == climate_zone and r["vintage"] == vintage),
                 key=lambda r: r["archetype"])
    if not sel:
        return
    fig, ax = plt.subplots(figsize=(10, 4.2))
    width = 0.38
    colors = dict(zip(GROUP_ORDER, plt.cm.Set2.colors))
    for i, r in enumerate(sel):
        for j, (key, hatch) in enumerate((("ref_end_uses", "//"), ("sim_end_uses", ""))):
            g = _grouped(r[key])
            bottom = 0.0
            for name in GROUP_ORDER:
                ax.bar(i + (j - 0.5) * width, g[name], width, bottom=bottom, color=colors[name], hatch=hatch,
                       edgecolor="k", linewidth=0.2, label=name if i == 0 and j == 1 else None)
                bottom += g[name]
    ax.set_xticks(range(len(sel)))
    ax.set_xticklabels([r["archetype"].replace("_", " ") for r in sel], rotation=40, ha="right", fontsize=7)
    ax.set_ylabel("Site EUI (MJ/m²)")
    ax.set_title(f"End uses, climate zone {climate_zone}, {vintage}: reference (hatched, left) "
                 "vs. UBEM-MCP (right)", fontsize=9)
    ax.legend(fontsize=7, ncol=4, frameon=False)
    fig.tight_layout()
    fig.savefig(out / f"validation_end_uses_{climate_zone}.png", dpi=200)
    plt.close(fig)


def scaling(bench_dir: Path, out: Path):
    data = json.loads((bench_dir / "scaling.json").read_text())
    runs = data["runs"]
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.6))
    for w in sorted({r["workers"] for r in runs}):
        rs = sorted((r for r in runs if r["workers"] == w), key=lambda r: r["n"])
        if len(rs) > 1:
            axes[0].plot([r["n"] for r in rs], [r["total_s"] / 60 for r in rs], "o-", label=f"{w} workers")
    axes[0].set_xlabel("Buildings")
    axes[0].set_ylabel("Wall time, generate + simulate + aggregate (min)")
    axes[0].legend(fontsize=8, frameon=False)
    with open(bench_dir / "per_building.csv", newline="") as f:
        pb = [r for r in csv.DictReader(f) if r["simulation_s"]]
    sc = axes[1].scatter([int(r["zones"]) for r in pb], [float(r["simulation_s"]) for r in pb], s=10,
                         c=[int(r["shading_surfaces"]) for r in pb], cmap="viridis", alpha=0.8)
    fig.colorbar(sc, ax=axes[1], label="Shading surfaces")
    axes[1].set_xlabel("Thermal zones")
    axes[1].set_ylabel("EnergyPlus run time per building (s)")
    fig.tight_layout()
    fig.savefig(out / "scaling.png", dpi=200)
    plt.close(fig)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Plot validation and scaling benchmark results")
    ap.add_argument("--validation", nargs="*", default=[])
    ap.add_argument("--scaling", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    if args.validation:
        rows = load_validation(args.validation)
        parity(rows, args.out)
        for cz in sorted({r["climate_zone"] for r in rows}):
            end_use_bars(rows, args.out, cz)
    if args.scaling:
        scaling(args.scaling, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
