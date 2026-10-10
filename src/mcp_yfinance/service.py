"""Yahoo Finance service layer for MCP integration.

This module provides the main YahooFinanceService class that wraps yfinance
functionality and returns JSON-formatted data suitable for MCP tools.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import yfinance as yf
from requests import Session

logger = logging.getLogger(__name__)

from .exceptions import (
    DataNotAvailableError,
    InvalidParameterError,
    TickerNotFoundError,
    YFinanceAPIError,
)
from .models import (
    FrequencyType,
    HolderInfoType,
    IntervalType,
    OptionChainType,
    PeriodType,
    RecommendationInfoType,
)
from .utils import format_dataframe_dates, normalize_ticker

SCREENER_OPERATORS = (
    "eq",
    "is-in",
    "btwn",
    "gt",
    "lt",
    "gte",
    "lte",
    "and",
    "or",
)
SCREENER_MAX_SIZE = 250
SCREENER_MAX_DEPTH = 10


def safe_float(value: Any) -> float | None:
    """Convert value to float, handling NaN/None gracefully.

    Args:
        value: Value to convert to float.

    Returns:
        Float value or None if value is NaN/None.
    """
    if pd.isna(value):
        return None
    return float(value)


def safe_int(value: Any) -> int | None:
    """Convert value to int, handling NaN/None gracefully.

    Args:
        value: Value to convert to int.

    Returns:
        Int value or None if value is NaN/None.
    """
    if pd.isna(value):
        return None
    return int(value)


def _is_number(value: Any) -> bool:
    """Return True for real numbers (bool is excluded)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _json_safe(value: Any) -> Any:
    """Recursively convert a value into JSON-native types.

    NaN/NaT/None become None, numpy/pandas scalars become Python scalars,
    timestamps become ISO strings and unknown objects become strings.
    """
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (datetime, pd.Timestamp)):
        return None if pd.isna(value) else value.isoformat()
    if hasattr(value, "item") and callable(value.item):
        try:
            return _json_safe(value.item())
        except (ValueError, TypeError):
            pass
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return None if pd.isna(value) or value in (float("inf"), float("-inf")) else value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def _build_equity_query(condition: Any, depth: int = 0, path: str = "filters") -> Any:
    """Recursively build a yfinance EquityQuery from a condition dictionary."""
    if depth > SCREENER_MAX_DEPTH:
        raise InvalidParameterError(path, "nesting too deep")
    if not isinstance(condition, dict):
        raise InvalidParameterError(path, condition, ["object with 'operator'"])

    operator = condition.get("operator")
    if not isinstance(operator, str) or operator.lower() not in SCREENER_OPERATORS:
        raise InvalidParameterError(f"{path}.operator", operator, list(SCREENER_OPERATORS))
    operator = operator.lower()

    if operator in ("and", "or"):
        children = condition.get("conditions")
        if not isinstance(children, list) or len(children) < 2:
            raise InvalidParameterError(
                f"{path}.conditions", children, ["list of at least 2 conditions"]
            )
        operands = [
            _build_equity_query(child, depth + 1, f"{path}.conditions[{i}]")
            for i, child in enumerate(children)
        ]
        return yf.EquityQuery(operator, operands)

    field = condition.get("field")
    if not isinstance(field, str) or not field:
        raise InvalidParameterError(f"{path}.field", field, ["non-empty string"])

    if operator == "is-in":
        values = condition.get("value", condition.get("values"))
        if not isinstance(values, list) or not values:
            raise InvalidParameterError(f"{path}.value", values, ["non-empty list"])
        operands = [field, *values]
    elif operator == "btwn":
        values = condition.get("value", condition.get("values"))
        if (
            not isinstance(values, list)
            or len(values) != 2
            or not all(_is_number(v) for v in values)
        ):
            raise InvalidParameterError(f"{path}.value", values, ["[low, high] numbers"])
        operands = [field, *values]
    else:
        if "value" not in condition:
            raise InvalidParameterError(f"{path}.value", None, ["required"])
        value = condition["value"]
        if isinstance(value, (list, dict)) or value is None:
            raise InvalidParameterError(f"{path}.value", value, ["scalar string or number"])
        if operator != "eq" and not _is_number(value):
            raise InvalidParameterError(f"{path}.value", value, ["number"])
        operands = [field, value]

    try:
        return yf.EquityQuery(operator, operands)
    except ValueError as e:
        raise InvalidParameterError(path, str(e)) from e


class YahooFinanceService:
    """Service class for interacting with Yahoo Finance API.

    This class provides methods to fetch stock data, financial statements,
    and other market information from Yahoo Finance.

    Attributes:
        session: Optional requests Session for connection pooling.
        verify: Whether to verify SSL certificates.
        default_market: Default market for ticker normalization.
    """

    def __init__(
        self,
        session: Session | None = None,
        verify: bool = True,
        default_market: str = "US",
    ) -> None:
        """Initialize the Yahoo Finance service.

        Args:
            session: Optional requests Session for connection pooling (DEPRECATED: yfinance 0.2.66+ manages this internally).
            verify: Whether to verify SSL certificates (default: True).
            default_market: Default market for ticker normalization (default: "US").
        """
        # Note: yfinance 0.2.66+ uses curl_cffi internally and doesn't accept requests.Session
        # We keep the parameter for backward compatibility but don't use it
        # yfinance will manage the session internally with proper headers
        self.session = None  # Let yfinance manage the session internally
        self.verify = verify
        self.default_market = default_market

    def _get_ticker(self, symbol: str) -> yf.Ticker:
        """Get a yfinance Ticker object for the given symbol.

        Args:
            symbol: Stock ticker symbol.

        Returns:
            yfinance Ticker object.

        Raises:
            TickerNotFoundError: If the ticker is invalid or not found.
        """
        try:
            normalized_symbol = normalize_ticker(symbol, self.default_market)
            # Don't pass session - let yfinance manage it internally with curl_cffi
            ticker = yf.Ticker(normalized_symbol)

            # Verify ticker exists by checking if info is available
            if not ticker.info or len(ticker.info) <= 1:
                raise TickerNotFoundError(symbol)

            return ticker
        except Exception as e:
            if isinstance(e, TickerNotFoundError):
                raise
            raise YFinanceAPIError(str(e), symbol) from e

    # ========== PRICING & HISTORICAL DATA (Methods 1-6) ==========

    def get_current_stock_price(self, symbol: str) -> str:
        """Get the current stock price for a given symbol.

        Args:
            symbol: Stock ticker symbol (e.g., "AAPL", "PETR4").

        Returns:
            JSON string with current price information.

        Raises:
            TickerNotFoundError: If the ticker is invalid.
            YFinanceAPIError: If the API request fails.
        """
        ticker = self._get_ticker(symbol)

        try:
            info = ticker.info
            current_price = info.get("currentPrice") or info.get("regularMarketPrice")

            if current_price is None:
                raise DataNotAvailableError("current price", symbol)

            result = {
                "symbol": symbol,
                "price": current_price,
                "currency": info.get("currency"),
                "market_time": info.get("regularMarketTime"),
            }

            return json.dumps(result, indent=2)
        except Exception as e:
            if isinstance(e, (DataNotAvailableError, TickerNotFoundError)):
                raise
            raise YFinanceAPIError(str(e), symbol) from e

    def get_stock_price_by_date(self, symbol: str, date: str) -> str:
        """Get the stock price for a specific date.

        Args:
            symbol: Stock ticker symbol.
            date: Date in YYYY-MM-DD format.

        Returns:
            JSON string with price data for the specified date.

        Raises:
            TickerNotFoundError: If the ticker is invalid.
            DataNotAvailableError: If no data is available for the date.
            YFinanceAPIError: If the API request fails.
        """
        ticker = self._get_ticker(symbol)

        try:
            # Validate date format
            date_obj = datetime.strptime(date, "%Y-%m-%d")

            # Try to get data for the specific date
            # Add 1 day to end to create proper interval for Yahoo Finance API
            end_date = (date_obj + timedelta(days=1)).strftime("%Y-%m-%d")
            hist = ticker.history(start=date, end=end_date, interval="1d")

            # If no data found (e.g., weekend/holiday), search for last trading day
            if hist.empty:
                logger.warning(
                    f"No data available for {symbol} on {date}, searching for previous trading day"
                )

                # Search up to 7 days back for the last trading day
                start_search = (date_obj - timedelta(days=7)).strftime("%Y-%m-%d")
                hist = ticker.history(start=start_search, end=end_date, interval="1d")

                if hist.empty:
                    raise DataNotAvailableError(f"price data for date {date}", symbol)

                # Get the last available trading day before or on the requested date
                # Convert index to normalized dates for comparison (remove timezone for comparison)
                hist_dates = pd.to_datetime(hist.index).tz_localize(None).normalize()
                target_date = pd.Timestamp(date_obj).normalize()
                mask = hist_dates <= target_date
                hist = hist[mask]

                if hist.empty:
                    raise DataNotAvailableError(
                        f"price data for date {date} (no prior trading days found)",
                        symbol,
                    )

                # Use the most recent trading day
                hist = hist.tail(1)
                actual_date = hist.index[0].strftime("%Y-%m-%d")

                logger.info(
                    f"Using data from {actual_date} (last trading day before {date}) for {symbol}"
                )
            else:
                actual_date = hist.index[0].strftime("%Y-%m-%d")

            # Convert to dictionary
            row = hist.iloc[0]
            result: dict[str, Any] = {
                "symbol": symbol,
                "requested_date": date,
                "actual_date": actual_date,
                "open": safe_float(row["Open"]),
                "high": safe_float(row["High"]),
                "low": safe_float(row["Low"]),
                "close": safe_float(row["Close"]),
                "volume": safe_int(row["Volume"]),
            }

            if "Adj Close" in row:
                result["adj_close"] = safe_float(row["Adj Close"])

            return json.dumps(result, indent=2)
        except ValueError as e:
            raise InvalidParameterError("date", date, ["YYYY-MM-DD format"]) from e
        except Exception as e:
            if isinstance(e, (DataNotAvailableError, TickerNotFoundError, InvalidParameterError)):
                raise
            raise YFinanceAPIError(str(e), symbol) from e

    def get_stock_price_date_range(self, symbol: str, start_date: str, end_date: str) -> str:
        """Get stock prices for a date range.

        Args:
            symbol: Stock ticker symbol.
            start_date: Start date in YYYY-MM-DD format.
            end_date: End date in YYYY-MM-DD format.

        Returns:
            JSON string with price data for the date range.

        Raises:
            TickerNotFoundError: If the ticker is invalid.
            DataNotAvailableError: If no data is available for the range.
            InvalidParameterError: If date format is invalid.
            YFinanceAPIError: If the API request fails.
        """
        ticker = self._get_ticker(symbol)

        try:
            # Validate date formats
            datetime.strptime(start_date, "%Y-%m-%d")
            datetime.strptime(end_date, "%Y-%m-%d")

            hist = ticker.history(start=start_date, end=end_date, interval="1d")

            if hist.empty:
                raise DataNotAvailableError(f"price data for range {start_date} to {end_date}", symbol)

            # Format dates in index
            hist = format_dataframe_dates(hist)

            # Convert to list of records
            data = []
            for date, row in hist.iterrows():
                record: dict[str, Any] = {
                    "date": str(date),
                    "open": safe_float(row["Open"]),
                    "high": safe_float(row["High"]),
                    "low": safe_float(row["Low"]),
                    "close": safe_float(row["Close"]),
                    "volume": safe_int(row["Volume"]),
                }
                if "Adj Close" in row:
                    record["adj_close"] = safe_float(row["Adj Close"])
                data.append(record)

            result = {
                "symbol": symbol,
                "start_date": start_date,
                "end_date": end_date,
                "data": data,
            }

            return json.dumps(result, indent=2)
        except ValueError as e:
            raise InvalidParameterError("date", f"{start_date} or {end_date}", ["YYYY-MM-DD format"]) from e
        except Exception as e:
            if isinstance(e, (DataNotAvailableError, TickerNotFoundError, InvalidParameterError)):
                raise
            raise YFinanceAPIError(str(e), symbol) from e

    def get_historical_stock_prices(
        self, symbol: str, period: PeriodType = "1mo", interval: IntervalType = "1d"
    ) -> str:
        """Get historical stock prices for a given period and interval.

        Args:
            symbol: Stock ticker symbol.
            period: Time period (e.g., "1mo", "1y", "max").
            interval: Data interval (e.g., "1d", "1wk", "1mo").

        Returns:
            JSON string with historical price data.

        Raises:
            TickerNotFoundError: If the ticker is invalid.
            DataNotAvailableError: If no data is available.
            YFinanceAPIError: If the API request fails.
        """
        ticker = self._get_ticker(symbol)

        try:
            hist = ticker.history(period=period, interval=interval)

            if hist.empty:
                raise DataNotAvailableError(f"historical data for period {period}", symbol)

            # Format dates in index
            hist = format_dataframe_dates(hist)

            # Convert to list of records
            data = []
            for date, row in hist.iterrows():
                record: dict[str, Any] = {
                    "date": str(date),
                    "open": safe_float(row["Open"]),
                    "high": safe_float(row["High"]),
                    "low": safe_float(row["Low"]),
                    "close": safe_float(row["Close"]),
                    "volume": safe_int(row["Volume"]),
                }
                if "Adj Close" in row:
                    record["adj_close"] = safe_float(row["Adj Close"])
                data.append(record)

            result = {
                "symbol": symbol,
                "period": period,
                "interval": interval,
                "data": data,
            }

            return json.dumps(result, indent=2)
        except Exception as e:
            if isinstance(e, (DataNotAvailableError, TickerNotFoundError)):
                raise
            raise YFinanceAPIError(str(e), symbol) from e

    def get_dividends(self, symbol: str) -> str:
        """Get dividend history for a stock.

        Args:
            symbol: Stock ticker symbol.

        Returns:
            JSON string with dividend history.

        Raises:
            TickerNotFoundError: If the ticker is invalid.
            DataNotAvailableError: If no dividend data is available.
            YFinanceAPIError: If the API request fails.
        """
        ticker = self._get_ticker(symbol)

        try:
            dividends = ticker.dividends

            if dividends.empty:
                raise DataNotAvailableError("dividend", symbol)

            # Format dates
            dividends_df = dividends.to_frame(name="amount")
            dividends_df = format_dataframe_dates(dividends_df)

            # Convert to list of records
            data = [{"date": str(date), "amount": safe_float(row["amount"])} for date, row in dividends_df.iterrows()]

            result = {
                "symbol": symbol,
                "dividends": data,
            }

            return json.dumps(result, indent=2)
        except Exception as e:
            if isinstance(e, (DataNotAvailableError, TickerNotFoundError)):
                raise
            raise YFinanceAPIError(str(e), symbol) from e

    def get_stock_actions(self, symbol: str) -> str:
        """Get stock actions (splits and dividends) history.

        Args:
            symbol: Stock ticker symbol.

        Returns:
            JSON string with stock actions history.

        Raises:
            TickerNotFoundError: If the ticker is invalid.
            DataNotAvailableError: If no actions data is available.
            YFinanceAPIError: If the API request fails.
        """
        ticker = self._get_ticker(symbol)

        try:
            actions = ticker.actions

            if actions.empty:
                raise DataNotAvailableError("stock actions", symbol)

            # Format dates
            actions = format_dataframe_dates(actions)

            # Convert to list of records
            data = []
            for date, row in actions.iterrows():
                record: dict[str, Any] = {"date": str(date)}
                if "Dividends" in row and row["Dividends"] > 0:
                    dividend_val = safe_float(row["Dividends"])
                    if dividend_val is not None:
                        record["dividend"] = dividend_val
                if "Stock Splits" in row and row["Stock Splits"] > 0:
                    split_val = safe_float(row["Stock Splits"])
                    if split_val is not None:
                        record["stock_split"] = split_val
                data.append(record)

            result = {
                "symbol": symbol,
                "actions": data,
            }

            return json.dumps(result, indent=2)
        except Exception as e:
            if isinstance(e, (DataNotAvailableError, TickerNotFoundError)):
                raise
            raise YFinanceAPIError(str(e), symbol) from e

    # ========== COMPANY INFO (Method 7) ==========

    def get_stock_info(self, symbol: str) -> str:
        """Get comprehensive stock information and metadata.

        Args:
            symbol: Stock ticker symbol.

        Returns:
            JSON string with stock information.

        Raises:
            TickerNotFoundError: If the ticker is invalid.
            YFinanceAPIError: If the API request fails.
        """
        ticker = self._get_ticker(symbol)

        try:
            info = ticker.info

            # Return the full info dictionary
            result = {
                "symbol": symbol,
                "info": info,
            }

            return json.dumps(result, indent=2, default=str)
        except Exception as e:
            if isinstance(e, TickerNotFoundError):
                raise
            raise YFinanceAPIError(str(e), symbol) from e

    # ========== FINANCIAL STATEMENTS (Methods 8-10) ==========

    def get_income_statement(self, symbol: str, freq: FrequencyType = "yearly") -> str:
        """Get income statement for a stock.

        Args:
            symbol: Stock ticker symbol.
            freq: Frequency - "yearly" or "quarterly".

        Returns:
            JSON string with income statement data.

        Raises:
            TickerNotFoundError: If the ticker is invalid.
            DataNotAvailableError: If no income statement data is available.
            InvalidParameterError: If frequency is invalid.
            YFinanceAPIError: If the API request fails.
        """
        ticker = self._get_ticker(symbol)

        try:
            if freq == "yearly":
                income_stmt = ticker.financials
            elif freq == "quarterly":
                income_stmt = ticker.quarterly_financials
            else:
                raise InvalidParameterError("freq", freq, ["yearly", "quarterly"])

            if income_stmt is None or income_stmt.empty:
                raise DataNotAvailableError(f"{freq} income statement", symbol)

            # Convert to JSON-serializable format
            income_stmt = format_dataframe_dates(income_stmt.T)
            data = income_stmt.to_dict(orient="index")

            result = {
                "symbol": symbol,
                "frequency": freq,
                "income_statement": data,
            }

            return json.dumps(result, indent=2, default=str)
        except Exception as e:
            if isinstance(e, (DataNotAvailableError, TickerNotFoundError, InvalidParameterError)):
                raise
            raise YFinanceAPIError(str(e), symbol) from e

    def get_balance_sheet(self, symbol: str, freq: FrequencyType = "yearly") -> str:
        """Get balance sheet for a stock.

        Args:
            symbol: Stock ticker symbol.
            freq: Frequency - "yearly" or "quarterly".

        Returns:
            JSON string with balance sheet data.

        Raises:
            TickerNotFoundError: If the ticker is invalid.
            DataNotAvailableError: If no balance sheet data is available.
            InvalidParameterError: If frequency is invalid.
            YFinanceAPIError: If the API request fails.
        """
        ticker = self._get_ticker(symbol)

        try:
            if freq == "yearly":
                balance_sheet = ticker.balance_sheet
            elif freq == "quarterly":
                balance_sheet = ticker.quarterly_balance_sheet
            else:
                raise InvalidParameterError("freq", freq, ["yearly", "quarterly"])

            if balance_sheet is None or balance_sheet.empty:
                raise DataNotAvailableError(f"{freq} balance sheet", symbol)

            # Convert to JSON-serializable format
            balance_sheet = format_dataframe_dates(balance_sheet.T)
            data = balance_sheet.to_dict(orient="index")

            result = {
                "symbol": symbol,
                "frequency": freq,
                "balance_sheet": data,
            }

            return json.dumps(result, indent=2, default=str)
        except Exception as e:
            if isinstance(e, (DataNotAvailableError, TickerNotFoundError, InvalidParameterError)):
                raise
            raise YFinanceAPIError(str(e), symbol) from e

    def get_cashflow(self, symbol: str, freq: FrequencyType = "yearly") -> str:
        """Get cash flow statement for a stock.

        Args:
            symbol: Stock ticker symbol.
            freq: Frequency - "yearly" or "quarterly".

        Returns:
            JSON string with cash flow data.

        Raises:
            TickerNotFoundError: If the ticker is invalid.
            DataNotAvailableError: If no cash flow data is available.
            InvalidParameterError: If frequency is invalid.
            YFinanceAPIError: If the API request fails.
        """
        ticker = self._get_ticker(symbol)

        try:
            if freq == "yearly":
                cashflow = ticker.cashflow
            elif freq == "quarterly":
                cashflow = ticker.quarterly_cashflow
            else:
                raise InvalidParameterError("freq", freq, ["yearly", "quarterly"])

            if cashflow is None or cashflow.empty:
                raise DataNotAvailableError(f"{freq} cash flow", symbol)

            # Convert to JSON-serializable format
            cashflow = format_dataframe_dates(cashflow.T)
            data = cashflow.to_dict(orient="index")

            result = {
                "symbol": symbol,
                "frequency": freq,
                "cashflow": data,
            }

            return json.dumps(result, indent=2, default=str)
        except Exception as e:
            if isinstance(e, (DataNotAvailableError, TickerNotFoundError, InvalidParameterError)):
                raise
            raise YFinanceAPIError(str(e), symbol) from e

    # ========== HOLDERS & OWNERSHIP (Method 11) ==========

    def get_holder_info(self, symbol: str, holder_type: HolderInfoType) -> str:
        """Get holder and ownership information for a stock.

        Args:
            symbol: Stock ticker symbol.
            holder_type: Type of holder information to retrieve. Options:
                - "major_holders": Major shareholders overview
                - "institutional_holders": Institutional ownership details
                - "mutualfund_holders": Mutual fund ownership details
                - "insider_transactions": Insider trading activity
                - "insider_purchases": Insider purchase transactions
                - "insider_roster_holders": Current insider roster

        Returns:
            JSON string with holder information.

        Raises:
            TickerNotFoundError: If the ticker is invalid.
            DataNotAvailableError: If holder data is not available.
            InvalidParameterError: If holder_type is invalid.
            YFinanceAPIError: If the API request fails.
        """
        ticker = self._get_ticker(symbol)

        try:
            # Map holder_type to ticker attribute
            holder_data = None

            if holder_type == "major_holders":
                holder_data = ticker.major_holders
            elif holder_type == "institutional_holders":
                holder_data = ticker.institutional_holders
            elif holder_type == "mutualfund_holders":
                holder_data = ticker.mutualfund_holders
            elif holder_type == "insider_transactions":
                holder_data = ticker.insider_transactions
            elif holder_type == "insider_purchases":
                holder_data = ticker.insider_purchases
            elif holder_type == "insider_roster_holders":
                holder_data = ticker.insider_roster_holders
            else:
                valid_types = [
                    "major_holders",
                    "institutional_holders",
                    "mutualfund_holders",
                    "insider_transactions",
                    "insider_purchases",
                    "insider_roster_holders",
                ]
                raise InvalidParameterError("holder_type", holder_type, valid_types)

            if holder_data is None or (isinstance(holder_data, pd.DataFrame) and holder_data.empty):
                raise DataNotAvailableError(f"{holder_type} data", symbol)

            # Convert DataFrame to JSON-serializable format
            if isinstance(holder_data, pd.DataFrame):
                # Format dates if present in index or columns
                holder_data = format_dataframe_dates(holder_data)

                # Convert to dictionary
                data = holder_data.to_dict(orient="records")
            else:
                data = holder_data

            result = {
                "symbol": symbol,
                "holder_type": holder_type,
                "data": data,
            }

            return json.dumps(result, indent=2, default=str)
        except Exception as e:
            if isinstance(e, (DataNotAvailableError, TickerNotFoundError, InvalidParameterError)):
                raise
            raise YFinanceAPIError(str(e), symbol) from e

    # ========== OPTIONS (Methods 12-13) ==========

    def get_option_expiration_dates(self, symbol: str) -> str:
        """Get available option expiration dates for a stock.

        Args:
            symbol: Stock ticker symbol.

        Returns:
            JSON string with list of expiration dates.

        Raises:
            TickerNotFoundError: If the ticker is invalid.
            DataNotAvailableError: If no options data is available.
            YFinanceAPIError: If the API request fails.
        """
        ticker = self._get_ticker(symbol)

        try:
            expiration_dates = ticker.options

            if not expiration_dates:
                raise DataNotAvailableError("option expiration dates", symbol)

            result = {
                "symbol": symbol,
                "expiration_dates": list(expiration_dates),
            }

            return json.dumps(result, indent=2)
        except Exception as e:
            if isinstance(e, (DataNotAvailableError, TickerNotFoundError)):
                raise
            raise YFinanceAPIError(str(e), symbol) from e

    def get_option_chain(
        self, symbol: str, expiration_date: str, option_type: OptionChainType = "both"
    ) -> str:
        """Get option chain data for a specific expiration date.

        Args:
            symbol: Stock ticker symbol.
            expiration_date: Option expiration date (YYYY-MM-DD format).
            option_type: Type of options - "calls", "puts", or "both".

        Returns:
            JSON string with option chain data.

        Raises:
            TickerNotFoundError: If the ticker is invalid.
            DataNotAvailableError: If no options data is available.
            InvalidParameterError: If option_type or expiration_date is invalid.
            YFinanceAPIError: If the API request fails.
        """
        ticker = self._get_ticker(symbol)

        try:
            # Verify expiration date exists
            if expiration_date not in ticker.options:
                raise InvalidParameterError("expiration_date", expiration_date, list(ticker.options))

            # Get option chain
            opt_chain = ticker.option_chain(expiration_date)

            result: dict[str, Any] = {
                "symbol": symbol,
                "expiration_date": expiration_date,
                "option_type": option_type,
            }

            # Process based on option_type
            if option_type == "calls":
                if opt_chain.calls.empty:
                    raise DataNotAvailableError("calls option chain", symbol)
                result["calls"] = opt_chain.calls.to_dict(orient="records")
            elif option_type == "puts":
                if opt_chain.puts.empty:
                    raise DataNotAvailableError("puts option chain", symbol)
                result["puts"] = opt_chain.puts.to_dict(orient="records")
            elif option_type == "both":
                if opt_chain.calls.empty and opt_chain.puts.empty:
                    raise DataNotAvailableError("option chain", symbol)
                result["calls"] = opt_chain.calls.to_dict(orient="records") if not opt_chain.calls.empty else []
                result["puts"] = opt_chain.puts.to_dict(orient="records") if not opt_chain.puts.empty else []
            else:
                raise InvalidParameterError("option_type", option_type, ["calls", "puts", "both"])

            return json.dumps(result, indent=2, default=str)
        except Exception as e:
            if isinstance(e, (DataNotAvailableError, TickerNotFoundError, InvalidParameterError)):
                raise
            raise YFinanceAPIError(str(e), symbol) from e

    # ========== NEWS & ANALYSIS (Methods 14-16) ==========

    def get_news(self, symbol: str) -> str:
        """Get recent news articles for a stock.

        Args:
            symbol: Stock ticker symbol.

        Returns:
            JSON string with news articles.

        Raises:
            TickerNotFoundError: If the ticker is invalid.
            DataNotAvailableError: If no news data is available.
            YFinanceAPIError: If the API request fails.
        """
        ticker = self._get_ticker(symbol)

        try:
            news = ticker.news

            if not news:
                raise DataNotAvailableError("news", symbol)

            # Format news data with validation
            formatted_news = []
            skipped_count = 0

            for article in news:
                # Validate required fields (title and link must exist and not be empty)
                title = article.get("title", "").strip()
                link = article.get("link", "").strip()

                if not title or not link:
                    skipped_count += 1
                    continue  # Skip articles with incomplete data

                formatted_article = {
                    "title": title,
                    "publisher": article.get("publisher", "Unknown"),
                    "link": link,
                }

                # Handle timestamp conversion
                if "providerPublishTime" in article:
                    formatted_article["published_date"] = datetime.fromtimestamp(
                        article["providerPublishTime"], tz=timezone.utc
                    ).isoformat()

                # Add thumbnail if available
                if "thumbnail" in article and article["thumbnail"]:
                    resolutions = article["thumbnail"].get("resolutions", [])
                    if resolutions:
                        formatted_article["thumbnail"] = resolutions[0].get("url", "")

                formatted_news.append(formatted_article)

            # Log warning if articles were skipped
            if skipped_count > 0:
                logger.warning(
                    f"Skipped {skipped_count} news article(s) with incomplete data for {symbol}"
                )

            # If no valid articles, log warning and return empty list
            if not formatted_news:
                logger.warning(
                    f"No valid news articles available for {symbol} (all articles had incomplete data)"
                )

            result = {
                "symbol": symbol,
                "news": formatted_news,
            }

            return json.dumps(result, indent=2)
        except Exception as e:
            if isinstance(e, (DataNotAvailableError, TickerNotFoundError)):
                raise
            raise YFinanceAPIError(str(e), symbol) from e

    def get_recommendations(
        self,
        symbol: str,
        recommendation_type: RecommendationInfoType = "recommendations",
        months_back: int = 12,
    ) -> str:
        """Get analyst recommendations for a stock.

        Args:
            symbol: Stock ticker symbol.
            recommendation_type: Type of recommendations - "recommendations" or "upgrades_downgrades".
            months_back: Number of months of historical recommendations to retrieve.

        Returns:
            JSON string with analyst recommendations.

        Raises:
            TickerNotFoundError: If the ticker is invalid.
            DataNotAvailableError: If no recommendations data is available.
            InvalidParameterError: If recommendation_type is invalid.
            YFinanceAPIError: If the API request fails.
        """
        ticker = self._get_ticker(symbol)

        try:
            if recommendation_type == "recommendations":
                recommendations = ticker.recommendations
            elif recommendation_type == "upgrades_downgrades":
                recommendations = ticker.upgrades_downgrades
            else:
                raise InvalidParameterError(
                    "recommendation_type", recommendation_type, ["recommendations", "upgrades_downgrades"]
                )

            if recommendations is None or recommendations.empty:
                raise DataNotAvailableError(f"{recommendation_type} data", symbol)

            # Format dates in index
            recommendations = format_dataframe_dates(recommendations)

            # Filter by months_back if applicable
            if months_back and isinstance(recommendations.index, pd.Index):
                # Limit to requested number of records
                recommendations = recommendations.tail(months_back * 4)  # Approximate weekly recommendations

            # Convert to dictionary
            data = recommendations.to_dict(orient="records")

            result = {
                "symbol": symbol,
                "recommendation_type": recommendation_type,
                "months_back": months_back,
                "data": data,
            }

            return json.dumps(result, indent=2, default=str)
        except Exception as e:
            if isinstance(e, (DataNotAvailableError, TickerNotFoundError, InvalidParameterError)):
                raise
            raise YFinanceAPIError(str(e), symbol) from e

    def get_earning_dates(self, symbol: str, limit: int = 12) -> str:
        """Get upcoming and historical earnings dates for a stock.

        Args:
            symbol: Stock ticker symbol.
            limit: Maximum number of earnings dates to retrieve.

        Returns:
            JSON string with earnings dates.

        Raises:
            TickerNotFoundError: If the ticker is invalid.
            DataNotAvailableError: If no earnings date data is available.
            YFinanceAPIError: If the API request fails.
        """
        ticker = self._get_ticker(symbol)

        try:
            earnings_dates = ticker.earnings_dates

            if earnings_dates is None or earnings_dates.empty:
                raise DataNotAvailableError("earnings dates", symbol)

            # Format dates in index
            earnings_dates = format_dataframe_dates(earnings_dates)

            # Limit results
            if limit:
                earnings_dates = earnings_dates.head(limit)

            # Convert to dictionary
            data = earnings_dates.to_dict(orient="index")

            result = {
                "symbol": symbol,
                "limit": limit,
                "earnings_dates": data,
            }

            return json.dumps(result, indent=2, default=str)
        except Exception as e:
            if isinstance(e, (DataNotAvailableError, TickerNotFoundError)):
                raise
            raise YFinanceAPIError(str(e), symbol) from e

    # ========== BONUS TOOLS (Methods 17-18) ==========

    def get_stock_splits(self, symbol: str) -> str:
        """Get stock split history for a stock.

        Args:
            symbol: Stock ticker symbol.

        Returns:
            JSON string with stock split history.

        Raises:
            TickerNotFoundError: If the ticker is invalid.
            DataNotAvailableError: If no stock split data is available.
            YFinanceAPIError: If the API request fails.
        """
        ticker = self._get_ticker(symbol)

        try:
            splits = ticker.splits

            if splits.empty:
                raise DataNotAvailableError("stock splits", symbol)

            # Format dates
            splits_df = splits.to_frame(name="split_ratio")
            splits_df = format_dataframe_dates(splits_df)

            # Convert to list of records
            data = [{"date": str(date), "split_ratio": safe_float(row["split_ratio"])} for date, row in splits_df.iterrows()]

            result = {
                "symbol": symbol,
                "splits": data,
            }

            return json.dumps(result, indent=2)
        except Exception as e:
            if isinstance(e, (DataNotAvailableError, TickerNotFoundError)):
                raise
            raise YFinanceAPIError(str(e), symbol) from e

    def get_analyst_price_targets(self, symbol: str) -> str:
        """Get analyst price targets and recommendations summary.

        Args:
            symbol: Stock ticker symbol.

        Returns:
            JSON string with analyst price targets.

        Raises:
            TickerNotFoundError: If the ticker is invalid.
            DataNotAvailableError: If no analyst target data is available.
            YFinanceAPIError: If the API request fails.
        """
        ticker = self._get_ticker(symbol)

        try:
            info = ticker.info

            # Extract analyst price target information
            price_targets = {
                "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
                "target_high_price": info.get("targetHighPrice"),
                "target_low_price": info.get("targetLowPrice"),
                "target_mean_price": info.get("targetMeanPrice"),
                "target_median_price": info.get("targetMedianPrice"),
                "recommendation_mean": info.get("recommendationMean"),
                "recommendation_key": info.get("recommendationKey"),
                "number_of_analyst_opinions": info.get("numberOfAnalystOpinions"),
            }

            # Check if any target data is available
            if all(v is None for k, v in price_targets.items() if k != "current_price"):
                raise DataNotAvailableError("analyst price targets", symbol)

            result = {
                "symbol": symbol,
                "price_targets": price_targets,
            }

            return json.dumps(result, indent=2, default=str)
        except Exception as e:
            if isinstance(e, (DataNotAvailableError, TickerNotFoundError)):
                raise
            raise YFinanceAPIError(str(e), symbol) from e

    def screen_stocks(
        self,
        predefined_query: str | None = None,
        filters: list[dict[str, Any]] | None = None,
        offset: int = 0,
        size: int = 25,
        sort_field: str | None = None,
        sort_ascending: bool | None = None,
    ) -> str:
        """Screen stocks using a predefined Yahoo screen or custom equity filters.

        Exactly one mode must be used: provide either predefined_query or filters.

        Predefined mode: pass a name such as day_gainers, day_losers,
        most_actives or aggressive_small_caps.

        Custom mode: pass filters, a list of condition objects (combined with AND).
        Each condition is {"operator": op, "field": name, "value": v}. Supported
        operators: eq, gt, lt, gte, lte (scalar value), is-in (value is a list of
        options), btwn (value is [low, high]), and the logical groups and/or,
        written {"operator": "and", "conditions": [...]} with at least 2 nested
        conditions (groups may be nested). Example: [{"operator": "and",
        "conditions": [{"operator": "gt", "field": "percentchange", "value": 3},
        {"operator": "eq", "field": "region", "value": "us"}]}]

        Args:
            predefined_query: Name of a predefined screen (predefined mode).
            filters: List of filter conditions as described above (custom mode).
            offset: Number of results to skip, must be >= 0.
            size: Number of results to return, between 1 and 250.
            sort_field: Field to sort by. Custom default is "ticker"; predefined screens keep their own default.
            sort_ascending: Sort ascending when true. Custom default is false.

        Returns:
            JSON string with start, count, total and quotes of the screen result.

        Raises:
            InvalidParameterError: If the mode, filters, size or offset are invalid.
            DataNotAvailableError: If the screener returns no result.
            YFinanceAPIError: If the API request fails.
        """
        has_predefined = predefined_query is not None
        has_filters = filters is not None
        if has_predefined == has_filters:
            raise InvalidParameterError(
                "predefined_query/filters",
                "both provided" if has_predefined else "neither provided",
                ["exactly one of predefined_query or filters"],
            )
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise InvalidParameterError("offset", offset, ["integer >= 0"])
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or not 1 <= size <= SCREENER_MAX_SIZE
        ):
            raise InvalidParameterError("size", size, [f"integer between 1 and {SCREENER_MAX_SIZE}"])
        if sort_field is not None and (not isinstance(sort_field, str) or not sort_field):
            raise InvalidParameterError("sort_field", sort_field, ["non-empty string"])

        kwargs: dict[str, Any] = {"offset": offset}

        if has_predefined:
            known = getattr(yf, "PREDEFINED_SCREENER_QUERIES", None)
            if (
                not isinstance(predefined_query, str)
                or not predefined_query
                or (known and predefined_query not in known)
            ):
                raise InvalidParameterError(
                    "predefined_query", predefined_query, sorted(known) if known else None
                )
            query: Any = predefined_query
            kwargs["count"] = size
        else:
            if isinstance(filters, dict):
                filters = [filters]
            if not isinstance(filters, list) or not filters:
                raise InvalidParameterError("filters", filters, ["non-empty list of conditions"])
            built = [
                _build_equity_query(f, path=f"filters[{i}]") for i, f in enumerate(filters)
            ]
            query = built[0] if len(built) == 1 else yf.EquityQuery("and", built)
            kwargs["size"] = size
            kwargs["sortField"] = sort_field or "ticker"
            kwargs["sortAsc"] = bool(sort_ascending)

        if has_predefined:
            if sort_field is not None:
                kwargs["sortField"] = sort_field
            if sort_ascending is not None:
                kwargs["sortAsc"] = sort_ascending

        try:
            response = yf.screen(query, **kwargs)
        except Exception as e:
            raise YFinanceAPIError(str(e)) from e

        if not isinstance(response, dict):
            raise DataNotAvailableError("screener results", predefined_query or "custom query")

        quotes = response.get("quotes") or []
        result = {
            "start": response.get("start", offset),
            "count": response.get("count", len(quotes)),
            "total": response.get("total"),
            "quotes": quotes,
        }
        return json.dumps(_json_safe(result), indent=2, allow_nan=False)
