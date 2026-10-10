"""MCP Server for Yahoo Finance integration.

This module implements the Model Context Protocol (MCP) server that exposes
Yahoo Finance data through a standardized interface for AI assistants.
"""

import logging
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .cache import CACHE_TTL, CacheManager
from .config import DEFAULT_MARKET
from .exceptions import (
    DataNotAvailableError,
    InvalidParameterError,
    TickerNotFoundError,
    YFinanceAPIError,
)
from .service import YahooFinanceService
from .utils import generate_cache_key, generate_tool_schema

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-yfinance")

# Initialize components
server = Server("mcp-yfinance")
service = YahooFinanceService(default_market=DEFAULT_MARKET)
cache = CacheManager()

# Tools that should not be cached (real-time data)
NO_CACHE_TOOLS = {
    "get_current_stock_price",
    "get_option_chain",
    "screen_stocks",
}


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List all available Yahoo Finance tools.

    Returns:
        List of MCP Tool objects describing all available operations
    """
    # Get all public methods from the service
    service_methods = [
        getattr(service, method_name)
        for method_name in dir(service)
        if callable(getattr(service, method_name))
        and not method_name.startswith("_")
    ]

    # Generate tool schemas
    tools = []
    for method in service_methods:
        try:
            schema = generate_tool_schema(method)
            tools.append(Tool(**schema))
        except Exception as e:
            logger.warning(f"Failed to generate schema for {method.__name__}: {e}")

    return tools


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Execute a Yahoo Finance tool.

    This handler manages tool execution with caching support. Real-time
    data tools bypass the cache, while historical and info tools use
    cached data when available.

    Args:
        name: Tool name (method name)
        arguments: Tool arguments as a dictionary

    Returns:
        List containing a single TextContent with the result
    """
    try:
        # Check if method exists on service
        if not hasattr(service, name):
            raise ValueError(f"Unknown tool: {name}")

        # Check cache if tool is cacheable
        if name not in NO_CACHE_TOOLS:
            cache_key = generate_cache_key(name, **arguments)
            cached_result = cache.get(cache_key)

            if cached_result is not None:
                logger.info(f"Cache hit for {name}")
                return [TextContent(type="text", text=cached_result)]

        # Execute the tool
        method = getattr(service, name)
        result = method(**arguments)

        # Cache the result if applicable
        if name not in NO_CACHE_TOOLS:
            # Determine TTL based on tool name
            ttl = CACHE_TTL.get("default")
            if "price" in name.lower() or "historical" in name.lower():
                ttl = CACHE_TTL.get("historical", CACHE_TTL["default"])
            elif "info" in name.lower():
                ttl = CACHE_TTL.get("info", CACHE_TTL["default"])
            elif "financial" in name.lower() or "statement" in name.lower():
                ttl = CACHE_TTL.get("financials", CACHE_TTL["default"])
            elif "holder" in name.lower():
                ttl = CACHE_TTL.get("holders", CACHE_TTL["default"])
            elif "option" in name.lower():
                ttl = CACHE_TTL.get("options", CACHE_TTL["default"])
            elif "news" in name.lower():
                ttl = CACHE_TTL.get("news", CACHE_TTL["default"])
            elif "recommendation" in name.lower():
                ttl = CACHE_TTL.get("recommendations", CACHE_TTL["default"])

            cache.set(cache_key, result, ttl=ttl)
            logger.info(f"Cached result for {name} with TTL={ttl}s")

        return [TextContent(type="text", text=result)]

    except TickerNotFoundError as e:
        error_msg = f"Ticker not found: {str(e)}"
        logger.error(error_msg)
        return [TextContent(type="text", text=error_msg)]

    except DataNotAvailableError as e:
        error_msg = f"Data not available: {str(e)}"
        logger.warning(error_msg)
        return [TextContent(type="text", text=error_msg)]

    except InvalidParameterError as e:
        error_msg = f"Invalid parameter: {str(e)}"
        logger.error(error_msg)
        return [TextContent(type="text", text=error_msg)]

    except YFinanceAPIError as e:
        error_msg = f"Yahoo Finance API error: {str(e)}"
        logger.error(error_msg)
        return [TextContent(type="text", text=error_msg)]

    except Exception as e:
        error_msg = f"Unexpected error executing {name}: {str(e)}"
        logger.exception(error_msg)
        return [TextContent(type="text", text=error_msg)]


async def main():
    """Run the MCP server using stdio transport.

    This is the main entry point for the server. It starts the server
    using standard input/output for communication with MCP clients.
    """
    logger.info(f"Starting MCP Yahoo Finance Server (Market: {DEFAULT_MARKET})")

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


# Export main for use as entry point
__all__ = ["main"]
