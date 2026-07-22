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

This project is distributed under the same BSD-3-Clause-LBNL license as
EnergyPlus-MCP; see [License.txt](License.txt) and [Copyright.txt](Copyright.txt).

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
