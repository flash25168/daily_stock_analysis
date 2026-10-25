# -*- coding: utf-8 -*-
"""
AkShare fundamental adapter (fail-open).

This adapter intentionally uses capability probing against multiple AkShare
endpoint candidates. It should never raise to caller; partial data is allowed.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

_DIVIDEND_KEYWORD_MAP: Dict[str, List[str]] = {
    "per_share": [
        "每股派息",
        "每股现金红利",
        "每股分红",
        "每股派现",
        "派现(元/股)",
        "派息(元/股)",
        "税前派息(元/股)",
        "现金分红(税前)",
    ],
    "plan_text": [
        "分配方案",
        "分红方案",
        "实施方案",
        "派息方案",
        "方案",
        "预案",
        "方案说明",
    ],
    "ex_dividend_date": ["除权除息日", "除息日", "除权日", "除权除息", "除息日期"],
    "record_date": ["股权登记日", "登记日"],
    "announce_date": ["公告日期", "公告日", "实施公告日", "预案公告日"],
    "report_date": ["报告期", "报告日期", "截止日期", "统计截止日期"],
}


def _safe_float(value: Any) -> Optional[float]:
    """Best-effort float conversion."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            result = float(value)
            return result if math.isfinite(result) else None
        except (TypeError, ValueError):
            return None
    s = str(value).strip().replace(",", "").replace("%", "")
    if not s:
        return None
    try:
        result = float(s)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _safe_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        parsed = pd.to_datetime(value)
    except Exception:
        return None
    if pd.isna(parsed):
        return None
    try:
        return parsed.to_pydatetime()
    except Exception:
        return None


def _normalize_code(raw: Any) -> str:
    s = _safe_str(raw).upper()
    if "." in s:
        s = s.split(".", 1)[0]
    s = re.sub(r"^(SH|SZ|BJ)", "", s)
    return s


def _pick_by_keywords(row: pd.Series, keywords: List[str]) -> Optional[Any]:
    """
    Return first non-empty row value whose column name contains any keyword.
    """
    for col in row.index:
        col_s = str(col)
        if any(k in col_s for k in keywords):
            val = row.get(col)
            if val is not None and str(val).strip() not in ("", "-", "nan", "None"):
                return val
    return None


def _parse_dividend_plan_to_per_share(plan_text: str) -> Optional[float]:
    """Parse per-share cash dividend from Chinese plan text."""
    text = _safe_str(plan_text)
    if not text:
        return None

    for pattern in (
        r"(?:每)?\s*10\s*股?\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元",
        r"10\s*派\s*([0-9]+(?:\.[0-9]+)?)\s*元",
    ):
        match = re.search(pattern, text)
        if match:
            parsed = _safe_float(match.group(1))
            if parsed is not None and parsed > 0:
                return parsed / 10.0

    match_per_share = re.search(r"每\s*股\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元", text)
    if match_per_share:
        parsed = _safe_float(match_per_share.group(1))
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _extract_cash_dividend_per_share(row: pd.Series) -> Optional[float]:
    """Extract pre-tax cash dividend per share from a row."""
    plan_text = _safe_str(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["plan_text"]))
    # Keep pre-tax semantics; skip explicit after-tax plans unless pre-tax marker exists.
    if "税后" in plan_text and "税前" not in plan_text and "含税" not in plan_text:
        return None

    direct = _safe_float(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["per_share"]))
    if direct is not None and direct > 0:
        return direct
    return _parse_dividend_plan_to_per_share(plan_text)


def _filter_rows_by_code(
    df: pd.DataFrame, stock_code: str, *, require_code: bool = False,
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "symbol", "ts_code"))]
    if not code_cols:
        if require_code:
            raise ValueError("Market-wide result is missing a stock code column")
        return df

    target = _normalize_code(stock_code)
    for col in code_cols:
        try:
            series = df[col].astype(str).map(_normalize_code)
            filtered = df[series == target]
            if not filtered.empty:
                return filtered
        except Exception:
            continue
    return pd.DataFrame()


def _recent_report_dates(now: Optional[datetime] = None) -> List[str]:
    """Bound report-period fallback to the two most recently completed quarters."""
    now = now or datetime.now()
    quarter_start = datetime(now.year, (now.month - 1) // 3 * 3 + 1, 1)
    dates = []
    for _ in range(2):
        end = quarter_start - timedelta(days=1)
        dates.append(end.strftime("%Y%m%d"))
        quarter_start = datetime(end.year, (end.month - 1) // 3 * 3 + 1, 1)
    return dates


def _normalize_report_date(value: Any) -> Optional[str]:
    parsed = _safe_datetime(value)
    return parsed.date().isoformat() if parsed else None


def _earnings_forecast_summary(row: pd.Series) -> str:
    """Read disclosure text by exact column, never an announcement date/rate."""
    for column in ("业绩变动", "业绩变动原因", "预告类型", "预告", "摘要"):
        value = row.get(column)
        if isinstance(value, str) and value.strip().lower() not in ("", "-", "nan", "none", "nat", "<na>"):
            return value.strip()[:200]
    return ""


def _earnings_quick_summary(row: pd.Series) -> str:
    """Summarize AkShare's numeric quick-report columns, excluding metadata."""
    fields = (
        ("营业收入-营业收入", "营业收入", "元"),
        ("营业收入-同比增长", "营收同比", "%"),
        ("净利润-净利润", "净利润", "元"),
        ("净利润-同比增长", "净利润同比", "%"),
        ("每股收益", "每股收益", "元"),
        ("净资产收益率", "净资产收益率", "%"),
    )
    parts = []
    for column, label, unit in fields:
        value = _safe_float(row.get(column))
        if value is not None and math.isfinite(value):
            parts.append(f"{label}{value:.12g}{unit}")
    return "；".join(parts)[:200]


def _build_dividend_payload(
    dividend_df: pd.DataFrame,
    stock_code: str,
    max_events: int = 5,
) -> Dict[str, Any]:
    work_df = _filter_rows_by_code(dividend_df, stock_code)
    if work_df.empty:
        return {}

    now_date = datetime.now().date()
    ttm_start_date = now_date - timedelta(days=365)
    dedupe_keys = set()
    events: List[Dict[str, Any]] = []

    for _, row in work_df.iterrows():
        if not isinstance(row, pd.Series):
            continue
        ex_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["ex_dividend_date"]))
        record_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["record_date"]))
        announce_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["announce_date"]))
        event_dt = ex_dt or record_dt or announce_dt
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if event_date > now_date:
            continue

        per_share = _extract_cash_dividend_per_share(row)
        if per_share is None or per_share <= 0:
            continue

        dedupe_key = (event_date.isoformat(), round(per_share, 6))
        if dedupe_key in dedupe_keys:
            continue
        dedupe_keys.add(dedupe_key)

        events.append(
            {
                "event_date": event_date.isoformat(),
                "ex_dividend_date": ex_dt.date().isoformat() if ex_dt else None,
                "record_date": record_dt.date().isoformat() if record_dt else None,
                "announcement_date": announce_dt.date().isoformat() if announce_dt else None,
                "cash_dividend_per_share": round(per_share, 6),
                "is_pre_tax": True,
            }
        )

    if not events:
        return {}

    events.sort(key=lambda item: item.get("event_date") or "", reverse=True)
    ttm_events: List[Dict[str, Any]] = []
    for item in events:
        event_dt = _safe_datetime(item.get("event_date"))
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if ttm_start_date <= event_date <= now_date:
            ttm_events.append(item)

    return {
        "events": events[:max(1, max_events)],
        "ttm_event_count": len(ttm_events),
        "ttm_cash_dividend_per_share": (
            round(sum(float(item.get("cash_dividend_per_share") or 0.0) for item in ttm_events), 6)
            if ttm_events else None
        ),
        "coverage": "cash_dividend_pre_tax",
        "as_of": now_date.isoformat(),
    }


def _extract_latest_row(df: pd.DataFrame, stock_code: str) -> Optional[pd.Series]:
    """
    Select the most relevant row for the given stock.
    """
    if df is None or df.empty:
        return None

    code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "ts_code", "symbol"))]
    target = _normalize_code(stock_code)
    if code_cols:
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                matched = df[series == target]
                if not matched.empty:
                    return matched.iloc[0]
            except Exception:
                continue
        return None

    # Fallback: use latest row
    return df.iloc[0]


def _financial_abstract_row(df: pd.DataFrame) -> Optional[pd.Series]:
    """Normalize Sina's metric rows and report-period columns into one row."""
    periods = [c for c in df.columns if re.fullmatch(r"\d{8}", str(c)) and _normalize_report_date(str(c))]
    if not periods:
        return None
    period = max(periods, key=str)
    metrics = df.drop_duplicates(subset=["指标"]).set_index("指标")[period]
    # Include AkShare's unit-bearing labels; exact aliases keep percentages
    # separate from amounts and preserve qualifiers such as weighted ROE.
    aliases = {
        "营业总收入": ["营业总收入", "营业收入", "营业总收入(元)"],
        "归母净利润": ["归母净利润", "归属于母公司股东的净利润", "归属净利润(元)"],
        "经营活动产生的现金流量净额": [
            "经营活动产生的现金流量净额", "经营活动产生的现金流量净额(元)", "经营现金流量净额(元)", "经营现金流量净额",
        ],
        "营业收入同比": ["营业总收入同比增长率", "营业收入同比增长率", "营业总收入同比增长(%)", "营业总收入增长率"],
        "净利润同比": ["净利润同比增长率", "归母净利润同比增长率", "归属净利润同比增长(%)", "归属母公司净利润增长率"],
        "净资产收益率": ["净资产收益率", "净资产收益率(加权)", "净资产收益率(%)", "净资产收益率(加权)(%)", "净资产收益率(ROE)"],
        "毛利率": ["销售毛利率", "毛利率", "销售毛利率(%)"],
    }
    values = {"报告期": str(period)}
    for key, names in aliases.items():
        for name in names:
            value = _safe_float(metrics.get(name))
            if value is not None and math.isfinite(value):
                values[key] = value
                break
    return pd.Series(values)


class AkshareFundamentalAdapter:
    """AkShare adapter for fundamentals, capital flow and dragon-tiger signals."""

    def _cashflow_statement_em(self, stock_code: str, report_date: str) -> pd.DataFrame:
        """Fetch only the requested issuer/period, amounts in CNY (not per share)."""
        import requests

        response = requests.get('https://datacenter-web.eastmoney.com/api/data/v1/get',
            params={'reportName': 'RPT_DMSK_FN_CASHFLOW', 'columns': 'ALL',
                'filter': f'(SECURITY_CODE="{stock_code}")(REPORT_DATE=\'{report_date}\')',
                'pageNumber': 1, 'pageSize': 2, 'sortColumns': 'REPORT_DATE', 'sortTypes': -1},
            timeout=5)
        response.raise_for_status()
        rows = (response.json().get('result') or {}).get('data') or []
        frame = pd.DataFrame(rows)
        required = {'SECURITY_CODE', 'REPORT_DATE', 'NOTICE_DATE', 'NETCASH_OPERATE'}
        if not required.issubset(frame.columns):
            raise ValueError('cashflow: missing disclosure fields')
        periods = pd.to_datetime(frame.REPORT_DATE, errors='coerce', utc=True)
        notices = pd.to_datetime(frame.NOTICE_DATE, errors='coerce', utc=True)
        valid = (frame.SECURITY_CODE.astype(str).eq(stock_code)
            & periods.dt.strftime('%Y-%m-%d').eq(report_date)
            & periods.le(pd.Timestamp.now(tz='UTC')) & notices.le(pd.Timestamp.now(tz='UTC')))
        frame = frame.loc[valid].copy()
        if frame.empty:
            raise ValueError('cashflow: wrong issuer, period or future disclosure')
        return frame

    def _capital_history_em(self, stock: str, market: str) -> pd.DataFrame:
        """Bounded version of AkShare's Eastmoney history request (10 sessions)."""
        import json
        import time
        from urllib.error import HTTPError, URLError
        from urllib.parse import urlencode
        from urllib.request import Request, urlopen

        params = {'lmt': 10, 'klt': 101, 'secid': f'{1 if market == "sh" else 0}.{stock}',
                'fields1': 'f1,f2,f3,f7',
                'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65',
                'ut': 'b2884a393a59ad64002292a3e90d46a5'}
        request = Request('https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?' + urlencode(params),
            headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://data.eastmoney.com/'})
        deadline = time.monotonic() + 5
        for attempt in range(2):
            try:
                with urlopen(request, timeout=max(0.1, deadline - time.monotonic())) as response:
                    payload = json.load(response)
                break
            except (URLError, TimeoutError, ConnectionError) as exc:
                if (attempt or time.monotonic() >= deadline
                        or isinstance(exc, HTTPError) and exc.code < 500):
                    raise
                # One bounded transport retry for intermittent disconnects;
                # schema/JSON/permission failures do not trigger this retry.
                time.sleep(min(0.5, max(0, deadline - time.monotonic())))
        data = payload.get('data') or {}
        if payload.get('rc') != 0 or str(data.get('code')) != stock:
            raise ValueError('capital history: invalid response identity')
        records = []
        for line in data.get('klines') or []:
            fields = line.split(',')
            if len(fields) != 15:
                raise ValueError('capital history: invalid row shape')
            records.append({'日期': fields[0], '主力净流入-净额': fields[1]})
        frame = pd.DataFrame(records)
        frame.attrs['transport_retries'] = attempt
        return frame

    def _capital_snapshot_em(self, stock: str, market: str) -> pd.DataFrame:
        """Closed-session fallback using the same vendor's verified flow fields.

        f62/f164/f174 are CNY net amounts (day/5/10 sessions), not ratios.
        f124 is the quote update timestamp, so accept it only after the close
        on the calendar's expected session. A fresh fetch is not a fresh quote.
        """
        import json
        from datetime import timezone
        from urllib.parse import urlencode
        from urllib.request import Request, urlopen
        from src.core.trading_calendar import build_market_phase_context

        if market not in ('sh', 'sz'):
            return pd.DataFrame()
        clock = build_market_phase_context(market='cn')
        if clock.phase.value not in ('postmarket', 'premarket', 'non_trading') or any(
            warning in clock.warnings for warning in ('calendar_error', 'calendar_unavailable')
        ):
            raise ValueError('capital snapshot: reliable closed-session calendar required')
        market_id = 1 if market == 'sh' else 0
        params = {'secids': f'{market_id}.{stock}', 'fields': 'f12,f13,f62,f124,f164,f174',
                  'fltt': 2, 'invt': 2}
        request = Request('https://push2.eastmoney.com/api/qt/ulist.np/get?' + urlencode(params),
            headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://data.eastmoney.com/'})
        with urlopen(request, timeout=3) as response:
            payload = json.load(response)
        rows = (payload.get('data') or {}).get('diff') or []
        if payload.get('rc') != 0 or not isinstance(rows, list) or len(rows) != 1:
            raise ValueError('capital snapshot: invalid response shape')
        row = rows[0]
        if str(row.get('f12')) != stock or row.get('f13') != market_id:
            raise ValueError('capital snapshot: wrong security')
        timestamp = _safe_float(row.get('f124'))
        amount = _safe_float(row.get('f62'))
        if timestamp is None or amount is None or isinstance(row.get('f62'), bool):
            raise ValueError('capital snapshot: invalid timestamp or net amount')
        updated = datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8)))
        if (updated.date() != clock.effective_daily_bar_date or updated.hour < 15
                or updated > datetime.now(updated.tzinfo)):
            raise ValueError('capital snapshot: stale, future or unclosed quote')
        frame = pd.DataFrame([{'日期': updated.date().isoformat(), '主力净流入-净额': amount}])
        frame.attrs.update({'inflow_5d': _safe_float(row.get('f164')),
            'inflow_10d': _safe_float(row.get('f174')), 'window_source': 'provider_snapshot'})
        return frame

    def _call_df_candidates(
        self,
        candidates: List[Tuple[str, Dict[str, Any]]],
        *,
        stock_code: Optional[str] = None,
    ) -> Tuple[Optional[pd.DataFrame], Optional[str], List[str]]:
        errors: List[str] = []
        for func_name, kwargs in candidates:
            try:
                if func_name in ('_cashflow_statement_em', '_capital_history_em', '_capital_snapshot_em'):
                    fn = getattr(self, func_name)
                else:
                    import akshare as ak
                    fn = getattr(ak, func_name, None)
                if fn is None:
                    continue
                df = fn(**kwargs)
                if isinstance(df, pd.Series):
                    df = df.to_frame().T
                if isinstance(df, pd.DataFrame) and not df.empty and stock_code is not None:
                    # Market-wide endpoints must contain the requested stock.
                    # A nonempty table for other stocks must not stop fallback.
                    df = _filter_rows_by_code(df, stock_code, require_code=True)
                if isinstance(df, pd.DataFrame) and not df.empty:
                    if func_name == 'stock_institute_hold':
                        quarter = str(kwargs.get('symbol', ''))
                        if re.fullmatch(r'\d{4}[1-4]', quarter):
                            df.attrs['report_date'] = quarter[:4] + ('-03-31', '-06-30', '-09-30', '-12-31')[int(quarter[-1]) - 1]
                    elif func_name == 'stock_gdfx_top_10_em':
                        df.attrs['report_date'] = _normalize_report_date(kwargs.get('date'))
                    return df, func_name, errors
            except Exception as exc:
                errors.append(f"{func_name}:{type(exc).__name__}")
                continue
        return None, None, errors

    def get_fundamental_bundle(self, stock_code: str, *, candidate_runner=None) -> Dict[str, Any]:
        """
        Return normalized fundamental blocks from AkShare with partial tolerance.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "growth": {},
            "earnings": {},
            "institution": {},
            "source_chain": [],
            "errors": [],
        }

        # Reuse the provider's market mapping after package initialization;
        # importing it at module scope would cycle through data_provider.base.
        from .akshare_fetcher import _to_sina_tx_symbol

        shareholder_symbol = _to_sina_tx_symbol(stock_code)
        stock_code = _normalize_code(stock_code)
        report_dates = _recent_report_dates()
        call_candidates = candidate_runner or self._call_df_candidates

        # Financial indicators
        fin_df, fin_source, fin_errors = call_candidates([
            ("stock_financial_analysis_indicator_em", {
                "symbol": f"{stock_code}.{shareholder_symbol[:2].upper()}",
                "indicator": "按报告期",
            }),
            ("stock_financial_abstract", {"symbol": stock_code}),
            ("stock_financial_analysis_indicator", {"symbol": stock_code,
                "start_year": str(datetime.now().year - 1)}),
        ])
        result["errors"].extend(fin_errors)
        if fin_df is not None:
            if fin_source == "stock_financial_analysis_indicator_em":
                # This endpoint uses English field names and yuan units.
                # Validate identity/period before translating into the existing contract.
                if not {'SECUCODE', 'REPORT_DATE'}.issubset(fin_df.columns):
                    fin_df = pd.DataFrame()
                    result['errors'].append('stock_financial_analysis_indicator_em:invalid identity or report date')
                else:
                    dates = pd.to_datetime(fin_df['REPORT_DATE'], errors='coerce', utc=True)
                    matched = fin_df['SECUCODE'].astype(str).map(_normalize_code).eq(stock_code)
                    valid = matched & dates.notna() & dates.le(pd.Timestamp.now(tz='UTC'))
                    if 'NOTICE_DATE' in fin_df:
                        notices = pd.to_datetime(fin_df['NOTICE_DATE'], errors='coerce', utc=True)
                        valid &= notices.notna() & notices.le(pd.Timestamp.now(tz='UTC'))
                    fin_df = fin_df.loc[valid].copy()
                    fin_df['_report_sort'] = dates.loc[valid]
                    fin_df = fin_df.sort_values('_report_sort', ascending=False).rename(columns={
                        'REPORT_DATE': '报告期', 'TOTALOPERATEREVETZ': '营业收入同比',
                        'PARENTNETPROFITTZ': '净利润同比', 'ROEJQ': '净资产收益率',
                        'XSMLL': '毛利率', 'TOTALOPERATEREVE': '营业总收入',
                        'PARENTNETPROFIT': '归母净利润', 'NETCASH_OPERATE': '经营活动产生的现金流量净额',
                    })
            row = _financial_abstract_row(fin_df) if "指标" in fin_df else _extract_latest_row(fin_df, stock_code)
            if row is not None:
                revenue_yoy = _safe_float(_pick_by_keywords(row, ["营业收入同比", "营收同比", "收入同比", "同比增长"]))
                profit_yoy = _safe_float(_pick_by_keywords(row, ["净利润同比", "净利同比", "归母净利润同比"]))
                roe = _safe_float(_pick_by_keywords(row, ["净资产收益率", "ROE", "净资产收益"]))
                gross_margin = _safe_float(_pick_by_keywords(row, ["毛利率"]))
                report_date = _normalize_report_date(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["report_date"]))
                is_wide = "指标" in fin_df or fin_source == 'stock_financial_analysis_indicator_em'
                revenue = _safe_float(
                    row.get("营业总收入") if is_wide else _pick_by_keywords(row, ["营业总收入", "营业收入", "营收"])
                )
                net_profit_parent = _safe_float(
                    row.get("归母净利润") if is_wide else _pick_by_keywords(row, ["归母净利润", "母公司股东净利润", "净利润"])
                )
                operating_cash_flow = _safe_float(
                    _pick_by_keywords(row, ["经营活动产生的现金流量净额", "经营现金流", "经营活动现金流"])
                )
                growth_payload = {
                    "revenue_yoy": revenue_yoy,
                    "net_profit_yoy": profit_yoy,
                    "roe": roe,
                    "gross_margin": gross_margin,
                }
                financial_report_payload = {
                    "report_date": report_date,
                    "revenue": revenue,
                    "net_profit_parent": net_profit_parent,
                    "operating_cash_flow": operating_cash_flow,
                    "roe": roe,
                }
                if any(v is not None for k, v in financial_report_payload.items() if k != "report_date"):
                    result["earnings"]["financial_report"] = financial_report_payload
                if any(v is not None for v in growth_payload.values()):
                    result["growth"] = growth_payload
                if result["growth"] or result["earnings"].get("financial_report"):
                    result["source_chain"].append(f"growth:{fin_source}")

        # The indicator endpoint omits cash flow totals. Supplement the exact
        # same disclosed period; never substitute cash flow per share or TTM.
        financial = result['earnings'].get('financial_report', {})
        cash_candidates = []
        if financial.get('report_date') and financial.get('operating_cash_flow') is None:
            cash_candidates = [('_cashflow_statement_em', {
                'stock_code': stock_code, 'report_date': financial['report_date']})]
        cash_df, cash_source, cash_errors = call_candidates(cash_candidates)
        result['errors'].extend(cash_errors)
        if cash_df is not None and not cash_df.empty:
            cash = _safe_float(cash_df.iloc[0].get('NETCASH_OPERATE'))
            if cash is not None:
                financial['operating_cash_flow'] = cash
                result['source_chain'].append(f'cashflow:{cash_source}')

        # Prioritize holdings before optional forecasts/dividends. Once the
        # issuer's statement period is known, do not spend the budget querying
        # an undisclosed quarter first or silently mix reporting periods.
        institution_dates = ([financial['report_date'].replace('-', '')]
            if financial.get('report_date') else report_dates)
        # Institution / top shareholders
        inst_df, inst_source, inst_errors = call_candidates([
            ("stock_institute_hold", {
                "symbol": f"{report_date[:4]}{int(report_date[4:6]) // 3}",
            })
            for report_date in institution_dates
        ], stock_code=stock_code)
        result["errors"].extend(inst_errors)
        if inst_df is not None:
            row = _extract_latest_row(inst_df, stock_code)
            if row is not None:
                # Institution count changes are not holding-ratio changes.
                inst_change = _safe_float(row.get('持股比例增幅'))
                result["institution"].update({
                    "report_date": inst_df.attrs.get('report_date'),
                    "institution_holding_change": inst_change,
                    "holding_change_metric": "持股比例增幅（来源原口径，%）",
                    "institution_count": _safe_float(row.get('机构数')),
                    "institution_count_change": _safe_float(row.get('机构数变化')),
                    "holding_ratio_pct": _safe_float(row.get('持股比例')),
                })
                result["source_chain"].append(f"institution:{inst_source}")

        top10_df, top10_source, top10_errors = call_candidates([
            ("stock_gdfx_top_10_em", {"symbol": shareholder_symbol, "date": report_date})
            for report_date in institution_dates
        ])
        result["errors"].extend(top10_errors)
        if top10_df is not None:
            row = _extract_latest_row(top10_df, stock_code)
            if row is not None:
                # One shareholder's change cannot represent the entire top ten.
                result['institution']['top10_holder_count'] = len(top10_df)
                result['institution']['top10_report_date'] = top10_df.attrs.get('report_date')
                result['institution']['top10_holder_change'] = None
                result['institution']['top10_change_note'] = '未取得可比较的两期完整股东名单，合计变化未知'
                result["source_chain"].append(f"top10:{top10_source}")

        # Earnings forecast
        forecast_df, forecast_source, forecast_errors = call_candidates([
            ("stock_yjyg_em", {"date": report_date})
            for report_date in report_dates
        ], stock_code=stock_code)
        result["errors"].extend(forecast_errors)
        if forecast_df is not None:
            row = _extract_latest_row(forecast_df, stock_code)
            if row is not None:
                summary = _earnings_forecast_summary(row)
                if summary:
                    result["earnings"]["forecast_summary"] = summary
                    result["source_chain"].append(f"earnings_forecast:{forecast_source}")

        # Earnings quick report
        quick_df, quick_source, quick_errors = call_candidates([
            ("stock_yjkb_em", {"date": report_date})
            for report_date in report_dates
        ], stock_code=stock_code)
        result["errors"].extend(quick_errors)
        if quick_df is not None:
            row = _extract_latest_row(quick_df, stock_code)
            if row is not None:
                summary = _earnings_quick_summary(row)
                if summary:
                    result["earnings"]["quick_report_summary"] = summary
                    result["source_chain"].append(f"earnings_quick:{quick_source}")

        # Dividend details (cash dividend, pre-tax)
        dividend_df, dividend_source, dividend_errors = call_candidates([
            ("stock_fhps_detail_em", {"symbol": stock_code}),
            ("stock_history_dividend_detail", {"symbol": stock_code, "indicator": "分红", "date": ""}),
            ("stock_dividend_cninfo", {"symbol": stock_code}),
        ])
        result["errors"].extend(dividend_errors)
        if dividend_df is not None:
            dividend_payload = _build_dividend_payload(dividend_df, stock_code, max_events=5)
            if dividend_payload:
                result["earnings"]["dividend"] = dividend_payload
                result["source_chain"].append(f"dividend:{dividend_source}")

        has_content = bool(result["growth"] or result["earnings"] or result["institution"])
        result["status"] = "partial" if has_content else ("failed" if result["errors"] else "not_supported")
        return result

    def get_capital_flow(self, stock_code: str, top_n: int = 5) -> Dict[str, Any]:
        """
        Return stock + sector capital flow.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "stock_flow": {},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": [],
            "errors": [],
        }

        from .akshare_fetcher import _to_sina_tx_symbol

        market = _to_sina_tx_symbol(stock_code)[:2]
        stock_code = _normalize_code(stock_code)
        stock_df, stock_source, stock_errors = self._call_df_candidates([
            ("_capital_history_em", {"stock": stock_code, "market": market}),
            ("_capital_snapshot_em", {"stock": stock_code, "market": market}),
            ("stock_individual_fund_flow", {"stock": stock_code, "market": market}),
        ])
        result["errors"].extend(stock_errors)
        if stock_df is not None and "日期" in stock_df:
            dates = pd.to_datetime(stock_df["日期"], errors="coerce")
            stock_df = stock_df.loc[dates.notna() & dates.le(pd.Timestamp.now().normalize())].copy()
            stock_df['_date'] = dates.loc[stock_df.index]
            stock_df = stock_df.sort_values('_date').drop_duplicates('_date')
            dates = stock_df['_date']
            if dates.notna().any():
                row = stock_df.loc[dates.idxmax()]
                net_inflow = _safe_float(row.get("主力净流入-净额"))
                if net_inflow is not None and math.isfinite(net_inflow):
                    result["stock_flow"] = {
                        **({'fetch_retries': stock_df.attrs['transport_retries']}
                           if stock_df.attrs.get('transport_retries') else {}),
                        **({'window_source': stock_df.attrs['window_source']}
                           if stock_df.attrs.get('window_source') else {}),
                        "trade_date": dates.max().date().isoformat(),
                        "currency": "CNY",
                        "main_net_inflow": net_inflow,
                        "inflow_5d": self._capital_window_sum(stock_df, 5),
                        "inflow_10d": self._capital_window_sum(stock_df, 10),
                    }
                    result["source_chain"].append(f"capital_stock:{stock_source}")

        sector_df, sector_source, sector_errors = self._call_df_candidates([
            ("stock_sector_fund_flow_rank", {}),
        ])
        result["errors"].extend(sector_errors)
        if sector_df is not None:
            name_col = next((c for c in sector_df.columns if any(k in str(c) for k in ("板块", "行业", "名称", "name"))), None)
            flow_col = "今日主力净流入-净额" if "今日主力净流入-净额" in sector_df else None
            if name_col and flow_col:
                work_df = sector_df[[name_col, flow_col]].copy()
                work_df[flow_col] = pd.to_numeric(work_df[flow_col], errors="coerce")
                work_df = work_df.loc[work_df[flow_col].between(-math.inf, math.inf, inclusive="neither")]
                top_df = work_df.nlargest(top_n, flow_col)
                bottom_df = work_df.nsmallest(top_n, flow_col)
                result["sector_rankings"] = {
                    "top": [{"name": _safe_str(r[name_col]), "net_inflow": float(r[flow_col])} for _, r in top_df.iterrows()],
                    "bottom": [{"name": _safe_str(r[name_col]), "net_inflow": float(r[flow_col])} for _, r in bottom_df.iterrows()],
                }
                result["source_chain"].append(f"capital_sector:{sector_source}")

        has_content = bool(result["stock_flow"] or result["sector_rankings"]["top"] or result["sector_rankings"]["bottom"])
        result["status"] = "partial" if has_content else ("failed" if result["errors"] else "not_supported")
        return result

    @staticmethod
    def _capital_window_sum(frame: pd.DataFrame, sessions: int) -> Optional[float]:
        key = f'inflow_{sessions}d'
        if key in frame.attrs:
            return _safe_float(frame.attrs[key])
        values = [_safe_float(v) for v in frame['主力净流入-净额'].tail(sessions)]
        return sum(values) if len(values) == sessions and all(v is not None for v in values) else None

    def get_dragon_tiger_flag(self, stock_code: str, lookback_days: int = 20) -> Dict[str, Any]:
        """
        Return dragon-tiger signal in lookback window.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "is_on_list": False,
            "recent_count": 0,
            "latest_date": None,
            "source_chain": [],
            "errors": [],
        }

        df, source, errors = self._call_df_candidates([
            ("stock_lhb_stock_statistic_em", {}),
            ("stock_lhb_detail_em", {}),
            ("stock_lhb_jgmmtj_em", {}),
        ])
        result["errors"].extend(errors)
        if df is None:
            return result

        # Try code filter
        code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码"))]
        target = _normalize_code(stock_code)
        matched = pd.DataFrame()
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                cur = df[series == target]
                if not cur.empty:
                    matched = cur
                    break
            except Exception:
                continue
        if matched.empty:
            result["source_chain"].append(f"dragon_tiger:{source}")
            result["status"] = "ok" if code_cols else "partial"
            return result

        date_col = next((c for c in matched.columns if any(k in str(c) for k in ("日期", "上榜", "交易日", "time"))), None)
        parsed_dates: List[datetime] = []
        if date_col is not None:
            for val in matched[date_col].astype(str).tolist():
                try:
                    parsed_dates.append(pd.to_datetime(val).to_pydatetime())
                except Exception:
                    continue
        now = datetime.now()
        start = now - timedelta(days=max(1, lookback_days))
        recent_dates = [d for d in parsed_dates if start <= d <= now]

        result["is_on_list"] = bool(recent_dates)
        result["recent_count"] = len(recent_dates) if recent_dates else int(len(matched))
        result["latest_date"] = max(recent_dates).date().isoformat() if recent_dates else (
            max(parsed_dates).date().isoformat() if parsed_dates else None
        )
        result["status"] = "ok"
        result["source_chain"].append(f"dragon_tiger:{source}")
        return result
