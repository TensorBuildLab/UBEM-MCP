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

**What's new in UBEM-MCP:** `batch_manager.py` and the six batch/portfolio
simulation tools (`run_batch_simulation`, `get_batch_status`,
`get_batch_results`, `list_batches`, `cancel_batch`, `discover_idf_files`) —
running many EnergyPlus models as a background, pollable job, which
EnergyPlus-MCP does not provide.

## Licensing

Two BSD-3-Clause licenses apply within this repository, covering different files:

- **[LICENSE](LICENSE)** — BSD-3-Clause, Copyright (c) 2026 TensorBuildLab.
  Covers UBEM-MCP's own original code: `batch_manager.py`, the six batch/portfolio
  simulation tools in `server.py`, and other TensorBuildLab-authored additions.
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
