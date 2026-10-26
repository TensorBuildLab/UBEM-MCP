"""Urban-scale building stock handling for UBEM-MCP (new in UBEM-MCP)."""

from .archetypes import ARCHETYPES, VINTAGES
from .stock import Building, BuildingStock, load_geojson, stock_from_geojson
from .stock_manager import StockManager

__all__ = [
    "ARCHETYPES", "VINTAGES", "Building", "BuildingStock",
    "load_geojson", "stock_from_geojson", "StockManager",
]
