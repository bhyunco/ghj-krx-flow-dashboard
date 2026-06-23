import json
import os
import re
import threading
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError

import pandas as pd
from flask import Flask, flash, redirect, render_template_string, request as flask_request, send_file, session, url_for
from markupsafe import escape


GET_JSON_URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
LOGIN_PAGE_URL = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001.cmd"
LOGIN_JSP_URL = "https://data.krx.co.kr/contents/MDC/COMS/client/view/login.jsp?site=mdc"
LOGIN_URL = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001D1.cmd"
REFERER_URL = "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201020303"

INVESTOR_CODES = {
    "기관합계": "7050",
    "외국인": "9000",
}

MARKET_CODES = {
    "ALL": "ALL",
    "KOSPI": "STK",
    "KOSDAQ": "KSQ",
    "KONEX": "KNX",
}

BASE_COLUMNS = [
    "종목코드",
    "종목명",
    "거래량_매도",
    "거래량_매수",
    "거래량_순매수",
    "거래대금_매도",
    "거래대금_매수",
    "거래대금_순매수",
]

METADATA_COLUMNS = [
    "d_today_year",
    "d_today_month",
    "d_today_day",
    "period(D-00)_start",
    "period(D-00)_end",
    "buyer",
]

OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", "/tmp/ghj-krx-outputs" if os.environ.get("VERCEL") else "outputs"))
DOC_PATH = Path("프로그램_상세설명.md")
RECENT_TRADING_DAYS = 5
MAX_CALENDAR_LOOKBACK = 20
REQUEST_TIMEOUT = 30
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_STORAGE_BUCKET = os.environ.get("SUPABASE_STORAGE_BUCKET", "krx-results")
SAVE_QUERY_ROWS = os.environ.get("SAVE_QUERY_ROWS", "true").lower() != "false"


@dataclass(frozen=True)
class FetchJob:
    buyer: str
    start_date: date
    end_date: date
    period_start: int
    period_end: int
    label: str


@dataclass(frozen=True)
class RunResult:
    output_path: Path
    base_rows: int
    last_rows: int
    trading_dates: list[str]
    charts: list[dict[str, str]]
    top_rows: list[dict[str, Any]]
    job_id: str | None = None
    storage_path: str | None = None


class KrxInvestorApi:
    def __init__(self, krx_id: str, krx_pw: str, timeout: int = 30):
        self.krx_id = krx_id
        self.krx_pw = krx_pw
        self.timeout = timeout
        self.cookie_jar = CookieJar()
        self.opener = request.build_opener(request.HTTPCookieProcessor(self.cookie_jar))
        self.login()

    def login(self) -> None:
        self._get(LOGIN_PAGE_URL, {"User-Agent": "Mozilla/5.0"})
        self._get(LOGIN_JSP_URL, {"User-Agent": "Mozilla/5.0", "Referer": LOGIN_PAGE_URL})

        payload = {
            "mbrNm": "",
            "telNo": "",
            "di": "",
            "certType": "",
            "mbrId": self.krx_id,
            "pw": self.krx_pw,
        }
        data = self._post_login(payload)

        if data.get("_error_code") == "CD011":
            payload["skipDup"] = "Y"
            data = self._post_login(payload)

        if data.get("_error_code") != "CD001":
            message = data.get("_error_message") or data
            raise RuntimeError(f"KRX 로그인 실패: {message}")

    def fetch_net_buy_top_stocks(
        self,
        buyer: str,
        start_date: date,
        end_date: date,
        market: str = "ALL",
    ) -> pd.DataFrame:
        if buyer not in INVESTOR_CODES:
            raise ValueError(f"buyer는 {', '.join(INVESTOR_CODES)} 중 하나여야 합니다.")

        market = market.upper()
        if market not in MARKET_CODES:
            raise ValueError(f"market은 {', '.join(MARKET_CODES)} 중 하나여야 합니다.")

        payload = {
            "bld": "dbms/MDC/STAT/standard/MDCSTAT02401",
            "locale": "ko_KR",
            "mktId": MARKET_CODES[market],
            "strtDd": start_date.strftime("%Y%m%d"),
            "endDd": end_date.strftime("%Y%m%d"),
            "invstTpCd": INVESTOR_CODES[buyer],
            "trdVolVal": "1",
            "share": "1",
            "money": "1",
            "csvxls_isNo": "false",
        }
        data = self._post_json(payload)
        rows = self._extract_rows(data)
        return normalize_rows(rows)

    def _get(self, url: str, headers: dict[str, str]) -> None:
        req = request.Request(url, headers=headers, method="GET")
        with self.opener.open(req, timeout=self.timeout):
            pass

    def _post_login(self, payload: dict[str, str]) -> dict[str, Any]:
        req = request.Request(
            LOGIN_URL,
            data=parse.urlencode(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0",
                "Referer": LOGIN_PAGE_URL,
            },
            method="POST",
        )
        with self.opener.open(req, timeout=self.timeout) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw)

    def _post_json(self, payload: dict[str, str]) -> dict[str, Any]:
        req = request.Request(
            GET_JSON_URL,
            data=parse.urlencode(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "User-Agent": "Mozilla/5.0",
                "Referer": REFERER_URL,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Origin": "https://data.krx.co.kr",
            },
            method="POST",
        )

        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace").strip()
            if body == "LOGOUT":
                raise RuntimeError("KRX 로그인 세션이 없거나 만료되었습니다.")
            raise RuntimeError(f"KRX API 오류 HTTP {exc.code}: {body[:300]}") from exc

        if raw.strip() == "LOGOUT":
            raise RuntimeError("KRX 로그인 세션이 없거나 만료되었습니다.")
        return json.loads(raw)

    @staticmethod
    def _extract_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("output", "OutBlock_1", "block1"):
            rows = data.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        return []


def supabase_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY and SUPABASE_SERVICE_ROLE_KEY)


def supabase_json_request(
    path: str,
    *,
    method: str = "GET",
    payload: Any | None = None,
    service_role: bool = False,
    access_token: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    if not SUPABASE_URL:
        raise RuntimeError("SUPABASE_URL 환경변수가 설정되지 않았습니다.")

    key = SUPABASE_SERVICE_ROLE_KEY if service_role else SUPABASE_ANON_KEY
    if not key:
        key_name = "SUPABASE_SERVICE_ROLE_KEY" if service_role else "SUPABASE_ANON_KEY"
        raise RuntimeError(f"{key_name} 환경변수가 설정되지 않았습니다.")

    headers = {
        "apikey": key,
        "Authorization": f"Bearer {access_token or key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    data = None
    if payload is not None:
        data = json.dumps(
            payload,
            ensure_ascii=False,
            default=lambda obj: obj.item() if hasattr(obj, "item") else str(obj),
        ).encode("utf-8")

    req = request.Request(
        f"{SUPABASE_URL}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with request.urlopen(req, timeout=45) as response:
            raw = response.read().decode("utf-8")
            if not raw:
                return None
            return json.loads(raw)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if "PGRST205" in body or "Could not find the table" in body:
            raise RuntimeError(
                "Supabase 테이블이 아직 생성되지 않았습니다. "
                "Supabase SQL Editor에서 저장소의 supabase_schema.sql 내용을 먼저 실행하세요."
            ) from exc
        raise RuntimeError(f"Supabase 요청 실패 HTTP {exc.code}: {body[:500]}") from exc


def supabase_signup(email: str, password: str) -> dict[str, Any]:
    return supabase_json_request(
        "/auth/v1/signup",
        method="POST",
        payload={"email": email, "password": password},
    )


def supabase_signin(email: str, password: str) -> dict[str, Any]:
    return supabase_json_request(
        "/auth/v1/token?grant_type=password",
        method="POST",
        payload={"email": email, "password": password},
    )


def set_auth_session(auth_data: dict[str, Any]) -> None:
    user = auth_data.get("user") or {}
    session["access_token"] = auth_data.get("access_token")
    session["refresh_token"] = auth_data.get("refresh_token")
    session["user_id"] = user.get("id")
    session["email"] = user.get("email")


def current_user() -> dict[str, str] | None:
    user_id = session.get("user_id")
    email = session.get("email")
    if not user_id:
        return None
    return {"id": str(user_id), "email": str(email or "")}


def require_current_user() -> dict[str, str]:
    user = current_user()
    if not user:
        raise RuntimeError("로그인이 필요합니다.")
    return user


def rest_insert(table: str, rows: Any, *, returning: bool = True) -> Any:
    headers = {"Prefer": "return=representation" if returning else "return=minimal"}
    return supabase_json_request(
        f"/rest/v1/{table}",
        method="POST",
        payload=rows,
        service_role=True,
        extra_headers=headers,
    )


def rest_update(table: str, filters: str, values: dict[str, Any]) -> Any:
    return supabase_json_request(
        f"/rest/v1/{table}?{filters}",
        method="PATCH",
        payload=values,
        service_role=True,
        extra_headers={"Prefer": "return=representation"},
    )


def rest_select(table: str, query: str) -> Any:
    return supabase_json_request(
        f"/rest/v1/{table}?{query}",
        method="GET",
        service_role=True,
    )


def storage_upload(local_path: Path, storage_path: str) -> None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Supabase Storage 업로드 환경변수가 설정되지 않았습니다.")

    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "x-upsert": "true",
    }
    req = request.Request(
        f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_STORAGE_BUCKET}/{parse.quote(storage_path)}",
        data=local_path.read_bytes(),
        headers=headers,
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=90):
            return
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase Storage 업로드 실패 HTTP {exc.code}: {body[:500]}") from exc


def storage_signed_url(storage_path: str, expires_in: int = 3600) -> str:
    data = supabase_json_request(
        f"/storage/v1/object/sign/{SUPABASE_STORAGE_BUCKET}/{parse.quote(storage_path)}",
        method="POST",
        payload={"expiresIn": expires_in},
        service_role=True,
    )
    signed_url = data.get("signedURL") or data.get("signedUrl")
    if not signed_url:
        raise RuntimeError("Supabase Storage signed URL 생성에 실패했습니다.")
    if signed_url.startswith("http"):
        return signed_url
    if signed_url.startswith("/storage/v1/"):
        return f"{SUPABASE_URL}{signed_url}"
    return f"{SUPABASE_URL}/storage/v1/{signed_url.lstrip('/')}"


def parse_yyyymmdd(value: str) -> date:
    digits = re.sub(r"\D", "", value or "")
    if not re.fullmatch(r"\d{8}", digits):
        raise ValueError("날짜는 YYYYMMDD 또는 YYYY-MM-DD 형식이어야 합니다.")
    return datetime.strptime(digits, "%Y%m%d").date()


def to_int(value: Any) -> int:
    cleaned = re.sub(r"[^0-9\-]", "", str(value or ""))
    if not cleaned or cleaned == "-":
        return 0
    return int(cleaned)


def pick_value(row: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    normalized = {str(k).strip().upper(): v for k, v in row.items()}
    for candidate in candidates:
        key = candidate.upper()
        if key in normalized:
            return normalized[key]
    return ""


def normalize_rows(rows: list[dict[str, Any]]) -> pd.DataFrame:
    records = []
    for row in rows:
        record = {
            "종목코드": str(pick_value(row, ("ISU_SRT_CD", "ISU_CD", "종목코드"))).zfill(6),
            "종목명": pick_value(row, ("ISU_ABBRV", "ISU_NM", "ISU_KOR_NM", "종목명")),
            "거래량_매도": to_int(pick_value(row, ("ASK_TRDVOL", "ASK_TRD_VOL", "매도거래량", "거래량_매도"))),
            "거래량_매수": to_int(pick_value(row, ("BID_TRDVOL", "BID_TRD_VOL", "매수거래량", "거래량_매수"))),
            "거래량_순매수": to_int(pick_value(row, ("NETBID_TRDVOL", "NETBID_TRD_VOL", "순매수거래량", "거래량_순매수"))),
            "거래대금_매도": to_int(pick_value(row, ("ASK_TRDVAL", "ASK_TRD_VAL", "매도거래대금", "거래대금_매도"))),
            "거래대금_매수": to_int(pick_value(row, ("BID_TRDVAL", "BID_TRD_VAL", "매수거래대금", "거래대금_매수"))),
            "거래대금_순매수": to_int(pick_value(row, ("NETBID_TRDVAL", "NETBID_TRD_VAL", "순매수거래대금", "거래대금_순매수"))),
        }
        if record["종목코드"] and record["종목명"]:
            records.append(record)
    return pd.DataFrame(records, columns=BASE_COLUMNS)


def add_metadata(df: pd.DataFrame, job: FetchJob, now: datetime) -> pd.DataFrame:
    enriched = df.copy()
    enriched["d_today_year"] = now.year
    enriched["d_today_month"] = now.month
    enriched["d_today_day"] = now.day
    enriched["period(D-00)_start"] = job.period_start
    enriched["period(D-00)_end"] = job.period_end
    enriched["buyer"] = job.buyer
    return enriched[BASE_COLUMNS + METADATA_COLUMNS]


def build_jobs(as_of: date) -> list[FetchJob]:
    jobs = []
    for buyer in INVESTOR_CODES:
        jobs.append(FetchJob(buyer, as_of - timedelta(days=180), as_of, -180, 0, "6개월"))
        jobs.append(FetchJob(buyer, as_of - timedelta(days=90), as_of, -90, 0, "3개월"))
        jobs.append(FetchJob(buyer, as_of - timedelta(days=30), as_of, -30, 0, "1개월"))
    return jobs


def collect_recent_trading_day_jobs(
    api: KrxInvestorApi,
    as_of: date,
    market: str,
    trading_days: int,
    max_calendar_lookback: int,
) -> tuple[list[FetchJob], list[str]]:
    jobs = []
    found_dates: list[tuple[date, int]] = []
    day_offset = 0

    while len(found_dates) < trading_days and day_offset < max_calendar_lookback:
        day_offset += 1
        target = as_of - timedelta(days=day_offset)
        probe = api.fetch_net_buy_top_stocks("외국인", target, target, market)
        if probe.empty:
            continue
        found_dates.append((target, -day_offset))

    if len(found_dates) < trading_days:
        raise RuntimeError(f"최근 영업일 {trading_days}개를 찾지 못했습니다.")

    for target, period in found_dates:
        for buyer in INVESTOR_CODES:
            jobs.append(FetchJob(buyer, target, target, period, period, target.strftime("%Y%m%d")))
    return jobs, [target.strftime("%Y%m%d") for target, _ in found_dates]


def create_pivot(base_df: pd.DataFrame) -> pd.DataFrame:
    last_df = pd.pivot_table(
        base_df,
        index=["종목코드", "종목명"],
        columns=["period(D-00)_start", "buyer"],
        values="거래량_순매수",
        aggfunc="sum",
    )
    last_df = last_df.sort_index(axis=1, ascending=False)

    def convert_period_label(period: int) -> str:
        period = abs(int(period))
        if period == 30:
            return "1개월누적"
        if period == 90:
            return "3개월누적"
        if period == 180:
            return "6개월누적"
        return f"{period}일전"

    last_df.columns = pd.MultiIndex.from_tuples(
        [(convert_period_label(period), buyer) for period, buyer in last_df.columns],
        names=last_df.columns.names,
    )
    return last_df


def flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        flat_columns = []
        for column in df.columns:
            parts = [str(part) for part in column if str(part) and not str(part).startswith("Unnamed")]
            flat_columns.append("_".join(parts) if parts else "")
        df = df.copy()
        df.columns = flat_columns
    return df


def stock_names_from_index(index_obj: pd.Index) -> list[str]:
    names = []
    for item in index_obj:
        if isinstance(item, tuple) and len(item) >= 2:
            names.append(str(item[1]))
        else:
            names.append(str(item))
    return names


def compact_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if number < 0 else ""
    number = abs(number)
    if number >= 100_000_000:
        return f"{sign}{number / 100_000_000:.1f}억"
    if number >= 10_000:
        return f"{sign}{number / 10_000:.1f}만"
    return f"{sign}{number:,.0f}"


BUY_COLOR = "#ff4d5f"
SELL_COLOR = "#2f80ff"


def make_chart_meta_html(note: str, legend: list[dict[str, str]]) -> str:
    legend_html = "".join(
        f"""
        <span class="legend-item">
          <span class="legend-dot" style="background:{escape(item["color"])}; box-shadow:0 0 10px {escape(item["color"])};"></span>
          {escape(item["label"])}
        </span>
        """
        for item in legend
    )
    return f"""
    <div class="chart-meta">
      <div class="chart-note">{escape(note)}</div>
      <div class="chart-legend">{legend_html}</div>
    </div>
    """


def format_chart_value(value: Any, unit: str = "주") -> str:
    number = compact_number(value)
    return f"{number}{unit}" if unit else number


def make_grouped_bar_html(
    labels: list[str],
    series: list[dict[str, Any]],
    *,
    note: str,
    unit: str = "주",
    legend: list[dict[str, str]] | None = None,
    color_by_sign: bool = False,
) -> str:
    all_values = [abs(float(value)) for item in series for value in item["values"]]
    max_abs = max(all_values) if all_values else 1
    rows = []
    default_colors = [BUY_COLOR, SELL_COLOR]
    if legend is None:
        if color_by_sign:
            legend = [
                {"label": "순매수(+)", "color": BUY_COLOR},
                {"label": "순매도(-)", "color": SELL_COLOR},
            ]
        else:
            legend = [
                {"label": str(item["name"]), "color": str(item.get("color") or default_colors[index % 2])}
                for index, item in enumerate(series)
            ]
    for row_index, label in enumerate(labels):
        bars = []
        for series_index, item in enumerate(series):
            value = float(item["values"][row_index])
            width = min(abs(value) / max_abs * 100, 100)
            color = BUY_COLOR if value >= 0 else SELL_COLOR
            if not color_by_sign:
                color = item.get("color") or default_colors[series_index % len(default_colors)]
            direction = "negative" if value < 0 else "positive"
            bars.append(
                f"""
                <div class="mini-bar-row">
                  <span class="series-name">{escape(item["name"])}</span>
                  <div class="mini-bar-track">
                    <span class="mini-bar {direction}" style="width:{width:.2f}%; background:{color}; box-shadow:0 0 13px {color};"></span>
                  </div>
                  <span class="bar-value">{escape(format_chart_value(value, unit))}</span>
                </div>
                """
            )
        rows.append(
            f"""
            <div class="chart-row">
              <div class="chart-label">{escape(label)}</div>
              <div class="chart-bars">{''.join(bars)}</div>
            </div>
            """
        )
    return f'<div class="html-chart grouped-bar">{make_chart_meta_html(note, legend)}{"".join(rows)}</div>'


def make_scatter_html(points: list[dict[str, Any]]) -> str:
    if not points:
        return '<p class="empty-chart">표시할 데이터가 없습니다.</p>'
    xs = [float(point["x"]) for point in points]
    ys = [float(point["y"]) for point in points]
    max_abs = max([abs(value) for value in xs + ys] or [1]) or 1
    svg_points = []
    for point in points[:700]:
        x = 50 + (float(point["x"]) / max_abs) * 42
        y = 50 - (float(point["y"]) / max_abs) * 42
        x = min(max(x, 5), 95)
        y = min(max(y, 5), 95)
        color = BUY_COLOR if float(point["x"]) >= 0 else SELL_COLOR
        svg_points.append(
            f'<circle cx="{x:.2f}%" cy="{y:.2f}%" r="2.6" fill="{color}" opacity="0.56">'
            f'<title>{escape(point["name"])} / 외국인 {escape(format_chart_value(point["x"]))} / 기관 {escape(format_chart_value(point["y"]))}</title>'
            '</circle>'
        )
    return f"""
    <div class="scatter-wrap">
      {make_chart_meta_html(
        "거래량 기준 순매수 비교입니다. 단위는 주이며, 빨강은 외국인 순매수(+), 파랑은 외국인 순매도(-)를 뜻합니다.",
        [
          {"label": "외국인 순매수(+)", "color": BUY_COLOR},
          {"label": "외국인 순매도(-)", "color": SELL_COLOR},
        ],
      )}
      <svg viewBox="0 0 100 100" role="img" aria-label="외국인 기관합계 산점도">
        <line x1="50" y1="4" x2="50" y2="96" stroke="#7b8a90" stroke-width="0.4" />
        <line x1="4" y1="50" x2="96" y2="50" stroke="#7b8a90" stroke-width="0.4" />
        {''.join(svg_points)}
      </svg>
      <div class="axis-label x-axis">외국인 순매수</div>
      <div class="axis-label y-axis">기관합계 순매수</div>
    </div>
    """


def add_html_chart(charts: list[dict[str, str]], title: str, html: str) -> None:
    charts.append({"title": title, "html": html})


def create_buy_sell_chart(base_df: pd.DataFrame) -> str:
    if base_df.empty:
        return '<p class="empty-chart">표시할 데이터가 없습니다.</p>'

    required_columns = {"buyer", "period(D-00)_start", "종목명", "거래량_매수", "거래량_매도", "거래량_순매수"}
    if not required_columns.issubset(set(base_df.columns)):
        return '<p class="empty-chart">매수/매도 차트를 만들 수 있는 컬럼이 없습니다.</p>'

    foreign_month = base_df[
        (base_df["buyer"] == "외국인")
        & (pd.to_numeric(base_df["period(D-00)_start"], errors="coerce") == -30)
    ].copy()
    if foreign_month.empty:
        return '<p class="empty-chart">1개월 외국인 매수/매도 데이터가 없습니다.</p>'

    for column in ["거래량_매수", "거래량_매도", "거래량_순매수"]:
        foreign_month[column] = pd.to_numeric(foreign_month[column], errors="coerce").fillna(0)

    top_flow = foreign_month.reindex(foreign_month["거래량_순매수"].abs().sort_values(ascending=False).index).head(10)
    return make_grouped_bar_html(
        top_flow["종목명"].astype(str).tolist(),
        [
            {"name": "매수", "values": top_flow["거래량_매수"].tolist(), "color": BUY_COLOR},
            {"name": "매도", "values": top_flow["거래량_매도"].tolist(), "color": SELL_COLOR},
        ],
        note="외국인 1개월 누적 거래량 기준입니다. 단위는 주이며, 빨강은 매수 수량, 파랑은 매도 수량입니다.",
        legend=[
            {"label": "매수 수량", "color": BUY_COLOR},
            {"label": "매도 수량", "color": SELL_COLOR},
        ],
    )


def create_visualizations(last_df: pd.DataFrame, base_df: pd.DataFrame | None = None) -> list[dict[str, str]]:
    charts: list[dict[str, str]] = []
    if last_df.empty:
        return charts

    df_clean = last_df.fillna(0).copy()

    if base_df is not None:
        add_html_chart(charts, "1개월 외국인 매수/매도 수량 TOP10", create_buy_sell_chart(base_df))

    if ("1개월누적", "외국인") in df_clean.columns and ("1개월누적", "기관합계") in df_clean.columns:
        top10_idx = df_clean[("1개월누적", "외국인")].nlargest(10).index
        plot_df = df_clean.loc[top10_idx, [("1개월누적", "외국인"), ("1개월누적", "기관합계")]].copy()
        plot_df.columns = ["외국인", "기관합계"]
        add_html_chart(
            charts,
            "1개월 외국인 순매수 TOP10",
            make_grouped_bar_html(
                stock_names_from_index(plot_df.index),
                [
                    {"name": "외국인 순매수", "values": plot_df["외국인"].tolist()},
                    {"name": "기관합계 순매수", "values": plot_df["기관합계"].tolist()},
                ],
                note="1개월 누적 거래량 기준 순매수입니다. 단위는 주이며, 빨강은 순매수(+), 파랑은 순매도(-)입니다.",
                color_by_sign=True,
            ),
        )

        scatter_df = df_clean[[("1개월누적", "외국인"), ("1개월누적", "기관합계")]].copy()
        scatter_df.columns = ["외국인", "기관합계"]
        scatter_names = stock_names_from_index(scatter_df.index)
        points = [
            {"name": scatter_names[index], "x": row["외국인"], "y": row["기관합계"]}
            for index, (_, row) in enumerate(scatter_df.iterrows())
        ]
        add_html_chart(charts, "외국인 vs 기관합계 순매수", make_scatter_html(points))

    recent_periods = [
        period
        for period in ["10일전", "9일전", "8일전", "7일전", "6일전", "5일전", "4일전", "3일전", "2일전", "1일전"]
        if (period, "외국인") in df_clean.columns and (period, "기관합계") in df_clean.columns
    ]
    if recent_periods:
        foreign_mean = df_clean.xs("외국인", level=1, axis=1)[recent_periods].mean()
        inst_mean = df_clean.xs("기관합계", level=1, axis=1)[recent_periods].mean()
        add_html_chart(
            charts,
            "최근 거래일 평균 순매수",
            make_grouped_bar_html(
                recent_periods,
                [
                    {"name": "외국인 순매수", "values": foreign_mean.tolist()},
                    {"name": "기관합계 순매수", "values": inst_mean.tolist()},
                ],
                note="최근 거래일별 전체 종목 평균 순매수입니다. 단위는 주이며, 빨강은 순매수(+), 파랑은 순매도(-)입니다.",
                color_by_sign=True,
            ),
        )

    acc_periods = [
        period
        for period in ["1개월누적", "3개월누적", "6개월누적"]
        if (period, "외국인") in df_clean.columns and (period, "기관합계") in df_clean.columns
    ]
    if acc_periods:
        foreign_acc = df_clean.xs("외국인", level=1, axis=1)[acc_periods].mean()
        inst_acc = df_clean.xs("기관합계", level=1, axis=1)[acc_periods].mean()
        add_html_chart(
            charts,
            "기간별 평균 순매수 비교",
            make_grouped_bar_html(
                acc_periods,
                [
                    {"name": "외국인 순매수", "values": foreign_acc.tolist()},
                    {"name": "기관합계 순매수", "values": inst_acc.tolist()},
                ],
                note="1개월/3개월/6개월 누적 구간의 전체 종목 평균 순매수입니다. 단위는 주이며, 빨강은 순매수(+), 파랑은 순매도(-)입니다.",
                color_by_sign=True,
            ),
        )

    return charts


def create_top_rows(last_df: pd.DataFrame, limit: int = 15) -> list[dict[str, Any]]:
    flat_df = flatten_columns(last_df.reset_index())
    if "1개월누적_외국인" in flat_df.columns:
        flat_df = flat_df.sort_values("1개월누적_외국인", ascending=False)
    fixed_cols = {
        "종목코드",
        "종목명",
        "1개월누적_외국인",
        "1개월누적_기관합계",
        "3개월누적_외국인",
        "3개월누적_기관합계",
        "6개월누적_외국인",
        "6개월누적_기관합계",
    }
    recent_cols = [
        col
        for col in flat_df.columns
        if re.match(r"^\d+일전_(외국인|기관합계)$", str(col))
    ]
    recent_cols = sorted(recent_cols, key=lambda col: int(str(col).split("일전_")[0]))
    display_cols = [col for col in flat_df.columns if col in fixed_cols]
    display_cols.extend([col for col in recent_cols if col not in display_cols])
    return flat_df[display_cols].head(limit).to_dict("records")


def json_safe(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except ValueError:
            pass
    return value


def period_label(period_start: int) -> str:
    period = abs(int(period_start))
    if period == 30:
        return "1개월누적"
    if period == 90:
        return "3개월누적"
    if period == 180:
        return "6개월누적"
    return f"{period}일전"


def base_rows_to_records(base_df: pd.DataFrame, job_id: str, user_id: str) -> list[dict[str, Any]]:
    records = []
    for row in base_df.to_dict("records"):
        period_start = int(json_safe(row.get("period(D-00)_start")) or 0)
        records.append({
            "job_id": job_id,
            "user_id": user_id,
            "stock_code": str(row.get("종목코드", "")).zfill(6),
            "stock_name": str(row.get("종목명", "")),
            "buyer": str(row.get("buyer", "")),
            "period_label": period_label(period_start),
            "period_start": period_start,
            "period_end": int(json_safe(row.get("period(D-00)_end")) or 0),
            "sell_volume": int(json_safe(row.get("거래량_매도")) or 0),
            "buy_volume": int(json_safe(row.get("거래량_매수")) or 0),
            "net_buy_volume": int(json_safe(row.get("거래량_순매수")) or 0),
            "sell_value": int(json_safe(row.get("거래대금_매도")) or 0),
            "buy_value": int(json_safe(row.get("거래대금_매수")) or 0),
            "net_buy_value": int(json_safe(row.get("거래대금_순매수")) or 0),
        })
    return records


def insert_in_chunks(table: str, records: list[dict[str, Any]], chunk_size: int = 1000) -> None:
    for start in range(0, len(records), chunk_size):
        rest_insert(table, records[start:start + chunk_size], returning=False)


def create_query_job(user_id: str, market: str, as_of: date) -> str:
    rows = rest_insert("query_jobs", [{
        "user_id": user_id,
        "status": "running",
        "market": market,
        "as_of": as_of.isoformat(),
        "started_at": datetime.now().isoformat(),
    }])
    if not rows:
        raise RuntimeError("query_jobs 생성에 실패했습니다.")
    return rows[0]["id"]


def complete_query_job(
    job_id: str,
    trading_dates: list[str],
    base_rows: int,
    last_rows: int,
    storage_path: str | None,
) -> None:
    rest_update(
        "query_jobs",
        f"id=eq.{job_id}",
        {
            "status": "completed",
            "trading_dates": trading_dates,
            "base_rows": base_rows,
            "last_rows": last_rows,
            "excel_path": storage_path,
            "completed_at": datetime.now().isoformat(),
        },
    )


def fail_query_job(job_id: str, message: str) -> None:
    rest_update(
        "query_jobs",
        f"id=eq.{job_id}",
        {
            "status": "failed",
            "error_message": message[:1000],
            "completed_at": datetime.now().isoformat(),
        },
    )


def write_outputs(base_df: pd.DataFrame, last_df: pd.DataFrame, out_dir: Path, now: datetime) -> tuple[Path, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    last_df_reset = flatten_columns(last_df.reset_index())

    filename = f"{now.strftime('%Y-%m-%d')}_통합파일.xlsx"
    output_path = out_dir / filename
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        base_df.to_excel(writer, index=False, sheet_name="base")
        last_df_reset.to_excel(writer, index=False, sheet_name="last")
    return output_path, len(last_df_reset)


def run_collection(
    krx_id: str,
    krx_pw: str,
    openapi_key: str,
    as_of: date,
    market: str,
    output_root: Path,
    user_id: str | None = None,
) -> RunResult:
    if not krx_id or not krx_pw:
        raise ValueError("KRX 아이디와 비밀번호를 모두 입력해야 합니다.")
    if not openapi_key:
        raise ValueError("OpenAPI 키를 입력해야 합니다.")
    if user_id and not supabase_configured():
        raise RuntimeError("Supabase 환경변수가 설정되지 않아 로그인 사용자 결과를 저장할 수 없습니다.")

    now = datetime.now()
    out_dir = output_root / now.strftime("%Y%m%d")
    job_id = create_query_job(user_id, market, as_of) if user_id else None

    try:
        api = KrxInvestorApi(krx_id, krx_pw, timeout=REQUEST_TIMEOUT)

        jobs = build_jobs(as_of)
        recent_jobs, trading_dates = collect_recent_trading_day_jobs(
            api=api,
            as_of=as_of,
            market=market,
            trading_days=RECENT_TRADING_DAYS,
            max_calendar_lookback=MAX_CALENDAR_LOOKBACK,
        )
        jobs.extend(recent_jobs)

        frames = []
        for job in jobs:
            df = api.fetch_net_buy_top_stocks(job.buyer, job.start_date, job.end_date, market)
            if not df.empty:
                frames.append(add_metadata(df, job, now))

        if not frames:
            raise RuntimeError("수집된 데이터가 없습니다.")

        base_df = pd.concat(frames, ignore_index=True)
        last_df = create_pivot(base_df)
        charts = create_visualizations(last_df, base_df)
        top_rows = create_top_rows(last_df)
        output_path, last_rows = write_outputs(base_df, last_df, out_dir, now)

        storage_path = None
        if job_id and user_id:
            storage_filename = f"krx_result_{now.strftime('%Y%m%d_%H%M%S')}.xlsx"
            storage_path = f"{user_id}/{job_id}/{storage_filename}"
            storage_upload(output_path, storage_path)
            rest_insert("query_summaries", [{
                "job_id": job_id,
                "user_id": user_id,
                "top_rows": top_rows,
                "chart_data": charts,
                "pivot_columns": list(flatten_columns(last_df.reset_index()).columns),
            }], returning=False)
            if SAVE_QUERY_ROWS:
                insert_in_chunks("query_rows", base_rows_to_records(base_df, job_id, user_id))
            complete_query_job(job_id, trading_dates, len(base_df), last_rows, storage_path)

        return RunResult(
            output_path=output_path,
            base_rows=len(base_df),
            last_rows=last_rows,
            trading_dates=trading_dates,
            charts=charts,
            top_rows=top_rows,
            job_id=job_id,
            storage_path=storage_path,
        )
    except Exception as exc:
        if job_id:
            try:
                fail_query_job(job_id, str(exc))
            except Exception:
                pass
        raise


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "ghj-codex-local-secret")


@app.after_request
def add_utf8_headers(response):
    if response.content_type and response.content_type.startswith("text/html"):
        response.headers["Content-Type"] = "text/html; charset=utf-8"
    return response


PAGE_TEMPLATE = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GHJ_PRJ_V_03</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #030705;
      --panel: #08110d;
      --panel-soft: #0c1712;
      --text: #eafff3;
      --muted: #8aae9f;
      --line: #1d3a2e;
      --navy: #050b09;
      --navy-2: #0b1712;
      --accent: #39ff88;
      --accent-dark: #14d86a;
      --cyan: #00e5ff;
      --red: #ff4d6d;
      --danger: #ff5f79;
      --success: #39ff88;
      --shadow: 0 22px 70px rgba(0, 255, 136, .10), 0 0 0 1px rgba(57,255,136,.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at 16% 0%, rgba(57,255,136,.16), transparent 28%),
        radial-gradient(circle at 88% 8%, rgba(0,229,255,.14), transparent 24%),
        linear-gradient(180deg, #020403 0%, #06100c 48%, #030705 100%);
      color: var(--text);
      font-family: "Segoe UI", "Malgun Gothic", Arial, sans-serif;
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background-image:
        linear-gradient(rgba(57, 255, 136, .055) 1px, transparent 1px),
        linear-gradient(90deg, rgba(0, 229, 255, .045) 1px, transparent 1px);
      background-size: 28px 28px;
      mask-image: linear-gradient(180deg, rgba(0,0,0,.8), transparent 58%);
    }
    main {
      position: relative;
      width: min(1240px, calc(100% - 32px));
      margin: 0 auto;
      padding: 34px 0;
    }
    .panel {
      background: var(--panel);
      border: 1px solid rgba(57,255,136,.22);
      border-radius: 8px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .system-bar {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
      min-height: 64px;
      padding: 0 26px;
      background: linear-gradient(90deg, #050b09 0%, #0b1712 62%, #072118 100%);
      color: var(--text);
      border-bottom: 1px solid rgba(57,255,136,.55);
      box-shadow: inset 0 -1px 0 rgba(0,229,255,.20);
    }
    .brand-lockup {
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 0;
    }
    .brand-mark {
      display: grid;
      place-items: center;
      width: 34px;
      height: 34px;
      border-radius: 7px;
      background: rgba(57,255,136,.12);
      border: 1px solid rgba(57,255,136,.68);
      color: var(--accent);
      box-shadow: 0 0 18px rgba(57,255,136,.22);
      font-weight: 1000;
      letter-spacing: 0;
    }
    .brand-name {
      font-weight: 950;
      letter-spacing: 0;
      white-space: nowrap;
    }
    .brand-sub {
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      white-space: nowrap;
    }
    .status-stack {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      flex-wrap: wrap;
    }
    .status-chip {
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      padding: 0 10px;
      border: 1px solid rgba(57,255,136,.28);
      border-radius: 999px;
      color: #d8ffe8;
      background: rgba(57,255,136,.06);
      font-size: 12px;
      font-weight: 900;
    }
    .content-area {
      padding: 28px;
    }
    .eyebrow {
      margin: 0 0 8px;
      color: var(--accent);
      font-size: 13px;
      font-weight: 800;
    }
    h1 { margin: 0; font-size: clamp(30px, 4vw, 46px); letter-spacing: 0; }
    .sub { margin: 10px 0 0; color: var(--muted); line-height: 1.6; }
    form {
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 16px;
      margin-top: 26px;
      align-items: end;
      padding: 20px;
      background: var(--panel-soft);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: inset 0 0 34px rgba(57,255,136,.025);
    }
    .field { display: grid; gap: 7px; }
    .span2 { grid-column: span 2; }
    .span3 { grid-column: span 3; }
    .span6 { grid-column: span 6; }
    .remember-field {
      grid-column: span 6;
      display: flex;
      align-items: flex-start;
      gap: 11px;
      border: 1px solid rgba(57,255,136,.18);
      border-radius: 8px;
      background: rgba(57,255,136,.035);
      padding: 13px 14px;
    }
    .remember-field input {
      width: 18px;
      min-height: 18px;
      height: 18px;
      margin-top: 2px;
      accent-color: var(--accent);
      flex: 0 0 auto;
    }
    .remember-field label {
      color: var(--text);
      cursor: pointer;
    }
    .remember-copy {
      display: grid;
      gap: 3px;
    }
    .remember-copy span {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }
    label {
      color: var(--muted);
      font-size: 13px;
      font-weight: 800;
    }
    .help {
      min-height: 42px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }
    .fixed-scope {
      grid-column: span 6;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(57,255,136,.035);
      padding: 14px 16px;
      color: var(--muted);
      line-height: 1.6;
    }
    .fixed-scope strong {
      color: var(--text);
    }
    input, select, button {
      width: 100%;
      min-height: 44px;
      border-radius: 6px;
      font: inherit;
    }
    input, select {
      border: 1px solid var(--line);
      padding: 0 12px;
      background: #07100c;
      color: var(--text);
    }
    input:focus,
    select:focus {
      outline: 2px solid rgba(57, 255, 136, .22);
      border-color: var(--accent);
    }
    button {
      border: 0;
      background: var(--accent);
      color: #001a0c;
      font-weight: 900;
      cursor: pointer;
    }
    button:hover { background: var(--accent-dark); box-shadow: 0 0 18px rgba(57,255,136,.22); }
    .messages { display: grid; gap: 8px; margin-top: 20px; }
    .message { margin: 0; padding: 12px 14px; border-radius: 6px; font-weight: 800; }
    .error { color: var(--danger); background: rgba(255,77,109,.10); border: 1px solid rgba(255,77,109,.28); }
    .success { color: var(--success); background: rgba(57,255,136,.08); border: 1px solid rgba(57,255,136,.24); }
    .result {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-top: 18px;
    }
    .dashboard-title {
      margin: 26px 0 0;
      font-size: 24px;
      letter-spacing: 0;
      color: var(--navy);
    }
    .metric {
      background: rgba(57,255,136,.045);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }
    .metric dt { color: var(--muted); font-weight: 800; font-size: 13px; }
    .metric dd { margin: 6px 0 0; font-size: 20px; font-weight: 900; }
    .download {
      display: inline-flex;
      margin-top: 18px;
      min-height: 44px;
      align-items: center;
      justify-content: center;
      padding: 0 18px;
      border-radius: 6px;
      background: var(--accent);
      color: #001a0c;
      font-weight: 900;
      text-decoration: none;
    }
    .top-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 18px;
    }
    .secondary-link {
      display: inline-flex;
      min-height: 44px;
      align-items: center;
      justify-content: center;
      padding: 0 18px;
      border-radius: 6px;
      border: 1px solid var(--accent);
      color: var(--accent);
      font-weight: 900;
      text-decoration: none;
      background: rgba(57,255,136,.035);
    }
    .visuals {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
      margin-top: 22px;
    }
    .chart-card {
      position: relative;
      overflow: hidden;
      border: 1px solid rgba(57,255,136,.18);
      border-radius: 8px;
      background:
        linear-gradient(135deg, rgba(57,255,136,.07), transparent 34%),
        radial-gradient(circle at 86% 12%, rgba(59,167,255,.12), transparent 30%),
        rgba(5,12,9,.82);
      padding: 16px;
      box-shadow: 0 18px 48px rgba(0,0,0,.22), inset 0 0 32px rgba(57,255,136,.03);
    }
    .chart-card::before {
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background-image: linear-gradient(rgba(57,255,136,.055) 1px, transparent 1px);
      background-size: 100% 16px;
      mix-blend-mode: screen;
      opacity: .38;
    }
    .chart-card h3 {
      position: relative;
      margin: 0 0 12px;
      font-size: 17px;
      letter-spacing: 0;
      color: #dfffea;
      text-shadow: 0 0 14px rgba(57,255,136,.18);
    }
    .html-chart {
      position: relative;
      display: grid;
      gap: 12px;
    }
    .chart-meta {
      display: grid;
      gap: 8px;
      margin-bottom: 2px;
      padding: 10px 12px;
      border: 1px solid rgba(212,255,232,.10);
      border-radius: 6px;
      background: rgba(0,0,0,.18);
    }
    .chart-note {
      color: #c8dfd2;
      font-size: 12px;
      line-height: 1.5;
      font-weight: 800;
    }
    .chart-legend {
      display: flex;
      flex-wrap: wrap;
      gap: 8px 14px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 900;
    }
    .legend-item {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      white-space: nowrap;
    }
    .legend-dot {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      box-shadow: 0 0 10px currentColor;
    }
    .chart-row {
      display: grid;
      grid-template-columns: minmax(92px, 160px) minmax(0, 1fr);
      gap: 12px;
      align-items: start;
      border-bottom: 1px solid rgba(57,255,136,.10);
      padding-bottom: 10px;
    }
    .chart-row:last-child {
      border-bottom: 0;
      padding-bottom: 0;
    }
    .chart-label {
      font-size: 13px;
      font-weight: 900;
      word-break: keep-all;
      overflow-wrap: anywhere;
    }
    .chart-bars {
      display: grid;
      gap: 6px;
    }
    .mini-bar-row {
      display: grid;
      grid-template-columns: 104px minmax(0, 1fr) 88px;
      gap: 8px;
      align-items: center;
    }
    .series-name {
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
    }
    .mini-bar-track {
      height: 12px;
      border-radius: 999px;
      background: rgba(212,255,232,.08);
      border: 1px solid rgba(212,255,232,.08);
      overflow: hidden;
    }
    .mini-bar {
      display: block;
      height: 100%;
      border-radius: 999px;
      min-width: 2px;
    }
    .mini-bar.negative {
      opacity: .62;
    }
    .bar-value {
      font-size: 12px;
      color: var(--text);
      font-weight: 800;
      text-align: right;
    }
    .scatter-wrap {
      position: relative;
      min-height: 360px;
      padding: 0 8px 34px 42px;
    }
    .scatter-wrap svg {
      display: block;
      width: 100%;
      height: 340px;
      border: 1px solid rgba(57,255,136,.18);
      border-radius: 8px;
      background:
        radial-gradient(circle at 50% 50%, rgba(57,255,136,.08), transparent 42%),
        linear-gradient(90deg, rgba(59,167,255,.08), transparent 34%, rgba(255,77,109,.08)),
        #07100c;
      filter: drop-shadow(0 0 12px rgba(57,255,136,.10));
    }
    .axis-label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 900;
    }
    .x-axis {
      position: absolute;
      left: 42px;
      right: 8px;
      bottom: 4px;
      text-align: center;
    }
    .y-axis {
      position: absolute;
      left: -4px;
      top: 48%;
      transform: rotate(-90deg);
      transform-origin: center;
    }
    .empty-chart {
      color: var(--muted);
      margin: 0;
    }
    .table-wrap {
      margin-top: 18px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 760px;
      background: #07100c;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 10px 12px;
      text-align: left;
      white-space: nowrap;
      font-size: 13px;
    }
    th {
      background: rgba(57,255,136,.09);
      color: var(--accent);
      font-weight: 900;
    }
    .loading {
      position: fixed;
      inset: 0;
      z-index: 20;
      display: none;
      align-items: center;
      justify-content: center;
      background: rgba(0, 0, 0, .72);
      padding: 20px;
    }
    .loading.active { display: flex; }
    .loading-box {
      width: min(460px, 100%);
      border-radius: 8px;
      background: #08110d;
      border: 1px solid rgba(57,255,136,.25);
      padding: 24px;
      box-shadow: 0 24px 70px rgba(15, 23, 42, .28);
    }
    .loading-title {
      margin: 0 0 8px;
      font-size: 20px;
      font-weight: 900;
    }
    .bar {
      height: 12px;
      overflow: hidden;
      border-radius: 999px;
      background: rgba(57,255,136,.12);
      margin-top: 16px;
    }
    .bar-fill {
      width: 0%;
      height: 100%;
      background: var(--accent);
      transition: width .35s ease;
    }
    .percent {
      margin-top: 10px;
      font-size: 28px;
      font-weight: 900;
      color: var(--accent-dark);
    }
    .guide {
      margin-top: 22px;
      padding-top: 22px;
      border-top: 1px solid var(--line);
    }
    .guide h2 {
      margin: 0 0 12px;
      font-size: 21px;
      letter-spacing: 0;
    }
    .guide ol {
      margin: 0;
      padding-left: 22px;
      color: var(--text);
      line-height: 1.7;
    }
    .guide a {
      color: var(--accent-dark);
      font-weight: 900;
    }
    .note {
      margin: 12px 0 0;
      color: var(--muted);
      line-height: 1.6;
      font-size: 14px;
    }
    @media (max-width: 860px) {
      main { width: min(100% - 20px, 1240px); padding: 16px 0; }
      .content-area { padding: 18px; }
      .system-bar { align-items: flex-start; flex-direction: column; padding: 16px 18px; }
      form, .result, .visuals { grid-template-columns: 1fr; }
      .span2, .span3, .span6, .fixed-scope, .remember-field { grid-column: auto; }
    }
  </style>
</head>
<body>
  <main>
    <section class="panel">
      <header class="system-bar">
        <div class="brand-lockup">
          <div class="brand-mark">D</div>
          <div>
            <div class="brand-name">GHJ_PRJ_V_03</div>
            <div class="brand-sub">INSTITUTIONAL FLOW TERMINAL</div>
          </div>
        </div>
        <div class="status-stack">
          <span class="status-chip">KRX FLOW</span>
          <span class="status-chip">SUPABASE LOGGED</span>
          <span class="status-chip">V.03</span>
        </div>
      </header>
      <div class="content-area">
      <p class="eyebrow">GHJ_PRJ_V_03</p>
      <h1>오늘 기준 KRX 수급 분석</h1>
      <p class="sub">아이디, 비밀번호, OpenAPI 키만 입력하면 기존 노트북처럼 누적 구간과 최근 일별 수급을 한 번에 조회하고 기본 시각화까지 바로 보여줍니다.</p>
      <div class="top-actions">
        <a class="secondary-link" href="/docs" target="_blank" rel="noopener">프로그램 상세 설명 보기</a>
        {% if user %}
          <a class="secondary-link" href="/history">조회 이력</a>
          <a class="secondary-link" href="/logout">로그아웃</a>
        {% endif %}
      </div>
      {% if user %}
        <p class="sub">로그인 계정: {{ user.email }}</p>
      {% endif %}

      {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
          <div class="messages">
            {% for category, message in messages %}
              <p class="message {{ category }}">{{ message }}</p>
            {% endfor %}
          </div>
        {% endif %}
      {% endwith %}

      <form method="post" id="collect-form">
        <div class="field span2">
          <label for="krx_id">KRX 아이디</label>
          <input id="krx_id" name="krx_id" value="{{ form.krx_id }}" autocomplete="username" required>
          <span class="help">KRX 정보데이터시스템 계정 아이디입니다.</span>
        </div>
        <div class="field span2">
          <label for="krx_pw">KRX 비밀번호</label>
          <input id="krx_pw" name="krx_pw" type="password" autocomplete="current-password" required>
          <span class="help">조회 때만 사용하고 파일에 저장하지 않습니다.</span>
        </div>
        <div class="field span2">
          <label for="openapi_key">OpenAPI 키</label>
          <input id="openapi_key" name="openapi_key" type="password" autocomplete="off" required>
          <span class="help">KRX OpenAPI 사이트에서 발급받은 인증키입니다.</span>
        </div>
        <div class="remember-field">
          <input id="remember_credentials" type="checkbox">
          <div class="remember-copy">
            <label for="remember_credentials">이 브라우저에 KRX 아이디/비밀번호/OpenAPI 키 저장</label>
            <span>개인 PC에서만 사용하세요. 저장값은 서버가 아니라 현재 브라우저에만 보관됩니다.</span>
          </div>
        </div>
        <div class="field span2">
          <label for="as_of">기준일</label>
          <input id="as_of" name="as_of" type="date" value="{{ form.as_of }}" required>
          <span class="help">기본값은 오늘입니다. 기존 노트북의 오늘 기준 실행과 같습니다.</span>
        </div>
        <div class="field span2">
          <label for="market">시장</label>
          <select id="market" name="market">
            {% for market in markets %}
              <option value="{{ market }}" {% if form.market == market %}selected{% endif %}>{{ market }}</option>
            {% endfor %}
          </select>
          <span class="help">전체, KOSPI, KOSDAQ, KONEX 중 조회 범위입니다.</span>
        </div>
        <div class="field span2">
          <button type="submit">전체 분석 실행</button>
          <span class="help">클릭하면 데이터 수집, 통합 엑셀 생성, 기본 차트 생성이 한 번에 진행됩니다.</span>
        </div>

        <div class="fixed-scope">
          <strong>분석 기간은 자동 고정됩니다.</strong>
          기준일 기준 6개월 누적, 3개월 누적, 1개월 누적을 조회하고, 1개월보다 짧은 구간은 기존 노트북처럼 최근 거래일 5개를 일별로 조회합니다. 주말과 휴장일은 프로그램이 자동으로 건너뜁니다.
        </div>
      </form>

      {% if result %}
        <h2 class="dashboard-title">노트북형 자동 분석 결과</h2>
        <dl class="result">
          <div class="metric"><dt>Base 행수</dt><dd>{{ "{:,}".format(result.base_rows) }}</dd></div>
          <div class="metric"><dt>Last 행수</dt><dd>{{ "{:,}".format(result.last_rows) }}</dd></div>
          <div class="metric"><dt>최근 거래일 분석</dt><dd>{{ result.trading_dates | length }}개</dd></div>
          <div class="metric"><dt>저장 파일</dt><dd>완료</dd></div>
        </dl>
        <p class="sub">자동 분석 범위: 기관합계/외국인 6개월 누적, 3개월 누적, 1개월 누적, 최근 거래일 {{ result.trading_dates | length }}개 일별 데이터. 최근 거래일: {{ ", ".join(result.trading_dates) }}</p>
        <a class="download" href="/download">엑셀 다운로드</a>

        {% if result.charts %}
          <section class="visuals">
            {% for chart in result.charts %}
              <article class="chart-card">
                <h3>{{ chart.title }}</h3>
                {{ chart.html | safe }}
              </article>
            {% endfor %}
          </section>
        {% endif %}

        {% if result.top_rows %}
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  {% for key in result.top_rows[0].keys() %}
                    <th>{{ key }}</th>
                  {% endfor %}
                </tr>
              </thead>
              <tbody>
                {% for row in result.top_rows %}
                  <tr>
                    {% for value in row.values() %}
                      <td>{{ "{:,}".format(value) if value is number else value }}</td>
                    {% endfor %}
                  </tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        {% endif %}
      {% endif %}

      <section class="guide">
        <h2>처음 사용하는 사람 준비 방법</h2>
        <ol>
          <li><a href="https://data.krx.co.kr/" target="_blank" rel="noopener">KRX 정보데이터시스템</a>에서 회원가입 후 아이디와 비밀번호를 준비합니다.</li>
          <li><a href="https://openapi.krx.co.kr/" target="_blank" rel="noopener">KRX OpenAPI 전용 사이트</a>에 접속합니다.</li>
          <li>OpenAPI 사이트에서 로그인 후 인증키 또는 API Key 발급/신청 메뉴로 이동합니다.</li>
          <li>발급된 API 키를 복사해 이 화면의 OpenAPI 키 입력칸에 붙여넣습니다.</li>
          <li>기준일과 시장을 선택한 뒤 수집 시작을 누르면 통합 엑셀 파일이 생성됩니다.</li>
        </ol>
        <p class="note">OpenAPI 발급 주소는 https://openapi.krx.co.kr 입니다. KRX 정보데이터시스템 메인에서도 OpenAPI 링크를 통해 이동할 수 있습니다. 메뉴명이 바뀌면 OpenAPI, API Key, 인증키, 활용신청 같은 단어로 찾아보세요.</p>
      </section>
      </div>
    </section>
  </main>
  <div class="loading" id="loading">
    <div class="loading-box">
      <p class="loading-title">KRX 데이터를 수집하고 있습니다</p>
      <p class="sub">로그인, 6개월/3개월/1개월 수집, 최근 거래일 자동 탐색, 시각화 생성 순서로 진행됩니다.</p>
      <div class="bar"><div class="bar-fill" id="bar-fill"></div></div>
      <div class="percent" id="percent">0%</div>
    </div>
  </div>
  <script>
    const form = document.getElementById("collect-form");
    const loading = document.getElementById("loading");
    const fill = document.getElementById("bar-fill");
    const percent = document.getElementById("percent");
    const rememberCredentials = document.getElementById("remember_credentials");
    const krxIdInput = document.getElementById("krx_id");
    const krxPwInput = document.getElementById("krx_pw");
    const openApiInput = document.getElementById("openapi_key");
    const credentialStorageKey = "ghj_prj_v_03_krx_credentials";

    function readSavedCredentials() {
      try {
        return JSON.parse(localStorage.getItem(credentialStorageKey) || "{}");
      } catch (error) {
        return {};
      }
    }

    function saveCredentials() {
      if (!rememberCredentials || !rememberCredentials.checked) {
        localStorage.removeItem(credentialStorageKey);
        return;
      }
      localStorage.setItem(credentialStorageKey, JSON.stringify({
        krxId: krxIdInput ? krxIdInput.value : "",
        krxPw: krxPwInput ? krxPwInput.value : "",
        openApiKey: openApiInput ? openApiInput.value : ""
      }));
    }

    if (rememberCredentials && krxIdInput && krxPwInput && openApiInput) {
      const savedCredentials = readSavedCredentials();
      if (savedCredentials.krxId || savedCredentials.krxPw || savedCredentials.openApiKey) {
        rememberCredentials.checked = true;
        if (!krxIdInput.value) krxIdInput.value = savedCredentials.krxId || "";
        krxPwInput.value = savedCredentials.krxPw || "";
        openApiInput.value = savedCredentials.openApiKey || "";
      }
      rememberCredentials.addEventListener("change", saveCredentials);
      [krxIdInput, krxPwInput, openApiInput].forEach((input) => {
        input.addEventListener("input", saveCredentials);
      });
    }

    if (form) {
      form.addEventListener("submit", () => {
        saveCredentials();
        loading.classList.add("active");
        let value = 0;
        const timer = setInterval(() => {
          const step = value < 45 ? 4 : value < 75 ? 2 : 1;
          value = Math.min(value + step, 95);
          fill.style.width = value + "%";
          percent.textContent = value + "%";
          if (value >= 95) clearInterval(timer);
        }, 450);
      });
    }
  </script>
</body>
</html>
"""

DOC_TEMPLATE = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GHJ_PRJ_V_03 상세 설명</title>
  <style>
    body {
      margin: 0;
      background: #f3f6f5;
      color: #1d282b;
      font-family: "Segoe UI", "Malgun Gothic", Arial, sans-serif;
      line-height: 1.65;
    }
    main {
      width: min(960px, calc(100% - 32px));
      margin: 0 auto;
      padding: 32px 0 56px;
    }
    article {
      background: #fff;
      border: 1px solid #d8e2e3;
      border-radius: 8px;
      padding: 30px;
      box-shadow: 0 16px 36px rgba(29, 40, 43, .08);
    }
    h1, h2, h3 { letter-spacing: 0; line-height: 1.3; }
    h1 { margin-top: 0; font-size: 34px; }
    h2 { margin-top: 30px; padding-top: 18px; border-top: 1px solid #d8e2e3; }
    code, pre {
      font-family: Consolas, "Courier New", monospace;
      background: #eef5f4;
      border-radius: 6px;
    }
    code { padding: 2px 5px; }
    pre { padding: 14px; overflow: auto; }
    a { color: #096653; font-weight: 800; }
    .back {
      display: inline-flex;
      margin-bottom: 16px;
      min-height: 42px;
      align-items: center;
      padding: 0 16px;
      border-radius: 6px;
      background: #0b8069;
      color: #fff;
      text-decoration: none;
      font-weight: 900;
    }
  </style>
</head>
<body>
  <main>
    <a class="back" href="/">분석 화면으로 돌아가기</a>
    <article>{{ content|safe }}</article>
  </main>
</body>
</html>
"""

AUTH_TEMPLATE = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GHJ_PRJ_V_03 · {{ title }}</title>
  <style>
    body { margin:0; min-height:100vh; display:grid; place-items:center; background:radial-gradient(circle at 18% 0%, rgba(57,255,136,.18), transparent 30%), linear-gradient(180deg,#020403,#06100c); color:#eafff3; font-family:"Segoe UI","Malgun Gothic",Arial,sans-serif; }
    .box { width:min(460px, calc(100% - 32px)); background:#08110d; border:1px solid rgba(57,255,136,.24); border-radius:8px; overflow:hidden; box-shadow:0 22px 70px rgba(0,255,136,.10); }
    .auth-head { padding:18px 24px; background:#050b09; color:#eafff3; border-bottom:1px solid rgba(57,255,136,.55); }
    .auth-head strong { display:block; font-size:18px; }
    .auth-head span { display:block; margin-top:4px; color:#8aae9f; font-size:12px; font-weight:800; }
    .auth-body { padding:28px; }
    h1 { margin:0 0 8px; letter-spacing:0; }
    p { color:#8aae9f; line-height:1.6; }
    form { display:grid; gap:14px; margin-top:18px; }
    label { color:#8aae9f; font-size:13px; font-weight:800; }
    input, button { width:100%; min-height:44px; border-radius:6px; font:inherit; box-sizing:border-box; }
    input { border:1px solid #1d3a2e; padding:0 12px; background:#07100c; color:#eafff3; }
    button { border:0; background:#39ff88; color:#001a0c; font-weight:900; cursor:pointer; }
    a { color:#39ff88; font-weight:900; }
    .message { padding:10px 12px; border-radius:6px; background:rgba(255,77,109,.10); color:#ff5f79; font-weight:800; border:1px solid rgba(255,77,109,.28); }
  </style>
</head>
<body>
  <main class="box">
    <div class="auth-head">
      <strong>GHJ_PRJ_V_03</strong>
      <span>INSTITUTIONAL FLOW TERMINAL</span>
    </div>
    <div class="auth-body">
    <h1>{{ title }}</h1>
    <p>{{ description }}</p>
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for category, message in messages %}
          <div class="message">{{ message }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}
    <form method="post">
      <div>
        <label for="email">이메일</label>
        <input id="email" name="email" type="email" autocomplete="email" required>
      </div>
      <div>
        <label for="password">비밀번호</label>
        <input id="password" name="password" type="password" autocomplete="current-password" required>
      </div>
      <button type="submit">{{ button }}</button>
    </form>
    <p>{{ switch_text }} <a href="{{ switch_url }}">{{ switch_label }}</a></p>
    </div>
  </main>
</body>
</html>
"""

HISTORY_TEMPLATE = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GHJ_PRJ_V_03 · 조회 이력</title>
  <style>
    body { margin:0; background:radial-gradient(circle at 16% 0%, rgba(57,255,136,.14), transparent 28%), linear-gradient(180deg,#020403,#06100c); color:#eafff3; font-family:"Segoe UI","Malgun Gothic",Arial,sans-serif; }
    main { width:min(1180px, calc(100% - 32px)); margin:0 auto; padding:32px 0; }
    .panel { background:#08110d; border:1px solid rgba(57,255,136,.24); border-radius:8px; overflow:hidden; box-shadow:0 22px 70px rgba(0,255,136,.10); }
    .hist-head { padding:20px 26px; background:#050b09; color:#eafff3; border-bottom:1px solid rgba(57,255,136,.55); }
    .hist-head p { margin:6px 0 0; color:#8aae9f; font-weight:800; }
    .hist-body { padding:26px; }
    h1 { margin:0 0 16px; letter-spacing:0; }
    a { color:#39ff88; font-weight:900; }
    table { width:100%; border-collapse:collapse; min-width:860px; }
    th, td { border-bottom:1px solid #1d3a2e; padding:10px 12px; text-align:left; white-space:nowrap; font-size:13px; }
    th { background:rgba(57,255,136,.09); color:#39ff88; }
    .wrap { overflow:auto; border:1px solid #1d3a2e; border-radius:8px; }
    .actions { display:flex; gap:10px; margin-bottom:18px; flex-wrap:wrap; }
  </style>
</head>
<body>
  <main>
    <section class="panel">
      <div class="hist-head">
        <h1>GHJ_PRJ_V_03 · 조회 이력</h1>
        <p>회원별 KRX 수급 조회 기록과 결과 파일을 추적합니다.</p>
      </div>
      <div class="hist-body">
      <div class="actions">
        <a href="/">분석 화면</a>
        <a href="/logout">로그아웃</a>
      </div>
      {% if jobs %}
        <div class="wrap">
          <table>
            <thead>
              <tr>
                <th>생성일</th>
                <th>상태</th>
                <th>기준일</th>
                <th>시장</th>
                <th>Base</th>
                <th>Last</th>
                <th>최근 거래일</th>
                <th>엑셀</th>
                <th>오류</th>
              </tr>
            </thead>
            <tbody>
              {% for job in jobs %}
                <tr>
                  <td>{{ job.created_at or "" }}</td>
                  <td>{{ job.status }}</td>
                  <td>{{ job.as_of }}</td>
                  <td>{{ job.market }}</td>
                  <td>{{ "{:,}".format(job.base_rows or 0) }}</td>
                  <td>{{ "{:,}".format(job.last_rows or 0) }}</td>
                  <td>{{ ", ".join(job.trading_dates or []) }}</td>
                  <td>{% if job.excel_path %}<a href="/download?path={{ job.excel_path|urlencode }}">다운로드</a>{% endif %}</td>
                  <td>{{ job.error_message or "" }}</td>
                </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      {% else %}
        <p>아직 조회 이력이 없습니다.</p>
      {% endif %}
      </div>
    </section>
  </main>
</body>
</html>
"""


def default_form() -> dict[str, Any]:
    return {
        "krx_id": "",
        "openapi_key": "",
        "as_of": date.today().isoformat(),
        "market": "ALL",
    }


@app.route("/", methods=["GET", "POST"])
def index():
    if supabase_configured() and not current_user():
        return redirect(url_for("login"))

    user = current_user()
    form = default_form()
    result = None

    if flask_request.method == "POST":
        form.update({
            "krx_id": flask_request.form.get("krx_id", "").strip(),
            "openapi_key": flask_request.form.get("openapi_key", "").strip(),
            "as_of": flask_request.form.get("as_of", form["as_of"]).strip(),
            "market": flask_request.form.get("market", "ALL").strip().upper(),
        })
        krx_pw = flask_request.form.get("krx_pw", "")

        try:
            result = run_collection(
                krx_id=form["krx_id"],
                krx_pw=krx_pw,
                openapi_key=form["openapi_key"],
                as_of=parse_yyyymmdd(form["as_of"]),
                market=form["market"],
                output_root=OUTPUT_ROOT,
                user_id=user["id"] if user else None,
            )
            session["last_output_path"] = str(result.output_path)
            if result.storage_path:
                session["last_storage_path"] = result.storage_path
            flash(f"수집 완료: {result.output_path}", "success")
        except Exception as exc:
            flash(str(exc), "error")

    return render_template_string(
        PAGE_TEMPLATE,
        form=form,
        markets=MARKET_CODES.keys(),
        result=result,
        user=user,
    )


@app.route("/download")
def download():
    requested_path = flask_request.args.get("path") or session.get("last_storage_path")
    if requested_path and supabase_configured():
        user = current_user()
        if not user:
            return redirect(url_for("login"))
        if not str(requested_path).startswith(f"{user['id']}/"):
            flash("해당 파일에 접근할 수 없습니다.", "error")
            return redirect(url_for("history"))
        return redirect(storage_signed_url(str(requested_path)))

    local_path_raw = session.get("last_output_path")
    local_path = Path(local_path_raw) if local_path_raw else None
    if not local_path or not local_path.exists() or not local_path.is_file():
        flash("다운로드할 결과 파일이 없습니다.", "error")
        return render_template_string(PAGE_TEMPLATE, form=default_form(), markets=MARKET_CODES.keys(), result=None, user=current_user())
    return send_file(local_path, as_attachment=True, download_name=local_path.name)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if not supabase_configured():
        flash("Supabase 환경변수가 설정되지 않았습니다.", "error")
        return redirect(url_for("index"))
    if flask_request.method == "POST":
        email = flask_request.form.get("email", "").strip()
        password = flask_request.form.get("password", "")
        try:
            data = supabase_signup(email, password)
            if data.get("access_token"):
                set_auth_session(data)
                return redirect(url_for("index"))
            flash("회원가입이 완료되었습니다. 이메일 인증을 켠 경우 메일 인증 후 로그인하세요.", "success")
            return redirect(url_for("login"))
        except Exception as exc:
            flash(str(exc), "error")
    return render_template_string(
        AUTH_TEMPLATE,
        title="회원가입",
        description="조회 이력을 저장하려면 먼저 계정을 만들어야 합니다.",
        button="회원가입",
        switch_text="이미 계정이 있나요?",
        switch_url=url_for("login"),
        switch_label="로그인",
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if not supabase_configured():
        flash("Supabase 환경변수가 설정되지 않았습니다.", "error")
        return redirect(url_for("index"))
    if flask_request.method == "POST":
        email = flask_request.form.get("email", "").strip()
        password = flask_request.form.get("password", "")
        try:
            set_auth_session(supabase_signin(email, password))
            return redirect(url_for("index"))
        except Exception as exc:
            flash(str(exc), "error")
    return render_template_string(
        AUTH_TEMPLATE,
        title="로그인",
        description="로그인 후 KRX 조회를 실행하면 결과가 Supabase에 저장됩니다.",
        button="로그인",
        switch_text="처음 사용하시나요?",
        switch_url=url_for("signup"),
        switch_label="회원가입",
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login" if supabase_configured() else "index"))


@app.route("/history")
def history():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    jobs = rest_select(
        "query_jobs",
        f"user_id=eq.{user['id']}&select=id,status,market,as_of,trading_dates,base_rows,last_rows,excel_path,error_message,created_at&order=created_at.desc&limit=50",
    )
    return render_template_string(HISTORY_TEMPLATE, jobs=jobs or [], user=user)


def markdown_to_html(markdown_text: str) -> str:
    html_parts = []
    in_list = False
    in_code = False
    code_lines = []

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            html_parts.append("</ul>")
            in_list = False

    for raw_line in markdown_text.splitlines():
        line = raw_line.rstrip()

        if line.startswith("```"):
            if in_code:
                html_parts.append(f"<pre><code>{escape(chr(10).join(code_lines))}</code></pre>")
                code_lines = []
                in_code = False
            else:
                close_list()
                in_code = True
            continue

        if in_code:
            code_lines.append(line)
            continue

        if not line:
            close_list()
            continue

        if line.startswith("# "):
            close_list()
            html_parts.append(f"<h1>{escape(line[2:])}</h1>")
        elif line.startswith("## "):
            close_list()
            html_parts.append(f"<h2>{escape(line[3:])}</h2>")
        elif line.startswith("### "):
            close_list()
            html_parts.append(f"<h3>{escape(line[4:])}</h3>")
        elif line.startswith("- "):
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
            html_parts.append(f"<li>{escape(line[2:])}</li>")
        elif re.match(r"^\d+\. ", line):
            close_list()
            text = re.sub(r"^\d+\. ", "", line)
            html_parts.append(f"<p>{escape(text)}</p>")
        else:
            close_list()
            safe_line = escape(line)
            if line.startswith("http://") or line.startswith("https://"):
                safe_line = f'<a href="{safe_line}" target="_blank" rel="noopener">{safe_line}</a>'
            html_parts.append(f"<p>{safe_line}</p>")

    close_list()
    return "\n".join(str(part) for part in html_parts)


@app.route("/docs")
def docs():
    if DOC_PATH.exists():
        content = DOC_PATH.read_text(encoding="utf-8")
    else:
        content = "# 프로그램 상세 설명\n\n문서 파일을 찾을 수 없습니다."
    return render_template_string(DOC_TEMPLATE, content=markdown_to_html(content))


def open_browser(port: int) -> None:
    webbrowser.open_new(f"http://127.0.0.1:{port}")


if __name__ == "__main__":
    port = int(os.environ.get("GHJ_PORT", "5000"))
    threading.Timer(1.0, open_browser, args=(port,)).start()
    app.run(host="127.0.0.1", port=port, debug=False)
