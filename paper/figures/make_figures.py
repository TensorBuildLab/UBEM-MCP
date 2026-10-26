"""
Make the manuscript figures from the result files in paper/results/.

    python paper/figures/make_figures.py          # writes fig*.png (600 dpi) and fig*.pdf next to this file

Needs matplotlib only. Fig. 3 draws building footprints from
ubem-mcp-server/agent_tests/fixtures/campus.geojson (© OpenStreetMap
contributors, ODbL).
"""

import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import PatchCollection
from matplotlib.patches import FancyBboxPatch, Polygon

HERE = Path(__file__).resolve().parent
RESULTS = HERE.parent / "results"
CAMPUS_GEOJSON = HERE.parents[1] / "ubem-mcp-server" / "agent_tests" / "fixtures" / "campus.geojson"

plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8, "axes.linewidth": 0.6,
                     "axes.spines.top": False, "axes.spines.right": False, "savefig.bbox": "tight",
                     "savefig.pad_inches": 0.03})
BLUE, ORANGE, GREY, DARK = "#2066a8", "#d55e00", "#9a9a9a", "#222222"


def save(fig, name):
    for ext, kw in (("png", {"dpi": 600}), ("pdf", {})):
        fig.savefig(HERE / f"{name}.{ext}", **kw)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# Fig. 1: architecture
# --------------------------------------------------------------------------- #

def fig1(tool_counts):
    fig, ax = plt.subplots(figsize=(7.2, 4.3))
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 62)
    ax.axis("off")

    def box(x, y, w, h, fc, text=None, size=6.8, weight="normal"):
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.2,rounding_size=1.0", fc=fc, ec=DARK,
                                    lw=0.7))
        if text:
            ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=size, weight=weight,
                    color=DARK, linespacing=1.3)

    def arrow(x0, y0, x1, y1):
        ax.annotate("", xy=(x1, y1), xytext=(x0, y0),
                    arrowprops=dict(arrowstyle="-|>", lw=0.8, color=DARK, shrinkA=0, shrinkB=0))

    def own(x, y, text):
        ax.text(x, y, text, ha="right", va="center", fontsize=6.2, color=ORANGE, style="italic")

    # entry points
    nl = chr(10)
    entries = [f"AI assistant: MCP tools{nl}(any MCP client and model)",
               f"Script or notebook:{nl}Python library (UBEM class)",
               f"Shell or batch job:{nl}command line (ubem-mcp)"]
    for i, t in enumerate(entries):
        x = 4 + i * 32
        box(x, 53, 28, 7, "#eef3fa", t)
        arrow(x + 14, 53, x + 14, 49.6)
    ax.text(50, 48.2, f"one set of {sum(tool_counts.values())} tools with the same arguments and results",
            ha="center", va="center", fontsize=7, style="italic")

    # urban layer
    box(3, 25.5, 94, 20.5, "#f3f8ef")
    ax.text(5, 43.6, f"Urban layer ({tool_counts['urban']} tools): named objects, each step taking the previous one's id",
            fontsize=7.2, weight="bold", va="center")
    chain = [f"Stock{nl}stock_id", f"Site weather{nl}EPW file,{nl}climate zone", f"Model set{nl}model_set_id",
             f"Batch{nl}batch_id", f"Results{nl}results_id", f"Scenario{nl}retrofit{nl}measures"]
    w, gap, x0 = 13.0, 2.6, 5.4
    for i, t in enumerate(chain):
        x = x0 + i * (w + gap)
        box(x, 32.5, w, 7.5, "white", t, size=5.9)
        if i < len(chain) - 1:
            arrow(x + w + 0.3, 36.25, x + w + gap - 0.3, 36.25)
    ax.text(50, 29.6, "data mode · provenance and licences · archetype library · measured data · "
                      "ComStock/ResStock · project records · guides",
            ha="center", va="center", fontsize=6.0, color="#444444")
    own(96, 27.3, "own GIS layers and measured energy enter here")

    # batch and single-model layers
    box(3, 10, 45, 12, "#fdf4ea", f"Batch layer ({tool_counts['batch']} tools){nl}any set of EnergyPlus models as a "
                                   f"background job;{nl}progress, failure diagnosis, cancel, restart-safe records",
        size=6.4)
    box(52, 10, 45, 12, "#f4f0f8", f"Single-model layer ({tool_counts['single']} tools, from EnergyPlus-MCP){nl}"
                                    "inspect, edit and run one EnergyPlus model", size=6.4)
    own(47, 11.6, "own EnergyPlus models enter here")
    arrow(25.5, 25.5, 25.5, 22.4)
    ax.text(26.5, 24.0, "model sets", fontsize=6.2, color="#444444", va="center")

    # engine
    box(20, 1.2, 60, 5.5, "#eeeeee", "EnergyPlus 26.1, one process per model (Docker image or local install)", size=6.4)
    arrow(25.5, 10, 35, 6.9)
    arrow(74.5, 10, 65, 6.9)
    save(fig, "fig1_architecture")


# --------------------------------------------------------------------------- #
# Fig. 2: archetype check
# --------------------------------------------------------------------------- #

LABELS = {"full_service_restaurant": "Full-service restaurant", "quick_service_restaurant": "Quick-service restaurant",
          "large_office": "Large office", "medium_office": "Medium office", "small_office": "Small office",
          "large_hotel": "Large hotel", "small_hotel": "Small hotel", "hospital": "Hospital",
          "outpatient": "Outpatient clinic", "primary_school": "Primary school", "secondary_school": "Secondary school",
          "standalone_retail": "Stand-alone retail", "strip_mall": "Strip mall", "supermarket": "Supermarket",
          "warehouse": "Warehouse", "midrise_apartment": "Midrise apartment", "highrise_apartment": "Highrise apartment",
          "single_family": "Single-family house"}
VINTAGE_MARK = {"pre1980": ("o", "pre-1980"), "1980_2004": ("s", "1980–2004"), "post2004": ("^", "post-2004")}


def fig2(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = [c for c in data["cases"] if c.get("diff_no_refrigeration_pct") is not None]
    order = sorted({c["archetype"] for c in cases}, key=lambda a: LABELS.get(a, a))
    y = {a: i for i, a in enumerate(order)}
    fig, ax = plt.subplots(figsize=(3.5, 4.0))
    ax.axvspan(-20, 20, color="#e8eef6", zorder=0)
    ax.axvline(0, color=GREY, lw=0.6, zorder=1)
    for vintage, (marker, label) in VINTAGE_MARK.items():
        pts = [(c["diff_no_refrigeration_pct"], y[c["archetype"]]) for c in cases if c["vintage"] == vintage]
        if pts:
            ax.scatter([p[0] for p in pts], [p[1] for p in pts], marker=marker, s=16, facecolor="white",
                       edgecolor=BLUE, lw=0.8, label=label, zorder=3)
    ax.set_yticks(range(len(order)))
    ax.set_yticklabels([LABELS.get(a, a) for a in order])
    ax.invert_yaxis()
    ax.set_xlabel("Site EUI difference from DOE reference (%)")
    s = data["summary"]
    ax.set_title(f"Tucson (2B): {s['within_20_pct']} of {s['compared']} cases within ±20%, "
                 f"median |difference| {s['median_abs_diff_pct']}%", fontsize=7)
    ax.legend(frameon=False, fontsize=6.5, loc="upper right", handletextpad=0.2, title="Vintage",
              title_fontsize=6.5)
    ax.tick_params(labelsize=7)
    save(fig, "fig2_archetypes")


# --------------------------------------------------------------------------- #
# Fig. 3: campus electrification
# --------------------------------------------------------------------------- #

def _local_xy(coords, lon0, lat0):
    import math
    kx = 111320 * math.cos(math.radians(lat0))
    return [((lon - lon0) * kx, (lat - lat0) * 110540) for lon, lat in coords]


def fig3(comparison_csv, hourly_base, hourly_scen, summary_json):
    savings = {}
    with open(comparison_csv, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            savings[r["building_id"]] = float(r["savings_pct"])
    gj = json.loads(CAMPUS_GEOJSON.read_text(encoding="utf-8"))
    summary = json.loads(summary_json.read_text(encoding="utf-8"))
    lon0, lat0 = summary["stock"]["origin_lon_lat"]

    fig = plt.figure(figsize=(7.2, 3.3))
    ax = fig.add_axes([0.0, 0.2, 0.44, 0.74])
    polys, vals, other = [], [], []
    for ft in gj["features"]:
        g = ft["geometry"]
        rings = [g["coordinates"][0]] if g["type"] == "Polygon" else [p[0] for p in g["coordinates"]]
        fid = str(ft.get("id") or ft["properties"].get("@id") or ft["properties"].get("id") or "").replace("/", "_")
        for ring in rings:
            poly = Polygon(_local_xy(ring, lon0, lat0), closed=True)
            if fid in savings:
                polys.append(poly)
                vals.append(savings[fid])
            else:
                other.append(poly)
    ax.add_collection(PatchCollection(other, facecolor="#e6e6e6", edgecolor="#bbbbbb", lw=0.2))
    pc = PatchCollection(polys, cmap="viridis", edgecolor="#333333", lw=0.2)
    pc.set_array(vals)
    pc.set_clim(0, 45)
    ax.add_collection(pc)
    ax.autoscale_view()
    ax.set_aspect("equal")
    ax.axis("off")
    cax = fig.add_axes([0.03, 0.1, 0.22, 0.03])
    cb = fig.colorbar(pc, cax=cax, orientation="horizontal")
    cb.set_label("Site energy savings (%)", fontsize=7, labelpad=2)
    cb.ax.tick_params(labelsize=6.5)
    xmax = max(p.get_xy()[:, 0].max() for p in polys + other)
    ymin = min(p.get_xy()[:, 1].min() for p in polys + other)
    ax.plot([xmax - 200, xmax], [ymin - 20, ymin - 20], color=DARK, lw=1.2, clip_on=False)
    ax.text(xmax - 100, ymin - 35, "200 m", ha="center", va="top", fontsize=6.5, clip_on=False)
    ax.text(0.0, 1.04, "(a)", transform=ax.transAxes, fontsize=9, weight="bold", va="bottom")
    fig.text(0.44, 0.09, "Footprints © OpenStreetMap" + chr(10) + "contributors (ODbL)", fontsize=5.5, ha="right",
             va="bottom", color="#666666")

    def series(path):
        with open(path, encoding="utf-8") as f:
            return [(int(r["month"]), int(r["day"]), int(r["hour"]), float(r["electricity_kw"]) / 1000)
                    for r in csv.DictReader(f)]

    base, scen = series(hourly_base), series(hourly_scen)
    pk = summary["comparison"]["district"]["peak_kw"]["electricity"]
    days = [("baseline peak day", pk["baseline"]), ("electrified peak day", pk["scenario"])]
    import calendar
    for k, (title, p) in enumerate(days):
        ax2 = fig.add_axes([0.56 + k * 0.23, 0.17, 0.19, 0.72], sharey=None)
        for data, color, label in ((base, GREY, "Baseline"), (scen, ORANGE, "Electrified")):
            day = [v for (m, d, h, v) in data if m == p["month"] and d == p["day"]]
            ax2.plot(range(1, len(day) + 1), day, color=color, lw=1.3, label=label)
        # mark the district peak that defines this day
        color = GREY if k == 0 else ORANGE
        ax2.plot([p["hour"]], [p["kw"] / 1000], "o", ms=3.5, color=color, zorder=4)
        ax2.annotate(f"peak {p['kw'] / 1000:.1f} MW, {p['hour']}:00", (p["hour"], p["kw"] / 1000),
                     xytext=(4 if k == 1 else -4, 4), textcoords="offset points", fontsize=6.3,
                     ha="left" if k == 1 else "right", color=DARK)
        ax2.set_xlim(1, 24)
        ax2.set_xticks([1, 6, 12, 18, 24])
        ax2.set_ylim(0, 12.5)
        ax2.tick_params(labelsize=6.5)
        ax2.set_xlabel("Hour", fontsize=7)
        ax2.set_title(f"{calendar.month_name[p['month']]} {p['day']}: {title}", fontsize=7)
        if k == 0:
            ax2.set_ylabel("District electric demand (MW)", fontsize=7)
            ax2.legend(frameon=False, fontsize=6.5, loc="lower center")
            ax2.text(-0.32, 1.06, "(b)", transform=ax2.transAxes, fontsize=9, weight="bold", va="bottom")
        else:
            ax2.set_yticklabels([])
    save(fig, "fig3_campus")


# --------------------------------------------------------------------------- #
# Fig. 4: Seattle comparison
# --------------------------------------------------------------------------- #

def fig4(before_csv, after_csv, suspect=("g0063",)):
    from matplotlib.ticker import FixedLocator, NullFormatter, NullLocator

    def load(p):
        with open(p, encoding="utf-8") as f:
            return {r["group_id"]: r for r in csv.DictReader(f) if r["group_id"] not in suspect}

    a, b = load(before_csv), load(after_csv)
    lo, hi = 30, 1500
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.4), sharex=True, sharey=True)
    for ax, data, title in ((axes[0], a, "(a) Archetype heating fuel"),
                            (axes[1], b, "(b) Heating fuel from the meters")):
        for gas in (True, False):
            pts = [(float(r["eui_measured"]), float(r["eui_modeled"])) for r in data.values()
                   if bool(float(r["natural_gas_measured_kwh"] or 0)) == gas]
            ax.scatter([x for x, _ in pts], [y for _, y in pts], s=12, lw=0.6, zorder=3,
                       facecolor="white" if gas else ORANGE, edgecolor=BLUE if gas else ORANGE,
                       label="Gas account (93)" if gas else "No gas account (48)")
        ax.plot([lo, hi], [lo, hi], color=GREY, lw=0.7)
        for k in (1.3, 1 / 1.3):
            ax.plot([lo, hi], [lo * k, hi * k], color=GREY, lw=0.5, ls=":")
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlim(lo, hi)
        ax.set_ylim(lo, hi)
        ticks = [30, 50, 100, 200, 500, 1000]
        for axis in (ax.xaxis, ax.yaxis):
            axis.set_major_locator(FixedLocator(ticks))
            axis.set_major_formatter(matplotlib.ticker.FixedFormatter([str(t) for t in ticks]))
            axis.set_minor_locator(NullLocator())
            axis.set_minor_formatter(NullFormatter())
        ax.set_aspect("equal")
        ax.tick_params(labelsize=7)
        ax.set_title(title, fontsize=8, loc="left")
        ax.set_xlabel("Measured site EUI, 2019 (kWh/m², log scale)", fontsize=7)
    axes[0].set_ylabel("Modeled site EUI (kWh/m², log scale)", fontsize=7)
    axes[0].legend(frameon=False, fontsize=6.5, loc="upper left")
    axes[1].text(hi * 0.9, lo * 1.15, "solid: equal; dotted: ±30%", ha="right", fontsize=6, color="#666666")
    save(fig, "fig4_measured")


def main():
    counts = json.loads((RESULTS / "tool_counts.json").read_text(encoding="utf-8"))
    fig1(counts)
    fig2(RESULTS / "section_5_2_archetypes_tucson.json")
    fig3(RESULTS / "campus_buildings_comparison.csv", RESULTS / "campus_district_hourly_baseline.csv",
         RESULTS / "campus_district_hourly_electrified.csv", RESULTS / "section_4_1_campus.json")
    fig4(RESULTS / "seattle_meters_archetype_fuel.csv", RESULTS / "seattle_meters_fuel_from_data.csv")


if __name__ == "__main__":
    main()
