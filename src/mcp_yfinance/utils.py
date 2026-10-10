"""Utility functions for ticker normalization, caching, and schema generation.

This module provides utilities for normalizing stock tickers across different
markets, formatting data, generating cache keys, and creating MCP tool schemas.
"""

import hashlib
import inspect
import re
import typing
from collections.abc import Callable
from enum import Enum
from typing import Any

import pandas as pd

# Market suffix configuration for ticker normalization
# This dictionary maps market codes to their Yahoo Finance suffixes
MARKET_SUFFIXES: dict[str, str] = {
    "US": "",  # United States - no suffix
    "BR": ".SA",  # Brazil - São Paulo Stock Exchange
    "UK": ".L",  # United Kingdom - London Stock Exchange
    "DE": ".DE",  # Germany - Deutsche Börse
    "FR": ".PA",  # France - Euronext Paris
    "JP": ".T",  # Japan - Tokyo Stock Exchange
    "IN_NSE": ".NS",  # India - National Stock Exchange
    "IN_BSE": ".BO",  # India - Bombay Stock Exchange
    "HK": ".HK",  # Hong Kong Stock Exchange
    "AU": ".AX",  # Australia - Australian Securities Exchange
    "CA": ".TO",  # Canada - Toronto Stock Exchange
    # Add more markets as needed - extensible design
}


def normalize_ticker(ticker: str, market: str = "US") -> str:
    """Normalize a stock ticker symbol for a specific market.

    Adds the appropriate market suffix to the ticker symbol based on the
    specified market. If the ticker already has a suffix, it is returned
    as-is.

    Args:
        ticker: The stock ticker symbol (e.g., "AAPL", "PETR4", "RELIANCE").
        market: The market code (e.g., "US", "BR", "UK", "IN_NSE").
                Defaults to "US".

    Returns:
        The normalized ticker with appropriate suffix (e.g., "AAPL",
        "PETR4.SA", "RELIANCE.NS").

    Raises:
        ValueError: If the market code is not supported.

    Examples:
        >>> normalize_ticker("AAPL", "US")
        'AAPL'
        >>> normalize_ticker("PETR4", "BR")
        'PETR4.SA'
        >>> normalize_ticker("RELIANCE", "IN_NSE")
        'RELIANCE.NS'
        >>> normalize_ticker("7203", "JP")
        '7203.T'
    """
    if market not in MARKET_SUFFIXES:
        raise ValueError(
            f"Unsupported market: {market}. "
            f"Supported markets: {', '.join(MARKET_SUFFIXES.keys())}"
        )

    # If ticker already has a suffix (contains a dot), return as-is
    if "." in ticker:
        return ticker

    # Get the suffix for the market and append to ticker
    suffix = MARKET_SUFFIXES[market]
    return f"{ticker}{suffix}"


def format_dataframe_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Convert DataFrame datetime index to ISO format strings.

    This function is useful for serializing DataFrames with datetime
    indexes to JSON format.

    Args:
        df: DataFrame with datetime index.

    Returns:
        DataFrame with datetime index converted to ISO format strings.

    Examples:
        >>> df = pd.DataFrame({'value': [1, 2]}, index=pd.date_range('2024-01-01', periods=2))
        >>> formatted = format_dataframe_dates(df)
        >>> print(formatted.index[0])
        '2024-01-01T00:00:00'
    """
    if isinstance(df.index, pd.DatetimeIndex):
        df = df.copy()
        df.index = df.index.strftime("%Y-%m-%dT%H:%M:%S")
    return df


def generate_cache_key(tool_name: str, **kwargs: Any) -> str:
    """Generate a consistent cache key for a tool call.

    Creates a deterministic hash-based cache key from the tool name
    and its parameters. This ensures consistent caching across calls
    with the same parameters.

    Args:
        tool_name: Name of the tool/function being called.
        **kwargs: Parameters passed to the tool.

    Returns:
        A cache key string in the format "tool_name:hash".

    Examples:
        >>> key1 = generate_cache_key("get_stock_info", symbol="AAPL")
        >>> key2 = generate_cache_key("get_stock_info", symbol="AAPL")
        >>> key1 == key2
        True
    """
    # Sort kwargs to ensure consistent ordering
    sorted_params = sorted(kwargs.items())
    params_str = str(sorted_params)

    # Generate hash of parameters
    params_hash = hashlib.md5(params_str.encode()).hexdigest()[:12]

    return f"{tool_name}:{params_hash}"


def parse_docstring(docstring: str) -> dict[str, str]:
    """Extract parameter descriptions from a function's docstring.

    Parses Google-style docstrings to extract parameter names and
    their descriptions.

    Args:
        docstring: The function's docstring.

    Returns:
        Dictionary mapping parameter names to their descriptions.

    Examples:
        >>> doc = '''Get stock price.
        ...
        ... Args:
        ...     symbol: The stock ticker symbol.
        ...     date: The date to query.
        ... '''
        >>> params = parse_docstring(doc)
        >>> params['symbol']
        'The stock ticker symbol.'
    """
    if not docstring:
        return {}

    param_descriptions = {}

    # Find the Args section
    args_match = re.search(r"Args:(.*?)(?:\n\n|\n[A-Z]|\Z)", docstring, re.DOTALL)
    if not args_match:
        return param_descriptions

    args_section = args_match.group(1)

    # Parse parameter lines (format: "param_name: description")
    param_pattern = r"\s*(\w+):\s*(.+?)(?=\n\s*\w+:|\Z)"
    for match in re.finditer(param_pattern, args_section, re.DOTALL):
        param_name = match.group(1)
        description = match.group(2).strip().replace("\n", " ")
        param_descriptions[param_name] = description

    return param_descriptions


def generate_tool_schema(func: Callable) -> dict[str, Any]:
    """Auto-generate MCP Tool schema from a function.

    Inspects a function's signature and docstring to automatically
    generate an MCP-compatible tool schema.

    Args:
        func: The function to generate a schema for.

    Returns:
        Dictionary containing the tool schema with name, description,
        and input schema.

    Examples:
        >>> def get_price(symbol: str, date: Optional[str] = None) -> str:
        ...     '''Get stock price.
        ...
        ...     Args:
        ...         symbol: Stock ticker symbol.
        ...         date: Optional date.
        ...     '''
        ...     pass
        >>> schema = generate_tool_schema(get_price)
        >>> schema['name']
        'get_price'
    """
    # Get function signature
    sig = inspect.signature(func)
    docstring = inspect.getdoc(func) or ""

    # Extract main description (first paragraph)
    description = docstring.split("\n\n")[0].strip()

    # Parse parameter descriptions
    param_descriptions = parse_docstring(docstring)

    # Build input schema
    properties = {}
    required = []

    for param_name, param in sig.parameters.items():
        # Skip 'self' parameter
        if param_name == "self":
            continue

        # Determine parameter type and enum values
        param_type = "string"  # Default to string
        enum_values = []  # List of valid values for Literal/Enum types

        if param.annotation != inspect.Parameter.empty:
            annotation = param.annotation
            annotation_str = str(annotation)

            # Check for Literal types (e.g., Literal["1d", "5d", "1mo"])
            if typing.get_origin(annotation) is typing.Literal:
                enum_values = list(typing.get_args(annotation))
                param_type = "string"

            # Check for Enum types (e.g., Period, Interval)
            elif inspect.isclass(annotation) and issubclass(annotation, Enum):
                enum_values = [e.value for e in annotation]
                param_type = "string"

            # Check for list types (e.g., list[dict[str, Any]] | None)
            elif annotation_str.lower().startswith("list[") or "list[" in annotation_str.lower():
                param_type = "array"

            # Check for basic types
            elif "int" in annotation_str.lower():
                param_type = "integer"
            elif "bool" in annotation_str.lower():
                param_type = "boolean"
            elif "float" in annotation_str.lower():
                param_type = "number"

        # Build parameter schema
        param_schema: dict[str, Any] = {"type": param_type}
        if param_type == "array":
            param_schema["items"] = {"type": "object"}

        # Add enum values if available (crucial for LLM to know valid inputs)
        if enum_values:
            param_schema["enum"] = enum_values

        # Add description if available
        if param_name in param_descriptions:
            param_schema["description"] = param_descriptions[param_name]

        properties[param_name] = param_schema

        # Add to required list if no default value
        if param.default == inspect.Parameter.empty:
            required.append(param_name)

    # Build complete schema
    schema = {
        "name": func.__name__,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }

    return schema
