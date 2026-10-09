"""KISBroker (한국투자증권) 테스트.

원칙
- 가짜/데모 시세를 만들지 않는다. 캔들 값(OHLCV)은 전부 tests/conftest.py 가 Upbit 공개 API 에서 받아 캐시한
  **실제** KRW-BTC/KRW-ETH 캔들이다. KIS 는 자격증명 없이 호출할 수 있는 공개 엔드포인트가 없으므로, 실제 캔들을
  KIS 응답 스키마(stck_bsop_date/stck_oprc/... , stck_cntg_hour/cntg_vol/...) 로 "재포장" 해 `responses` 로 서빙한다.
  (날짜/시각은 KRX 달력에 맞춰 재배치한다.) 네트워크가 없으면 해당 테스트는 skip.
- 비공개 API(주문/잔고/체결) 모킹은 KIS Developers 공식 응답 스키마(필드명)를 그대로 따르며 이 파일 안에만 둔다.
  주문가격 등 가격 값은 실제 캔들에서 가져온다. 호가단위 테스트는 손계산 벡터.
"""

from __future__ import annotations

import json
import math
import os
import stat
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest
import requests
import responses
from freezegun import freeze_time

from tradingbot.brokers import create_broker, get_broker_class
from tradingbot.brokers.kis import (
    PAPER_BASE_URL,
    PAPER_REQUEST_INTERVAL,
    PATH_BALANCE,
    PATH_DAILY_CCLD,
    PATH_DAILY_CHART,
    PATH_HOLIDAY,
    PATH_ORDER_CASH,
    PATH_ORDER_RVSECNCL,
    PATH_PAST_MINUTE_CHART,
    PATH_PRICE,
    PATH_PSBL_ORDER,
    PATH_TODAY_MINUTE_CHART,
    PATH_TOKEN,
    REAL_BASE_URL,
    REAL_REQUEST_INTERVAL,
    KISBroker,
    KISBuyingPower,
    krx_tick_size,
    round_to_tick,
)
from tradingbot.config import AppConfig
from tradingbot.exceptions import (
    AuthenticationError,
    BrokerError,
    ConfigError,
    InsufficientFunds,
    OrderError,
    RateLimitError,
)
from tradingbot.models import Candle, OrderSide, OrderStatus, OrderType
from tradingbot.strategies.base import df_to_candles
from tradingbot.utils.timeutil import KST

SYMBOL = "005930"
APP_KEY = "PSxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
APP_SECRET = "test-app-secret-not-a-real-secret"
ACCOUNT = "50012345-01"
# 고정 기준일 (금요일) — freezegun 과 조합해 결정적으로 만든다
END_DAY = date(2025, 9, 26)
assert END_DAY.weekday() == 4

approx = pytest.approx


# ---------------------------------------------------------------------------- 공통 헬퍼
def ok(**body: object) -> dict:
    out: dict = {"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "정상처리 되었습니다!"}
    out.update(body)
    return out


def kis_error(msg_cd: str, msg1: str) -> dict:
    return {"rt_cd": "1", "msg_cd": msg_cd, "msg1": msg1}


def token_body(
    expired_kst: str | None = None, token: str = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.test"
) -> dict:
    """/oauth2/tokenP 응답 스키마. 만료시각(KST 표기)은 기본적으로 지금 + 1일."""
    if expired_kst is None:
        expired_kst = (datetime.now(KST) + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": 86400,
        "access_token_token_expired": expired_kst,
    }


def add_token(rsps: responses.RequestsMock, base: str = PAPER_BASE_URL, **kw: object) -> None:
    rsps.add(responses.POST, base + PATH_TOKEN, json=token_body(**kw), status=200)


def make_broker(tmp_path, *, sandbox: bool = True, account: str | None = ACCOUNT, **kw) -> KISBroker:
    kw.setdefault("request_interval", 0)
    kw.setdefault("holiday_check", False)
    return KISBroker(
        APP_KEY,
        APP_SECRET,
        account,
        sandbox=sandbox,
        token_path=tmp_path / "kis_token.json",
        **kw,
    )


def query(request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(request.url).query, keep_blank_values=True).items()}


def body_of(call) -> dict:
    return json.loads(call.request.body)


def weekdays_ending(end: date, n: int) -> list[date]:
    """end(평일 포함) 로 끝나는 평일 n 개, 오래된→최신."""
    out: list[date] = []
    d = end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def kst(d: date, t: dtime) -> datetime:
    return datetime.combine(d, t, tzinfo=KST)


def fmt_int(x: float) -> str:
    return str(int(round(x)))


@pytest.fixture
def rsps():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as m:
        yield m


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


@pytest.fixture
def eth_daily_candles(eth_daily_df) -> list[Candle]:
    return df_to_candles(eth_daily_df)


# ---------------------------------------------------------------------------- KIS 차트 응답 서버 (실제 캔들 재포장)
def daily_output1(last: Candle) -> dict:
    """inquire-daily-itemchartprice output1 (종목 요약) — 값은 실제 캔들에서."""
    return {
        "prdy_vrss": "0",
        "prdy_vrss_sign": "3",
        "prdy_ctrt": "0.00",
        "stck_prdy_clpr": fmt_int(last.close),
        "acml_vol": fmt_int(last.volume),
        "acml_tr_pbmn": fmt_int(last.close * last.volume),
        "hts_kor_isnm": "삼성전자",
        "stck_prpr": fmt_int(last.close),
        "stck_shrn_iscd": SYMBOL,
        "prdy_vol": fmt_int(last.volume),
        "stck_mxpr": fmt_int(last.high),
        "stck_llam": fmt_int(last.low),
        "stck_oprc": fmt_int(last.open),
        "stck_hgpr": fmt_int(last.high),
        "stck_lwpr": fmt_int(last.low),
        "stck_prdy_oprc": fmt_int(last.open),
        "stck_prdy_hgpr": fmt_int(last.high),
        "stck_prdy_lwpr": fmt_int(last.low),
        "askp": fmt_int(last.close),
        "bidp": fmt_int(last.close),
        "prdy_vrss_vol": "0",
        "vol_tnrt": "0.00",
        "stck_fcam": "100",
        "lstn_stcn": "5969782550",
        "cpfn": "7780",
        "hts_avls": "0",
        "per": "0.00",
        "eps": "0",
        "pbr": "0.00",
        "itewhol_loan_rmnd_ratem": "0.00",
    }


def daily_row(d: date, c: Candle) -> dict:
    """inquire-daily-itemchartprice output2 행 (일/주봉)."""
    return {
        "stck_bsop_date": d.strftime("%Y%m%d"),
        "stck_clpr": fmt_int(c.close),
        "stck_oprc": fmt_int(c.open),
        "stck_hgpr": fmt_int(c.high),
        "stck_lwpr": fmt_int(c.low),
        "acml_vol": fmt_int(c.volume),
        "acml_tr_pbmn": fmt_int(c.close * c.volume),
        "flng_cls_code": "00",
        "prtt_rate": "0.00",
        "mod_yn": "N",
        "prdy_vrss_sign": "3",
        "prdy_vrss": "0",
        "revl_issu_reas": "",
    }


class DailyChartServer:
    """FHKST03010100: FID_INPUT_DATE_1~2 범위의 행을 최신→과거로 최대 100개."""

    def __init__(self, by_date: dict[date, Candle], period: str = "D") -> None:
        self.by_date = by_date
        self.period = period
        self.requests: list[dict[str, str]] = []

    def __call__(self, request):
        q = query(request)
        self.requests.append(q)
        assert q["FID_COND_MRKT_DIV_CODE"] == "J"
        assert q["FID_INPUT_ISCD"] == SYMBOL
        assert q["FID_PERIOD_DIV_CODE"] == self.period
        assert q["FID_ORG_ADJ_PRC"] == "0"
        d1 = datetime.strptime(q["FID_INPUT_DATE_1"], "%Y%m%d").date()
        d2 = datetime.strptime(q["FID_INPUT_DATE_2"], "%Y%m%d").date()
        dates = sorted((d for d in self.by_date if d1 <= d <= d2), reverse=True)[:100]
        rows = [daily_row(d, self.by_date[d]) for d in dates]
        last = self.by_date[max(self.by_date)]
        body = ok(output1=daily_output1(last), output2=rows)
        return 200, {"tr_id": "FHKST03010100"}, json.dumps(body, ensure_ascii=False)


def minute_output1(rows: list[dict]) -> dict:
    last = rows[0] if rows else {"stck_prpr": "0"}
    return {
        "prdy_vrss": "0",
        "prdy_vrss_sign": "3",
        "prdy_ctrt": "0.00",
        "stck_prdy_clpr": last["stck_prpr"],
        "acml_vol": str(sum(int(r["cntg_vol"]) for r in rows)),
        "acml_tr_pbmn": str(sum(int(r["acml_tr_pbmn"]) for r in rows)),
        "hts_kor_isnm": "삼성전자",
        "stck_prpr": last["stck_prpr"],
    }


def minute_row(d: date, t: dtime, c: Candle) -> dict:
    """inquire-time-itemchartprice / inquire-time-dailychartprice output2 행 (1분봉)."""
    return {
        "stck_bsop_date": d.strftime("%Y%m%d"),
        "stck_cntg_hour": t.strftime("%H%M%S"),
        "stck_prpr": fmt_int(c.close),
        "stck_oprc": fmt_int(c.open),
        "stck_hgpr": fmt_int(c.high),
        "stck_lwpr": fmt_int(c.low),
        "cntg_vol": fmt_int(c.volume),
        "acml_tr_pbmn": fmt_int(c.close * c.volume),
    }


class MinuteChartServer:
    """당일분봉(30행/호출, FHKST03010200) + 일별분봉(120행/호출, FHKST03010230) 을 함께 흉내낸다."""

    def __init__(self, today: date, past_ok: bool = True) -> None:
        self.today = today
        self.past_ok = past_ok
        self.days: dict[date, dict[str, dict]] = {}
        self.today_requests: list[dict[str, str]] = []
        self.past_requests: list[dict[str, str]] = []

    def add_day(
        self, d: date, candles: list[Candle], start: dtime = dtime(9, 0), times: list[dtime] | None = None
    ):
        rows = self.days.setdefault(d, {})
        if times is None:
            base = kst(d, start)
            times = [(base + timedelta(minutes=i)).time() for i in range(len(candles))]
        for t, c in zip(times, candles, strict=True):
            rows[t.strftime("%H%M%S")] = minute_row(d, t, c)
        return rows

    def _page(self, d: date, until: str, size: int) -> list[dict]:
        rows = self.days.get(d, {})
        keys = sorted((k for k in rows if k <= until), reverse=True)[:size]
        return [rows[k] for k in keys]

    def today_handler(self, request):
        q = query(request)
        self.today_requests.append(q)
        assert q["FID_COND_MRKT_DIV_CODE"] == "J"
        assert q["FID_PW_DATA_INCU_YN"] == "Y"
        assert q["FID_INPUT_ISCD"] == SYMBOL
        rows = self._page(self.today, q["FID_INPUT_HOUR_1"], 30)
        return 200, {}, json.dumps(ok(output1=minute_output1(rows), output2=rows), ensure_ascii=False)

    def past_handler(self, request):
        q = query(request)
        self.past_requests.append(q)
        if not self.past_ok:
            return (
                200,
                {},
                json.dumps(kis_error("OPSQ0002", "모의투자 미지원 TR 입니다."), ensure_ascii=False),
            )
        assert q["FID_PW_DATA_INCU_YN"] == "Y"
        d = datetime.strptime(q["FID_INPUT_DATE_1"], "%Y%m%d").date()
        rows = self._page(d, q["FID_INPUT_HOUR_1"], 120)
        return 200, {}, json.dumps(ok(output1=minute_output1(rows), output2=rows), ensure_ascii=False)

    def register(self, rsps: responses.RequestsMock, base: str = PAPER_BASE_URL) -> None:
        rsps.add_callback(
            responses.GET,
            base + PATH_TODAY_MINUTE_CHART,
            callback=self.today_handler,
            content_type="application/json",
        )
        rsps.add_callback(
            responses.GET,
            base + PATH_PAST_MINUTE_CHART,
            callback=self.past_handler,
            content_type="application/json",
        )


def minute_df(rows: dict[str, dict], d: date) -> pd.DataFrame:
    """서버가 들고 있는 1분봉 행 → KST 인덱스 DataFrame (독립 검증용)."""
    recs = []
    for k, r in rows.items():
        ts = kst(d, dtime(int(k[:2]), int(k[2:4]), int(k[4:6])))
        recs.append(
            {
                "ts": ts,
                "open": float(r["stck_oprc"]),
                "high": float(r["stck_hgpr"]),
                "low": float(r["stck_lwpr"]),
                "close": float(r["stck_prpr"]),
                "volume": float(r["cntg_vol"]),
            }
        )
    df = pd.DataFrame(recs).set_index("ts").sort_index()
    return df


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return agg.dropna(subset=["open"])


# ---------------------------------------------------------------------------- 계좌/주문 응답 스키마
def balance_row(pdno: str, hldg_qty: int, avg: float, prpr: float, name: str = "삼성전자") -> dict:
    """inquire-balance output1 행."""
    return {
        "pdno": pdno,
        "prdt_name": name,
        "trad_dvsn_name": "현금",
        "bfdy_buy_qty": "0",
        "bfdy_sll_qty": "0",
        "thdt_buyqty": "0",
        "thdt_sll_qty": "0",
        "hldg_qty": str(hldg_qty),
        "ord_psbl_qty": str(hldg_qty),
        "pchs_avg_pric": f"{avg:.4f}",
        "pchs_amt": fmt_int(avg * hldg_qty),
        "prpr": fmt_int(prpr),
        "evlu_amt": fmt_int(prpr * hldg_qty),
        "evlu_pfls_amt": fmt_int((prpr - avg) * hldg_qty),
        "evlu_pfls_rt": "0.00",
        "evlu_erng_rt": "0.00",
        "loan_dt": "",
        "loan_amt": "0",
        "stln_slng_chgs": "0",
        "expd_dt": "",
        "fltt_rt": "0.00",
        "bfdy_cprs_icdc": "0",
        "item_mgna_rt_name": "20%",
        "grta_rt_name": "",
        "sbst_pric": "0",
        "stck_loan_unpr": "0",
    }


def balance_summary(dnca: float, d2: float, scts: float, nass: float | None = None) -> dict:
    """inquire-balance output2 (계좌 요약)."""
    tot = dnca + scts
    return {
        "dnca_tot_amt": fmt_int(dnca),
        "nxdy_excc_amt": fmt_int(dnca),
        "prvs_rcdl_excc_amt": fmt_int(d2),
        "cma_evlu_amt": "0",
        "bfdy_buy_amt": "0",
        "thdt_buy_amt": "0",
        "nxdy_auto_rdpt_amt": "0",
        "bfdy_sll_amt": "0",
        "thdt_sll_amt": "0",
        "d2_auto_rdpt_amt": "0",
        "bfdy_tlex_amt": "0",
        "thdt_tlex_amt": "0",
        "tot_loan_amt": "0",
        "scts_evlu_amt": fmt_int(scts),
        "tot_evlu_amt": fmt_int(tot),
        "nass_amt": fmt_int(nass if nass is not None else tot),
        "fncg_gld_auto_rdpt_yn": "N",
        "pchs_amt_smtl_amt": fmt_int(scts),
        "evlu_amt_smtl_amt": fmt_int(scts),
        "evlu_pfls_smtl_amt": "0",
        "tot_stln_slng_chgs": "0",
        "bfdy_tot_asst_evlu_amt": fmt_int(tot),
        "asst_icdc_amt": "0",
        "asst_icdc_erng_rt": "0.00",
    }


def ccld_row(
    *,
    odno: str,
    ord_dt: str,
    pdno: str = SYMBOL,
    side: str = "02",
    ord_qty: int,
    ord_unpr: float,
    tot_ccld_qty: int = 0,
    avg_prvs: float = 0.0,
    rmn_qty: int | None = None,
    rjct_qty: int = 0,
    cncl_yn: str = "N",
    ord_dvsn_cd: str = "00",
    orgno: str = "91252",
    ord_tmd: str = "100102",
) -> dict:
    """inquire-daily-ccld output1 행."""
    rmn = ord_qty - tot_ccld_qty - rjct_qty if rmn_qty is None else rmn_qty
    return {
        "ord_dt": ord_dt,
        "ord_gno_brno": orgno,
        "odno": odno,
        "orgn_odno": "",
        "ord_dvsn_name": "지정가" if ord_dvsn_cd == "00" else "시장가",
        "sll_buy_dvsn_cd": side,
        "sll_buy_dvsn_cd_name": "현금매수" if side == "02" else "현금매도",
        "pdno": pdno,
        "prdt_name": "삼성전자",
        "ord_qty": str(ord_qty),
        "ord_unpr": fmt_int(ord_unpr),
        "ord_tmd": ord_tmd,
        "tot_ccld_qty": str(tot_ccld_qty),
        "avg_prvs": f"{avg_prvs:.2f}",
        "cncl_yn": cncl_yn,
        "tot_ccld_amt": fmt_int(avg_prvs * tot_ccld_qty),
        "loan_dt": "",
        "ordr_empno": "",
        "ord_dvsn_cd": ord_dvsn_cd,
        "cnc_cfrm_qty": "0",
        "rmn_qty": str(rmn),
        "rjct_qty": str(rjct_qty),
        "ccld_cndt_name": "",
        "inqr_ip_addr": "",
        "cpbc_ordp_ord_rcit_dvsn_cd": "",
        "cpbc_ordp_infm_mthd_dvsn_cd": "",
        "infm_tmd": "",
        "ctac_tlno": "",
        "prdt_type_cd": "300",
        "excg_dvsn_cd": "02",
        "cpbc_ordp_mtrl_dvsn_cd": "",
        "ord_orgno": "",
        "rsvn_ord_end_dt": "",
        "excg_id_dvsn_Cd": "KRX",
        "stpm_cndt_pric": "0",
        "stpm_efct_occr_dtmd": "",
    }


def ccld_body(rows: list[dict], *, fk: str = "", nk: str = "") -> dict:
    tot_qty = sum(int(r["tot_ccld_qty"]) for r in rows)
    return ok(
        ctx_area_fk100=fk,
        ctx_area_nk100=nk,
        output1=rows,
        output2={
            "tot_ord_qty": str(sum(int(r["ord_qty"]) for r in rows)),
            "tot_ccld_qty": str(tot_qty),
            "tot_ccld_amt": str(sum(int(r["tot_ccld_amt"]) for r in rows)),
            "prsm_tlex_smtl": "0",
            "pchs_avg_pric": "0.00",
        },
    )


def order_output(odno: str = "0000117057", orgno: str = "91252", tmd: str = "121052") -> dict:
    """order-cash output (공식 샘플: 대문자 키)."""
    return ok(output={"KRX_FWDG_ORD_ORGNO": orgno, "ODNO": odno, "ORD_TMD": tmd})


def price_output(price: float) -> dict:
    """inquire-price output 의 핵심 필드."""
    return ok(
        output={
            "iscd_stat_cls_code": "55",
            "marg_rate": "20.00",
            "rprs_mrkt_kor_name": "KOSPI200",
            "bstp_kor_isnm": "전기.전자",
            "temp_stop_yn": "N",
            "stck_prpr": fmt_int(price),
            "prdy_vrss": "0",
            "prdy_vrss_sign": "3",
            "prdy_ctrt": "0.00",
            "acml_tr_pbmn": "0",
            "acml_vol": "0",
            "stck_oprc": fmt_int(price),
            "stck_hgpr": fmt_int(price),
            "stck_lwpr": fmt_int(price),
            "stck_mxpr": fmt_int(price),
            "stck_llam": fmt_int(price),
            "stck_sdpr": fmt_int(price),
            "aspr_unit": str(krx_tick_size(price)),
            "stck_shrn_iscd": SYMBOL,
        }
    )


def upper_limit(base_price: float) -> int:
    """KRX 상한가 (기준가 +30%, 호가단위 미만 절사). 시장가 매수가능수량의 계산단가."""
    px = base_price * 1.3
    tick = krx_tick_size(px)
    return int(math.floor(px / tick) * tick)


def psbl_output(cash: int, calc_price: int, *, max_cash: int | None = None) -> dict:
    """inquire-psbl-order output (공식 샘플 chk_inquire_psbl_order.py 의 컬럼). 미수 미사용 계좌.

    수량 = 금액 // 계산단가. 시장가 조회면 계산단가는 상한가, 지정가면 주문단가.
    """
    mx = cash if max_cash is None else max_cash
    return ok(
        output={
            "ord_psbl_cash": str(cash),
            "ord_psbl_sbst": "0",
            "ruse_psbl_amt": "0",
            "fund_rpch_chgs": "0",
            "psbl_qty_calc_unpr": str(calc_price),
            "nrcvb_buy_amt": str(cash),
            "nrcvb_buy_qty": str(cash // calc_price),
            "max_buy_amt": str(mx),
            "max_buy_qty": str(mx // calc_price),
            "cma_evlu_amt": "0",
            "ovrs_re_use_amt_wcrc": "0",
            "ord_psbl_frcr_amt_wcrc": "0",
        }
    )


def add_psbl(rsps: responses.RequestsMock, cash: int, calc_price: int, base: str = PAPER_BASE_URL) -> None:
    rsps.add(responses.GET, base + PATH_PSBL_ORDER, json=psbl_output(cash, calc_price))


def kis_urls(rsps: responses.RequestsMock) -> list[str]:
    return [c.request.url.split("?")[0] for c in rsps.calls]


# ============================================================================ 순수 로직
class TestTickAndRounding:
    @pytest.mark.parametrize(
        "price,tick",
        [
            (1, 1),
            (1999, 1),
            (2000, 5),
            (4999, 5),
            (5000, 10),
            (19999, 10),
            (20000, 50),
            (49999, 50),
            (50000, 100),
            (199999, 100),
            (200000, 500),
            (499999, 500),
            (500000, 1000),
            (150_000_000, 1000),
        ],
    )
    def test_tick_table(self, price, tick):
        assert krx_tick_size(price) == tick

    @pytest.mark.parametrize(
        "price,expected",
        [
            (1999.4, 1999.0),
            (1999.6, 2000.0),
            (2001, 2000.0),
            (2003, 2005.0),
            (4997.5, 5000.0),
            (12_345, 12_350.0),
            (19_995, 20_000.0),
            (70_049, 70_000.0),
            (70_050, 70_100.0),
            (199_990, 200_000.0),
            (333_333, 333_500.0),
            (499_999, 500_000.0),
            (1_234_567, 1_235_000.0),
        ],
    )
    def test_round_to_tick(self, price, expected):
        assert round_to_tick(price) == expected

    def test_round_price_and_quantity(self, tmp_path):
        b = make_broker(tmp_path)
        assert b.round_price(SYMBOL, 70_050) == 70_100.0
        assert b.round_quantity(SYMBOL, 3.99) == 3.0
        assert b.round_quantity(SYMBOL, 0.9) == 0.0
        assert b.round_quantity(SYMBOL, -2) == 0.0
        assert b.round_quantity(SYMBOL, float("nan")) == 0.0
        assert b.round_quantity(SYMBOL, 2.9999999999) == 3.0  # 부동소수 오차 허용
        with pytest.raises(OrderError):
            b.round_price(SYMBOL, 0)
        with pytest.raises(OrderError):
            b.round_price(SYMBOL, float("inf"))

    def test_meta(self, tmp_path):
        b = make_broker(tmp_path)
        assert b.quote_currency(SYMBOL) == "KRW"
        assert b.base_currency(SYMBOL) == SYMBOL
        assert b.min_order_value(SYMBOL) == 0.0
        assert b.name == "kis"
        assert b.asset_class.value == "stock"
        assert "4h" not in b.supported_intervals
        assert "1h" in b.supported_intervals and "1w" in b.supported_intervals


class TestAccountParsing:
    @pytest.mark.parametrize("raw", ["50012345-01", "5001234501", " 50012345-01 ", "50012345 01"])
    def test_valid(self, raw):
        assert KISBroker.parse_account_no(raw) == ("50012345", "01")

    @pytest.mark.parametrize("raw", ["5001234", "500123450", "50012345-1", "abcdefgh-01", "", "50012345-012"])
    def test_invalid(self, raw):
        with pytest.raises(ConfigError):
            KISBroker.parse_account_no(raw)

    def test_broker_without_credentials_is_constructible(self, tmp_path):
        b = KISBroker(token_path=tmp_path / "t.json", request_interval=0)
        assert b.account_no is None
        with pytest.raises(AuthenticationError, match="KIS_APP_KEY"):
            b.get_ticker(SYMBOL)
        with pytest.raises(AuthenticationError):
            b.get_balances()

    def test_account_required_for_private_calls(self, tmp_path):
        b = make_broker(tmp_path, account=None)
        with pytest.raises(AuthenticationError, match="KIS_ACCOUNT_NO"):
            b.get_balances()
        with pytest.raises(AuthenticationError, match="KIS_ACCOUNT_NO"):
            b.place_order(SYMBOL, OrderSide.BUY, 1)
        assert b.account_no is None
        assert make_broker(tmp_path).account_no == "50012345-01"

    def test_invalid_symbol(self, tmp_path):
        b = make_broker(tmp_path)
        with pytest.raises(OrderError):
            b.place_order("KRW-BTC", OrderSide.BUY, 1)
        with pytest.raises(BrokerError):
            b.get_ticker("")

    def test_sandbox_flag_selects_base_url(self, tmp_path):
        assert make_broker(tmp_path, sandbox=True).base_url == PAPER_BASE_URL
        assert make_broker(tmp_path, sandbox=False).base_url == REAL_BASE_URL


class TestFromConfig:
    def test_from_env_and_registry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIS_APP_KEY", APP_KEY)
        monkeypatch.setenv("KIS_APP_SECRET", APP_SECRET)
        monkeypatch.setenv("KIS_ACCOUNT_NO", "5001234501")
        cfg = AppConfig.model_validate(
            {
                "broker": {
                    "name": "kis",
                    "sandbox": False,
                    "extra": {"token_path": str(tmp_path / "tok.json"), "request_interval": 0},
                },
                "symbols": [SYMBOL],
                "interval": "1d",
            }
        )
        b = create_broker("kis", cfg)
        assert isinstance(b, KISBroker)
        assert get_broker_class("kis") is KISBroker
        assert b.sandbox is False and b.base_url == REAL_BASE_URL
        assert b.account_no == "50012345-01"
        assert b.token_path == tmp_path / "tok.json"

    def test_from_config_without_env(self, tmp_path, monkeypatch):
        for k in ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setattr("tradingbot.config.load_dotenv", lambda *a, **k: None)
        cfg = AppConfig.model_validate({"broker": {"name": "kis", "sandbox": True}, "symbols": [SYMBOL]})
        b = KISBroker.from_config(cfg)
        assert b.sandbox is True and b.account_no is None
        with pytest.raises(AuthenticationError):
            b.get_ticker(SYMBOL)

    def test_bad_timeout_extra(self, tmp_path, monkeypatch):
        cfg = AppConfig.model_validate(
            {"broker": {"name": "kis", "extra": {"timeout": "fast"}}, "symbols": [SYMBOL]}
        )
        with pytest.raises(ConfigError):
            KISBroker.from_config(cfg)


# ============================================================================ 인증/토큰
class TestToken:
    @freeze_time("2025-09-26 01:00:00+00:00")
    def test_issue_caches_to_file_and_reuses(self, tmp_path, rsps, daily_candles):
        add_token(rsps, expired_kst="2025-09-27 10:00:00")
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_PRICE, json=price_output(daily_candles[-1].close))
        b = make_broker(tmp_path)
        price = b.get_ticker(SYMBOL)
        assert price == float(fmt_int(daily_candles[-1].close))

        # 파일 저장 + 권한 600
        path = tmp_path / "kis_token.json"
        assert path.is_file()
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        data = json.loads(path.read_text())
        assert data["access_token"] == token_body()["access_token"]
        assert data["env"] == "paper"
        # 만료 시각: KST 2025-09-27 10:00 → UTC 01:00
        assert datetime.fromisoformat(data["expires_at"]) == datetime(2025, 9, 27, 1, 0, tzinfo=timezone.utc)

        # 새 인스턴스가 파일 토큰을 재사용 → 토큰 POST 는 1번만
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_PRICE, json=price_output(daily_candles[-2].close))
        b2 = make_broker(tmp_path)
        b2.get_ticker(SYMBOL)
        token_calls = [c for c in rsps.calls if c.request.url.endswith(PATH_TOKEN)]
        assert len(token_calls) == 1
        assert body_of(token_calls[0]) == {
            "grant_type": "client_credentials",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
        }

    def test_expired_file_token_is_reissued(self, tmp_path, rsps, daily_candles):
        path = tmp_path / "kis_token.json"
        path.write_text(
            json.dumps(
                {
                    "access_token": "stale",
                    "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                    "env": "paper",
                }
            )
        )
        add_token(rsps, token="fresh-token")
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_PRICE, json=price_output(daily_candles[-1].close))
        make_broker(tmp_path).get_ticker(SYMBOL)
        assert rsps.calls[0].request.url.endswith(PATH_TOKEN)
        assert rsps.calls[1].request.headers["authorization"] == "Bearer fresh-token"
        assert json.loads(path.read_text())["access_token"] == "fresh-token"

    def test_file_token_from_other_app_key_ignored(self, tmp_path, rsps, daily_candles):
        path = tmp_path / "kis_token.json"
        path.write_text(
            json.dumps(
                {
                    "access_token": "other-app",
                    "expires_at": (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat(),
                    "env": "paper",
                    "app_key_fingerprint": "deadbeefdeadbeef",
                }
            )
        )
        add_token(rsps, token="mine")
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_PRICE, json=price_output(daily_candles[-1].close))
        make_broker(tmp_path).get_ticker(SYMBOL)
        assert rsps.calls[1].request.headers["authorization"] == "Bearer mine"

    def test_corrupt_token_file_is_ignored(self, tmp_path, rsps, daily_candles):
        (tmp_path / "kis_token.json").write_text("{not json")
        add_token(rsps)
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_PRICE, json=price_output(daily_candles[-1].close))
        make_broker(tmp_path).get_ticker(SYMBOL)
        assert len(rsps.calls) == 2

    def test_token_without_expiry_uses_expires_in(self, tmp_path, rsps, daily_candles):
        rsps.add(
            responses.POST,
            PAPER_BASE_URL + PATH_TOKEN,
            json={"access_token": "t", "token_type": "Bearer", "expires_in": 7200},
        )
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_PRICE, json=price_output(daily_candles[-1].close))
        with freeze_time("2025-09-26 01:00:00+00:00"):
            make_broker(tmp_path).get_ticker(SYMBOL)
        data = json.loads((tmp_path / "kis_token.json").read_text())
        assert datetime.fromisoformat(data["expires_at"]) == datetime(2025, 9, 26, 3, 0, tzinfo=timezone.utc)

    def test_issue_failure_raises_authentication_error(self, tmp_path, rsps):
        rsps.add(
            responses.POST,
            PAPER_BASE_URL + PATH_TOKEN,
            status=403,
            json={
                "error_description": "접근토큰 발급 잠시 후 다시 시도하세요(1분당 1회)",
                "error_code": "EGW00133",
            },
        )
        with pytest.raises(AuthenticationError, match="EGW00133"):
            make_broker(tmp_path).get_ticker(SYMBOL)
        assert not (tmp_path / "kis_token.json").exists()

    def test_token_network_error(self, tmp_path, rsps):
        rsps.add(
            responses.POST, PAPER_BASE_URL + PATH_TOKEN, body=requests.exceptions.ConnectionError("boom")
        )
        with pytest.raises(BrokerError):
            make_broker(tmp_path).get_ticker(SYMBOL)

    def test_expired_token_refreshed_once(self, tmp_path, rsps, daily_candles):
        add_token(rsps, token="old")
        rsps.add(
            responses.GET,
            PAPER_BASE_URL + PATH_PRICE,
            json=kis_error("EGW00123", "기간이 만료된 token 입니다."),
        )
        add_token(rsps, token="new")
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_PRICE, json=price_output(daily_candles[-1].close))
        b = make_broker(tmp_path)
        assert b.get_ticker(SYMBOL) > 0
        urls = [c.request.url.split("?")[0] for c in rsps.calls]
        assert urls == [
            PAPER_BASE_URL + PATH_TOKEN,
            PAPER_BASE_URL + PATH_PRICE,
            PAPER_BASE_URL + PATH_TOKEN,
            PAPER_BASE_URL + PATH_PRICE,
        ]
        assert rsps.calls[1].request.headers["authorization"] == "Bearer old"
        assert rsps.calls[3].request.headers["authorization"] == "Bearer new"
        assert json.loads((tmp_path / "kis_token.json").read_text())["access_token"] == "new"

    def test_http_401_refreshed_then_fails(self, tmp_path, rsps):
        add_token(rsps, token="old")
        rsps.add(
            responses.GET,
            PAPER_BASE_URL + PATH_PRICE,
            status=401,
            json=kis_error("EGW00123", "기간이 만료된 token 입니다."),
        )
        add_token(rsps, token="new")
        rsps.add(
            responses.GET,
            PAPER_BASE_URL + PATH_PRICE,
            status=401,
            json=kis_error("EGW00123", "기간이 만료된 token 입니다."),
        )
        with pytest.raises(AuthenticationError, match="EGW00123"):
            make_broker(tmp_path).get_ticker(SYMBOL)
        assert len(rsps.calls) == 4  # token, price, token, price — 재발급은 1회만


class TestHeaders:
    def test_request_headers(self, tmp_path, rsps, daily_candles):
        add_token(rsps)
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_PRICE, json=price_output(daily_candles[-1].close))
        make_broker(tmp_path).get_ticker(SYMBOL)
        req = rsps.calls[1].request
        h = req.headers
        assert h["authorization"] == f"Bearer {token_body()['access_token']}"
        assert h["appkey"] == APP_KEY
        assert h["appsecret"] == APP_SECRET
        assert h["tr_id"] == "FHKST01010100"
        assert h["custtype"] == "P"
        assert h["tr_cont"] == ""
        assert h["content-type"].startswith("application/json")
        assert "hashkey" not in {k.lower() for k in h}
        q = query(req)
        assert q == {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": SYMBOL}


# ============================================================================ 현재가 / 오류 매핑
class TestTickerAndErrors:
    def test_ticker(self, tmp_path, rsps, daily_candles):
        add_token(rsps)
        px = daily_candles[-1].close
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_PRICE, json=price_output(px))
        assert make_broker(tmp_path).get_ticker(SYMBOL) == float(fmt_int(px))

    def test_rt_cd_error_raises_broker_error(self, tmp_path, rsps):
        add_token(rsps)
        rsps.add(
            responses.GET,
            PAPER_BASE_URL + PATH_PRICE,
            json=kis_error("OPSQ0002", "조회할 수 없는 종목코드 입니다."),
        )
        with pytest.raises(BrokerError, match="조회할 수 없는 종목코드") as ei:
            make_broker(tmp_path).get_ticker(SYMBOL)
        assert ei.value.payload["msg_cd"] == "OPSQ0002"
        assert not isinstance(ei.value, AuthenticationError)

    def test_missing_price_field(self, tmp_path, rsps):
        add_token(rsps)
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_PRICE, json=ok(output={"stck_prpr": ""}))
        with pytest.raises(BrokerError, match="stck_prpr"):
            make_broker(tmp_path).get_ticker(SYMBOL)

    def test_non_json_response(self, tmp_path, rsps):
        add_token(rsps)
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_PRICE, body="<html>maintenance</html>", status=200)
        with pytest.raises(BrokerError, match="응답 형식"):
            make_broker(tmp_path).get_ticker(SYMBOL)

    def test_get_retries_server_error_then_succeeds(self, tmp_path, rsps, daily_candles, no_sleep):
        add_token(rsps)
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_PRICE, status=503, body="gateway")
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_PRICE, json=price_output(daily_candles[-1].close))
        assert make_broker(tmp_path).get_ticker(SYMBOL) > 0
        assert len(rsps.calls) == 3

    def test_get_network_error_exhausts_retries(self, tmp_path, rsps, no_sleep):
        add_token(rsps)
        for _ in range(4):
            rsps.add(
                responses.GET, PAPER_BASE_URL + PATH_PRICE, body=requests.exceptions.ConnectionError("reset")
            )
        with pytest.raises(BrokerError, match="네트워크"):
            make_broker(tmp_path).get_ticker(SYMBOL)
        assert len(rsps.calls) == 5  # token + 4 attempts (max_retries=3)

    def test_rate_limit_retry_then_success(self, tmp_path, rsps, daily_candles, no_sleep):
        add_token(rsps)
        rsps.add(
            responses.GET,
            PAPER_BASE_URL + PATH_PRICE,
            status=500,
            json=kis_error("EGW00201", "초당 거래건수를 초과하였습니다."),
        )
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_PRICE, json=price_output(daily_candles[-1].close))
        assert make_broker(tmp_path).get_ticker(SYMBOL) > 0

    def test_rate_limit_exhausted(self, tmp_path, rsps, no_sleep):
        add_token(rsps)
        for _ in range(5):
            rsps.add(
                responses.GET,
                PAPER_BASE_URL + PATH_PRICE,
                status=500,
                json=kis_error("EGW00201", "초당 거래건수를 초과하였습니다."),
            )
        with pytest.raises(RateLimitError):
            make_broker(tmp_path).get_ticker(SYMBOL)

    def test_business_error_with_500_is_not_retried(self, tmp_path, rsps, no_sleep):
        add_token(rsps)
        rsps.add(
            responses.GET,
            PAPER_BASE_URL + PATH_PRICE,
            status=500,
            json=kis_error("EGW00001", "유효하지 않은 요청입니다."),
        )
        with pytest.raises(BrokerError, match="EGW00001"):
            make_broker(tmp_path).get_ticker(SYMBOL)
        assert len(rsps.calls) == 2


# ============================================================================ 일봉/주봉
class TestDailyCandles:
    @pytest.fixture
    def by_date(self, daily_candles) -> dict[date, Candle]:
        dates = weekdays_ending(END_DAY, len(daily_candles))
        return dict(zip(dates, daily_candles, strict=True))

    def test_parsing_order_utc_and_pagination(self, tmp_path, rsps, by_date):
        add_token(rsps)
        server = DailyChartServer(by_date)
        rsps.add_callback(
            responses.GET, PAPER_BASE_URL + PATH_DAILY_CHART, callback=server, content_type="application/json"
        )
        b = make_broker(tmp_path)
        with freeze_time("2025-09-26 08:00:00+00:00"):  # 17:00 KST 금요일 — 당일 종가 확정
            candles = b.get_candles(SYMBOL, "1d", limit=150)

        assert len(candles) == 150
        dates = sorted(by_date)[-150:]
        assert [c.timestamp for c in candles] == sorted(c.timestamp for c in candles)
        for c, d in zip(candles, dates, strict=True):
            src = by_date[d]
            assert c.timestamp == kst(d, dtime(9, 0)).astimezone(timezone.utc)
            assert c.timestamp.tzinfo is timezone.utc
            assert c.timestamp == datetime(
                d.year, d.month, d.day, tzinfo=timezone.utc
            )  # 09:00 KST == 00:00 UTC
            assert c.open == float(fmt_int(src.open))
            assert c.high == float(fmt_int(src.high))
            assert c.low == float(fmt_int(src.low))
            assert c.close == float(fmt_int(src.close))
            assert c.volume == float(fmt_int(src.volume))
        # 100행 제한 → 2페이지, 두 번째 요청의 종료일은 첫 페이지 가장 오래된 날짜 - 1일
        assert len(server.requests) == 2
        first_oldest = sorted(by_date)[-100]
        assert server.requests[0]["FID_INPUT_DATE_2"] == END_DAY.strftime("%Y%m%d")
        assert server.requests[1]["FID_INPUT_DATE_2"] == (first_oldest - timedelta(days=1)).strftime("%Y%m%d")

    def test_forming_daily_candle_excluded_during_session(self, tmp_path, rsps, by_date):
        add_token(rsps)
        server = DailyChartServer(by_date)
        rsps.add_callback(
            responses.GET, PAPER_BASE_URL + PATH_DAILY_CHART, callback=server, content_type="application/json"
        )
        b = make_broker(tmp_path)
        with freeze_time("2025-09-26 01:00:00+00:00"):  # 10:00 KST 장중
            closed = b.get_candles(SYMBOL, "1d", limit=5)
            partial = b.get_candles(SYMBOL, "1d", limit=5, include_partial=True)
        assert closed[-1].timestamp.date() == date(2025, 9, 25)
        assert len(closed) == 5
        assert partial[-1].timestamp.date() == END_DAY
        assert len(partial) == 5
        assert partial[-1].close == float(fmt_int(by_date[END_DAY].close))

    def test_close_grace_after_1530(self, tmp_path, rsps, by_date):
        add_token(rsps)
        rsps.add_callback(
            responses.GET,
            PAPER_BASE_URL + PATH_DAILY_CHART,
            callback=DailyChartServer(by_date),
            content_type="application/json",
        )
        b = make_broker(tmp_path)
        with freeze_time("2025-09-26 06:31:00+00:00"):  # 15:31 KST — 종가 확정 유예(5분) 이내
            assert b.get_candles(SYMBOL, "1d", limit=3)[-1].timestamp.date() == date(2025, 9, 25)
        with freeze_time("2025-09-26 06:36:00+00:00"):  # 15:36 KST
            assert b.get_candles(SYMBOL, "1d", limit=3)[-1].timestamp.date() == END_DAY

    def test_end_parameter(self, tmp_path, rsps, by_date):
        add_token(rsps)
        server = DailyChartServer(by_date)
        rsps.add_callback(
            responses.GET, PAPER_BASE_URL + PATH_DAILY_CHART, callback=server, content_type="application/json"
        )
        b = make_broker(tmp_path)
        end = kst(date(2025, 9, 10), dtime(12, 0))  # 수요일 장중
        with freeze_time("2025-09-26 08:00:00+00:00"):
            candles = b.get_candles(SYMBOL, "1d", limit=10, end=end)
        assert server.requests[0]["FID_INPUT_DATE_2"] == "20250910"
        assert len(candles) == 10
        assert candles[-1].timestamp.date() == date(2025, 9, 9)  # 9/10 은 end 시점에 미완성
        assert all(c.timestamp < end for c in candles)

    def test_limit_zero_and_unsupported_interval(self, tmp_path, rsps):
        b = make_broker(tmp_path)
        assert b.get_candles(SYMBOL, "1d", limit=0) == []
        with pytest.raises(ValueError):
            b.get_candles(SYMBOL, "4h")
        with pytest.raises(ValueError):
            b.get_candles(SYMBOL, "2m")
        assert len(rsps.calls) == 0

    def test_weekly(self, tmp_path, rsps, eth_daily_candles):
        # 실제 ETH 일봉을 "주봉" 행으로 재포장 (KIS 주봉 stck_bsop_date 는 그 주의 거래일)
        mondays = [
            END_DAY - timedelta(days=END_DAY.weekday()) - timedelta(weeks=i)
            for i in range(len(eth_daily_candles))
        ]
        by_date = {
            m + timedelta(days=1): c for m, c in zip(mondays, eth_daily_candles, strict=True)
        }  # 화요일 날짜로 표기
        add_token(rsps)
        rsps.add_callback(
            responses.GET,
            PAPER_BASE_URL + PATH_DAILY_CHART,
            callback=DailyChartServer(by_date, "W"),
            content_type="application/json",
        )
        b = make_broker(tmp_path)
        with freeze_time("2025-09-27 03:00:00+00:00"):  # 토요일 → 이번 주 봉 완성
            candles = b.get_candles(SYMBOL, "1w", limit=8)
        assert len(candles) == 8
        assert all(c.timestamp.weekday() == 0 and c.timestamp.time() == dtime(0, 0) for c in candles)
        assert candles[-1].timestamp.date() == date(2025, 9, 22)
        with freeze_time("2025-09-24 03:00:00+00:00"):  # 수요일 → 이번 주 봉은 미완성
            candles = b.get_candles(SYMBOL, "1w", limit=3)
        assert candles[-1].timestamp.date() == date(2025, 9, 15)

    def test_rows_with_empty_padding_are_skipped(self, tmp_path, rsps, daily_candles):
        add_token(rsps)
        d = date(2025, 9, 25)
        body = ok(
            output1=daily_output1(daily_candles[-1]),
            output2=[daily_row(d, daily_candles[-1]), {}, {"stck_bsop_date": ""}],
        )
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_DAILY_CHART, json=body)
        rsps.add(
            responses.GET,
            PAPER_BASE_URL + PATH_DAILY_CHART,
            json=ok(output1=daily_output1(daily_candles[-1]), output2=[]),
        )
        with freeze_time("2025-09-26 08:00:00+00:00"):
            candles = make_broker(tmp_path).get_candles(SYMBOL, "1d", limit=5)
        assert len(candles) == 1 and candles[0].timestamp.date() == d


# ============================================================================ 분봉 합성
class TestMinuteCandles:
    def test_5m_and_1h_aggregation_with_pagination(self, tmp_path, rsps, candles):
        server = MinuteChartServer(END_DAY)
        rows = server.add_day(END_DAY, candles)  # 09:00 ~ 12:19 (200개 1분봉, 실제 KRW-BTC 1시간봉 OHLCV)
        server.register(rsps)
        add_token(rsps)
        b = make_broker(tmp_path)
        df = minute_df(rows, END_DAY)

        with freeze_time(kst(END_DAY, dtime(12, 20, 10))):
            five = b.get_candles(SYMBOL, "5m", limit=200)
            # 페이지네이션: 30행씩 거슬러 올라감 (200행 → 7페이지), 첫 요청은 현재 시각
            assert len(server.today_requests) == 7
            assert server.today_requests[0]["FID_INPUT_HOUR_1"] == "122010"
            assert server.today_requests[1]["FID_INPUT_HOUR_1"] == "114900"
            assert server.today_requests[-1]["FID_INPUT_HOUR_1"] == "091900"
            hours = [q["FID_INPUT_HOUR_1"] for q in server.today_requests]
            assert hours == sorted(hours, reverse=True)
            one_h = b.get_candles(SYMBOL, "1h", limit=200)
            one_h_partial = b.get_candles(SYMBOL, "1h", limit=200, include_partial=True)
            one_m = b.get_candles(SYMBOL, "1m", limit=1000)
        # 이후 호출은 캐시된 구간에 닿으면 중단 → 최신 페이지 1회씩만
        assert len(server.today_requests) == 10

        exp5 = resample_ohlcv(df, "5min")
        assert len(five) == len(exp5) == 40
        for c, (ts, row) in zip(five, exp5.iterrows(), strict=True):
            assert c.timestamp == ts.to_pydatetime().astimezone(timezone.utc)
            assert c.timestamp.tzinfo is timezone.utc
            assert (
                c.open == row["open"]
                and c.high == row["high"]
                and c.low == row["low"]
                and c.close == row["close"]
            )
            assert c.volume == approx(row["volume"])
        # KST 시계 정렬: 09:00, 09:05, ...
        assert [c.timestamp.astimezone(KST).time() for c in five[:3]] == [
            dtime(9, 0),
            dtime(9, 5),
            dtime(9, 10),
        ]

        exp1h = resample_ohlcv(df, "1h")
        assert [c.timestamp.astimezone(KST).time() for c in one_h] == [
            dtime(9, 0),
            dtime(10, 0),
            dtime(11, 0),
        ]  # 12시 봉은 진행중
        for c, (ts, row) in zip(one_h, exp1h.iloc[:3].iterrows(), strict=True):
            assert c.timestamp == ts.to_pydatetime().astimezone(timezone.utc)
            assert (c.open, c.high, c.low, c.close) == (row["open"], row["high"], row["low"], row["close"])
            assert c.volume == approx(row["volume"])
        assert len(one_h_partial) == 4
        last = exp1h.iloc[3]
        assert one_h_partial[-1].timestamp.astimezone(KST).time() == dtime(12, 0)
        assert (one_h_partial[-1].open, one_h_partial[-1].close) == (last["open"], last["close"])
        assert one_h_partial[-1].volume == approx(last["volume"])

        assert len(one_m) == 200
        assert one_m[-1].timestamp.astimezone(KST).time() == dtime(12, 19)
        assert one_m[0].close == df.iloc[0]["close"]

    def test_forming_minute_bar_excluded(self, tmp_path, rsps, candles):
        server = MinuteChartServer(END_DAY)
        server.add_day(END_DAY, candles)
        server.register(rsps)
        add_token(rsps)
        b = make_broker(tmp_path)
        with freeze_time(kst(END_DAY, dtime(12, 19, 30))):  # 12:19 봉 진행중 (종료 12:20:00 + 5초 유예)
            got = b.get_candles(SYMBOL, "1m", limit=3)
        assert [c.timestamp.astimezone(KST).time() for c in got] == [
            dtime(12, 16),
            dtime(12, 17),
            dtime(12, 18),
        ]
        with freeze_time(kst(END_DAY, dtime(12, 20, 3))):  # 아직 유예 이내
            got = b.get_candles(SYMBOL, "1m", limit=1)
        assert got[-1].timestamp.astimezone(KST).time() == dtime(12, 18)
        with freeze_time(kst(END_DAY, dtime(12, 20, 6))):
            got = b.get_candles(SYMBOL, "1m", limit=1)
        assert got[-1].timestamp.astimezone(KST).time() == dtime(12, 19)

    def test_closing_auction_folded_and_after_hours_dropped(self, tmp_path, rsps, candles):
        server = MinuteChartServer(END_DAY)
        # 15:25~15:29 정규장 5분 + 15:30 동시호가 종가 + 15:40/16:00 시간외 (실제 캔들 값 사용)
        times = [
            dtime(15, 25),
            dtime(15, 26),
            dtime(15, 27),
            dtime(15, 28),
            dtime(15, 29),
            dtime(15, 30),
            dtime(15, 40),
            dtime(16, 0),
        ]
        rows = server.add_day(END_DAY, candles[:8], times=times)
        server.register(rsps)
        add_token(rsps)
        b = make_broker(tmp_path)
        with freeze_time(kst(END_DAY, dtime(16, 30))):
            five = b.get_candles(SYMBOL, "5m", limit=10)
            one_h = b.get_candles(SYMBOL, "1h", limit=10)
            one_m = b.get_candles(SYMBOL, "1m", limit=10)
        regular = {k: v for k, v in rows.items() if k <= "153000"}
        df = minute_df(regular, END_DAY)
        assert len(five) == 1
        assert five[0].timestamp.astimezone(KST).time() == dtime(15, 25)
        assert five[0].open == df.iloc[0]["open"]
        assert five[0].close == float(rows["153000"]["stck_prpr"])  # 종가 = 동시호가 체결가
        assert five[0].high == df["high"].max() and five[0].low == df["low"].min()
        assert five[0].volume == approx(df["volume"].sum())  # 시간외 거래량은 제외
        assert len(one_h) == 1 and one_h[0].timestamp.astimezone(KST).time() == dtime(15, 0)
        assert one_h[0].close == float(rows["153000"]["stck_prpr"])
        # 1분봉: 15:30 체결은 15:29 봉에 합쳐지고 15:30 봉은 생기지 않는다
        assert [c.timestamp.astimezone(KST).time() for c in one_m] == [dtime(15, 25 + i) for i in range(5)]
        assert one_m[-1].close == float(rows["153000"]["stck_prpr"])
        assert one_m[-1].volume == approx(
            float(rows["152900"]["cntg_vol"]) + float(rows["153000"]["cntg_vol"])
        )

    def test_hourly_bar_complete_at_session_close(self, tmp_path, rsps, candles):
        server = MinuteChartServer(END_DAY)
        times = [dtime(15, 0), dtime(15, 10), dtime(15, 29), dtime(15, 30)]
        server.add_day(END_DAY, candles[:4], times=times)
        server.register(rsps)
        add_token(rsps)
        b = make_broker(tmp_path)
        with freeze_time(kst(END_DAY, dtime(15, 29, 50))):
            assert b.get_candles(SYMBOL, "1h", limit=5) == []
        with freeze_time(
            kst(END_DAY, dtime(15, 30, 10))
        ):  # 장 마감 + 5초 유예 경과 → 15시 봉 완성 (16:00 까지 기다리지 않음)
            got = b.get_candles(SYMBOL, "1h", limit=5)
        assert len(got) == 1 and got[0].timestamp.astimezone(KST).time() == dtime(15, 0)

    def test_previous_days_via_daily_minute_api_and_cache(self, tmp_path, rsps, candles, eth_daily_candles):
        today = END_DAY
        prev = END_DAY - timedelta(days=1)  # 목요일
        server = MinuteChartServer(today)
        server.add_day(prev, candles)  # 전일 09:00~12:19 (200행 → 120행 페이지 2개)
        server.add_day(today, eth_daily_candles[:30])  # 당일 09:00~09:29
        server.register(rsps)
        add_token(rsps)
        b = make_broker(tmp_path)

        with freeze_time(kst(today, dtime(9, 35, 10))):
            got = b.get_candles(SYMBOL, "1h", limit=4)
        # 당일 09시 봉은 진행중 → 전일 09,10,11,12시 봉 (12시 봉은 15:30 마감으로 완성)
        assert [(c.timestamp.astimezone(KST).date(), c.timestamp.astimezone(KST).time()) for c in got] == [
            (prev, dtime(9, 0)),
            (prev, dtime(10, 0)),
            (prev, dtime(11, 0)),
            (prev, dtime(12, 0)),
        ]
        exp = resample_ohlcv(minute_df(server.days[prev], prev), "1h")
        for c, (_, row) in zip(got, exp.iterrows(), strict=True):
            assert (c.open, c.high, c.low, c.close) == (row["open"], row["high"], row["low"], row["close"])
            assert c.volume == approx(row["volume"])
        assert len(server.past_requests) == 2
        assert server.past_requests[0]["FID_INPUT_DATE_1"] == prev.strftime("%Y%m%d")
        assert server.past_requests[0]["FID_INPUT_HOUR_1"] == "153000"
        assert (
            server.past_requests[1]["FID_INPUT_HOUR_1"] == "101900"
        )  # 120행(12:19~10:20) 다음은 10:20 - 1분
        assert len(server.today_requests) == 1
        assert server.today_requests[0]["FID_INPUT_HOUR_1"] == "093510"

        # 두 번째 호출: 과거 일자는 캐시, 당일은 최신 페이지 1회만
        with freeze_time(kst(today, dtime(9, 36, 10))):
            again = b.get_candles(SYMBOL, "1h", limit=4)
        assert again == got
        assert len(server.past_requests) == 2
        assert len(server.today_requests) == 2

    def test_today_incremental_fetch_stops_at_cached_rows(self, tmp_path, rsps, candles):
        server = MinuteChartServer(END_DAY)
        server.add_day(END_DAY, candles)  # 09:00~12:19
        server.register(rsps)
        add_token(rsps)
        b = make_broker(tmp_path)
        with freeze_time(kst(END_DAY, dtime(12, 20, 10))):
            first = b.get_candles(SYMBOL, "5m", limit=100)
        assert len(server.today_requests) == 7
        with freeze_time(kst(END_DAY, dtime(12, 21, 10))):
            second = b.get_candles(SYMBOL, "5m", limit=100)
        assert second == first
        assert len(server.today_requests) == 8  # 최신 30행 1페이지만 추가 조회

    def test_past_minute_api_unavailable_falls_back_to_today(self, tmp_path, rsps, candles, caplog):
        today = END_DAY
        server = MinuteChartServer(today, past_ok=False)
        server.add_day(today, candles)
        server.register(rsps)
        add_token(rsps)
        b = make_broker(tmp_path)
        with freeze_time(kst(today, dtime(12, 20, 10))):
            got = b.get_candles(SYMBOL, "1h", limit=10)
        assert [c.timestamp.astimezone(KST).time() for c in got] == [dtime(9, 0), dtime(10, 0), dtime(11, 0)]
        assert len(server.past_requests) == 1  # 실패 후 과거 일자 조회 중단
        with freeze_time(kst(today, dtime(12, 21, 10))):
            b.get_candles(SYMBOL, "1h", limit=10)
        assert len(server.past_requests) == 1
        assert any("과거 일자 분봉" in r.message for r in caplog.records)

    def test_minute_end_parameter_on_past_day(self, tmp_path, rsps, candles):
        prev = END_DAY - timedelta(days=1)
        server = MinuteChartServer(END_DAY)
        server.add_day(prev, candles)
        server.register(rsps)
        add_token(rsps)
        b = make_broker(tmp_path)
        end = kst(prev, dtime(10, 30))
        with freeze_time(kst(END_DAY, dtime(10, 0))):
            got = b.get_candles(SYMBOL, "15m", limit=3, end=end)
        assert [c.timestamp.astimezone(KST).time() for c in got] == [
            dtime(9, 45),
            dtime(10, 0),
            dtime(10, 15),
        ]
        assert all(c.timestamp.astimezone(KST).date() == prev for c in got)
        assert len(server.today_requests) == 0  # end 가 과거 일자이므로 당일 조회 없음


# ============================================================================ 잔고/포지션
class TestBalances:
    def test_pagination_balances_positions_equity(self, tmp_path, rsps, daily_candles, eth_daily_candles):
        add_token(rsps)
        btc, eth = daily_candles[-1], eth_daily_candles[-1]
        page1 = ok(
            ctx_area_fk100="50012345^01^N^^01^01^N^N^00^",
            ctx_area_nk100="000660   ",
            output1=[
                balance_row(SYMBOL, 10, btc.open, btc.close),
                balance_row("035420", 0, eth.open, eth.close, "NAVER"),
            ],
            output2=[balance_summary(dnca=5_000_000, d2=4_200_000, scts=btc.close * 10 + eth.close * 3)],
        )
        page2 = ok(
            ctx_area_fk100="",
            ctx_area_nk100="",
            output1=[balance_row("000660", 3, eth.open, eth.close, "SK하이닉스")],
            output2=[balance_summary(dnca=5_000_000, d2=4_200_000, scts=btc.close * 10 + eth.close * 3)],
        )
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_BALANCE, json=page1, headers={"tr_cont": "M"})
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_BALANCE, json=page2, headers={"tr_cont": "D"})
        b = make_broker(tmp_path)

        positions = b.get_positions()
        assert set(positions) == {SYMBOL, "000660"}  # hldg_qty 0 인 NAVER 제외
        assert positions[SYMBOL].quantity == 10.0
        assert positions[SYMBOL].average_price == approx(float(f"{btc.open:.4f}"))
        assert positions[SYMBOL].meta["name"] == "삼성전자"
        assert positions[SYMBOL].meta["current_price"] == float(fmt_int(btc.close))
        assert positions["000660"].quantity == 3.0

        # 연속조회 헤더/파라미터
        req1, req2 = rsps.calls[1].request, rsps.calls[2].request
        assert req1.headers["tr_id"] == "VTTC8434R"
        assert req1.headers["tr_cont"] == ""
        assert req2.headers["tr_cont"] == "N"
        q1, q2 = query(req1), query(req2)
        assert q1["CANO"] == "50012345" and q1["ACNT_PRDT_CD"] == "01"
        assert q1["CTX_AREA_FK100"] == "" and q1["CTX_AREA_NK100"] == ""
        assert (
            q2["CTX_AREA_FK100"] == page1["ctx_area_fk100"]
            and q2["CTX_AREA_NK100"] == page1["ctx_area_nk100"]
        )
        for key in (
            "AFHR_FLPR_YN",
            "OFL_YN",
            "INQR_DVSN",
            "UNPR_DVSN",
            "FUND_STTL_ICLD_YN",
            "FNCG_AMT_AUTO_RDPT_YN",
            "PRCS_DVSN",
        ):
            assert key in q1

        rsps.add(responses.GET, PAPER_BASE_URL + PATH_BALANCE, json=page2, headers={"tr_cont": "D"})
        bal = b.get_balances()
        assert set(bal) == {"KRW"}
        assert bal["KRW"].total == 5_000_000.0
        assert bal["KRW"].available == 4_200_000.0
        assert bal["KRW"].locked == 800_000.0

        rsps.add(responses.GET, PAPER_BASE_URL + PATH_BALANCE, json=page2, headers={"tr_cont": "D"})
        assert b.get_equity() == float(fmt_int(5_000_000 + btc.close * 10 + eth.close * 3))

    def test_real_tr_id_and_empty_account(self, tmp_path, rsps):
        add_token(rsps, base=REAL_BASE_URL)
        rsps.add(
            responses.GET,
            REAL_BASE_URL + PATH_BALANCE,
            json=ok(
                ctx_area_fk100="",
                ctx_area_nk100="",
                output1=[],
                output2=[balance_summary(dnca=1_000_000, d2=1_000_000, scts=0)],
            ),
            headers={"tr_cont": "D"},
        )
        b = make_broker(tmp_path, sandbox=False)
        assert b.get_positions() == {}
        assert rsps.calls[1].request.headers["tr_id"] == "TTTC8434R"
        rsps.add(
            responses.GET,
            REAL_BASE_URL + PATH_BALANCE,
            json=ok(
                ctx_area_fk100="",
                ctx_area_nk100="",
                output1=[],
                output2=[balance_summary(dnca=1_000_000, d2=1_000_000, scts=0)],
            ),
        )
        assert b.get_balances()["KRW"].available == 1_000_000.0

    def test_balance_error(self, tmp_path, rsps):
        add_token(rsps)
        rsps.add(
            responses.GET, PAPER_BASE_URL + PATH_BALANCE, json=kis_error("APBK0656", "계좌번호를 확인하세요")
        )
        with pytest.raises(BrokerError, match="APBK0656"):
            make_broker(tmp_path).get_balances()

    def test_duplicate_rows_merge_weighted_average(self, tmp_path, rsps, daily_candles):
        add_token(rsps)
        a, c = daily_candles[-1], daily_candles[-2]
        rsps.add(
            responses.GET,
            PAPER_BASE_URL + PATH_BALANCE,
            json=ok(
                ctx_area_fk100="",
                ctx_area_nk100="",
                output1=[balance_row(SYMBOL, 2, a.open, a.close), balance_row(SYMBOL, 6, c.open, c.close)],
                output2=[balance_summary(1, 1, 1)],
            ),
        )
        pos = make_broker(tmp_path).get_positions()[SYMBOL]
        assert pos.quantity == 8.0
        expected = (float(f"{a.open:.4f}") * 2 + float(f"{c.open:.4f}") * 6) / 8
        assert pos.average_price == approx(expected)


# ============================================================================ 주문
class TestOrders:
    def test_market_buy_sandbox(self, tmp_path, rsps, daily_candles):
        add_token(rsps)
        close = daily_candles[-1].close
        add_psbl(rsps, cash=int(close * 100), calc_price=upper_limit(close))  # 3주는 상한가 기준으로도 충분
        rsps.add(
            responses.POST,
            PAPER_BASE_URL + PATH_ORDER_CASH,
            json=order_output("0000117057", "91252", "121052"),
        )
        b = make_broker(tmp_path)
        with freeze_time("2025-09-26 03:00:00+00:00"):
            order = b.place_order(SYMBOL, OrderSide.BUY, 3.7)
        assert kis_urls(rsps) == [
            PAPER_BASE_URL + PATH_TOKEN,
            PAPER_BASE_URL + PATH_PSBL_ORDER,
            PAPER_BASE_URL + PATH_ORDER_CASH,
        ]
        req = rsps.calls[2].request
        assert req.headers["tr_id"] == "VTTC0012U"
        assert body_of(rsps.calls[2]) == {
            "CANO": "50012345",
            "ACNT_PRDT_CD": "01",
            "PDNO": SYMBOL,
            "ORD_DVSN": "01",
            "ORD_QTY": "3",
            "ORD_UNPR": "0",
            "EXCG_ID_DVSN_CD": "KRX",
            "SLL_TYPE": "",
            "CNDT_PRIC": "",
        }
        assert order.id == "0000117057"
        assert order.symbol == SYMBOL and order.side == OrderSide.BUY and order.type == OrderType.MARKET
        assert order.quantity == 3.0 and order.price is None
        assert order.status == OrderStatus.OPEN and not order.status.is_terminal
        assert order.raw["KRX_FWDG_ORD_ORGNO"] == "91252"
        assert order.raw["ODNO"] == "0000117057"
        assert order.created_at == kst(END_DAY, dtime(12, 10, 52)).astimezone(timezone.utc)
        assert "CANO" not in order.raw

    def test_limit_sell_real_with_tick_rounding(self, tmp_path, rsps, daily_candles):
        add_token(rsps, base=REAL_BASE_URL)
        rsps.add(responses.POST, REAL_BASE_URL + PATH_ORDER_CASH, json=order_output("0000000123"))
        b = make_broker(tmp_path, sandbox=False)
        price = daily_candles[-1].close + 0.4 * krx_tick_size(daily_candles[-1].close)  # 호가단위 사이 값
        order = b.place_order(SYMBOL, OrderSide.SELL, 2, OrderType.LIMIT, price=price)
        expected_px = round_to_tick(price)
        assert expected_px % krx_tick_size(expected_px) == 0
        assert rsps.calls[1].request.headers["tr_id"] == "TTTC0011U"
        sent = body_of(rsps.calls[1])
        assert sent["ORD_DVSN"] == "00"
        assert sent["ORD_UNPR"] == str(int(expected_px))
        assert sent["ORD_QTY"] == "2"
        assert order.price == expected_px and order.type == OrderType.LIMIT
        assert order.raw["tr_id"] == "TTTC0011U"

    def test_buy_real_and_sell_sandbox_tr_ids(self, tmp_path, rsps, daily_candles):
        add_token(rsps, base=REAL_BASE_URL)
        add_token(rsps)
        px = int(round_to_tick(daily_candles[-1].close))
        add_psbl(rsps, cash=px * 10, calc_price=px, base=REAL_BASE_URL)
        rsps.add(responses.POST, REAL_BASE_URL + PATH_ORDER_CASH, json=order_output("1"))
        rsps.add(responses.POST, PAPER_BASE_URL + PATH_ORDER_CASH, json=order_output("2"))
        make_broker(tmp_path / "r", sandbox=False).place_order(
            SYMBOL, OrderSide.BUY, 1, OrderType.LIMIT, price=daily_candles[-1].close
        )
        make_broker(tmp_path / "p", sandbox=True).place_order(SYMBOL, OrderSide.SELL, 1)
        tr_ids = {
            c.request.url.split("?")[0]: c.request.headers["tr_id"]
            for c in rsps.calls
            if c.request.url.endswith(PATH_ORDER_CASH)
        }
        assert tr_ids == {
            REAL_BASE_URL + PATH_ORDER_CASH: "TTTC0012U",
            PAPER_BASE_URL + PATH_ORDER_CASH: "VTTC0011U",
        }
        psbl = [c for c in rsps.calls if c.request.url.startswith(REAL_BASE_URL + PATH_PSBL_ORDER)]
        assert len(psbl) == 1 and psbl[0].request.headers["tr_id"] == "TTTC8908R"

    def test_order_validation(self, tmp_path, rsps, daily_candles):
        b = make_broker(tmp_path)
        with pytest.raises(OrderError, match="STOP"):
            b.place_order(SYMBOL, OrderSide.BUY, 1, OrderType.STOP, price=daily_candles[-1].close)
        with pytest.raises(OrderError, match="1주 이상"):
            b.place_order(SYMBOL, OrderSide.BUY, 0.5)
        with pytest.raises(OrderError, match="1주 이상"):
            b.place_order(SYMBOL, OrderSide.BUY, 0)
        with pytest.raises(OrderError, match="price"):
            b.place_order(SYMBOL, OrderSide.BUY, 1, OrderType.LIMIT)
        with pytest.raises(OrderError):
            b.place_order(SYMBOL, OrderSide.BUY, 1, OrderType.LIMIT, price=-1)
        assert len(rsps.calls) == 0  # 검증 실패는 네트워크 호출 전에

    def test_order_rejected_maps_errors(self, tmp_path, rsps, daily_candles):
        add_token(rsps)
        close = daily_candles[-1].close
        add_psbl(rsps, cash=int(close * 10), calc_price=upper_limit(close))
        rsps.add(
            responses.POST,
            PAPER_BASE_URL + PATH_ORDER_CASH,
            json=kis_error("APBK0918", "주문가능금액을 초과하였습니다."),
        )
        rsps.add(
            responses.POST,
            PAPER_BASE_URL + PATH_ORDER_CASH,
            json=kis_error("APBK0013", "주문 전송 완료 되지 않았습니다."),
        )
        rsps.add(
            responses.POST,
            PAPER_BASE_URL + PATH_ORDER_CASH,
            json=ok(output={"KRX_FWDG_ORD_ORGNO": "", "ODNO": "", "ORD_TMD": ""}),
        )
        b = make_broker(tmp_path)
        with pytest.raises(InsufficientFunds, match="주문가능금액"):
            b.place_order(SYMBOL, OrderSide.BUY, 1)
        with pytest.raises(OrderError, match="APBK0013") as ei:
            b.place_order(SYMBOL, OrderSide.BUY, 1)
        assert not isinstance(ei.value, InsufficientFunds)
        with pytest.raises(OrderError, match="ODNO"):
            b.place_order(SYMBOL, OrderSide.BUY, 1)

    def test_post_is_not_retried_on_network_error(self, tmp_path, rsps, no_sleep, daily_candles):
        add_token(rsps)
        close = daily_candles[-1].close
        add_psbl(rsps, cash=int(close * 10), calc_price=upper_limit(close))
        rsps.add(
            responses.POST,
            PAPER_BASE_URL + PATH_ORDER_CASH,
            body=requests.exceptions.ConnectionError("reset"),
        )
        rsps.add(responses.POST, PAPER_BASE_URL + PATH_ORDER_CASH, json=order_output("9"))
        with pytest.raises(BrokerError, match="네트워크"):
            make_broker(tmp_path).place_order(SYMBOL, OrderSide.BUY, 1)
        assert len(rsps.calls) == 3  # token + 매수가능조회 + 1 attempt

    def test_order_token_expiry_refreshes_once(self, tmp_path, rsps, daily_candles):
        add_token(rsps, token="old")
        close = daily_candles[-1].close
        add_psbl(rsps, cash=int(close * 10), calc_price=upper_limit(close))
        rsps.add(
            responses.POST,
            PAPER_BASE_URL + PATH_ORDER_CASH,
            json=kis_error("EGW00123", "기간이 만료된 token 입니다."),
        )
        add_token(rsps, token="new")
        rsps.add(responses.POST, PAPER_BASE_URL + PATH_ORDER_CASH, json=order_output("77"))
        order = make_broker(tmp_path).place_order(SYMBOL, OrderSide.BUY, 1)
        assert order.id == "77"
        assert rsps.calls[-1].request.headers["authorization"] == "Bearer new"

    def test_cancel_uses_cached_orgno(self, tmp_path, rsps, daily_candles):
        add_token(rsps)
        px = int(round_to_tick(daily_candles[-1].close))
        add_psbl(rsps, cash=px * 10, calc_price=px)
        rsps.add(responses.POST, PAPER_BASE_URL + PATH_ORDER_CASH, json=order_output("0000117057", "91252"))
        rsps.add(
            responses.POST,
            PAPER_BASE_URL + PATH_ORDER_RVSECNCL,
            json=ok(output={"krx_fwdg_ord_orgno": "91252", "odno": "0000117058", "ord_tmd": "121500"}),
        )
        b = make_broker(tmp_path)
        order = b.place_order(SYMBOL, OrderSide.BUY, 1, OrderType.LIMIT, price=daily_candles[-1].close)
        assert b.cancel_order(order.id) is True
        req = rsps.calls[3].request
        assert req.headers["tr_id"] == "VTTC0013U"
        assert body_of(rsps.calls[3]) == {
            "CANO": "50012345",
            "ACNT_PRDT_CD": "01",
            "KRX_FWDG_ORD_ORGNO": "91252",
            "ORGN_ODNO": "0000117057",
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": "0",
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",
            "EXCG_ID_DVSN_CD": "KRX",
        }
        assert len(rsps.calls) == 4  # token + 매수가능조회 + 주문 + 취소: 조회 없이 캐시된 조직번호 사용

    def test_cancel_looks_up_order_when_unknown(self, tmp_path, rsps, daily_candles):
        add_token(rsps)
        px = daily_candles[-1].close
        today = "20250926"
        rsps.add(
            responses.GET,
            PAPER_BASE_URL + PATH_DAILY_CCLD,
            json=ccld_body(
                [
                    ccld_row(
                        odno="0000000501",
                        ord_dt=today,
                        ord_qty=5,
                        ord_unpr=px,
                        tot_ccld_qty=2,
                        avg_prvs=px,
                        orgno="06010",
                        ord_dvsn_cd="00",
                    )
                ]
            ),
            headers={"tr_cont": "D"},
        )
        rsps.add(
            responses.POST,
            PAPER_BASE_URL + PATH_ORDER_RVSECNCL,
            json=ok(output={"krx_fwdg_ord_orgno": "06010", "odno": "0000000502", "ord_tmd": "130000"}),
        )
        b = make_broker(tmp_path)
        with freeze_time("2025-09-26 04:00:00+00:00"):
            assert b.cancel_order("0000000501", SYMBOL) is True
        q = query(rsps.calls[1].request)
        assert q["ODNO"] == "0000000501" and q["PDNO"] == SYMBOL
        sent = body_of(rsps.calls[2])
        assert (
            sent["KRX_FWDG_ORD_ORGNO"] == "06010"
            and sent["ORGN_ODNO"] == "0000000501"
            and sent["ORD_DVSN"] == "00"
        )

    def test_cancel_terminal_order_returns_false(self, tmp_path, rsps, daily_candles):
        add_token(rsps)
        px = daily_candles[-1].close
        rsps.add(
            responses.GET,
            PAPER_BASE_URL + PATH_DAILY_CCLD,
            json=ccld_body(
                [
                    ccld_row(
                        odno="0000000777",
                        ord_dt="20250926",
                        ord_qty=1,
                        ord_unpr=px,
                        tot_ccld_qty=1,
                        avg_prvs=px,
                    )
                ]
            ),
        )
        b = make_broker(tmp_path)
        with freeze_time("2025-09-26 04:00:00+00:00"):
            assert b.cancel_order("0000000777") is False
        assert len(rsps.calls) == 2

    def test_cancel_error(self, tmp_path, rsps, daily_candles):
        add_token(rsps)
        close = daily_candles[-1].close
        add_psbl(rsps, cash=int(close * 10), calc_price=upper_limit(close))
        rsps.add(responses.POST, PAPER_BASE_URL + PATH_ORDER_CASH, json=order_output("0000000001", "91252"))
        rsps.add(
            responses.POST,
            PAPER_BASE_URL + PATH_ORDER_RVSECNCL,
            json=kis_error("APBK0919", "정정취소 가능수량이 없습니다."),
        )
        b = make_broker(tmp_path)
        b.place_order(SYMBOL, OrderSide.BUY, 1)
        with pytest.raises(OrderError, match="APBK0919"):
            b.cancel_order("0000000001")


class TestBuyingPower:
    """시장가 매수는 ORD_UNPR=0 이라 서버가 상한가 × 수량을 주문가능금액에서 잡는다 (공식 order_cash 안내).

    현재가로 사이징한 수량을 그대로 보내면 예수금의 약 77% 를 넘는 순간 APBK0918 로 거부되므로 주문 전에
    매수가능조회(inquire-psbl-order, ORD_DVSN=01) 로 수량을 nrcvb_buy_qty 이하로 맞춰야 한다.
    """

    def test_market_buy_capped_at_buyable_quantity(self, tmp_path, rsps, daily_candles, caplog):
        add_token(rsps)
        close = daily_candles[-1].close
        limit_up = upper_limit(close)
        cash = int(close * 14)  # 현재가 기준으로는 14주가 들어가는 예수금
        requested = 14
        buyable = cash // limit_up  # 상한가 기준 가능 수량 (≈ 14 / 1.3 → 10)
        assert 0 < buyable < requested
        add_psbl(rsps, cash=cash, calc_price=limit_up)
        rsps.add(responses.POST, PAPER_BASE_URL + PATH_ORDER_CASH, json=order_output("0000117058"))
        b = make_broker(tmp_path)
        with caplog.at_level("WARNING", logger="tradingbot.brokers.kis"):
            order = b.place_order(SYMBOL, OrderSide.BUY, requested)
        # 매수가능조회: 공식 파라미터, 시장가는 ORD_UNPR 공란 (공식 샘플 "시장가로 조회 시 공란으로 입력")
        psbl = rsps.calls[1].request
        assert psbl.headers["tr_id"] == "VTTC8908R"
        assert query(psbl) == {
            "CANO": "50012345",
            "ACNT_PRDT_CD": "01",
            "PDNO": SYMBOL,
            "ORD_UNPR": "",
            "ORD_DVSN": "01",
            "CMA_EVLU_AMT_ICLD_YN": "N",
            "OVRS_ICLD_YN": "N",
        }
        sent = body_of(rsps.calls[2])
        assert sent["ORD_DVSN"] == "01" and sent["ORD_UNPR"] == "0"
        assert sent["ORD_QTY"] == str(buyable)
        assert int(sent["ORD_QTY"]) * limit_up <= cash  # 서버가 잡는 상한가 × 수량이 주문가능금액 이내
        assert order.quantity == float(buyable) and order.raw["ORD_QTY"] == str(buyable)
        assert any("축소" in r.getMessage() and str(buyable) in r.getMessage() for r in caplog.records)

    def test_market_buy_within_buyable_quantity_is_not_changed(self, tmp_path, rsps, daily_candles, caplog):
        add_token(rsps)
        close = daily_candles[-1].close
        limit_up = upper_limit(close)
        add_psbl(rsps, cash=limit_up * 5, calc_price=limit_up)  # 상한가 기준으로 정확히 5주
        rsps.add(responses.POST, PAPER_BASE_URL + PATH_ORDER_CASH, json=order_output("0000117059"))
        with caplog.at_level("WARNING", logger="tradingbot.brokers.kis"):
            order = make_broker(tmp_path).place_order(SYMBOL, OrderSide.BUY, 5)
        assert body_of(rsps.calls[2])["ORD_QTY"] == "5" and order.quantity == 5.0
        assert not any("축소" in r.getMessage() for r in caplog.records)

    def test_zero_buyable_quantity_raises_before_sending_order(self, tmp_path, rsps, daily_candles):
        add_token(rsps)
        close = daily_candles[-1].close
        add_psbl(rsps, cash=int(close * 0.5), calc_price=upper_limit(close))  # 1주도 못 산다
        rsps.add(responses.POST, PAPER_BASE_URL + PATH_ORDER_CASH, json=order_output("1"))
        with pytest.raises(InsufficientFunds, match="매수가능수량 0주") as ei:
            make_broker(tmp_path).place_order(SYMBOL, OrderSide.BUY, 1)
        assert ei.value.payload["nrcvb_buy_qty"] == "0"
        assert kis_urls(rsps) == [
            PAPER_BASE_URL + PATH_TOKEN,
            PAPER_BASE_URL + PATH_PSBL_ORDER,
        ]  # 주문 안 나감

    def test_limit_buy_queries_with_limit_price(self, tmp_path, rsps, daily_candles):
        add_token(rsps)
        px = int(round_to_tick(daily_candles[-1].close))
        add_psbl(rsps, cash=px * 5, calc_price=px)  # 지정가: 계산단가 = 주문단가
        rsps.add(responses.POST, PAPER_BASE_URL + PATH_ORDER_CASH, json=order_output("5"))
        order = make_broker(tmp_path).place_order(SYMBOL, OrderSide.BUY, 7, OrderType.LIMIT, price=px)
        q = query(rsps.calls[1].request)
        assert q["ORD_DVSN"] == "00" and q["ORD_UNPR"] == str(px)
        sent = body_of(rsps.calls[2])
        assert sent["ORD_DVSN"] == "00" and sent["ORD_UNPR"] == str(px) and sent["ORD_QTY"] == "5"
        assert order.quantity == 5.0 and order.price == float(px)

    def test_sell_does_not_query_buying_power(self, tmp_path, rsps):
        add_token(rsps)
        rsps.add(responses.POST, PAPER_BASE_URL + PATH_ORDER_CASH, json=order_output("3"))
        make_broker(tmp_path).place_order(SYMBOL, OrderSide.SELL, 2)
        assert kis_urls(rsps) == [PAPER_BASE_URL + PATH_TOKEN, PAPER_BASE_URL + PATH_ORDER_CASH]

    def test_query_business_error_falls_back_to_requested_quantity(self, tmp_path, rsps, caplog):
        add_token(rsps)
        rsps.add(
            responses.GET,
            PAPER_BASE_URL + PATH_PSBL_ORDER,
            json=kis_error("OPSQ0002", "조회할 수 없는 종목코드 입니다."),
        )
        rsps.add(responses.POST, PAPER_BASE_URL + PATH_ORDER_CASH, json=order_output("8"))
        with caplog.at_level("WARNING", logger="tradingbot.brokers.kis"):
            order = make_broker(tmp_path).place_order(SYMBOL, OrderSide.BUY, 4)
        assert body_of(rsps.calls[2])["ORD_QTY"] == "4" and order.quantity == 4.0  # 최종 판정은 서버
        assert any("매수가능조회 실패" in r.getMessage() for r in caplog.records)

    def test_query_auth_error_propagates_without_order(self, tmp_path, rsps):
        add_token(rsps, token="old")
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_PSBL_ORDER, status=401, json={"error": "unauthorized"})
        add_token(rsps, token="new")
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_PSBL_ORDER, status=401, json={"error": "unauthorized"})
        rsps.add(responses.POST, PAPER_BASE_URL + PATH_ORDER_CASH, json=order_output("8"))
        with pytest.raises(AuthenticationError):
            make_broker(tmp_path).place_order(SYMBOL, OrderSide.BUY, 1)
        assert not any(u.endswith(PATH_ORDER_CASH) for u in kis_urls(rsps))

    def test_get_buying_power_parses_official_fields_real_tr_id(self, tmp_path, rsps, daily_candles):
        add_token(rsps, base=REAL_BASE_URL)
        close = daily_candles[-1].close
        limit_up = upper_limit(close)
        cash, max_cash = int(close * 20), int(close * 50)
        rsps.add(
            responses.GET,
            REAL_BASE_URL + PATH_PSBL_ORDER,
            json=psbl_output(cash, limit_up, max_cash=max_cash),
        )
        bp = make_broker(tmp_path, sandbox=False).get_buying_power(" 005930 ")
        req = rsps.calls[1].request
        assert req.headers["tr_id"] == "TTTC8908R"
        assert query(req)["PDNO"] == SYMBOL and query(req)["ORD_DVSN"] == "01"
        assert isinstance(bp, KISBuyingPower) and bp.symbol == SYMBOL
        assert bp.cash == bp.amount == float(cash)
        assert bp.quantity == cash // limit_up
        assert bp.max_amount == float(max_cash) and bp.max_quantity == max_cash // limit_up
        assert bp.calc_price == float(limit_up)
        assert bp.raw["nrcvb_buy_qty"] == str(cash // limit_up)

    def test_get_buying_power_validation(self, tmp_path, rsps, daily_candles):
        add_token(rsps)
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_PSBL_ORDER, json=ok(output={"ord_psbl_cash": "0"}))
        b = make_broker(tmp_path)
        with pytest.raises(OrderError, match="price"):
            b.get_buying_power(SYMBOL, OrderType.LIMIT)
        with pytest.raises(OrderError, match="STOP"):
            b.get_buying_power(SYMBOL, OrderType.STOP, price=daily_candles[-1].close)
        with pytest.raises(AuthenticationError, match="KIS_ACCOUNT_NO"):
            make_broker(tmp_path, account=None).get_buying_power(SYMBOL)
        assert len(rsps.calls) == 0
        with pytest.raises(BrokerError, match="nrcvb_buy_qty"):
            b.get_buying_power(SYMBOL)


class TestOrderLookup:
    @pytest.fixture
    def px(self, daily_candles) -> float:
        return daily_candles[-1].close

    def _lookup(self, tmp_path, rsps, rows, **kw):
        add_token(rsps)
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_DAILY_CCLD, json=ccld_body(rows, **kw))
        return make_broker(tmp_path)

    def test_filled_order(self, tmp_path, rsps, px):
        row = ccld_row(
            odno="0000117057",
            ord_dt="20250926",
            ord_qty=3,
            ord_unpr=0,
            tot_ccld_qty=3,
            avg_prvs=px,
            ord_dvsn_cd="01",
            ord_tmd="121052",
        )
        b = self._lookup(tmp_path, rsps, [row])
        with freeze_time("2025-09-26 04:00:00+00:00"):
            order = b.get_order("0000117057")
        assert order.status == OrderStatus.FILLED and order.is_filled
        assert order.type == OrderType.MARKET and order.side == OrderSide.BUY
        assert order.quantity == 3.0 and order.filled_quantity == 3.0
        assert order.average_price == approx(float(f"{px:.2f}"))
        assert order.filled_value == approx(3 * float(f"{px:.2f}"))
        assert order.created_at == kst(END_DAY, dtime(12, 10, 52)).astimezone(timezone.utc)
        assert order.raw["KRX_FWDG_ORD_ORGNO"] == "91252"
        q = query(rsps.calls[1].request)
        assert rsps.calls[1].request.headers["tr_id"] == "VTTC0081R"
        assert (
            q["ODNO"] == "0000117057" and q["INQR_END_DT"] == "20250926" and q["INQR_STRT_DT"] == "20250919"
        )
        assert q["CCLD_DVSN"] == "00" and q["SLL_BUY_DVSN_CD"] == "00" and q["INQR_DVSN_3"] == "00"

    def test_partial_open_canceled_rejected_expired(self, tmp_path, rsps, px):
        rows = [
            ccld_row(
                odno="0000000001", ord_dt="20250926", ord_qty=5, ord_unpr=px, tot_ccld_qty=2, avg_prvs=px
            ),
            ccld_row(odno="0000000002", ord_dt="20250926", ord_qty=5, ord_unpr=px, side="01"),
            ccld_row(
                odno="0000000003",
                ord_dt="20250926",
                ord_qty=5,
                ord_unpr=px,
                tot_ccld_qty=1,
                avg_prvs=px,
                rmn_qty=0,
            ),
            ccld_row(odno="0000000004", ord_dt="20250926", ord_qty=5, ord_unpr=px, cncl_yn="Y"),
            ccld_row(odno="0000000005", ord_dt="20250926", ord_qty=5, ord_unpr=px, rjct_qty=5),
            ccld_row(odno="0000000006", ord_dt="20250925", ord_qty=5, ord_unpr=px),
        ]
        add_token(rsps)
        for row in rows:
            rsps.add(responses.GET, PAPER_BASE_URL + PATH_DAILY_CCLD, json=ccld_body([row]))
        b = make_broker(tmp_path)
        with freeze_time("2025-09-26 04:00:00+00:00"):
            got = [b.get_order(r["odno"]) for r in rows]
        assert [o.status for o in got] == [
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.OPEN,
            OrderStatus.CANCELED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        ]
        assert got[0].filled_quantity == 2.0 and got[0].remaining_quantity == 3.0
        assert (
            got[1].side == OrderSide.SELL
            and got[1].price == float(fmt_int(px))
            and got[1].average_price is None
        )
        assert got[2].filled_quantity == 1.0

    def test_not_found(self, tmp_path, rsps, px):
        b = self._lookup(
            tmp_path, rsps, [ccld_row(odno="0000000009", ord_dt="20250926", ord_qty=1, ord_unpr=px)]
        )
        with freeze_time("2025-09-26 04:00:00+00:00"):
            with pytest.raises(OrderError, match="찾을 수 없습니다"):
                b.get_order("0000000008")

    def test_open_orders_pagination_and_symbol_filter(self, tmp_path, rsps, px):
        add_token(rsps)
        page1 = ccld_body(
            [
                ccld_row(odno="0000000011", ord_dt="20250926", ord_qty=1, ord_unpr=px),
                ccld_row(odno="0000000012", ord_dt="20250926", pdno="000660", ord_qty=2, ord_unpr=px),
            ],
            fk="fk-page-1",
            nk="nk-page-1",
        )
        page2 = ccld_body(
            [
                ccld_row(
                    odno="0000000013", ord_dt="20250926", ord_qty=4, ord_unpr=px, tot_ccld_qty=4, avg_prvs=px
                )
            ]
        )
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_DAILY_CCLD, json=page1, headers={"tr_cont": "F"})
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_DAILY_CCLD, json=page2, headers={"tr_cont": "E"})
        b = make_broker(tmp_path)
        with freeze_time("2025-09-26 04:00:00+00:00"):
            open_orders = b.get_open_orders()
        assert [o.id for o in open_orders] == ["0000000011", "0000000012"]  # 체결 완료 건 제외
        q1, q2 = query(rsps.calls[1].request), query(rsps.calls[2].request)
        assert q1["CCLD_DVSN"] == "02" and q1["INQR_STRT_DT"] == "20250926" and q1["PDNO"] == ""
        assert rsps.calls[2].request.headers["tr_cont"] == "N"
        assert q2["CTX_AREA_FK100"] == "fk-page-1" and q2["CTX_AREA_NK100"] == "nk-page-1"

        rsps.add(responses.GET, PAPER_BASE_URL + PATH_DAILY_CCLD, json=page1, headers={"tr_cont": "D"})
        with freeze_time("2025-09-26 04:00:00+00:00"):
            only = b.get_open_orders("000660")
        assert [o.id for o in only] == ["0000000012"]
        assert query(rsps.calls[3].request)["PDNO"] == "000660"


# ============================================================================ 장 운영시간 / 휴장일
class TestMarketOpen:
    @pytest.mark.parametrize(
        "when,expected",
        [
            ("2025-09-26 00:00:00+00:00", True),  # 금 09:00 KST
            ("2025-09-26 06:30:00+00:00", True),  # 금 15:30 KST
            ("2025-09-26 06:31:00+00:00", False),  # 금 15:31 KST
            ("2025-09-25 23:59:00+00:00", False),  # 금 08:59 KST
            ("2025-09-27 02:00:00+00:00", False),  # 토 11:00 KST
            ("2025-09-28 02:00:00+00:00", False),  # 일
            ("2025-09-29 03:00:00+00:00", True),  # 월 12:00 KST
        ],
    )
    def test_clock_only(self, tmp_path, when, expected):
        b = make_broker(tmp_path, holiday_check=False)
        with freeze_time(when):
            assert b.is_market_open() is expected

    def test_holiday_check(self, tmp_path, rsps):
        add_token(rsps)
        holiday_rows = [
            {
                "bass_dt": "20251003",
                "wday_dvsn_cd": "06",
                "bzdy_yn": "N",
                "tr_day_yn": "N",
                "opnd_yn": "N",
                "sttl_day_yn": "N",
            },
            {
                "bass_dt": "20251004",
                "wday_dvsn_cd": "07",
                "bzdy_yn": "N",
                "tr_day_yn": "N",
                "opnd_yn": "N",
                "sttl_day_yn": "N",
            },
            {
                "bass_dt": "20251005",
                "wday_dvsn_cd": "01",
                "bzdy_yn": "N",
                "tr_day_yn": "N",
                "opnd_yn": "N",
                "sttl_day_yn": "N",
            },
            {
                "bass_dt": "20251006",
                "wday_dvsn_cd": "02",
                "bzdy_yn": "N",
                "tr_day_yn": "N",
                "opnd_yn": "N",
                "sttl_day_yn": "N",
            },
            {
                "bass_dt": "20251010",
                "wday_dvsn_cd": "06",
                "bzdy_yn": "Y",
                "tr_day_yn": "Y",
                "opnd_yn": "Y",
                "sttl_day_yn": "Y",
            },
        ]
        rsps.add(
            responses.GET,
            PAPER_BASE_URL + PATH_HOLIDAY,
            json=ok(ctx_area_nk="", ctx_area_fk="", output=holiday_rows),
        )
        b = make_broker(tmp_path, holiday_check=True)
        with freeze_time("2025-10-03 02:00:00+00:00"):  # 개천절(금) 11:00 KST
            assert b.is_market_open() is False
            assert b.is_market_open() is False  # 캐시 → 추가 호출 없음
        assert len([c for c in rsps.calls if PATH_HOLIDAY in c.request.url]) == 1
        q = query(rsps.calls[1].request)
        assert q["BASS_DT"] == "20251003" and rsps.calls[1].request.headers["tr_id"] == "CTCA0903R"
        with freeze_time("2025-10-10 02:00:00+00:00"):
            assert b.is_market_open() is True  # 같은 응답에 포함된 날짜도 캐시
        assert b.is_trading_day(date(2025, 10, 6)) is False
        assert len([c for c in rsps.calls if PATH_HOLIDAY in c.request.url]) == 1

    def test_holiday_api_failure_falls_back_to_clock(self, tmp_path, rsps, caplog):
        add_token(rsps)
        rsps.add(
            responses.GET, PAPER_BASE_URL + PATH_HOLIDAY, json=kis_error("OPSQ0001", "서비스 점검중입니다.")
        )
        rsps.add(
            responses.GET, PAPER_BASE_URL + PATH_HOLIDAY, json=kis_error("OPSQ0001", "서비스 점검중입니다.")
        )
        b = make_broker(tmp_path, holiday_check=True)
        with freeze_time("2025-09-26 02:00:00+00:00"):
            assert b.is_market_open() is True
            assert b.is_market_open() is True
        assert sum("휴장일조회 실패" in r.message for r in caplog.records) == 1  # 하루 1회만 경고
        with freeze_time("2025-09-27 02:00:00+00:00"):  # 주말: 시계만으로 판단, API 호출 없음
            assert b.is_market_open() is False
        assert len([c for c in rsps.calls if PATH_HOLIDAY in c.request.url]) == 2

    def test_holiday_missing_base_date(self, tmp_path, rsps):
        add_token(rsps)
        rsps.add(
            responses.GET, PAPER_BASE_URL + PATH_HOLIDAY, json=ok(ctx_area_nk="", ctx_area_fk="", output=[])
        )
        with pytest.raises(BrokerError, match="기준일"):
            make_broker(tmp_path).is_trading_day(date(2025, 9, 26))

    def test_no_credentials_uses_clock_only(self, tmp_path, rsps):
        b = KISBroker(token_path=tmp_path / "t.json", request_interval=0, holiday_check=True)
        with freeze_time("2025-09-26 02:00:00+00:00"):
            assert b.is_market_open() is True
        assert len(rsps.calls) == 0


class TestThrottle:
    def test_request_interval_sleeps_between_calls(self, tmp_path, rsps, daily_candles, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
        clock = iter([100.0, 100.0, 100.01, 100.01])
        monkeypatch.setattr(time, "monotonic", lambda: next(clock))
        add_token(rsps)
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_PRICE, json=price_output(daily_candles[-1].close))
        make_broker(tmp_path, request_interval=0.2).get_ticker(SYMBOL)
        assert len(slept) == 1 and slept[0] == approx(0.19)

    def test_default_interval_follows_official_sample(self, tmp_path, monkeypatch):
        """공식 샘플 kis_auth.py: 실전 0.05s, 모의 0.5s (모의 서버는 초당 한도가 낮다)."""
        assert (PAPER_REQUEST_INTERVAL, REAL_REQUEST_INTERVAL) == (0.5, 0.05)
        assert make_broker(tmp_path, request_interval=None).request_interval == 0.5
        assert make_broker(tmp_path, sandbox=False, request_interval=None).request_interval == 0.05
        assert make_broker(tmp_path, request_interval=0.2).request_interval == 0.2
        assert KISBroker(token_path=tmp_path / "t.json").request_interval == 0.5  # sandbox 기본값
        # from_config: extra.request_interval 이 없으면 기본값, 있으면 그대로
        monkeypatch.setenv("KIS_APP_KEY", APP_KEY)
        monkeypatch.setenv("KIS_APP_SECRET", APP_SECRET)
        monkeypatch.setenv("KIS_ACCOUNT_NO", ACCOUNT)
        base = {"symbols": [SYMBOL], "interval": "1d"}
        for sandbox, extra, expected in (
            (True, {}, 0.5),
            (False, {}, 0.05),
            (True, {"request_interval": 0.1}, 0.1),
        ):
            cfg = AppConfig.model_validate(
                {
                    "broker": {
                        "name": "kis",
                        "sandbox": sandbox,
                        "extra": {"token_path": str(tmp_path / "tok.json"), **extra},
                    },
                    **base,
                }
            )
            assert KISBroker.from_config(cfg).request_interval == expected

    def test_close_is_safe(self, tmp_path):
        b = make_broker(tmp_path)
        b.close()
        b.close()


class TestEquityFallback:
    def test_equity_without_tot_evlu_amt(self, tmp_path, rsps, daily_candles, eth_daily_candles):
        add_token(rsps)
        btc, eth = daily_candles[-1], eth_daily_candles[-1]
        summary = balance_summary(dnca=2_000_000, d2=2_000_000, scts=0)
        summary["tot_evlu_amt"] = "0"
        rsps.add(
            responses.GET,
            PAPER_BASE_URL + PATH_BALANCE,
            json=ok(
                ctx_area_fk100="",
                ctx_area_nk100="",
                output1=[
                    balance_row(SYMBOL, 4, btc.open, btc.close),
                    balance_row("000660", 0, eth.open, eth.close),
                ],
                output2=[summary],
            ),
        )
        expected = 2_000_000 + float(fmt_int(btc.close * 4))
        assert make_broker(tmp_path).get_equity() == approx(expected)

    def test_symbol_is_normalized(self, tmp_path, rsps, daily_candles):
        add_token(rsps)
        rsps.add(responses.GET, PAPER_BASE_URL + PATH_PRICE, json=price_output(daily_candles[-1].close))
        make_broker(tmp_path).get_ticker(" 005930 ")
        assert query(rsps.calls[1].request)["FID_INPUT_ISCD"] == SYMBOL
