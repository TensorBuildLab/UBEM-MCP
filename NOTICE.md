# Notice

UBEM-MCP is derived from **EnergyPlus-MCP**, developed at Lawrence Berkeley
National Laboratory (LBNL):

> https://github.com/LBNL-ETA/EnergyPlus-MCP

The large majority of this repository's code — configuration management,
authentication, the streamable-HTTP transport, the EnergyPlus/eppy
integration layer (`energyplus_tools.py`), and the model-inspection/modification
utilities under `utils/` — is carried over from EnergyPlus-MCP with only
mechanical renames (package name, project identifiers, log file names).
See the header of each source file for a pointer back to this notice.

**What's new in UBEM-MCP:**

- `batch_manager.py` and the six batch/portfolio simulation tools
  (`run_batch_simulation`, `get_batch_status`, `get_batch_results`,
  `list_batches`, `cancel_batch`, `discover_idf_files`) — running many
  EnergyPlus models as a background, pollable job, which EnergyPlus-MCP does
  not provide.
- The `urban/` package and the six building-stock tools (`list_archetypes`,
  `load_building_stock`, `fetch_osm_buildings`, `enrich_building_stock`,
  `get_building_stock`, `list_building_stocks`) — GIS footprint intake,
  archetype classification and attribute enrichment for urban-scale modeling.
- Archetype model generation (`urban/library.py`, `urban/modelgen.py`,
  `urban/generate.py`), the archetype-data builders and validation under
  `scripts/`, and the `generate_building_models` / `simulate_building_stock`
  tools.
- Site weather and climate zones (`urban/weather.py`) and the
  `find_weather_stations` / `get_site_weather` tools.
- Results aggregation (`urban/aggregate.py`) and the `aggregate_stock_results`
  tool.

The archetype data in `ubem_mcp_server/urban/data/` are derived from U.S.
Department of Energy datasets (DOE Commercial Reference Buildings, CC BY 4.0;
DOE/PNNL residential prototype models); see that folder's README for
sources and citations.

Weather files and the weather-station index are not redistributed with
UBEM-MCP; they are downloaded at run time from Climate.OneBuilding.Org
(Lawrie, L. K., Crawley, D. B., Development of Global Typical Meteorological
Years (TMYx), https://climate.onebuilding.org), whose terms apply to them.

## Licensing

Two BSD-3-Clause licenses apply within this repository, covering different files:

- **[LICENSE](LICENSE)** — BSD-3-Clause, Copyright (c) 2026 TensorBuildLab.
  Covers UBEM-MCP's own original code: `batch_manager.py`, the `urban/` package,
  the batch/portfolio and building-stock tools in `server.py`, the tests and CI
  added in UBEM-MCP, and other TensorBuildLab-authored additions.
- **[License.txt](License.txt)** / **[Copyright.txt](Copyright.txt)** — BSD-3-Clause-LBNL,
  Copyright (c) 2025 The Regents of the University of California, through Lawrence
  Berkeley National Laboratory. Covers the code carried over from EnergyPlus-MCP
  (see the header of each source file for a pointer back to this notice).

Both are permissive BSD-3-Clause variants; keeping them side by side preserves
LBNL's required copyright notice on the code it originated, without relicensing
it under TensorBuildLab's name.

## Citation

If you use UBEM-MCP in your research, please also cite the original
EnergyPlus-MCP paper this project is built on:

> Han Li, Yujie Xu, Tianzhen Hong, EnergyPlus-MCP: A model-context-protocol
> server for ai-driven building energy modeling, SoftwareX, Volume 32, 2025,
> 102367, ISSN 2352-7110, https://doi.org/10.1016/j.softx.2025.102367.

```bibtex
@article{li2025energyplus,
  title={EnergyPlus-MCP: A model-context-protocol server for ai-driven building energy modeling},
  author={Li, Han and Xu, Yujie and Hong, Tianzhen},
  journal={SoftwareX},
  volume={32},
  pages={102367},
  year={2025},
  issn={2352-7110},
  doi={10.1016/j.softx.2025.102367},
  url={https://www.sciencedirect.com/science/article/pii/S2352711025003334}
}
```
