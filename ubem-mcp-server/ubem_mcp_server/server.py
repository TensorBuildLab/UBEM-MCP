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
import json
from typing import Optional, Dict, Any, List
from pathlib import Path
from datetime import datetime

# Import FastMCP instead of the low-level Server
from mcp.server.fastmcp import FastMCP

# Import our EnergyPlus utilities and configuration
from ubem_mcp_server.energyplus_tools import EnergyPlusManager
from ubem_mcp_server.config import get_config, Config
from ubem_mcp_server.batch_manager import BatchManager, discover_idf_files as _discover_idf_files
from ubem_mcp_server.urban import ARCHETYPES, StockManager

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
mcp = FastMCP(config.server.name, **_mcp_kwargs)

# Initialize EnergyPlus manager with configuration
ep_manager = EnergyPlusManager(config)

# Initialize batch manager for multi-model (portfolio) simulation
batch_manager = BatchManager(config)

# Initialize building-stock registry for urban (GIS-based) modeling
stock_manager = StockManager(config)

logger.info(f"UBEM-MCP Server '{config.server.name}' v{config.server.version} initialized")


# Add this tool function to server.py

@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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

@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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
# Batch Simulation (portfolio / multi-model) — new in UBEM-MCP
# ============================================================================

@mcp.tool()
async def discover_idf_files(directory: str, pattern: str = "*.idf") -> str:
    """
    Glob a directory for IDF files to use as batch simulation inputs

    Args:
        directory: Directory to search (absolute, or relative to the server workspace)
        pattern: Glob pattern to match (default: "*.idf")

    Returns:
        JSON string listing matched IDF files with path, size, and basic file info.
        Feed the "idf_path" values straight into run_batch_simulation's `models` list.
    """
    try:
        logger.info(f"Discovering IDF files in: {directory} (pattern={pattern})")
        files = _discover_idf_files(config, directory, pattern)
        return f"Discovered {len(files)} IDF file(s) in {directory}:\n{json.dumps(files, indent=2)}"
    except FileNotFoundError as e:
        logger.warning(f"Directory not found: {directory}")
        return f"Directory not found: {str(e)}"
    except Exception as e:
        logger.error(f"Error discovering IDF files in {directory}: {str(e)}")
        return f"Error discovering IDF files: {str(e)}"


@mcp.tool()
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
) -> str:
    """
    Run EnergyPlus simulations for a batch (portfolio) of models in parallel background workers

    This is the key UBEM-MCP addition over single-model EnergyPlus-MCP: it runs many
    IDFs concurrently (one worker process per model, since eppy's global IDD state
    can't safely be shared across threads) and returns immediately with a batch_id.
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
    try:
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
        return f"Batch simulation submitted:\n{json.dumps(result, indent=2)}"
    except ValueError as e:
        logger.warning(f"Invalid arguments for run_batch_simulation: {str(e)}")
        return f"Invalid arguments: {str(e)}"
    except Exception as e:
        logger.error(f"Error submitting batch simulation: {str(e)}")
        return f"Error submitting batch simulation: {str(e)}"


@mcp.tool()
async def get_batch_status(batch_id: str) -> str:
    """
    Get the current status of a batch simulation job

    Args:
        batch_id: Batch ID returned by run_batch_simulation

    Returns:
        JSON string with overall status, per-status counts, and completion flag.
        Poll this until "is_complete" is true, then call get_batch_results.
    """
    try:
        result = batch_manager.get_status(batch_id)
        return f"Batch status for {batch_id}:\n{json.dumps(result, indent=2)}"
    except KeyError as e:
        logger.warning(f"Unknown batch_id: {batch_id}")
        return f"Unknown batch_id: {str(e)}"
    except Exception as e:
        logger.error(f"Error getting batch status for {batch_id}: {str(e)}")
        return f"Error getting batch status: {str(e)}"


@mcp.tool()
async def get_batch_results(batch_id: str) -> str:
    """
    Get detailed per-model results for a batch simulation job

    Args:
        batch_id: Batch ID returned by run_batch_simulation

    Returns:
        JSON string with overall status plus per-model results (success/failure,
        duration, output directory, error details). Safe to call before the batch
        finishes — check "is_complete" and per-model "status" fields for partial results.
    """
    try:
        result = batch_manager.get_results(batch_id)
        return f"Batch results for {batch_id}:\n{json.dumps(result, indent=2)}"
    except KeyError as e:
        logger.warning(f"Unknown batch_id: {batch_id}")
        return f"Unknown batch_id: {str(e)}"
    except Exception as e:
        logger.error(f"Error getting batch results for {batch_id}: {str(e)}")
        return f"Error getting batch results: {str(e)}"


@mcp.tool()
async def list_batches() -> str:
    """
    List all known batch simulation jobs (in-memory plus any persisted on disk)

    Returns:
        JSON string with a summary of every batch job this server knows about
    """
    try:
        result = batch_manager.list_batches()
        return f"Batch jobs ({len(result)}):\n{json.dumps(result, indent=2)}"
    except Exception as e:
        logger.error(f"Error listing batches: {str(e)}")
        return f"Error listing batches: {str(e)}"


@mcp.tool()
async def cancel_batch(batch_id: str) -> str:
    """
    Cancel a batch simulation job

    Args:
        batch_id: Batch ID returned by run_batch_simulation

    Returns:
        JSON string with updated batch status. Models not yet started are cancelled;
        this is best-effort for models already running (an in-flight EnergyPlus
        subprocess in a worker cannot be forcibly killed from here).
    """
    try:
        result = batch_manager.cancel_batch(batch_id)
        return f"Batch cancellation requested for {batch_id}:\n{json.dumps(result, indent=2)}"
    except KeyError as e:
        logger.warning(f"Unknown batch_id: {batch_id}")
        return f"Unknown batch_id: {str(e)}"
    except Exception as e:
        logger.error(f"Error cancelling batch {batch_id}: {str(e)}")
        return f"Error cancelling batch: {str(e)}"


# ============================================================================
# Urban Building Stock (GIS footprints -> normalized buildings) — new in UBEM-MCP
# ============================================================================

def _stock_loader_options(**opts) -> Dict[str, Any]:
    return {k: v for k, v in opts.items() if v is not None}


@mcp.tool()
async def list_archetypes() -> str:
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
    return json.dumps(data, indent=2)


@mcp.tool()
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
    min_footprint_m2: float = 20.0,
) -> str:
    """
    Load building footprints from a GeoJSON file as a building stock for urban modeling

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
        min_footprint_m2: Smaller footprints are excluded

    Returns:
        JSON summary: stock_id, building counts by archetype and vintage, floor
        areas, inferred-attribute shares, and exclusions by reason.
    """
    try:
        opts = _stock_loader_options(
            source_crs=source_crs, height_field=height_field, floors_field=floors_field,
            use_field=use_field, year_field=year_field, id_field=id_field, height_unit=height_unit,
            default_archetype=default_archetype, default_year=default_year,
            min_footprint_m2=min_footprint_m2,
        )
        result = stock_manager.load_from_file(geojson_path, stock_id=stock_id, **opts)
        return f"Building stock loaded:\n{json.dumps(result, indent=2)}"
    except (ValueError, FileNotFoundError) as e:
        return f"Could not load building stock: {str(e)}"
    except Exception as e:
        logger.error(f"Error loading building stock {geojson_path}: {str(e)}")
        return f"Error loading building stock: {str(e)}"


@mcp.tool()
async def fetch_osm_buildings(
    bbox: Optional[List[float]] = None,
    center_lat: Optional[float] = None,
    center_lon: Optional[float] = None,
    radius_m: Optional[float] = None,
    stock_id: Optional[str] = None,
    default_archetype: str = "single_family",
    default_year: int = 1980,
    min_footprint_m2: float = 20.0,
) -> str:
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
        min_footprint_m2: Smaller footprints are excluded

    Returns:
        JSON summary of the stock, as for load_building_stock.
    """
    try:
        opts = _stock_loader_options(
            default_archetype=default_archetype, default_year=default_year,
            min_footprint_m2=min_footprint_m2,
        )
        result = await asyncio.to_thread(
            stock_manager.fetch_osm, bbox=bbox, center_lat=center_lat, center_lon=center_lon,
            radius_m=radius_m, stock_id=stock_id, **opts,
        )
        return f"OpenStreetMap buildings loaded:\n{json.dumps(result, indent=2)}"
    except ValueError as e:
        return f"Could not fetch OSM buildings: {str(e)}"
    except Exception as e:
        logger.error(f"Error fetching OSM buildings: {str(e)}")
        return f"Error fetching OSM buildings (the Overpass API may be busy; retry shortly): {str(e)}"


@mcp.tool()
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
) -> str:
    """
    Fill a building stock's guessed attributes from another GIS layer (e.g. assessor parcels)

    Footprint sources such as OpenStreetMap rarely carry year built or use. This
    joins a GeoJSON layer onto the footprints: each building takes the attributes
    of the layer polygon containing its centroid (or covering most of its
    footprint), or of a point feature inside it. By default only attributes that
    were inferred are filled. The result is saved as a new stock; the original
    is unchanged.

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

    Returns:
        JSON with match counts, filled attribute counts, unrecognized use values,
        inferred shares before/after, and the new stock's summary.
    """
    try:
        result = stock_manager.enrich(
            stock_id, layer_path, field_map, new_stock_id=new_stock_id,
            use_value_map=use_value_map, layer_crs=layer_crs, overwrite=overwrite,
            height_unit=height_unit, min_overlap=min_overlap,
        )
        return f"Building stock enriched:\n{json.dumps(result, indent=2)}"
    except (KeyError, ValueError, FileNotFoundError) as e:
        return f"Could not enrich building stock: {str(e)}"
    except Exception as e:
        logger.error(f"Error enriching building stock {stock_id}: {str(e)}")
        return f"Error enriching building stock: {str(e)}"


@mcp.tool()
async def get_building_stock(
    stock_id: str,
    archetypes: Optional[List[str]] = None,
    vintages: Optional[List[str]] = None,
    min_gfa_m2: Optional[float] = None,
    max_gfa_m2: Optional[float] = None,
    limit: int = 50,
) -> str:
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
    try:
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
            "num_matching": len(matches),
            "buildings": buildings,
        }
        return json.dumps(data, indent=2)
    except (KeyError, ValueError) as e:
        return f"Could not get building stock: {str(e)}"
    except Exception as e:
        logger.error(f"Error reading building stock {stock_id}: {str(e)}")
        return f"Error reading building stock: {str(e)}"


@mcp.tool()
async def list_building_stocks() -> str:
    """
    List building stocks loaded so far (persisted across server restarts)

    Returns:
        JSON list of stocks with stock_id, creation time, source and building count.
    """
    try:
        return json.dumps(stock_manager.list_stocks(), indent=2)
    except Exception as e:
        logger.error(f"Error listing building stocks: {str(e)}")
        return f"Error listing building stocks: {str(e)}"


# ============================================================================
# Archetype model generation and stock simulation — new in UBEM-MCP
# ============================================================================

@mcp.tool()
async def find_weather_stations(
    stock_id: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    n: int = 5,
) -> str:
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
    try:
        result = await asyncio.to_thread(stock_manager.find_weather_stations, latitude, longitude, stock_id, n)
        return f"Nearest weather stations:\n{json.dumps(result, indent=2)}"
    except (KeyError, ValueError) as e:
        return f"Could not find weather stations: {str(e)}"
    except Exception as e:
        logger.error(f"Error finding weather stations: {str(e)}")
        return f"Error finding weather stations: {str(e)}"


@mcp.tool()
async def get_site_weather(
    stock_id: str,
    station: Optional[str] = None,
    dataset: Optional[str] = None,
    weather_file: Optional[str] = None,
) -> str:
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
    try:
        result = await asyncio.to_thread(stock_manager.resolve_weather, stock_id, station=station,
                                         dataset=dataset, weather_file=weather_file)
        return f"Site weather set:\n{json.dumps(result, indent=2)}"
    except (KeyError, ValueError, FileNotFoundError) as e:
        return f"Could not set site weather: {str(e)}"
    except Exception as e:
        logger.error(f"Error resolving weather for stock {stock_id}: {str(e)}")
        return f"Error setting site weather: {str(e)}"


@mcp.tool()
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
) -> str:
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
    try:
        result = await asyncio.to_thread(
            stock_manager.generate_models, stock_id, climate_zone, weather_file=weather_file, hvac=hvac,
            archetypes=archetypes, vintages=vintages, building_ids=building_ids,
            max_buildings=max_buildings, model_set_id=model_set_id,
        )
        return f"Building models generated:\n{json.dumps(result, indent=2)}"
    except (KeyError, ValueError, FileNotFoundError) as e:
        return f"Could not generate building models: {str(e)}"
    except Exception as e:
        logger.error(f"Error generating models for stock {stock_id}: {str(e)}")
        return f"Error generating building models: {str(e)}"


@mcp.tool()
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
) -> str:
    """
    Generate archetype models for a building stock and simulate them all as a background batch

    Combines generate_building_models and run_batch_simulation: every selected
    building gets a model, and the models run in parallel worker processes.
    Returns immediately with a batch_id; poll get_batch_status until complete.

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
    try:
        gen = await asyncio.to_thread(
            stock_manager.generate_models, stock_id, climate_zone, weather_file=weather_file, hvac=hvac,
            archetypes=archetypes, vintages=vintages, building_ids=building_ids,
            max_buildings=max_buildings, model_set_id=model_set_id,
        )
        if not gen["weather_file"]:
            return ("Could not simulate building stock: no weather file. Give weather_file, call "
                    "get_site_weather, or omit climate_zone to resolve both from the nearest station.")
        manifest = stock_manager.model_manifest(stock_id, gen["model_set_id"])
        models = [{"idf_path": m["idf_path"], "label": m["building_id"]} for m in manifest["models"]]
        if not models:
            return f"No models were generated:\n{json.dumps(gen, indent=2)}"
        batch = batch_manager.submit_batch(
            models=models, weather_file=gen["weather_file"], max_workers=max_workers,
            output_directory=str(Path(gen["model_directory"]) / "runs"),
            metadata={"stock_id": stock_id, "model_set_id": gen["model_set_id"]},
        )
        return f"Building stock simulation submitted:\n{json.dumps({'batch': batch, 'models': gen}, indent=2)}"
    except (KeyError, ValueError, FileNotFoundError) as e:
        return f"Could not simulate building stock: {str(e)}"
    except Exception as e:
        logger.error(f"Error simulating stock {stock_id}: {str(e)}")
        return f"Error simulating building stock: {str(e)}"


@mcp.tool()
async def aggregate_stock_results(
    batch_id: Optional[str] = None,
    stock_id: Optional[str] = None,
    model_set_id: Optional[str] = None,
    emission_factors: Optional[Dict[str, float]] = None,
    make_map: bool = True,
) -> str:
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
    try:
        outputs = None
        results_id = None
        if batch_id:
            status = batch_manager.get_status(batch_id)
            if not status["is_complete"]:
                return f"Batch {batch_id} is still running ({status['counts']}); aggregate when it completes."
            meta = status.get("metadata") or {}
            stock_id = stock_id or meta.get("stock_id")
            model_set_id = model_set_id or meta.get("model_set_id")
            if not stock_id or not model_set_id:
                return (f"Batch {batch_id} was not created by simulate_building_stock; "
                        "pass stock_id and model_set_id as well.")
            outputs = {m["label"]: m for m in batch_manager.get_results(batch_id)["models"]}
            results_id = batch_id
        if not stock_id or not model_set_id:
            return "Provide batch_id, or stock_id and model_set_id."
        summary = await asyncio.to_thread(
            stock_manager.aggregate_results, stock_id, model_set_id, outputs=outputs, results_id=results_id,
            emission_factors=emission_factors, make_map=make_map,
        )
        return f"Stock results aggregated:\n{json.dumps(summary, indent=2, default=str)}"
    except (KeyError, ValueError, FileNotFoundError) as e:
        return f"Could not aggregate results: {str(e)}"
    except Exception as e:
        logger.error(f"Error aggregating results: {str(e)}")
        return f"Error aggregating results: {str(e)}"


@mcp.tool()
async def list_retrofit_measures() -> str:
    """
    List the retrofit measures available for retrofit scenarios

    Returns:
        JSON list of measure types with their parameters and descriptions.
    """
    from ubem_mcp_server.urban.retrofit import list_measures
    return f"Retrofit measures:\n{json.dumps(list_measures(), indent=2)}"


@mcp.tool()
async def simulate_retrofit_scenario(
    stock_id: str,
    baseline_model_set_id: str,
    scenario: Dict[str, Any],
    max_workers: Optional[int] = None,
    model_set_id: Optional[str] = None,
) -> str:
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
    try:
        gen = await asyncio.to_thread(stock_manager.generate_scenario, stock_id, baseline_model_set_id,
                                      scenario, model_set_id=model_set_id)
        if not gen["weather_file"]:
            return "Could not simulate scenario: the baseline model set has no weather file."
        manifest = stock_manager.model_manifest(stock_id, gen["model_set_id"])
        models = [{"idf_path": m["idf_path"], "label": m["building_id"]} for m in manifest["models"]]
        if not models:
            return f"No scenario models were generated:\n{json.dumps(gen, indent=2)}"
        batch = batch_manager.submit_batch(
            models=models, weather_file=gen["weather_file"], max_workers=max_workers,
            output_directory=str(Path(gen["model_directory"]) / "runs"),
            metadata={"stock_id": stock_id, "model_set_id": gen["model_set_id"],
                      "scenario": gen["scenario"]["name"], "baseline_model_set_id": baseline_model_set_id},
        )
        return f"Retrofit scenario submitted:\n{json.dumps({'batch': batch, 'models': gen}, indent=2)}"
    except (KeyError, ValueError, FileNotFoundError) as e:
        return f"Could not simulate retrofit scenario: {str(e)}"
    except Exception as e:
        logger.error(f"Error simulating retrofit scenario for stock {stock_id}: {str(e)}")
        return f"Error simulating retrofit scenario: {str(e)}"


@mcp.tool()
async def compare_retrofit_scenario(
    stock_id: str,
    scenario_model_set_id: str,
    baseline_results_id: Optional[str] = None,
    scenario_results_id: Optional[str] = None,
    emission_factors: Optional[Dict[str, float]] = None,
) -> str:
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
    try:
        result = await asyncio.to_thread(
            stock_manager.compare_scenario, stock_id, scenario_model_set_id,
            baseline_results_id=baseline_results_id, scenario_results_id=scenario_results_id,
            emission_factors=emission_factors)
        return f"Retrofit scenario comparison:\n{json.dumps(result, indent=2, default=str)}"
    except (KeyError, ValueError, FileNotFoundError) as e:
        return f"Could not compare scenario: {str(e)}"
    except Exception as e:
        logger.error(f"Error comparing scenario {scenario_model_set_id}: {str(e)}")
        return f"Error comparing retrofit scenario: {str(e)}"


if __name__ == "__main__":
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
