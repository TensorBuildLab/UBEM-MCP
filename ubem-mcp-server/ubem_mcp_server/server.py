"""
UBEM-MCP Server with FastMCP

Adapted from EnergyPlus-MCP (LBNL) for UBEM-MCP; see NOTICE.md for attribution.

EnergyPlus Model Context Protocol Server (EnergyPlus-MCP)
Copyright (c) 2025, The Regents of the University of California,
through Lawrence Berkeley National Laboratory (subject to receipt of
any required approvals from the U.S. Dept. of Energy). All rights reserved.

See License.txt in the parent directory for license details.
"""

import os
import asyncio
import logging
import functools
import inspect
import json
from typing import Optional, Dict, Any, List
from pathlib import Path
from datetime import datetime

# Import FastMCP instead of the low-level Server
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict

# Import our EnergyPlus utilities and configuration
from ubem_mcp_server.energyplus_tools import EnergyPlusManager
from ubem_mcp_server.config import get_config, Config
from ubem_mcp_server.batch_manager import BatchManager, discover_idf_files as _discover_idf_files
from ubem_mcp_server.urban import ARCHETYPES, StockManager, provenance
from ubem_mcp_server import guides as workflow_guides

logger = logging.getLogger(__name__)

# Initialize configuration and set up logging
config = get_config()

# Initialize FastMCP server.
#
# DNS rebinding protection (in mcp>=1.10) defaults to allowed_hosts=[], which
# makes the SDK reject ANY Host header with 421 "Invalid Host header". That's
# correct for browser-attack scenarios but unnecessary when bearer-token auth
# is enforced on every non-/health request.
#
# Behavior:
#   - MCP_ALLOWED_HOSTS unset (default) -> disable DNS rebinding check
#     (suitable for HTTP behind our AuthMiddleware, or stdio).
#   - MCP_ALLOWED_HOSTS="host1,host2"   -> keep check on with that allowlist.
_allowed_hosts_env = os.getenv("MCP_ALLOWED_HOSTS", "").strip()
if _allowed_hosts_env:
    from mcp.server.transport_security import TransportSecuritySettings
    _transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[h.strip() for h in _allowed_hosts_env.split(",") if h.strip()],
    )
    logger.info(
        "DNS rebinding protection ENABLED, allowed_hosts=%s",
        _transport_security.allowed_hosts,
    )
else:
    try:
        from mcp.server.transport_security import TransportSecuritySettings
        _transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
        logger.info("DNS rebinding protection DISABLED (MCP_ALLOWED_HOSTS unset)")
    except ImportError:
        # mcp<1.10 has no transport_security module; behavior was equivalent
        # to disabled-protection (no host check existed). Pass nothing.
        _transport_security = None

_mcp_kwargs: dict = {}
if _transport_security is not None:
    _mcp_kwargs["transport_security"] = _transport_security
# Sent to every MCP client on connection. Plain guidance, no client-specific features.
SERVER_INSTRUCTIONS = """\
UBEM-MCP runs urban building energy modeling (UBEM): building stock data from GIS
footprints and parcels, archetype EnergyPlus models or the user's own models,
background batch simulation, district results and retrofit scenarios.

How the tools fit together:
- Work flows through named objects passed by ID: a stock (stock_id) -> its site
  weather -> a model set (model_set_id) -> a batch (batch_id) -> results -> a
  scenario derived from a baseline. Keep the IDs a tool returns and pass them on.
- Nothing is overwritten: enrichment, archetype assignment and scenarios create
  new objects, so earlier work stays available.
- Long simulations run in the background. After simulate_building_stock,
  simulate_retrofit_scenario, simulate_model_set or run_batch_simulation, call
  get_batch_status with wait_seconds=50 (it waits for the batch) until
  is_complete, then aggregate. Follow next_steps.
- Use the tools rather than writing your own scripts for what they already do.
- Data-only questions need no model: load with mode="data", then query_stock and
  export_stock.

Every UBEM-MCP tool returns {"ok": ...}. On ok=false, read error_type and error;
"unavailable" means an outside service (OpenStreetMap, weather, NREL data) did
not answer, so try again later rather than changing the request.
Pass warnings on to the user, together with how much of the input was guessed
(inferred_share) or is known (known_share), and the attribution line for any
map or report.

Step-by-step guides: call get_workflow_guide (no arguments lists them), or use
the MCP prompts or the resources under ubem://guides/.
"""

mcp = FastMCP(config.server.name, instructions=SERVER_INSTRUCTIONS, **_mcp_kwargs)

# Initialize EnergyPlus manager with configuration
ep_manager = EnergyPlusManager(config)

# Initialize batch manager for multi-model (portfolio) simulation
batch_manager = BatchManager(config)

# Initialize building-stock registry for urban (GIS-based) modeling
stock_manager = StockManager(config)

logger.info(f"UBEM-MCP Server '{config.server.name}' v{config.server.version} initialized")


# Add this tool function to server.py

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
async def copy_file(source_path: str, target_path: str, overwrite: bool = False, file_types: Optional[List[str]] = None) -> str:
    """
    Copy a file from source to target location with intelligent path resolution
    
    Args:
        source_path: Source file path. Can be:
                    - Absolute path: "/full/path/to/file.idf"
                    - Relative path: "models/mymodel.idf"
                    - Filename only: "1ZoneUncontrolled.idf" (searches in sample_files)
                    - Fuzzy name: Will search in sample_files, example_files, weather_data, etc.
        target_path: Target path for the copy. Can be:
                    - Absolute path: "/full/path/to/copy.idf"
                    - Relative path: "outputs/modified_file.idf"  
                    - Filename only: "my_copy.idf" (saves to outputs directory)
        overwrite: Whether to overwrite existing target file (default: False)
        file_types: List of acceptable file extensions (e.g., [".idf", ".epw"]). If None, accepts any file type.
    
    Returns:
        JSON string with copy operation results including resolved paths, file sizes, and validation status
        
    Examples:
        # Copy IDF file with validation
        copy_file("1ZoneUncontrolled.idf", "my_model.idf", file_types=[".idf"])
        
        # Copy weather file
        copy_file("USA_CA_San.Francisco.epw", "sf_weather.epw", file_types=[".epw"])
        
        # Copy any file type
        copy_file("sample.idf", "outputs/test.idf", overwrite=True)
        
        # Copy with fuzzy matching (e.g., city name for weather files)
        copy_file("san francisco", "my_weather.epw", file_types=[".epw"])
    """
    try:
        logger.info(f"Copying file: '{source_path}' -> '{target_path}' (overwrite={overwrite}, file_types={file_types})")
        result = ep_manager.copy_file(source_path, target_path, overwrite, file_types)
        return f"File copy operation completed:\n{result}"
    except ValueError as e:
        logger.warning(f"Invalid arguments for copy_file: {str(e)}")
        return f"Invalid arguments: {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error copying file: {str(e)}")
        return f"Error copying file: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def load_idf_model(idf_path: str) -> str:
    """
    Load and validate an EnergyPlus IDF file
    
    Args:
        idf_path: Path to the IDF file (can be absolute, relative, or just filename for sample files)
    
    Returns:
        JSON string with model information and loading status
    """
    try:
        logger.info(f"Loading IDF model: {idf_path}")
        result = ep_manager.load_idf(idf_path)
        return f"Successfully loaded IDF: {result['original_path']}\nModel info: {result}"
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except ValueError as e:
        logger.warning(f"Invalid input for load_idf_model: {str(e)}")
        return f"Invalid input: {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error loading IDF {idf_path}: {str(e)}")
        return f"Error loading IDF {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def get_model_summary(idf_path: str) -> str:
    """
    Get basic model information (Building, Site, SimulationControl, Version)
    
    Args:
        idf_path: Path to the IDF file
    
    Returns:
        JSON string with model summary information
    """
    try:
        logger.info(f"Getting model summary: {idf_path}")
        summary = ep_manager.get_model_basics(idf_path)
        return f"Model Summary for {idf_path}:\n{summary}"
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except Exception as e:
        logger.error(f"Error getting model summary for {idf_path}: {str(e)}")
        return f"Error getting model summary for {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def check_simulation_settings(idf_path: str) -> str:
    """
    Check SimulationControl and RunPeriod settings with information about modifiable fields
    
    Args:
        idf_path: Path to the IDF file
    
    Returns:
        JSON string with current settings and descriptions of modifiable fields
    """
    try:
        logger.info(f"Checking simulation settings: {idf_path}")
        settings = ep_manager.check_simulation_settings(idf_path)
        return f"Simulation settings for {idf_path}:\n{settings}"
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except Exception as e:
        logger.error(f"Error checking simulation settings for {idf_path}: {str(e)}")
        return f"Error checking simulation settings for {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def inspect_schedules(idf_path: str, include_values: bool = False) -> str:
    """
    Inspect and inventory all schedule objects in the EnergyPlus model
    
    Args:
        idf_path: Path to the IDF file
        include_values: Whether to extract actual schedule values (default: False)
    
    Returns:
        JSON string with detailed schedule inventory and analysis
    """
    try:
        logger.info(f"Inspecting schedules: {idf_path} (include_values={include_values})")
        schedules_info = ep_manager.inspect_schedules(idf_path, include_values)
        return f"Schedule inspection for {idf_path}:\n{schedules_info}"
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except Exception as e:
        logger.error(f"Error inspecting schedules for {idf_path}: {str(e)}")
        return f"Error inspecting schedules for {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def inspect_people(idf_path: str) -> str:
    """
    Inspect and list all People objects in the EnergyPlus model
    
    Args:
        idf_path: Path to the IDF file
    
    Returns:
        JSON string with detailed People objects information including:
        - Name, zone, and schedule associations
        - Calculation method (People, People/Area, Area/Person)
        - Occupancy values and thermal comfort settings
        - Summary statistics by zone and calculation method
    """
    try:
        logger.info(f"Inspecting People objects: {idf_path}")
        result = ep_manager.inspect_people(idf_path)
        return f"People objects inspection for {idf_path}:\n{result}"
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except Exception as e:
        logger.error(f"Error inspecting People objects for {idf_path}: {str(e)}")
        return f"Error inspecting People objects for {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
async def modify_people(
    idf_path: str,
    modifications: List[Dict[str, Any]],
    output_path: Optional[str] = None
) -> str:
    """
    Modify People objects in the EnergyPlus model
    
    Args:
        idf_path: Path to the input IDF file
        modifications: List of modification specifications. Each item should contain:
                      - "target": Specifies which People objects to modify
                        - "all": Apply to all People objects
                        - "zone:ZoneName": Apply to People objects in specific zone
                        - "name:PeopleName": Apply to specific People object by name
                      - "field_updates": Dictionary of field names and new values
                        Valid fields include:
                        - Number_of_People_Schedule_Name
                        - Number_of_People_Calculation_Method (People, People/Area, Area/Person)
                        - Number_of_People
                        - People_per_Floor_Area
                        - Floor_Area_per_Person
                        - Fraction_Radiant
                        - Sensible_Heat_Fraction
                        - Activity_Level_Schedule_Name
                        - Carbon_Dioxide_Generation_Rate
                        - Clothing_Insulation_Schedule_Name
                        - Air_Velocity_Schedule_Name
                        - Thermal_Comfort_Model_1_Type
                        - Thermal_Comfort_Model_2_Type
        output_path: Optional path for output file (if None, creates one with _modified suffix)
    
    Returns:
        JSON string with modification results
        
    Examples:
        # Modify all People objects to use 0.1 people/m2
        modify_people("model.idf", [
            {
                "target": "all",
                "field_updates": {
                    "Number_of_People_Calculation_Method": "People/Area",
                    "People_per_Floor_Area": 0.1
                }
            }
        ])
        
        # Modify People objects in specific zone
        modify_people("model.idf", [
            {
                "target": "zone:Office Zone",
                "field_updates": {
                    "Number_of_People": 10,
                    "Activity_Level_Schedule_Name": "Office Activity"
                }
            }
        ])
        
        # Modify specific People object by name
        modify_people("model.idf", [
            {
                "target": "name:Office People",
                "field_updates": {
                    "Fraction_Radiant": 0.3,
                    "Sensible_Heat_Fraction": 0.6
                }
            }
        ])
    """
    try:
        logger.info(f"Modifying People objects: {idf_path}")
        result = ep_manager.modify_people(idf_path, modifications, output_path)
        return f"People modification results:\n{result}"
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except ValueError as e:
        logger.warning(f"Invalid input for modify_people: {str(e)}")
        return f"Invalid input: {str(e)}"
    except Exception as e:
        logger.error(f"Error modifying People objects for {idf_path}: {str(e)}")
        return f"Error modifying People objects for {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def inspect_lights(idf_path: str) -> str:
    """
    Inspect and list all Lights objects in the EnergyPlus model
    
    Args:
        idf_path: Path to the IDF file
    
    Returns:
        JSON string with detailed Lights objects information including:
        - Name, zone, and schedule associations
        - Calculation method (LightingLevel, Watts/Area, Watts/Person)
        - Lighting power values and heat fraction settings
        - Summary statistics by zone and calculation method
    """
    try:
        logger.info(f"Inspecting Lights objects: {idf_path}")
        result = ep_manager.inspect_lights(idf_path)
        return f"Lights objects inspection for {idf_path}:\n{result}"
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except Exception as e:
        logger.error(f"Error inspecting Lights objects for {idf_path}: {str(e)}")
        return f"Error inspecting Lights objects for {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
async def modify_lights(
    idf_path: str,
    modifications: List[Dict[str, Any]],
    output_path: Optional[str] = None
) -> str:
    """
    Modify Lights objects in the EnergyPlus model
    
    Args:
        idf_path: Path to the input IDF file
        modifications: List of modification specifications. Each item should contain:
                      - "target": Specifies which Lights objects to modify
                        - "all": Apply to all Lights objects
                        - "zone:ZoneName": Apply to Lights objects in specific zone
                        - "name:LightsName": Apply to specific Lights object by name
                      - "field_updates": Dictionary of field names and new values
                        Valid fields include:
                        - Schedule_Name
                        - Design_Level_Calculation_Method (LightingLevel, Watts/Area, Watts/Person)
                        - Lighting_Level
                        - Watts_per_Floor_Area
                        - Watts_per_Person
                        - Return_Air_Fraction
                        - Fraction_Radiant
                        - Fraction_Visible
                        - Fraction_Replaceable
                        - EndUse_Subcategory
                        - Return_Air_Fraction_Calculated_from_Plenum_Temperature
                        - Return_Air_Fraction_Function_of_Plenum_Temperature_Coefficient_1
                        - Return_Air_Fraction_Function_of_Plenum_Temperature_Coefficient_2
                        - Return_Air_Heat_Gain_Node_Name
                        - Exhaust_Air_Heat_Gain_Node_Name
        output_path: Optional path for output file (if None, creates one with _modified suffix)
    
    Returns:
        JSON string with modification results
        
    Examples:
        # Modify all Lights objects to use 10 W/m2
        modify_lights("model.idf", [
            {
                "target": "all",
                "field_updates": {
                    "Design_Level_Calculation_Method": "Watts/Area",
                    "Watts_per_Floor_Area": 10.0
                }
            }
        ])
        
        # Modify Lights objects in specific zone
        modify_lights("model.idf", [
            {
                "target": "zone:Office Zone",
                "field_updates": {
                    "Lighting_Level": 2000,
                    "Schedule_Name": "Office Lighting Schedule"
                }
            }
        ])
        
        # Modify specific Lights object by name
        modify_lights("model.idf", [
            {
                "target": "name:Office Lights",
                "field_updates": {
                    "Fraction_Radiant": 0.42,
                    "Fraction_Visible": 0.18
                }
            }
        ])
    """
    try:
        logger.info(f"Modifying Lights objects: {idf_path}")
        result = ep_manager.modify_lights(idf_path, modifications, output_path)
        return f"Lights modification results:\n{result}"
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except ValueError as e:
        logger.warning(f"Invalid input for modify_lights: {str(e)}")
        return f"Invalid input: {str(e)}"
    except Exception as e:
        logger.error(f"Error modifying Lights objects for {idf_path}: {str(e)}")
        return f"Error modifying Lights objects for {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def inspect_electric_equipment(idf_path: str) -> str:
    """
    Inspect and list all ElectricEquipment objects in the EnergyPlus model
    
    Args:
        idf_path: Path to the IDF file
    
    Returns:
        JSON string with detailed ElectricEquipment objects information including:
        - Name, zone, and schedule associations
        - Calculation method (EquipmentLevel, Watts/Area, Watts/Person)
        - Equipment power values and heat fraction settings
        - Summary statistics by zone and calculation method
    """
    try:
        logger.info(f"Inspecting ElectricEquipment objects: {idf_path}")
        result = ep_manager.inspect_electric_equipment(idf_path)
        return f"ElectricEquipment objects inspection for {idf_path}:\n{result}"
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except Exception as e:
        logger.error(f"Error inspecting ElectricEquipment objects for {idf_path}: {str(e)}")
        return f"Error inspecting ElectricEquipment objects for {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
async def modify_electric_equipment(
    idf_path: str,
    modifications: List[Dict[str, Any]],
    output_path: Optional[str] = None
) -> str:
    """
    Modify ElectricEquipment objects in the EnergyPlus model
    
    Args:
        idf_path: Path to the input IDF file
        modifications: List of modification specifications. Each item should contain:
                      - "target": Specifies which ElectricEquipment objects to modify
                        - "all": Apply to all ElectricEquipment objects
                        - "zone:ZoneName": Apply to ElectricEquipment objects in specific zone
                        - "name:ElectricEquipmentName": Apply to specific ElectricEquipment object by name
                      - "field_updates": Dictionary of field names and new values
                        Valid fields include:
                        - Schedule_Name
                        - Design_Level_Calculation_Method (EquipmentLevel, Watts/Area, Watts/Person)
                        - Design_Level
                        - Watts_per_Floor_Area
                        - Watts_per_Person
                        - Fraction_Latent
                        - Fraction_Radiant
                        - Fraction_Lost
                        - EndUse_Subcategory
        output_path: Optional path for output file (if None, creates one with _modified suffix)
    
    Returns:
        JSON string with modification results
        
    Examples:
        # Modify all ElectricEquipment objects to use 15 W/m2
        modify_electric_equipment("model.idf", [
            {
                "target": "all",
                "field_updates": {
                    "Design_Level_Calculation_Method": "Watts/Area",
                    "Watts_per_Floor_Area": 15.0
                }
            }
        ])
        
        # Modify ElectricEquipment objects in specific zone
        modify_electric_equipment("model.idf", [
            {
                "target": "zone:Office Zone",
                "field_updates": {
                    "Design_Level": 3000,
                    "Schedule_Name": "Office Equipment Schedule"
                }
            }
        ])
        
        # Modify specific ElectricEquipment object by name
        modify_electric_equipment("model.idf", [
            {
                "target": "name:Office Equipment",
                "field_updates": {
                    "Fraction_Radiant": 0.3,
                    "Fraction_Latent": 0.1
                }
            }
        ])
    """
    try:
        logger.info(f"Modifying ElectricEquipment objects: {idf_path}")
        result = ep_manager.modify_electric_equipment(idf_path, modifications, output_path)
        return f"ElectricEquipment modification results:\n{result}"
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except ValueError as e:
        logger.warning(f"Invalid input for modify_electric_equipment: {str(e)}")
        return f"Invalid input: {str(e)}"
    except Exception as e:
        logger.error(f"Error modifying ElectricEquipment objects for {idf_path}: {str(e)}")
        return f"Error modifying ElectricEquipment objects for {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
async def modify_simulation_control(
    idf_path: str, 
    field_updates: Dict[str, Any],  # Changed from str to Dict[str, Any]
    output_path: Optional[str] = None
) -> str:
    """
    Modify SimulationControl settings and save to a new file
    
    Args:
        idf_path: Path to the input IDF file
        field_updates: Dictionary with field names and new values (e.g., {"Run_Simulation_for_Weather_File_Run_Periods": "Yes"})
        output_path: Optional path for output file (if None, creates one with _modified suffix)
    
    Returns:
        JSON string with modification results
    """
    try:
        logger.info(f"Modifying SimulationControl: {idf_path}")
        
        # No need to parse JSON since we're receiving a dict directly
        result = ep_manager.modify_simulation_settings(
            idf_path=idf_path,
            object_type="SimulationControl",
            field_updates=field_updates,  # Pass the dict directly
            output_path=output_path
        )
        return f"SimulationControl modification results:\n{result}"
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except Exception as e:
        logger.error(f"Error modifying SimulationControl for {idf_path}: {str(e)}")
        return f"Error modifying SimulationControl for {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
async def modify_run_period(
    idf_path: str, 
    field_updates: Dict[str, Any],  # Changed from str to Dict[str, Any]
    run_period_index: int = 0,
    output_path: Optional[str] = None
) -> str:
    """
    Modify RunPeriod settings and save to a new file
    
    Args:
        idf_path: Path to the input IDF file
        field_updates: Dictionary with field names and new values (e.g., {"Begin_Month": 1, "End_Month": 3})
        run_period_index: Index of RunPeriod to modify (default 0 for first RunPeriod)
        output_path: Optional path for output file (if None, creates one with _modified suffix)
    
    Returns:
        JSON string with modification results
    """
    try:
        logger.info(f"Modifying RunPeriod: {idf_path}")
        
        # No need to parse JSON since we're receiving a dict directly
        result = ep_manager.modify_simulation_settings(
            idf_path=idf_path,
            object_type="RunPeriod",
            field_updates=field_updates,  # Pass the dict directly
            run_period_index=run_period_index,
            output_path=output_path
        )
        return f"RunPeriod modification results:\n{result}"
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except Exception as e:
        logger.error(f"Error modifying RunPeriod for {idf_path}: {str(e)}")
        return f"Error modifying RunPeriod for {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
async def change_infiltration_by_mult(
    idf_path: str, 
    mult: float,
    output_path: Optional[str] = None
) -> str:
    """
    Modify infiltration in ZoneInfiltration:DesignFlowRate and save to a new file
    
    Args:
        idf_path: Path to the input IDF file
        mult: Multiplicative factor to apply to all ZoneInfiltration:DesignFlowRate objects
        output_path: Optional path for output file (if None, creates one with _modified suffix)
    
    Returns:
        JSON string with modification results
    """
    try:
        logger.info(f"Modifying Infiltration: {idf_path}")
        
        # No need to parse JSON since we're receiving a dict directly
        result = ep_manager.change_infiltration_by_mult(
            idf_path=idf_path,
            mult=mult,  # Pass the float directly
            output_path=output_path
        )
        return f"Infiltration modification results:\n{result}"
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except Exception as e:
        logger.error(f"Error Infiltration modification for {idf_path}: {str(e)}")
        return f"Error Infiltration modification for {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
async def add_window_film_outside(
    idf_path: str,
    u_value: float = 4.94,
    shgc: float = 0.45,
    visible_transmittance: float = 0.66,
    output_path: Optional[str] = None
) -> str:
    """
    Add exterior window film to all exterior windows using WindowMaterial:SimpleGlazingSystem
    
    Args:
        idf_path: Path to the input IDF file
        u_value: U-value of the window film (default: 4.94 W/m²·K from CBES)
        shgc: Solar Heat Gain Coefficient of the window film (default: 0.45)
        visible_transmittance: Visible transmittance of the window film (default: 0.66)
        output_path: Optional path for output file (if None, creates one with _modified suffix)
    
    Returns:
        JSON string with modification results
    """
    try:
        logger.info(f"Adding window film to exterior windows: {idf_path}")
        result = ep_manager.add_window_film_outside(
            idf_path=idf_path,
            u_value=u_value,
            shgc=shgc,
            visible_transmittance=visible_transmittance,
            output_path=output_path
        )
        return f"Window film modification results:\n{result}"
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except Exception as e:
        logger.error(f"Error adding window film for {idf_path}: {str(e)}")
        return f"Error adding window film for {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
async def add_coating_outside(
    idf_path: str,
    location: str,
    solar_abs: float = 0.4,
    thermal_abs: float = 0.9,
    output_path: Optional[str] = None
) -> str:
    """
    Add exterior coating to all exterior surfaces of the specified location (wall or roof)
    
    Args:
        idf_path: Path to the input IDF file
        location: Surface type - either "wall" or "roof"
        solar_abs: Solar Absorptance of the exterior coating (default: 0.4)
        thermal_abs: Thermal Absorptance of the exterior coating (default: 0.9)
        output_path: Optional path for output file (if None, creates one with _modified suffix)
    
    Returns:
        JSON string with modification results
    """
    try:
        logger.info(f"Adding exterior coating to {location} surfaces: {idf_path}")
        result = ep_manager.add_coating_outside(
            idf_path=idf_path,
            location=location,
            solar_abs=solar_abs,
            thermal_abs=thermal_abs,
            output_path=output_path
        )
        return f"Exterior coating modification results:\n{result}"
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except ValueError as e:
        logger.warning(f"Invalid location parameter: {location}")
        return f"Invalid location (must be 'wall' or 'roof'): {str(e)}"
    except Exception as e:
        logger.error(f"Error adding exterior coating for {idf_path}: {str(e)}")
        return f"Error adding exterior coating for {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def list_zones(idf_path: str) -> str:
    """
    List all zones in the EnergyPlus model
    
    Args:
        idf_path: Path to the IDF file
    
    Returns:
        JSON string with detailed zone information
    """
    try:
        logger.info(f"Listing zones: {idf_path}")
        zones = ep_manager.list_zones(idf_path)
        return f"Zones in {idf_path}:\n{zones}"
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except Exception as e:
        logger.error(f"Error listing zones for {idf_path}: {str(e)}")
        return f"Error listing zones for {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def get_surfaces(idf_path: str) -> str:
    """
    Get detailed surface information from the EnergyPlus model
    
    Args:
        idf_path: Path to the IDF file
    
    Returns:
        JSON string with surface details
    """
    try:
        logger.info(f"Getting surfaces: {idf_path}")
        surfaces = ep_manager.get_surfaces(idf_path)
        return f"Surfaces in {idf_path}:\n{surfaces}"
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except Exception as e:
        logger.error(f"Error getting surfaces for {idf_path}: {str(e)}")
        return f"Error getting surfaces for {idf_path}: {str(e)}"

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def get_materials(idf_path: str) -> str:
    """
    Get material information from the EnergyPlus model
    
    Args:
        idf_path: Path to the IDF file
    
    Returns:
        JSON string with material details
    """
    try:
        logger.info(f"Getting materials: {idf_path}")
        materials = ep_manager.get_materials(idf_path)
        return f"Materials in {idf_path}:\n{materials}"
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except Exception as e:
        logger.error(f"Error getting materials for {idf_path}: {str(e)}")
        return f"Error getting materials for {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def validate_idf(idf_path: str) -> str:
    """
    Validate an EnergyPlus IDF file and return validation results
    
    Args:
        idf_path: Path to the IDF file
    
    Returns:
        JSON string with validation results, warnings, and errors
    """
    try:
        logger.info(f"Validating IDF: {idf_path}")
        validation_result = ep_manager.validate_idf(idf_path)
        return f"Validation results for {idf_path}:\n{validation_result}"
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except Exception as e:
        logger.error(f"Error validating IDF {idf_path}: {str(e)}")
        return f"Error validating IDF {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def get_output_variables(idf_path: str, discover_available: bool = False, run_days: int = 1) -> str:
    """
    Get output variables from the model - either configured variables or discover all available ones
    
    Args:
        idf_path: Path to the IDF file (can be absolute, relative, or just filename for sample files)
        discover_available: If True, runs a short simulation to discover all available variables. 
                          If False, returns currently configured variables in the IDF (default: False)
        run_days: Number of days to run for discovery simulation (default: 1, only used if discover_available=True)
    
    Returns:
        JSON string with output variables information. When discover_available=True, includes
        all possible variables with units, frequencies, and ready-to-use Output:Variable lines.
        When discover_available=False, shows only currently configured Output:Variable and Output:Meter objects.
    """
    try:
        logger.info(f"Getting output variables: {idf_path} (discover_available={discover_available})")
        result = ep_manager.get_output_variables(idf_path, discover_available, run_days)
        
        mode = "available variables discovery" if discover_available else "configured variables"
        return f"Output variables ({mode}) for {idf_path}:\n{result}"
        
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except Exception as e:
        logger.error(f"Error getting output variables for {idf_path}: {str(e)}")
        return f"Error getting output variables for {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def get_output_meters(idf_path: str, discover_available: bool = False, run_days: int = 1) -> str:
    """
    Get output meters from the model - either configured meters or discover all available ones
    
    Args:
        idf_path: Path to the IDF file (can be absolute, relative, or just filename for sample files)
        discover_available: If True, runs a short simulation to discover all available meters.
                          If False, returns currently configured meters in the IDF (default: False)
        run_days: Number of days to run for discovery simulation (default: 1, only used if discover_available=True)
    
    Returns:
        JSON string with meter information. When discover_available=True, includes
        all possible meters with units, frequencies, and ready-to-use Output:Meter lines.
        When discover_available=False, shows only currently configured Output:Meter objects.
    """
    try:
        logger.info(f"Getting output meters: {idf_path} (discover_available={discover_available})")
        result = ep_manager.get_output_meters(idf_path, discover_available, run_days)
        
        mode = "available meters discovery" if discover_available else "configured meters"
        return f"Output meters ({mode}) for {idf_path}:\n{result}"
        
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except Exception as e:
        logger.error(f"Error getting output meters for {idf_path}: {str(e)}")
        return f"Error getting output meters for {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
async def add_output_variables(
    idf_path: str,
    variables: List,  # Can be List[Dict], List[str], or mixed
    validation_level: str = "moderate",
    allow_duplicates: bool = False,
    output_path: Optional[str] = None
) -> str:
    """
    Add output variables to an EnergyPlus IDF file with intelligent validation
    
    Args:
        idf_path: Path to the input IDF file (can be absolute, relative, or filename for sample files)
        variables: List of variable specifications. Can be:
                  - Simple strings: ["Zone Air Temperature", "Surface Inside Face Temperature"] 
                  - [name, frequency] pairs: [["Zone Air Temperature", "hourly"], ["Surface Temperature", "daily"]]
                  - Full specifications: [{"key_value": "*", "variable_name": "Zone Air Temperature", "frequency": "hourly"}]
                  - Mixed formats in the same list
        validation_level: Validation strictness level:
                         - "strict": Full validation with model checking (recommended for beginners)
                         - "moderate": Basic validation with helpful warnings (default)
                         - "lenient": Minimal validation (for advanced users)
        allow_duplicates: Whether to allow duplicate output variable specifications (default: False)
        output_path: Optional path for output file (if None, creates one with _with_outputs suffix)
    
    Returns:
        JSON string with detailed results including validation report, added variables, and performance metrics
        
    Examples:
        # Simple usage
        add_output_variables("model.idf", ["Zone Air Temperature", "Zone Air Relative Humidity"])
        
        # With custom frequencies  
        add_output_variables("model.idf", [["Zone Air Temperature", "daily"], ["Surface Temperature", "hourly"]])
        
        # Full control
        add_output_variables("model.idf", [
            {"key_value": "Zone1", "variable_name": "Zone Air Temperature", "frequency": "hourly"},
            {"key_value": "*", "variable_name": "Surface Inside Face Temperature", "frequency": "daily"}
        ], validation_level="strict")
    """
    try:
        logger.info(f"Adding output variables: {idf_path} ({len(variables)} variables, {validation_level} validation)")
        
        result = ep_manager.add_output_variables(
            idf_path=idf_path,
            variables=variables,
            validation_level=validation_level,
            allow_duplicates=allow_duplicates,
            output_path=output_path
        )
        
        return f"Output variables addition results:\n{result}"
        
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except ValueError as e:
        logger.warning(f"Invalid arguments for add_output_variables: {str(e)}")
        return f"Invalid arguments: {str(e)}"
    except Exception as e:
        logger.error(f"Error adding output variables: {str(e)}")
        return f"Error adding output variables: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
async def add_output_meters(
    idf_path: str,
    meters: List,  # Can be List[Dict], List[str], or mixed
    validation_level: str = "moderate",
    allow_duplicates: bool = False,
    output_path: Optional[str] = None
) -> str:
    """
    Add output meters to an EnergyPlus IDF file with intelligent validation
    
    Args:
        idf_path: Path to the input IDF file (can be absolute, relative, or filename for sample files)
        meters: List of meter specifications. Can be:
               - Simple strings: ["Electricity:Facility", "NaturalGas:Facility"] 
               - [name, frequency] pairs: [["Electricity:Facility", "hourly"], ["NaturalGas:Facility", "daily"]]
               - [name, frequency, type] triplets: [["Electricity:Facility", "hourly", "Output:Meter"]]
               - Full specifications: [{"meter_name": "Electricity:Facility", "frequency": "hourly", "meter_type": "Output:Meter"}]
               - Mixed formats in the same list
        validation_level: Validation strictness level:
                         - "strict": Full validation with model checking (recommended for beginners)
                         - "moderate": Basic validation with helpful warnings (default)
                         - "lenient": Minimal validation (for advanced users)
        allow_duplicates: Whether to allow duplicate output meter specifications (default: False)
        output_path: Optional path for output file (if None, creates one with _with_meters suffix)
    
    Returns:
        JSON string with detailed results including validation report, added meters, and performance metrics
        
    Examples:
        # Simple usage
        add_output_meters("model.idf", ["Electricity:Facility", "NaturalGas:Facility"])
        
        # With custom frequencies  
        add_output_meters("model.idf", [["Electricity:Facility", "daily"], ["NaturalGas:Facility", "hourly"]])
        
        # Full control with meter types
        add_output_meters("model.idf", [
            {"meter_name": "Electricity:Facility", "frequency": "hourly", "meter_type": "Output:Meter"},
            {"meter_name": "NaturalGas:Facility", "frequency": "daily", "meter_type": "Output:Meter:Cumulative"}
        ], validation_level="strict")
    """
    try:
        logger.info(f"Adding output meters: {idf_path} ({len(meters)} meters, {validation_level} validation)")
        
        result = ep_manager.add_output_meters(
            idf_path=idf_path,
            meters=meters,
            validation_level=validation_level,
            allow_duplicates=allow_duplicates,
            output_path=output_path
        )
        
        return f"Output meters addition results:\n{result}"
        
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except ValueError as e:
        logger.warning(f"Invalid arguments for add_output_meters: {str(e)}")
        return f"Invalid arguments: {str(e)}"
    except Exception as e:
        logger.error(f"Error adding output meters: {str(e)}")
        return f"Error adding output meters: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def list_available_files(
    include_example_files: bool = False,
    include_weather_data: bool = False
) -> str:
    """
    List available files in specified directories
    
    Args:
        include_example_files: Whether to include EnergyPlus example files directory (default: False)
        include_weather_data: Whether to include EnergyPlus weather data directory (default: False)
    
    Returns:
        JSON string with available files organized by source and type. Always includes sample_files directory.
    """
    try:
        logger.info(f"Listing available files (example_files={include_example_files}, weather_data={include_weather_data})")
        files = ep_manager.list_available_files(include_example_files, include_weather_data)
        return f"Available files:\n{files}"
    except Exception as e:
        logger.error(f"Error listing available files: {str(e)}")
        return f"Error listing available files: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def get_server_configuration() -> str:
    """
    Get current server configuration information
    
    Returns:
        JSON string with configuration details
    """
    try:
        logger.info("Getting server configuration")
        config_info = ep_manager.get_configuration_info()
        return f"Current server configuration:\n{config_info}"
    except Exception as e:
        logger.error(f"Error getting configuration: {str(e)}")
        return f"Error getting configuration: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def get_server_status() -> str:
    """
    Get current server status and health information
    
    Returns:
        JSON string with server status
    """
    try:
        import sys
        import platform
        from datetime import datetime
        
        status_info = {
            "server": {
                "name": config.server.name,
                "version": config.server.version,
                "status": "running",
                "startup_time": datetime.now().isoformat(),
                "debug_mode": config.debug_mode
            },
            "system": {
                "python_version": sys.version,
                "platform": platform.platform(),
                "architecture": platform.architecture()[0]
            },
            "energyplus": {
                "version": config.energyplus.version,
                "idd_available": os.path.exists(config.energyplus.idd_path) if config.energyplus.idd_path else False,
                "executable_available": os.path.exists(config.energyplus.executable_path) if config.energyplus.executable_path else False
            },
            "paths": {
                "sample_files_available": os.path.exists(config.paths.sample_files_path),
                "temp_dir_available": os.path.exists(config.paths.temp_dir),
                "output_dir_available": os.path.exists(config.paths.output_dir)
            }
        }
        
        import json
        return f"Server status:\n{json.dumps(status_info, indent=2)}"
        
    except Exception as e:
        logger.error(f"Error getting server status: {str(e)}")
        return f"Error getting server status: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def discover_hvac_loops(idf_path: str) -> str:
    """
    Discover all HVAC loops (Plant, Condenser, Air) in the EnergyPlus model
    
    Args:
        idf_path: Path to the IDF file
    
    Returns:
        JSON string with all HVAC loops found, organized by type
    """
    try:
        logger.info(f"Discovering HVAC loops: {idf_path}")
        loops = ep_manager.discover_hvac_loops(idf_path)
        return f"HVAC loops discovered in {idf_path}:\n{loops}"
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except Exception as e:
        logger.error(f"Error discovering HVAC loops for {idf_path}: {str(e)}")
        return f"Error discovering HVAC loops for {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def get_loop_topology(idf_path: str, loop_name: str) -> str:
    """
    Get detailed topology information for a specific HVAC loop
    
    Args:
        idf_path: Path to the IDF file
        loop_name: Name of the specific loop to analyze
    
    Returns:
        JSON string with detailed loop topology including supply/demand sides, branches, and components
    """
    try:
        logger.info(f"Getting loop topology for '{loop_name}': {idf_path}")
        topology = ep_manager.get_loop_topology(idf_path, loop_name)
        return f"Loop topology for '{loop_name}' in {idf_path}:\n{topology}"
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except ValueError as e:
        logger.warning(f"Loop not found: {loop_name}")
        return f"Loop not found: {str(e)}"
    except Exception as e:
        logger.error(f"Error getting loop topology for {idf_path}: {str(e)}")
        return f"Error getting loop topology for {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
async def visualize_loop_diagram(
    idf_path: str, 
    loop_name: Optional[str] = None,
    output_path: Optional[str] = None,
    format: str = "png",
    show_legend: bool = True
) -> str:
    """
    Generate and save a visual diagram of HVAC loop(s)
    
    Args:
        idf_path: Path to the IDF file
        loop_name: Optional specific loop name (if None, shows all loops)
        output_path: Optional custom output path (if None, creates one automatically)
        format: Image format for the diagram (png, jpg, pdf, svg)
        show_legend: Whether to include a legend in the diagram (default: True)
    
    Returns:
        JSON string with diagram generation results and file path
    """
    try:
        logger.info(f"Creating loop diagram for '{loop_name or 'all loops'}': {idf_path} (show_legend={show_legend})")
        result = ep_manager.visualize_loop_diagram(idf_path, loop_name, output_path, format, show_legend)
        return f"Loop diagram created:\n{result}"
    except FileNotFoundError as e:
        logger.warning(f"IDF file not found: {idf_path}")
        return f"File not found: {str(e)}"
    except Exception as e:
        logger.error(f"Error creating loop diagram for {idf_path}: {str(e)}")
        return f"Error creating loop diagram for {idf_path}: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
async def run_energyplus_simulation(
    idf_path: str, 
    weather_file: Optional[str] = None,
    output_directory: Optional[str] = None,
    annual: bool = True,
    design_day: bool = False,
    readvars: bool = True,
    expandobjects: bool = True
) -> str:
    """
    Run EnergyPlus simulation with specified IDF and weather file
    
    Args:
        idf_path: Path to the IDF file (can be absolute, relative, or just filename for sample files)
        weather_file: Path to weather file (.epw) or city name (e.g., 'San Francisco'). If None, simulation runs without weather file
        output_directory: Directory for simulation outputs (if None, creates timestamped directory in outputs/)
        annual: Run annual simulation (default: True)
        design_day: Run design day only simulation (default: False) 
        readvars: Run ReadVarsESO after simulation to process outputs (default: True)
        expandobjects: Run ExpandObjects prior to simulation for HVAC templates (default: True)
    
    Returns:
        JSON string with simulation results, duration, and output file paths
    """
    try:
        logger.info(f"Running EnergyPlus simulation: {idf_path}")
        if weather_file:
            logger.info(f"With weather file: {weather_file}")
        
        result = ep_manager.run_simulation(
            idf_path=idf_path,
            weather_file=weather_file,
            output_directory=output_directory,
            annual=annual,
            design_day=design_day,
            readvars=readvars,
            expandobjects=expandobjects
        )
        return f"EnergyPlus simulation completed:\n{result}"
    except FileNotFoundError as e:
        logger.warning(f"File not found for simulation: {str(e)}")
        return f"File not found: {str(e)}"
    except Exception as e:
        logger.error(f"Error running EnergyPlus simulation: {str(e)}")
        return f"Error running simulation: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
async def create_interactive_plot(
    output_directory: str,
    idf_name: Optional[str] = None,
    file_type: str = "auto",
    custom_title: Optional[str] = None
) -> str:
    """
    Create interactive HTML plot from EnergyPlus output files (meter or variable outputs)
    
    Args:
        output_directory: Directory containing the CSV output files from simulation
        idf_name: Name of the IDF file (without extension). If None, auto-detects from files
        file_type: Type of file to plot - "meter", "variable", or "auto" (default: auto)
        custom_title: Custom title for the plot (optional)
    
    Returns:
        JSON string with plot creation results and file path
    """
    try:
        logger.info(f"Creating interactive plot from: {output_directory}")
        result = ep_manager.create_interactive_plot(output_directory, idf_name, file_type, custom_title)
        return f"Interactive plot created:\n{result}"
    except FileNotFoundError as e:
        logger.warning(f"Output files not found: {str(e)}")
        return f"Files not found: {str(e)}"
    except Exception as e:
        logger.error(f"Error creating interactive plot: {str(e)}")
        return f"Error creating interactive plot: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def get_server_logs(lines: int = 50) -> str:
    """
    Get recent server log entries
    
    Args:
        lines: Number of recent log lines to return (default 50)
    
    Returns:
        Recent log entries as text
    """
    try:
        log_file = Path(config.paths.workspace_root) / "logs" / "ubem_mcp_server.log"
        
        if not log_file.exists():
            return "Log file not found. Server may be using console logging only."
        
        # Read last N lines efficiently
        with open(log_file, 'r') as f:
            all_lines = f.readlines()
            recent_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
        
        log_content = {
            "log_file": str(log_file),
            "total_lines": len(all_lines),
            "showing_lines": len(recent_lines),
            "recent_logs": "".join(recent_lines)
        }
        
        return f"Recent server logs:\n{json.dumps(log_content, indent=2)}"
        
    except Exception as e:
        logger.error(f"Error reading server logs: {str(e)}")
        return f"Error reading server logs: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def get_error_logs(lines: int = 20) -> str:
    """
    Get recent error log entries
    
    Args:
        lines: Number of recent error lines to return (default 20)
    
    Returns:
        Recent error log entries as text
    """
    try:
        error_log_file = Path(config.paths.workspace_root) / "logs" / "ubem_mcp_errors.log"
        
        if not error_log_file.exists():
            return "Error log file not found. No errors logged yet."
        
        with open(error_log_file, 'r') as f:
            all_lines = f.readlines()
            recent_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
        
        error_content = {
            "error_log_file": str(error_log_file),
            "total_error_lines": len(all_lines),
            "showing_lines": len(recent_lines),
            "recent_errors": "".join(recent_lines)
        }
        
        return f"Recent error logs:\n{json.dumps(error_content, indent=2)}"
        
    except Exception as e:
        logger.error(f"Error reading error logs: {str(e)}")
        return f"Error reading error logs: {str(e)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
async def clear_logs() -> str:
    """
    Clear/rotate current log files (creates backup)
    
    Returns:
        Status of log clearing operation
    """
    try:
        log_dir = Path(config.paths.workspace_root) / "logs"
        
        if not log_dir.exists():
            return "No log directory found."
        
        cleared_files = []
        
        # Main log file
        main_log = log_dir / "ubem_mcp_server.log"
        if main_log.exists():
            backup_name = f"ubem_mcp_server_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            main_log.rename(log_dir / backup_name)
            cleared_files.append(str(main_log))
        
        # Error log file
        error_log = log_dir / "ubem_mcp_errors.log"
        if error_log.exists():
            backup_name = f"ubem_mcp_errors_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            error_log.rename(log_dir / backup_name)
            cleared_files.append(str(error_log))
        
        result = {
            "success": True,
            "cleared_files": cleared_files,
            "backup_location": str(log_dir),
            "message": "Log files cleared and backed up successfully"
        }
        
        logger.info("Log files cleared and backed up")
        return json.dumps(result, indent=2)
        
    except Exception as e:
        logger.error(f"Error clearing logs: {str(e)}")
        return f"Error clearing logs: {str(e)}"


# ============================================================================
# Response envelope for the UBEM-MCP tools — new in UBEM-MCP
# ============================================================================
#
# Every UBEM-MCP tool returns one structured object (MCP structured content,
# with the same JSON as text for clients that read text only):
#   success: {"ok": true, ...data, "warnings": [...], "next_steps": [...]}
#   failure: {"ok": false, "error_type": "not_found" | "invalid_input" | "unavailable" |
#             "internal", "error": "..."}   (unavailable: an outside service did not answer)
# Tool annotations tell any MCP client which tools only read, which may
# change or discard work, and which reach the internet.

class ToolResult(BaseModel):
    """The envelope every UBEM-MCP tool returns; tool data are extra fields."""
    model_config = ConfigDict(extra="allow")

    ok: bool
    warnings: List[str] = []
    error_type: Optional[str] = None
    error: Optional[str] = None


def _ok(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        out = {"ok": True, **result}
    elif isinstance(result, list):
        out = {"ok": True, "count": len(result), "items": result}
    else:
        out = {"ok": True, "result": result}
    out.setdefault("warnings", [])
    return out


def _fail(error_type: str, error: BaseException) -> Dict[str, Any]:
    message = error.args[0] if isinstance(error, KeyError) and error.args else str(error)
    return {"ok": False, "error_type": error_type, "error": str(message)}


def ubem_tool(*, read_only: bool = False, destructive: bool = False, idempotent: bool = False,
              open_world: bool = False):
    """Register a UBEM-MCP tool with annotations and the response envelope."""
    def decorate(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                out = _ok(await fn(*args, **kwargs))
            except (KeyError, FileNotFoundError) as e:
                out = _fail("not_found", e)
            except (ValueError, TypeError) as e:
                out = _fail("invalid_input", e)
            except (ConnectionError, TimeoutError) as e:
                out = _fail("unavailable", e)
            except Exception as e:  # noqa: BLE001 - tool boundary: always answer the client
                logger.exception(f"Error in {fn.__name__}")
                out = _fail("internal", e)
            # Round-trip through JSON so numpy numbers, paths and dates become plain values.
            return ToolResult(**json.loads(json.dumps(out, default=str)))

        wrapper.__signature__ = inspect.signature(fn).replace(return_annotation=ToolResult)

        annotations = ToolAnnotations(readOnlyHint=read_only, destructiveHint=destructive,
                                      idempotentHint=idempotent or read_only, openWorldHint=open_world)
        return mcp.tool(annotations=annotations, structured_output=True)(wrapper)
    return decorate


def _submit_model_set(stock_id: str, model_set_id: str, max_workers: Optional[int] = None,
                      metadata: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Run every model of a model set as one background batch, linked to the
    stock and model set so aggregate_stock_results can find it."""
    manifest = stock_manager.model_manifest(stock_id, model_set_id)
    models = [{"idf_path": m["idf_path"], "label": m["building_id"]} for m in manifest["models"]]
    if not models:
        return None
    if not manifest.get("weather_file"):
        raise ValueError(f"Model set {model_set_id!r} has no weather file. Give weather_file when generating, "
                         f"or call get_site_weather first.")
    model_dir = stock_manager.stock_dir(stock_id) / "models" / model_set_id
    return batch_manager.submit_batch(
        models=models, weather_file=manifest["weather_file"], max_workers=max_workers,
        output_directory=str(model_dir / "runs"),
        metadata={"stock_id": stock_id, "model_set_id": model_set_id, **(metadata or {})},
    )


MAX_WAIT_SECONDS = 50  # below common MCP client tool-call timeouts


def _batch_next_steps(batch_id: str) -> List[str]:
    return [f"wait with get_batch_status(batch_id={batch_id!r}, wait_seconds={MAX_WAIT_SECONDS}), repeating "
            f"until is_complete is true",
            f"then aggregate_stock_results(batch_id={batch_id!r})"]


# ============================================================================
# Batch Simulation (portfolio / multi-model) — new in UBEM-MCP
# ============================================================================

@ubem_tool(read_only=True)
async def discover_idf_files(directory: str, pattern: str = "*.idf") -> Dict[str, Any]:
    """
    Glob a directory for IDF files to use as batch simulation inputs

    Args:
        directory: Directory to search (absolute, or relative to the server workspace)
        pattern: Glob pattern to match (default: "*.idf")

    Returns:
        JSON string listing matched IDF files with path, size, and basic file info.
        Feed the "idf_path" values straight into run_batch_simulation's `models` list.
    """
    logger.info(f"Discovering IDF files in: {directory} (pattern={pattern})")
    files = _discover_idf_files(config, directory, pattern)
    return files


@ubem_tool()
async def run_batch_simulation(
    models: List[Any],
    weather_file: Optional[str] = None,
    max_workers: Optional[int] = None,
    continue_on_error: bool = True,
    output_directory: Optional[str] = None,
    annual: bool = True,
    design_day: bool = False,
    readvars: bool = True,
    expandobjects: bool = True,
) -> Dict[str, Any]:
    """
    Run EnergyPlus simulations for a batch (portfolio) of models in parallel background workers

    This is the key UBEM-MCP addition over single-model EnergyPlus-MCP: it runs many
    IDFs concurrently (one worker process per model, each calling the EnergyPlus
    command line) and returns immediately with a batch_id. Any complete IDF works,
    including models built outside UBEM-MCP.
    Poll get_batch_status / get_batch_results to track progress and retrieve results —
    a portfolio of many buildings can take far longer than a single tool call should block for.

    Args:
        models: List of model specs. Each item can be:
                - A plain IDF path string: "sample_files/1ZoneUncontrolled.idf"
                - A dict with overrides: {"idf_path": "...", "weather_file": "...", "label": "..."}
        weather_file: Default weather file for models that don't specify their own (optional)
        max_workers: Max concurrent simulation processes (default: server config, usually CPU count)
        continue_on_error: If True (default), one model's failure doesn't stop the rest.
                           If False, remaining not-yet-finished models are cancelled on first failure.
        output_directory: Root directory for this batch's outputs (default: auto-generated under outputs/batches/)
        annual: Run annual simulation (default: True)
        design_day: Run design day only simulation (default: False)
        readvars: Run ReadVarsESO after each simulation (default: True)
        expandobjects: Run ExpandObjects prior to each simulation (default: True)

    Returns:
        JSON string with batch_id and initial status. Use batch_id with get_batch_status
        and get_batch_results.

    Examples:
        # Batch from explicit paths
        run_batch_simulation(["bldg_001.idf", "bldg_002.idf", "bldg_003.idf"],
                              weather_file="USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw")

        # Per-model overrides
        run_batch_simulation([
            {"idf_path": "bldg_001.idf", "label": "office_a"},
            {"idf_path": "bldg_002.idf", "weather_file": "chicago.epw", "label": "office_b"}
        ], continue_on_error=False)
    """
    logger.info(f"Submitting batch simulation: {len(models)} model(s)")
    result = batch_manager.submit_batch(
        models=models,
        weather_file=weather_file,
        max_workers=max_workers,
        continue_on_error=continue_on_error,
        output_directory=output_directory,
        annual=annual,
        design_day=design_day,
        readvars=readvars,
        expandobjects=expandobjects,
    )
    return {**result, "next_steps": [f"wait with get_batch_status(batch_id={result['batch_id']!r}, "
                                     f"wait_seconds={MAX_WAIT_SECONDS}) until is_complete, then get_batch_results"]}


@ubem_tool(read_only=True)
async def get_batch_status(batch_id: str, wait_seconds: int = 0) -> Dict[str, Any]:
    """
    Get the current status of a batch simulation job

    Args:
        batch_id: Batch ID returned by run_batch_simulation
        wait_seconds: Wait up to this many seconds (at most 50) for the batch to
                      finish before answering, instead of polling repeatedly

    Returns:
        JSON with overall status, per-status counts (only as many models are
        "running" as there are workers), the completion flag, progress (elapsed
        time, mean EnergyPlus run time, estimated time left) and, when models
        fail, a diagnosis: failures grouped by cause from the EnergyPlus .err
        files, with example buildings and a suggested fix. Poll this until
        "is_complete" is true, then call get_batch_results.
    """
    result = batch_manager.get_status(batch_id)
    deadline = asyncio.get_running_loop().time() + max(0, min(int(wait_seconds or 0), MAX_WAIT_SECONDS))
    while not result["is_complete"] and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(min(2.0, max(0.1, deadline - asyncio.get_running_loop().time())))
        result = batch_manager.get_status(batch_id)
    if not result["is_complete"]:
        result["next_steps"] = [f"call get_batch_status(batch_id={batch_id!r}, wait_seconds={MAX_WAIT_SECONDS}) "
                                f"again to keep waiting"]
    return result


@ubem_tool(read_only=True)
async def get_batch_results(batch_id: str, status: Optional[str] = None, limit: int = 50,
                            offset: int = 0) -> Dict[str, Any]:
    """
    Get detailed per-model results for a batch simulation job

    Args:
        batch_id: Batch ID returned by run_batch_simulation
        status: Only models with this status, e.g. "failed" or "success"
        limit: Models to return per page (up to 200)
        offset: First model to return; page with next_offset

    Returns:
        JSON with overall status and counts plus one page of per-model results
        (status, duration, output directory, error details). Safe to call before
        the batch finishes; check "is_complete".
    """
    result = batch_manager.get_results(batch_id)
    models = [m for m in result.pop("models") if status is None or m.get("status") == status]
    limit = max(1, min(limit, 200))
    page = models[offset:offset + limit]
    out = {**result, "num_models": len(models), "models": page}
    if offset + limit < len(models):
        out["next_offset"] = offset + limit
    return out


@ubem_tool(read_only=True)
async def list_batches(limit: int = 20) -> Dict[str, Any]:
    """
    List batch simulation jobs (in memory plus any persisted on disk), newest first

    Args:
        limit: Jobs to return (up to 200)

    Returns:
        JSON with the total number of jobs and a summary of the newest ones
    """
    jobs = batch_manager.list_batches()
    jobs = sorted(jobs, key=lambda j: str(j.get("created_at") or ""), reverse=True)
    return {"num_batches": len(jobs), "batches": jobs[:max(1, min(limit, 200))]}


@ubem_tool(destructive=True)
async def cancel_batch(batch_id: str) -> Dict[str, Any]:
    """
    Cancel a batch simulation job

    Args:
        batch_id: Batch ID returned by run_batch_simulation

    Returns:
        JSON string with updated batch status. Models not yet started are cancelled;
        this is best-effort for models already running (an in-flight EnergyPlus
        subprocess in a worker cannot be forcibly killed from here).
    """
    result = batch_manager.cancel_batch(batch_id)
    return result


# ============================================================================
# Urban Building Stock (GIS footprints -> normalized buildings) — new in UBEM-MCP
# ============================================================================

def _stock_loader_options(**opts) -> Dict[str, Any]:
    return {k: v for k, v in opts.items() if v is not None}


@ubem_tool(read_only=True)
async def list_archetypes() -> Dict[str, Any]:
    """
    List the building archetypes (use types) and vintage bins UBEM-MCP models

    Archetypes follow the U.S. DOE commercial prototype buildings plus the DOE
    single-family residential prototype. Use these keys for `default_archetype`
    or in an explicit use column of your GIS data.

    Returns:
        JSON with archetype keys, labels, sector, default floor count and
        floor-to-floor height, and the vintage bins (by year built).
    """
    data = {
        "archetypes": [
            {"key": a.key, "label": a.label, "sector": a.sector,
             "default_floors": a.default_floors, "floor_to_floor_m": a.floor_to_floor_m}
            for a in ARCHETYPES.values()
        ],
        "vintages": {"pre1980": "built before 1980", "1980_2004": "built 1980-2003",
                     "post2004": "built 2004 or later"},
    }
    return data


@ubem_tool()
async def load_building_stock(
    geojson_path: str,
    stock_id: Optional[str] = None,
    source_crs: Optional[str] = None,
    height_field: Optional[str] = None,
    floors_field: Optional[str] = None,
    use_field: Optional[str] = None,
    year_field: Optional[str] = None,
    id_field: Optional[str] = None,
    height_unit: str = "m",
    default_archetype: str = "single_family",
    default_year: int = 1980,
    min_footprint_m2: Optional[float] = None,
    license: Optional[str] = None,
    attribution: Optional[str] = None,
    keep_personal_fields: bool = False,
    mode: str = "model",
) -> Dict[str, Any]:
    """
    Load building footprints (or any building inventory) from a GeoJSON file as a building stock

    Each footprint is projected to a local metric frame and given a normalized
    archetype, year built / vintage, floor count and height. Attribute columns are
    auto-detected (e.g. height, building:levels, building/amenity, start_date,
    year_built); pass the *_field arguments to name them explicitly. Values that
    had to be guessed are listed per building in `inferred_fields`, and the
    summary reports the inferred share per attribute. Sheds, garages and
    footprints under `min_footprint_m2` are excluded with a reason.

    The normalized stock is saved as <stock_directory>/buildings.geojson. To
    correct attributes, edit that file and load it again under a new stock_id.

    Args:
        geojson_path: GeoJSON FeatureCollection of building polygons
        stock_id: Optional name for the stock (letters, digits, _ or -); auto-generated if omitted
        source_crs: CRS of the coordinates if not WGS84 lon/lat, e.g. "EPSG:2868"
        height_field / floors_field / use_field / year_field / id_field: column names to use
        height_unit: "m" or "ft" for numeric heights without a unit
        default_archetype: Archetype for buildings with no recognizable use (see list_archetypes)
        default_year: Year built for buildings with no construction year
        min_footprint_m2: Smaller footprints are excluded (default 20 m2 in model mode, 0 in data mode)
        mode: "model" (default) prepares buildings for simulation: unknown values are filled with
              defaults (listed in inferred_fields) and sheds and tiny footprints are excluded.
              "data" keeps the inventory as the data describe it: every structure, full geometry,
              raw use, unknown values left empty. Use it for data work (query_stock, export_stock);
              assign_archetypes derives a modeling stock from it later.
        license: Licence of the input data, e.g. "ODbL-1.0", "CC-BY-4.0" (recorded with the
                 stock and its results; a warning is returned while it is unknown)
        attribution: Credit line the data's licence requires, e.g. "City of Tucson Open Data"
        keep_personal_fields: Keep columns about people (owner, mailing address, phone, e-mail).
                              They are dropped by default because modeling never needs them.

    Returns:
        JSON summary: stock_id, building counts by archetype and vintage, floor
        areas, inferred-attribute shares, exclusions by reason, data sources and
        licences, required attribution, and warnings (unknown licence, dropped
        personal columns).
    """
    opts = _stock_loader_options(
        source_crs=source_crs, height_field=height_field, floors_field=floors_field,
        use_field=use_field, year_field=year_field, id_field=id_field, height_unit=height_unit,
        default_archetype=default_archetype, default_year=default_year,
        min_footprint_m2=min_footprint_m2,
        keep_personal_fields=keep_personal_fields or None,
        mode=mode if mode != "model" else None,
    )
    result = stock_manager.load_from_file(geojson_path, stock_id=stock_id, license=license,
                                          attribution=attribution, **opts)
    return result


@ubem_tool(open_world=True)
async def fetch_osm_buildings(
    bbox: Optional[List[float]] = None,
    center_lat: Optional[float] = None,
    center_lon: Optional[float] = None,
    radius_m: Optional[float] = None,
    stock_id: Optional[str] = None,
    default_archetype: str = "single_family",
    default_year: int = 1980,
    min_footprint_m2: Optional[float] = None,
    mode: str = "model",
) -> Dict[str, Any]:
    """
    Download building footprints from OpenStreetMap and load them as a building stock

    Queries the Overpass API for every building way/multipolygon in the area,
    then normalizes it exactly like load_building_stock (OSM tags such as
    building, amenity, shop, building:levels, height and start_date are used).
    The raw OSM response and OSM data timestamp are saved with the stock so the
    study can be reproduced. Areas are limited to 25 km2 per request.

    Args:
        bbox: [south, west, north, east] in decimal degrees, or use center + radius
        center_lat, center_lon: Center of the area (decimal degrees)
        radius_m: Half-width of a square area around the center, in metres
        stock_id: Optional name for the stock
        default_archetype: Archetype for buildings tagged only building=yes (see list_archetypes)
        default_year: Year built when OSM has no start_date (most buildings)
        min_footprint_m2: Smaller footprints are excluded (default 20 m2 in model mode, 0 in data mode)
        mode: "model" (default) prepares buildings for simulation: unknown values are filled with
              defaults (listed in inferred_fields) and sheds and tiny footprints are excluded.
              "data" keeps the inventory as the data describe it: every structure, full geometry,
              raw use, unknown values left empty. Use it for data work (query_stock, export_stock);
              assign_archetypes derives a modeling stock from it later.

    Returns:
        JSON summary of the stock, as for load_building_stock.
    """
    opts = _stock_loader_options(
        default_archetype=default_archetype, default_year=default_year,
        min_footprint_m2=min_footprint_m2, mode=mode if mode != "model" else None,
    )
    result = await asyncio.to_thread(
        stock_manager.fetch_osm, bbox=bbox, center_lat=center_lat, center_lon=center_lon,
        radius_m=radius_m, stock_id=stock_id, **opts,
    )
    return result


@ubem_tool()
async def enrich_building_stock(
    stock_id: str,
    layer_path: str,
    field_map: Dict[str, str],
    use_value_map: Optional[Dict[str, str]] = None,
    layer_crs: Optional[str] = None,
    overwrite: bool = False,
    height_unit: str = "m",
    min_overlap: float = 0.5,
    new_stock_id: Optional[str] = None,
    layer_license: Optional[str] = None,
    layer_attribution: Optional[str] = None,
    keep_columns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Fill a building stock's guessed attributes from another GIS layer (e.g. assessor parcels)

    Footprint sources such as OpenStreetMap rarely carry year built or use. This
    joins a GeoJSON layer onto the footprints: each building takes the attributes
    of the layer polygon containing its centroid (or covering most of its
    footprint), or of a point feature inside it. By default only attributes that
    were inferred are filled, and a parcel's use is not given to structures the
    footprints mark as garages, sheds or carports. The result is saved as a new
    stock; the original is unchanged.

    Args:
        stock_id: Stock to enrich
        layer_path: GeoJSON FeatureCollection (polygons such as parcels, or points)
        field_map: Target attribute -> layer column, using any of
                   "year_built", "use", "num_floors", "height_m",
                   e.g. {"year_built": "YEAR_BUILT", "use": "LAND_USE"}
        use_value_map: Translate layer use values (e.g. land-use codes) to archetype
                       keys or use words, e.g. {"0131": "single_family", "0310": "office"}
        layer_crs: CRS of the layer if not WGS84 lon/lat, e.g. "EPSG:2868"
        overwrite: Also replace values that came from the footprint source
        height_unit: "m" or "ft" for the layer's height column
        min_overlap: Minimum footprint share a polygon must cover when the centroid test fails
        new_stock_id: Optional name for the enriched stock
        layer_license: Licence of the layer, e.g. "public domain" or "Pima County terms of use";
                       mixing it with ODbL footprints returns a warning when it may conflict
        layer_attribution: Credit line the layer's licence requires
        keep_columns: Layer columns to copy onto matched buildings (e.g. ["PARCEL", "PARCEL_USE"])
                      so query_stock and export_stock can use them; never copy personal columns

    Returns:
        JSON with match counts, filled attribute counts, unrecognized use values,
        inferred shares before/after, and the new stock's summary.
    """
    result = stock_manager.enrich(
        stock_id, layer_path, field_map, new_stock_id=new_stock_id,
        use_value_map=use_value_map, layer_crs=layer_crs, overwrite=overwrite,
        height_unit=height_unit, min_overlap=min_overlap,
        layer_license=layer_license, layer_attribution=layer_attribution, keep_columns=keep_columns,
    )
    return result


@ubem_tool(read_only=True)
async def get_building_stock(
    stock_id: str,
    archetypes: Optional[List[str]] = None,
    vintages: Optional[List[str]] = None,
    min_gfa_m2: Optional[float] = None,
    max_gfa_m2: Optional[float] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """
    Summarize a building stock and list its buildings, optionally filtered

    Args:
        stock_id: ID from load_building_stock or fetch_osm_buildings
        archetypes: Only buildings with these archetype keys
        vintages: Only these vintages ("pre1980", "1980_2004", "post2004")
        min_gfa_m2 / max_gfa_m2: Gross floor area range
        limit: Max buildings to list (the summary always covers the whole stock)

    Returns:
        JSON with the stock summary, the number of matching buildings, and up to
        `limit` matching buildings with their normalized attributes.
    """
    stock = stock_manager.get(stock_id)
    matches = stock.filter(archetypes=archetypes, vintages=vintages,
                           min_gfa_m2=min_gfa_m2, max_gfa_m2=max_gfa_m2)
    buildings = []
    for b in matches[:max(0, limit)]:
        props = b.to_properties()
        props.pop("source_properties", None)
        buildings.append(props)
    data = {
        "stock_id": stock_id,
        "stock_directory": str(stock_manager.stock_dir(stock_id)),
        "summary": stock.summary(),
        "sources": [{"name": src["name"], "license": src["license"]}
                    for src in stock_manager.get_meta(stock_id).get("sources") or []],
        "attribution": provenance.attribution_text(stock_manager.get_meta(stock_id).get("sources") or []),
        "num_matching": len(matches),
        "buildings": buildings,
    }
    return data


@ubem_tool(read_only=True)
async def query_stock(
    stock_id: str,
    filters: Optional[Dict[str, Any]] = None,
    group_by: Optional[List[str]] = None,
    metrics: Optional[List[str]] = None,
    max_groups: int = 20,
    sample: int = 5,
    offset: int = 0,
    columns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Filter, group and summarize the buildings of a stock, with no model or simulation

    Works on data-mode and model-mode stocks. Any attribute can be used: the
    standard fields (archetype, use, year_built, vintage, num_floors, height_m,
    footprint_area_m2, gross_floor_area_m2), `decade` (of year built), or any
    column of the source data, such as a parcel's land-use code. Results are
    summaries: counts, totals and the share of buildings whose attributes are
    known (and, in model mode, guessed) per group, plus a few sample rows. Use
    export_stock for the full table.

    Args:
        stock_id: Stock to query
        filters: Attribute -> condition, all of which must hold. A condition is a value,
                 a list of values (any of), {"min": x, "max": y} or {"is_null": true|false}.
                 Example: {"archetype": "single_family", "year_built": {"max": 1979}}
        group_by: Attributes to group by, e.g. ["vintage"] or ["use", "decade"]
        metrics: "count" or "<sum|mean|median|min|max>:<attribute>";
                 default ["sum:footprint_area_m2", "sum:gross_floor_area_m2", "median:year_built"]
        max_groups: Largest groups to list (the rest are pooled under other_groups)
        sample: Sample rows to return (up to 50); page with offset / next_offset
        offset: First sample row to return
        columns: Extra source-data columns to include in the sample rows

    Returns:
        JSON with the number of matching buildings, overall metrics and data completeness,
        the groups (largest first), sample rows and next_offset when more rows exist.
    """
    result = stock_manager.query_stock(stock_id, filters=filters, group_by=group_by, metrics=metrics,
                                       max_groups=max_groups, sample=sample, offset=offset,
                                       columns=columns)
    return result


@ubem_tool(idempotent=True)
async def export_stock(
    stock_id: str,
    format: str = "csv",
    filters: Optional[Dict[str, Any]] = None,
    columns: Optional[List[str]] = None,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Export a stock's buildings to a CSV or GeoJSON file, with its data sources and licences

    CSV has one row per building with its centroid longitude and latitude;
    GeoJSON keeps the full footprint geometry. Next to the file, a
    <name>.metadata.json records the filters, the data sources, their licences
    and the attribution to show wherever the data are used.

    Args:
        stock_id: Stock to export
        format: "csv" or "geojson"
        filters: Same conditions as query_stock, to export part of the stock
        columns: Extra source-data columns to include, e.g. ["PARCEL", "LAND_USE"]
        name: File name without extension (letters, digits, _ or -); default stock id + time

    Returns:
        JSON with the file path, row count, columns, metadata path, attribution and
        licence warnings.
    """
    result = stock_manager.export_stock(stock_id, fmt=format, filters=filters, columns=columns, name=name)
    return result


@ubem_tool()
async def assign_archetypes(
    stock_id: str,
    new_stock_id: Optional[str] = None,
    default_archetype: str = "single_family",
    default_year: int = 1980,
    min_footprint_m2: float = 20.0,
) -> Dict[str, Any]:
    """
    Turn a data-mode stock into a modeling stock ready for generate_building_models

    Classifies every building into a DOE archetype from its use, fills unknown
    year built, floor count and height with defaults (each recorded in the
    building's inferred_fields), and excludes unconditioned structures (sheds,
    garages, parking) and footprints under min_footprint_m2. The result is a new
    stock; the data-mode stock is unchanged.

    Args:
        stock_id: Data-mode stock (from load_building_stock or fetch_osm_buildings with mode="data")
        new_stock_id: Optional name for the modeling stock
        default_archetype: Archetype for buildings with no recognizable use (see list_archetypes)
        default_year: Year built for buildings with no construction year
        min_footprint_m2: Smaller footprints are excluded

    Returns:
        JSON summary of the new stock, as for load_building_stock.
    """
    result = stock_manager.assign_archetypes(
        stock_id, new_stock_id=new_stock_id, default_archetype=default_archetype,
        default_year=default_year, min_footprint_m2=min_footprint_m2)
    return result


@ubem_tool(read_only=True)
async def list_building_stocks() -> Dict[str, Any]:
    """
    List building stocks loaded so far (persisted across server restarts)

    Returns:
        JSON list of stocks with stock_id, creation time, source and building count.
    """
    return stock_manager.list_stocks()


# ============================================================================
# Archetype model generation and stock simulation — new in UBEM-MCP
# ============================================================================

@ubem_tool(read_only=True, open_world=True)
async def find_weather_stations(
    stock_id: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    n: int = 5,
) -> Dict[str, Any]:
    """
    List the weather stations nearest to a building stock or a point

    Stations come from the Climate.OneBuilding.Org TMYx index (about 17,000
    stations worldwide), downloaded and cached on first use.

    Args:
        stock_id: Use the center of this stock as the site
        latitude / longitude: Or give the site directly (decimal degrees)
        n: Number of stations to return (default 5)

    Returns:
        JSON with each station's id, name, distance, elevation and available
        typical-year datasets (e.g. "TMYx.2009-2023"); pass a station id to
        get_site_weather to use a station other than the nearest one.
    """
    result = await asyncio.to_thread(stock_manager.find_weather_stations, latitude, longitude, stock_id, n)
    return result


@ubem_tool(open_world=True)
async def get_site_weather(
    stock_id: str,
    station: Optional[str] = None,
    dataset: Optional[str] = None,
    weather_file: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Set the weather file and ASHRAE climate zone of a building stock

    By default downloads the typical-year weather (EPW, STAT, DDY) of the
    station nearest to the stock from Climate.OneBuilding.Org. The ASHRAE 169
    climate zone is read from the station's .stat file, or estimated from the
    EPW if there is none. Both are saved with the stock and used by
    generate_building_models and simulate_building_stock when they are not
    given explicitly.

    Args:
        stock_id: Stock to set the weather for
        station: Station id from find_weather_stations (default: nearest)
        dataset: Typical-year dataset, e.g. "TMYx.2009-2023" (default: most recent 15-year TMYx)
        weather_file: Use this local EPW instead of downloading one

    Returns:
        JSON with the station and its distance, the EPW path, the ASHRAE climate
        zone, the archetype-library zone used for models, and any notes or warnings.
    """
    result = await asyncio.to_thread(stock_manager.resolve_weather, stock_id, station=station,
                                     dataset=dataset, weather_file=weather_file)
    return result


@ubem_tool()
async def generate_building_models(
    stock_id: str,
    climate_zone: Optional[str] = None,
    weather_file: Optional[str] = None,
    hvac: str = "template",
    archetypes: Optional[List[str]] = None,
    vintages: Optional[List[str]] = None,
    building_ids: Optional[List[str]] = None,
    max_buildings: Optional[int] = None,
    model_set_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate an EnergyPlus archetype model (IDF) for each building of a stock

    Each footprint is extruded into floors (buildings over three floors are
    modeled as ground, middle and top floors with a multiplier), split into
    core/perimeter zones when large enough, given windows, party walls with
    adjacent buildings and shading from nearby buildings, and filled with the
    constructions, internal loads, schedules and HVAC of its archetype, vintage
    and climate zone (DOE Commercial Reference Buildings; DOE/PNNL IECC 2006
    single-family prototype). Every assumption is listed per building in the
    model set's manifest.json.

    Args:
        stock_id: Stock from load_building_stock / fetch_osm_buildings / enrich_building_stock
        climate_zone: ASHRAE climate zone of the site, e.g. "2B" for Tucson or Phoenix
                      (1A, 2A, 2B, 3A, 3B, 3B-CA, 3C, 4A, 4B, 4C, 5A, 5B, 6A, 6B, 7, 8).
                      Default: the stock's zone from get_site_weather, resolved
                      automatically from the nearest weather station if not set yet
        weather_file: EPW used later for simulation; sets the model's site location.
                      Default: the stock's weather from get_site_weather
        hvac: "template" (reference-building HVAC systems, autosized) or "ideal"
              (ideal loads; faster, reports loads rather than equipment energy)
        archetypes / vintages / building_ids: Only generate models for these buildings
        max_buildings: Cap on the number of models (useful for a quick test)
        model_set_id: Name for this set of models; defaults to a timestamp

    Returns:
        JSON with the model set id and directory, counts by HVAC system and zoning,
        and any buildings that failed.
    """
    result = await asyncio.to_thread(
        stock_manager.generate_models, stock_id, climate_zone, weather_file=weather_file, hvac=hvac,
        archetypes=archetypes, vintages=vintages, building_ids=building_ids,
        max_buildings=max_buildings, model_set_id=model_set_id,
    )
    return result


@ubem_tool()
async def simulate_building_stock(
    stock_id: str,
    climate_zone: Optional[str] = None,
    weather_file: Optional[str] = None,
    hvac: str = "template",
    archetypes: Optional[List[str]] = None,
    vintages: Optional[List[str]] = None,
    building_ids: Optional[List[str]] = None,
    max_buildings: Optional[int] = None,
    max_workers: Optional[int] = None,
    model_set_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate archetype models for a building stock and simulate them all as a background batch

    Combines generate_building_models and run_batch_simulation: every selected
    building gets a model, and the models run in parallel worker processes.
    Returns immediately with a batch_id; wait with get_batch_status(wait_seconds=50) until complete.

    Args:
        stock_id: Stock to simulate
        climate_zone: ASHRAE climate zone of the site, e.g. "2B" (default: from the site weather)
        weather_file: EPW weather file for the site (default: the stock's weather from
                      get_site_weather, downloaded from the nearest station if not set yet)
        hvac: "template" (default) or "ideal"
        archetypes / vintages / building_ids: Only simulate these buildings
        max_buildings: Cap on the number of buildings (useful for a quick test)
        max_workers: Parallel EnergyPlus processes (default: server setting)
        model_set_id: Name for the generated model set

    Returns:
        JSON with the batch_id, the model set, and generation summary.
    """
    gen = await asyncio.to_thread(
        stock_manager.generate_models, stock_id, climate_zone, weather_file=weather_file, hvac=hvac,
        archetypes=archetypes, vintages=vintages, building_ids=building_ids,
        max_buildings=max_buildings, model_set_id=model_set_id,
    )
    if not gen["weather_file"]:
        raise ValueError("No weather file. Give weather_file, call get_site_weather, or omit climate_zone "
                         "to resolve both from the nearest station.")
    batch = _submit_model_set(stock_id, gen["model_set_id"], max_workers)
    if batch is None:
        return {"batch": None, "models": gen, "warnings": ["no models were generated; see models.failures"]}
    return {"batch": batch, "models": gen, "next_steps": _batch_next_steps(batch["batch_id"])}


@ubem_tool()
async def simulate_model_set(stock_id: str, model_set_id: str, max_workers: Optional[int] = None) -> Dict[str, Any]:
    """
    Simulate an existing model set as a background batch

    Use for a model set that was generated without simulating, one that
    includes your own models (set_building_models), or one regenerated from
    a project record. Results go to the model set's runs/ folder, so
    aggregate_stock_results(batch_id=...) works when the batch completes.

    Args:
        stock_id: Stock the model set belongs to
        model_set_id: Model set to simulate
        max_workers: Parallel EnergyPlus processes (default: server setting)

    Returns:
        JSON with the batch_id and next steps.
    """
    batch = _submit_model_set(stock_id, model_set_id, max_workers)
    if batch is None:
        raise ValueError(f"Model set {model_set_id!r} has no models")
    return {"batch": batch, "next_steps": _batch_next_steps(batch["batch_id"])}


@ubem_tool()
async def set_building_models(
    stock_id: str,
    model_set_id: str,
    building_models: Dict[str, str],
    new_model_set_id: Optional[str] = None,
    license: Optional[str] = None,
    attribution: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Use your own EnergyPlus models for some buildings in a model set

    Creates a new model set in which the given buildings use your models
    (from OpenStudio, a consultant, a calibration study or another tool) and
    every other building keeps its archetype model; the original set is not
    changed. Your models are copied as they are; only the outputs district
    aggregation needs (SQLite, annual summary, hourly electricity and gas
    meters) are added if missing, and each addition is reported. Buildings
    that were not in the set are added. The batch runs every model with the
    model set's weather file. Retrofit scenarios generated from the new set
    leave your models unchanged.

    Args:
        stock_id: Stock the model set belongs to
        model_set_id: Model set to start from (not a retrofit scenario)
        building_models: building_id -> path of an EnergyPlus .idf file,
                         e.g. {"way_123": "models/library.idf"}
        new_model_set_id: Name for the new set (default: <model_set_id>__own)
        license: Licence of the supplied models, e.g. "CC-BY-4.0" or "proprietary"
        attribution: Credit to show with results that use them

    Returns:
        JSON with the new model set, which buildings use supplied models, the
        outputs added to each, and warnings (e.g. EnergyPlus version mismatch).
    """
    result = await asyncio.to_thread(stock_manager.set_building_models, stock_id, model_set_id, building_models,
                                     new_model_set_id=new_model_set_id, license=license, attribution=attribution)
    result["next_steps"] = [f"simulate_model_set(stock_id={stock_id!r}, model_set_id={result['model_set_id']!r})"]
    return result


@ubem_tool(idempotent=True)
async def aggregate_stock_results(
    batch_id: Optional[str] = None,
    stock_id: Optional[str] = None,
    model_set_id: Optional[str] = None,
    emission_factors: Optional[Dict[str, float]] = None,
    make_map: bool = True,
) -> Dict[str, Any]:
    """
    Aggregate the simulation results of a building stock into district results

    Reads every building's EnergyPlus output and writes, under the model set's
    results/ folder: buildings.csv (annual energy by fuel and end use, site
    EUI, peak demand per building), results.geojson (footprints with results,
    for GIS), district_hourly.csv (hourly district demand by fuel),
    summary.json (totals, breakdowns by archetype and vintage, peaks, load
    factors, coverage) and map.html (interactive EUI map).

    Args:
        batch_id: Batch from simulate_building_stock (preferred; it names the stock and model set)
        stock_id / model_set_id: Or name the model set directly; its runs/ folder is scanned
        emission_factors: Optional kg CO2e per kWh by fuel, e.g. {"electricity": 0.4, "natural_gas": 0.18}
        make_map: Write map.html (default true)

    Returns:
        JSON summary: coverage (buildings with/without results), district totals
        and EUI, peak demand and when it occurs, breakdowns, and output file paths.
    """
    outputs = None
    results_id = None
    if batch_id:
        status = batch_manager.get_status(batch_id)
        if not status["is_complete"]:
            return {"is_complete": False, "batch_id": batch_id, "counts": status["counts"],
                    "next_steps": [f"call aggregate_stock_results(batch_id={batch_id!r}) again when "
                                   f"get_batch_status reports is_complete"]}
        meta = status.get("metadata") or {}
        stock_id = stock_id or meta.get("stock_id")
        model_set_id = model_set_id or meta.get("model_set_id")
        if not stock_id or not model_set_id:
            raise ValueError(f"Batch {batch_id} was not created by simulate_building_stock; "
                             "pass stock_id and model_set_id as well.")
        outputs = {m["label"]: m for m in batch_manager.get_results(batch_id)["models"]}
        results_id = batch_id
    if not stock_id or not model_set_id:
        raise ValueError("Provide batch_id, or stock_id and model_set_id.")
    summary = await asyncio.to_thread(
        stock_manager.aggregate_results, stock_id, model_set_id, outputs=outputs, results_id=results_id,
        emission_factors=emission_factors, make_map=make_map,
    )
    return summary


@ubem_tool(read_only=True)
async def list_retrofit_measures() -> Dict[str, Any]:
    """
    List the retrofit measures available for retrofit scenarios

    Returns:
        JSON list of measure types with their parameters and descriptions.
    """
    from ubem_mcp_server.urban.retrofit import list_measures
    return list_measures()


@ubem_tool()
async def simulate_retrofit_scenario(
    stock_id: str,
    baseline_model_set_id: str,
    scenario: Dict[str, Any],
    max_workers: Optional[int] = None,
    model_set_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Apply a retrofit scenario to a simulated stock and simulate it as a background batch

    The scenario's models use the baseline model set's buildings, climate,
    weather and HVAC option, so the only differences from the baseline are
    the measures. Each model's manifest entry lists the measures it received.

    Args:
        stock_id: The stock
        baseline_model_set_id: The baseline model set (from simulate_building_stock)
        scenario: Scenario specification, e.g.
            {"name": "led_and_heat_pumps",
             "measures": [{"type": "lighting", "target_w_m2": 6.5},
                          {"type": "heat_pump", "heating_cop": 3.3, "archetypes": ["single_family"]}],
             "vintages": ["pre1980"],          # optional: buildings targeted
             "adoption_rate": 0.5, "seed": 1}  # optional: share of targeted buildings retrofitted
            Use list_retrofit_measures for measure types and parameters.
        max_workers: Parallel EnergyPlus processes (default: server setting)
        model_set_id: Name for the scenario's model set (default: <baseline>__<scenario name>)

    Returns:
        JSON with the batch_id, the scenario model set and adoption counts. When the
        batch completes, run aggregate_stock_results on it and then compare_retrofit_scenario.
    """
    gen = await asyncio.to_thread(stock_manager.generate_scenario, stock_id, baseline_model_set_id,
                                  scenario, model_set_id=model_set_id)
    if not gen["weather_file"]:
        raise ValueError("The baseline model set has no weather file.")
    manifest = stock_manager.model_manifest(stock_id, gen["model_set_id"])
    models = [{"idf_path": m["idf_path"], "label": m["building_id"]} for m in manifest["models"]]
    if not models:
        return {"batch": None, "models": gen, "warnings": ["no scenario models were generated"]}
    batch = batch_manager.submit_batch(
        models=models, weather_file=gen["weather_file"], max_workers=max_workers,
        output_directory=str(Path(gen["model_directory"]) / "runs"),
        metadata={"stock_id": stock_id, "model_set_id": gen["model_set_id"],
                  "scenario": gen["scenario"]["name"], "baseline_model_set_id": baseline_model_set_id},
    )
    return {"batch": batch, "models": gen,
            "next_steps": _batch_next_steps(batch["batch_id"]) + [
                f"then compare_retrofit_scenario(stock_id={stock_id!r}, "
                f"scenario_model_set_id={gen['model_set_id']!r})"]}


@ubem_tool(idempotent=True)
async def compare_retrofit_scenario(
    stock_id: str,
    scenario_model_set_id: str,
    baseline_results_id: Optional[str] = None,
    scenario_results_id: Optional[str] = None,
    emission_factors: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Compare a retrofit scenario's results with its baseline, building by building

    Both the baseline and the scenario must have been aggregated with
    aggregate_stock_results. Only buildings with results in both are compared.

    Args:
        stock_id: The stock
        scenario_model_set_id: The scenario's model set
        baseline_results_id / scenario_results_id: Aggregated results to use (the
            batch_id they were aggregated from); default: the most recent of each
        emission_factors: Optional kg CO2e per kWh by fuel, e.g. {"electricity": 0.4, "natural_gas": 0.18}

    Returns:
        JSON with district savings (total and by fuel, including fuel switching),
        peak demand change, avoided emissions, savings by archetype, the savings
        distribution, retrofitted buildings whose use went up, and the path of a
        per-building comparison CSV.
    """
    result = await asyncio.to_thread(
        stock_manager.compare_scenario, stock_id, scenario_model_set_id,
        baseline_results_id=baseline_results_id, scenario_results_id=scenario_results_id,
        emission_factors=emission_factors)
    return result


# ============================================================================
# ComStock / ResStock reference data — new in UBEM-MCP
# ============================================================================

@ubem_tool(open_world=True, idempotent=True)
async def fetch_comstock_resstock(
    dataset: str,
    state: str,
    county: Optional[str] = None,
    upgrade: int = 0,
    release: Optional[str] = None,
    dataset_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Download NREL ComStock (commercial) or ResStock (residential) annual results for a county or state

    Fetches the sample of simulated buildings, with their characteristics,
    annual energy by fuel and weights (how many real buildings each sample
    stands for), from the Open Energy Data Initiative, and keeps a compact
    copy for query_comstock_resstock. Use it to characterise a region's stock
    without modeling, to see what an upgrade saves, or to benchmark your own
    models. Data are CC BY 4.0; the attribution is returned.

    Args:
        dataset: "comstock" or "resstock"
        state: Two-letter state code, e.g. "AZ"
        county: 5-digit county FIPS code, e.g. "04019" for Pima County AZ (required
                for ComStock; for ResStock it narrows the state file to the county)
        upgrade: 0 for the baseline, or an upgrade number (listed in upgrades_available)
        release: Data release (default: the latest supported)
        dataset_id: Name for the downloaded data (default from dataset, area and upgrade)

    Returns:
        JSON with the dataset_id, samples, buildings represented, building types,
        available upgrades, the data dictionary link and attribution.
    """
    return await asyncio.to_thread(stock_manager.fetch_reference, dataset, state, county=county, upgrade=upgrade,
                                   release=release, dataset_id=dataset_id)


@ubem_tool(read_only=True)
async def query_comstock_resstock(
    dataset_id: str,
    group_by: Optional[List[str]] = None,
    filters: Optional[Dict[str, Any]] = None,
    baseline_dataset_id: Optional[str] = None,
    min_samples: int = 30,
    max_groups: int = 30,
) -> Dict[str, Any]:
    """
    Weighted summaries of ComStock/ResStock data: building counts, floor area, energy, EUI, upgrade savings

    Every number is weighted by how many real buildings each sample stands
    for, and every group reports its number of samples; groups with fewer
    than min_samples are flagged as uncertain. With baseline_dataset_id (the
    upgrade-0 data of the same area), returns the upgrade's savings for the
    buildings it applies to.

    Args:
        dataset_id: From fetch_comstock_resstock
        group_by: Fields to group by: building_type, vintage, climate_zone, county,
                  or any characteristic, e.g. "in.heating_fuel", "in.hvac_system_type"
        filters: Field -> value, list of values, or {"min": x, "max": y},
                 e.g. {"building_type": ["SmallOffice", "MediumOffice"]}
        baseline_dataset_id: Baseline data to compute an upgrade's savings against
        min_samples: Groups with fewer samples are flagged (default 30)
        max_groups: Largest groups to return

    Returns:
        JSON with overall and per-group weighted statistics (or savings), sample
        counts, warnings about small samples, and attribution.
    """
    return await asyncio.to_thread(stock_manager.query_reference, dataset_id, filters=filters, group_by=group_by,
                                   baseline_dataset_id=baseline_dataset_id, min_samples=min_samples,
                                   max_groups=max_groups)


@ubem_tool(read_only=True)
async def benchmark_with_comstock_resstock(stock_id: str, model_set_id: str, dataset_ids: List[str],
                                           results_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Compare your modeled EUI by archetype with ComStock/ResStock for the same area

    For each archetype in an aggregated model set, places the median modeled
    site EUI within the weighted EUI distribution (25th, 50th, 75th
    percentile) of the matching ComStock or ResStock building type. Both are
    models: this shows whether your models are in a plausible range for the
    area, not which is right; use compare_to_measured for measurements.

    Args:
        stock_id: Stock
        model_set_id: Aggregated model set (run aggregate_stock_results first)
        dataset_ids: Baseline (upgrade 0) datasets from fetch_comstock_resstock, e.g. one
                     ComStock and one ResStock dataset for the same county
        results_id: Aggregated results to use (default: the latest)

    Returns:
        JSON with, per archetype, the modeled median EUI, the reference distribution
        and sample count, and the modeled median's percentile in the reference.
    """
    return await asyncio.to_thread(stock_manager.benchmark_reference, stock_id, model_set_id, dataset_ids,
                                   results_id=results_id)


# ============================================================================
# Measured energy and validation — new in UBEM-MCP
# ============================================================================

@ubem_tool()
async def load_measured_energy(
    stock_id: str,
    csv_path: str,
    columns: Dict[str, str],
    units: Optional[Dict[str, str]] = None,
    id_column: Optional[str] = None,
    match_field: Optional[str] = None,
    lat_column: Optional[str] = None,
    lon_column: Optional[str] = None,
    month_column: Optional[str] = None,
    year: Optional[int] = None,
    measured_id: Optional[str] = None,
    license: Optional[str] = None,
    attribution: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Load measured energy use (bills, benchmarking disclosures, meters) and match it to buildings

    Reads a CSV with one row per meter or property, annual or monthly (with
    month_column). Rows are matched to buildings by an identifier shared with
    the stock (id_column in the table, match_field in the stock, e.g. a parcel
    or property id) and/or by location (lat_column, lon_column: the building
    containing or within 10 m of the point). A row that covers several
    buildings is later compared with the sum of their models. Only the mapped
    columns are read; other columns (names, addresses) are never stored.

    Args:
        stock_id: Stock the data describe
        csv_path: CSV file
        columns: Quantity -> column name. Quantities: electricity, natural_gas,
                 district_heating, district_cooling, total, floor_area, e.g.
                 {"electricity": "Electricity Use - Grid Purchase (kWh)",
                  "natural_gas": "Natural Gas Use (therms)", "floor_area": "Property GFA (ft2)"}
        units: Quantity -> unit, when the column name does not say it: kWh, MWh,
               kBtu, MMBtu, therm, MJ, GJ; m2, ft2
        id_column: Table column with the identifier
        match_field: Stock attribute holding the same identifier (default building_id)
        lat_column / lon_column: Table columns with each property's location
        month_column: Column with the month (1-12, "2023-07" or "Jul") for monthly data
        year: Year the data are from
        measured_id: Name for this dataset (default measured_<year>)
        license / attribution: Terms and credit of the data source

    Returns:
        JSON with how many rows matched buildings, meters covering several
        buildings, unmatched examples and warnings.
    """
    return await asyncio.to_thread(
        stock_manager.load_measured, stock_id, csv_path, columns, units=units, id_column=id_column,
        match_field=match_field, lat_column=lat_column, lon_column=lon_column, month_column=month_column,
        year=year, measured_id=measured_id, license=license, attribution=attribution)


@ubem_tool(idempotent=True)
async def compare_to_measured(stock_id: str, model_set_id: str, measured_id: str) -> Dict[str, Any]:
    """
    Compare simulated energy use with measured data, building by building and for the district

    Needs a simulated model set and measured data from load_measured_energy.
    Reports, for each quantity: district totals measured vs modeled; NMBE and
    CV(RMSE) across meters (ASHRAE Guideline 14 formulas); the spread of
    percent errors (median, share within 10/25/50%); median error by
    archetype; the largest differences; EUI where floor areas are known; and,
    for monthly data, Guideline 14 monthly NMBE and CV(RMSE) per meter. Also
    lists caveats (weather year, floor area mismatch, meters not compared).

    Args:
        stock_id: Stock
        model_set_id: Simulated model set to compare
        measured_id: Measured dataset

    Returns:
        JSON summary with the statistics above and the paths of per-meter
        tables (CSV and JSON).
    """
    return await asyncio.to_thread(stock_manager.compare_measured, stock_id, model_set_id, measured_id)


# ============================================================================
# Project records (reproducibility) — new in UBEM-MCP
# ============================================================================

@ubem_tool(idempotent=True)
async def export_project_record(stock_id: str) -> Dict[str, Any]:
    """
    Write a project record: everything needed to repeat a stock's analysis

    The record (<stock_directory>/project_record.json) lists every step from
    the original footprints to this stock (load or fetch, enrich, assign
    archetypes) with its arguments and the SHA-256 of each input file; the
    weather file and its hash; every model set with how it was generated and
    a hash of each model; the aggregated results; and the software versions
    (UBEM-MCP, EnergyPlus version and build, archetype library, Python
    packages). Share it with the results so others can check or repeat them.

    Args:
        stock_id: Stock whose analysis to record

    Returns:
        JSON with the record path, the steps, model sets and results it covers,
        software versions and attribution.
    """
    return await asyncio.to_thread(stock_manager.project_record, stock_id)


@ubem_tool()
async def rerun_project_record(
    record_path: str,
    id_suffix: str = "_rerun",
    simulate: bool = False,
    max_workers: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Repeat the analysis in a project record and check that it reproduces

    Rebuilds every stock in the record from the recorded input files (warning
    if a file has changed), under new ids (old id + id_suffix), regenerates
    every model set and compares each model with its recorded hash. Also warns
    when the EnergyPlus version or archetype library differ from the record.
    With simulate=true the regenerated model sets are also simulated as
    background batches; aggregate them and compare with recorded_results.

    Args:
        record_path: Path to project_record.json (or the stock folder holding it)
        id_suffix: Appended to every stock id for the rerun (letters, digits, _ or -)
        simulate: Also run the regenerated model sets
        max_workers: Parallel EnergyPlus processes when simulating

    Returns:
        JSON with the new stock ids, for each model set how many models are
        identical to the record, the recorded district results, batches if
        simulating, and warnings.
    """
    result = await asyncio.to_thread(stock_manager.rerun_record, record_path, id_suffix)
    if simulate:
        batches = []
        for ms in result["model_sets"]:
            try:
                batch = _submit_model_set(result["stock_id"], ms["model_set_id"], max_workers)
            except ValueError as e:
                batch = None
                result["warnings"].append(str(e))
            if batch is None:
                result["warnings"].append(f"model set {ms['model_set_id']!r} was not simulated")
                continue
            batches.append({"model_set_id": ms["model_set_id"], "batch_id": batch["batch_id"]})
        result["batches"] = batches
        result["next_steps"] = [s for b in batches for s in _batch_next_steps(b["batch_id"])]
    else:
        result["next_steps"] = ["call again with simulate=true to also run the models, or aggregate later"]
    return result


# ============================================================================
# Workflow guides and reference data for any MCP client — new in UBEM-MCP
# ============================================================================
# The same guides are offered as a tool (works in every client), as MCP prompts
# and as MCP resources. Reference catalogues are generated from the code, so they
# cannot drift from what the tools accept.

@ubem_tool(read_only=True)
async def get_workflow_guide(name: Optional[str] = None) -> Dict[str, Any]:
    """
    Step-by-step guides for common UBEM tasks; call with no name to list them

    Guides cover: modeling a district from footprints, retrofit and
    decarbonisation scenarios, working with building data without a model,
    and running your own EnergyPlus models as a batch. Each guide names the
    exact tools and arguments to use, in order.

    Args:
        name: Guide to return, e.g. "stock-to-district"; omit to list all guides

    Returns:
        JSON with the list of guides, or one guide's title, use case and Markdown text.
    """
    if name is None:
        return {"guides": workflow_guides.list_guides()}
    return workflow_guides.get_guide(name)


def _register_guide_prompt(guide: Dict[str, str]) -> None:
    def prompt() -> str:
        return workflow_guides.get_guide(guide["name"])["text"]

    prompt.__name__ = guide["name"].replace("-", "_")
    mcp.prompt(name=guide["name"], title=guide["title"], description=guide["description"])(prompt)

    def resource() -> str:
        return workflow_guides.get_guide(guide["name"])["text"]

    resource.__name__ = "guide_" + guide["name"].replace("-", "_")
    mcp.resource(f"ubem://guides/{guide['name']}", name=guide["name"], title=guide["title"],
                 description=guide["description"], mime_type="text/markdown")(resource)


for _guide in workflow_guides.list_guides():
    _register_guide_prompt(_guide)


@mcp.resource("ubem://archetypes", name="archetypes", title="Building archetypes and vintages",
              description="The DOE prototype archetypes and vintage bins UBEM-MCP models", mime_type="application/json")
def archetypes_resource() -> str:
    return json.dumps({
        "archetypes": [{"key": a.key, "label": a.label, "sector": a.sector, "default_floors": a.default_floors,
                        "floor_to_floor_m": a.floor_to_floor_m} for a in ARCHETYPES.values()],
        "vintages": {"pre1980": "built before 1980", "1980_2004": "built 1980-2003", "post2004": "built 2004 or later"},
    }, indent=1)


@mcp.resource("ubem://retrofit-measures", name="retrofit-measures", title="Retrofit measures",
              description="Measure types and parameters for retrofit scenarios", mime_type="application/json")
def retrofit_measures_resource() -> str:
    from ubem_mcp_server.urban.retrofit import list_measures
    return json.dumps(list_measures(), indent=1)


@mcp.resource("ubem://scenario-schema", name="scenario-schema", title="Retrofit scenario format",
              description="Keys a retrofit scenario accepts, with an example", mime_type="application/json")
def scenario_schema_resource() -> str:
    from ubem_mcp_server.urban.retrofit import _FILTER_KEYS, _SCENARIO_KEYS
    return json.dumps({
        "scenario_keys": sorted(_SCENARIO_KEYS),
        "measure_filter_keys": sorted(_FILTER_KEYS - {"type"}),
        "example": {"name": "electrify", "measures": [{"type": "heat_pump", "heating_cop": 3.3},
                                                      {"type": "lighting", "reduction": 0.4}],
                    "vintages": ["pre1980"], "adoption_rate": 0.5, "seed": 1},
    }, indent=1)


def main() -> None:
    """Run the MCP server with the configured transport (stdio or streamable HTTP)."""
    logger.info(f"Starting {config.server.name} v{config.server.version}")
    logger.info(f"EnergyPlus version: {config.energyplus.version}")
    logger.info(f"Sample files path: {config.paths.sample_files_path}")
    logger.info(f"Transport: {config.transport.transport}")

    try:
        if config.transport.transport == "stdio":
            mcp.run(transport="stdio")
        elif config.transport.transport == "streamable-http":
            import uvicorn
            from ubem_mcp_server.http_app import build_app

            app = build_app(mcp, config)
            logger.info(
                "Listening on http://%s:%d (path=%s, %d tokens)",
                config.transport.http_host,
                config.transport.http_port,
                config.transport.http_path,
                len(config.auth.tokens),
            )
            uvicorn.run(
                app,
                host=config.transport.http_host,
                port=config.transport.http_port,
                log_level=config.server.log_level.lower(),
            )
        else:
            raise RuntimeError(
                f"Unknown transport: {config.transport.transport!r} "
                f"(should have been caught at config load)"
            )
    except KeyboardInterrupt:
        logger.info("Server shutdown requested")
    except Exception as e:
        logger.error(f"Server error: {e}", exc_info=True)
        raise
    finally:
        logger.info("Server stopped")


if __name__ == "__main__":
    main()
