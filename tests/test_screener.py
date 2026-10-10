"""Mocked tests for YahooFinanceService.screen_stocks."""

import json
from unittest.mock import patch

import numpy as np
import pytest
import yfinance as yf

from src.mcp_yfinance.exceptions import (
    DataNotAvailableError,
    InvalidParameterError,
    YFinanceAPIError,
)
from src.mcp_yfinance.service import YahooFinanceService
from src.mcp_yfinance.utils import generate_tool_schema

RESPONSE = {
    "start": 0,
    "count": 2,
    "total": 100,
    "quotes": [
        {"symbol": "AAA", "price": np.float64(1.5), "volume": np.int64(10), "x": float("nan")},
        {"symbol": "BBB", "price": 2.0, "volume": 5, "x": None},
    ],
}

NESTED = {
    "operator": "and",
    "conditions": [
        {"operator": "gt", "field": "percentchange", "value": 3},
        {"operator": "eq", "field": "region", "value": "us"},
    ],
}


@pytest.fixture
def service():
    return YahooFinanceService()


def test_predefined_mode(service):
    with patch("yfinance.screen", return_value=RESPONSE) as screen:
        result = json.loads(service.screen_stocks(predefined_query="day_gainers", size=10))
    screen.assert_called_once_with("day_gainers", offset=0, count=10)
    assert result["start"] == 0 and result["count"] == 2 and result["total"] == 100
    assert result["quotes"][0]["price"] == 1.5
    assert result["quotes"][0]["volume"] == 10
    assert result["quotes"][0]["x"] is None


def test_predefined_with_sort(service):
    with patch("yfinance.screen", return_value=RESPONSE) as screen:
        service.screen_stocks(
            predefined_query="day_losers", offset=5, sort_field="percentchange", sort_ascending=True
        )
    screen.assert_called_once_with(
        "day_losers", offset=5, count=25, sortField="percentchange", sortAsc=True
    )


def test_unknown_predefined(service):
    with patch("yfinance.screen") as screen:
        with pytest.raises(InvalidParameterError):
            service.screen_stocks(predefined_query="nope")
    screen.assert_not_called()


def test_custom_nested_query(service):
    with patch("yfinance.screen", return_value=RESPONSE) as screen:
        service.screen_stocks(
            filters=[NESTED], size=50, offset=3, sort_field="percentchange", sort_ascending=True
        )
    args, kwargs = screen.call_args
    assert isinstance(args[0], yf.EquityQuery)
    assert args[0].to_dict() == {
        "operator": "AND",
        "operands": [
            {"operator": "GT", "operands": ["percentchange", 3]},
            {"operator": "EQ", "operands": ["region", "us"]},
        ],
    }
    assert kwargs == {"offset": 3, "size": 50, "sortField": "percentchange", "sortAsc": True}


def test_custom_defaults_and_implicit_and(service):
    filters = [
        {"operator": "btwn", "field": "intradaymarketcap", "value": [1000, 2000]},
        {"operator": "is-in", "field": "exchange", "value": ["NMS", "NYQ"]},
        {"operator": "or", "conditions": [
            {"operator": "lte", "field": "peratio.lasttwelvemonths", "value": 20},
            {"operator": "gte", "field": "percentchange", "value": 1},
            {"operator": "lt", "field": "percentchange", "value": 9},
        ]},
    ]
    with patch("yfinance.screen", return_value=RESPONSE) as screen:
        service.screen_stocks(filters=filters)
    args, kwargs = screen.call_args
    d = args[0].to_dict()
    assert d["operator"] == "AND" and len(d["operands"]) == 3
    assert d["operands"][0] == {"operator": "BTWN", "operands": ["intradaymarketcap", 1000, 2000]}
    assert d["operands"][1]["operator"] == "OR"
    assert kwargs == {"offset": 0, "size": 25, "sortField": "ticker", "sortAsc": False}


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"predefined_query": "day_gainers", "filters": [NESTED]},
        {"filters": []},
    ],
)
def test_invalid_modes(service, kwargs):
    with pytest.raises(InvalidParameterError):
        service.screen_stocks(**kwargs)


@pytest.mark.parametrize(
    "condition",
    [
        "gt",
        {"operator": "like", "field": "region", "value": "us"},
        {"field": "region", "value": "us"},
        {"operator": "gt", "value": 1},
        {"operator": "gt", "field": "percentchange"},
        {"operator": "gt", "field": "percentchange", "value": "high"},
        {"operator": "eq", "field": "region", "value": ["us"]},
        {"operator": "btwn", "field": "percentchange", "value": [1]},
        {"operator": "is-in", "field": "exchange", "value": "NMS"},
        {"operator": "and", "conditions": [{"operator": "gt", "field": "percentchange", "value": 1}]},
        {"operator": "gt", "field": "notafield", "value": 1},
    ],
)
def test_malformed_conditions(service, condition):
    with patch("yfinance.screen") as screen:
        with pytest.raises(InvalidParameterError):
            service.screen_stocks(filters=[condition])
    screen.assert_not_called()


@pytest.mark.parametrize(
    "kwargs",
    [{"size": 0}, {"size": 251}, {"offset": -1}, {"size": True}, {"offset": 1.5}],
)
def test_size_offset_validation(service, kwargs):
    with pytest.raises(InvalidParameterError):
        service.screen_stocks(predefined_query="day_gainers", **kwargs)


def test_size_bounds_accepted(service):
    with patch("yfinance.screen", return_value=RESPONSE):
        service.screen_stocks(predefined_query="day_gainers", size=1)
        service.screen_stocks(predefined_query="day_gainers", size=250)


def test_api_error_mapped(service):
    with patch("yfinance.screen", side_effect=RuntimeError("boom")):
        with pytest.raises(YFinanceAPIError, match="boom"):
            service.screen_stocks(predefined_query="day_gainers")


def test_empty_response(service):
    with patch("yfinance.screen", return_value=None):
        with pytest.raises(DataNotAvailableError):
            service.screen_stocks(predefined_query="day_gainers")


def test_empty_quotes_returned(service):
    with patch("yfinance.screen", return_value={"start": 0, "count": 0, "total": 0, "quotes": []}):
        result = json.loads(service.screen_stocks(predefined_query="day_gainers"))
    assert result["quotes"] == [] and result["total"] == 0


def test_tool_discovery_schema():
    schema = generate_tool_schema(YahooFinanceService.screen_stocks)
    props = schema["inputSchema"]["properties"]
    assert schema["name"] == "screen_stocks"
    assert props["filters"]["type"] == "array"
    assert props["size"]["type"] == "integer"
    assert props["sort_ascending"]["type"] == "boolean"
    assert schema["inputSchema"]["required"] == []
