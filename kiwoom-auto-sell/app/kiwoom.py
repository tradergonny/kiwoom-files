"""
키움증권 REST API 래퍼
- 접근토큰 발급/갱신 (au10001)
- 계좌 보유종목 조회 (kt00018)
- 현재가/시가/전일종가 조회 (ka10001)
- 호가 조회 (ka10004)
- 일봉 조회 (ka10081) - 5일 이동평균용
- 매도/정정/취소 주문 (kt10001/kt10002/kt10003)
- 미체결 조회 (ka10075)
"""
from __future__ import annotations

import httpx
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

log = logging.getLogger("kiwoom")

REAL_HOST = "https://api.kiwoom.com"
MOCK_HOST = "https://mockapi.kiwoom.com"

# 한국표준시 (KST, UTC+9)
KST = timezone(timedelta(hours=9))


class KiwoomAPIError(Exception):
    """키움 API 요청 실패"""
    def __init__(self, message: str, return_code: Optional[int] = None, raw: Any = None):
        super().__init__(message)
        self.return_code = return_code
        self.raw = raw


class KiwoomClient:
    """
    키움 REST API 클라이언트. appkey/secretkey로 토큰 받고
    각종 TR(api-id)을 POST로 호출함.
    """

    def __init__(self, appkey: str, secretkey: str, is_mock: bool = True):
        self.appkey = appkey
        self.secretkey = secretkey
        self.is_mock = is_mock
        self.host = MOCK_HOST if is_mock else REAL_HOST
        self._token: Optional[str] = None
        self._token_expires: Optional[datetime] = None
        self._lock = asyncio.Lock()

    # ---------- 공통 ----------

    async def _request(
        self,
        api_id: str,
        path: str,
        body: dict,
        need_token: bool = True,
        cont_yn: str = "N",
        next_key: str = "",
    ) -> dict:
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "api-id": api_id,
            "cont-yn": cont_yn,
            "next-key": next_key,
        }
        if need_token:
            token = await self.get_token()
            headers["authorization"] = f"Bearer {token}"

        url = self.host + path
        async with httpx.AsyncClient(timeout=15.0) as cli:
            log.debug("POST %s api-id=%s body=%s", url, api_id, body)
            resp = await cli.post(url, headers=headers, json=body)
            try:
                data = resp.json()
            except Exception:
                raise KiwoomAPIError(
                    f"응답 파싱 실패 status={resp.status_code} text={resp.text[:200]}"
                )
            rc = data.get("return_code")
            if resp.status_code != 200 or (rc is not None and rc != 0):
                msg = data.get("return_msg") or f"HTTP {resp.status_code}"
                raise KiwoomAPIError(f"{api_id} 실패: {msg}", return_code=rc, raw=data)
            return data

    # ---------- 토큰 ----------

    async def get_token(self) -> str:
        """유효 토큰 반환. 만료 5분 전이면 새로 발급."""
        async with self._lock:
            now = datetime.now(KST)
            if self._token and self._token_expires and self._token_expires - now > timedelta(minutes=5):
                return self._token

            url = self.host + "/oauth2/token"
            body = {
                "grant_type": "client_credentials",
                "appkey": self.appkey,
                "secretkey": self.secretkey,
            }
            headers = {"Content-Type": "application/json;charset=UTF-8"}
            try:
                async with httpx.AsyncClient(timeout=15.0) as cli:
                    resp = await cli.post(url, headers=headers, json=body)
                    data = resp.json()
            except Exception as e:
                raise KiwoomAPIError(f"토큰 발급 네트워크 오류: {e}")
            if resp.status_code != 200 or data.get("return_code") not in (0, None):
                raise KiwoomAPIError(f"토큰 발급 실패: {data}")
            self._token = data["token"]
            # expires_dt: "YYYYMMDDHHmmss" - 키움은 KST 기준으로 제공
            exp_str = data.get("expires_dt", "")
            try:
                parsed = datetime.strptime(exp_str, "%Y%m%d%H%M%S").replace(tzinfo=KST)
                # 현재 시각보다 너무 과거면 (6시간 미만 남음) 이상한 것 → fallback
                if (parsed - now).total_seconds() < 3600:
                    log.warning("expires_dt 이상 감지 (남은시간 < 1시간): %s, fallback 사용", exp_str)
                    self._token_expires = now + timedelta(hours=23)
                else:
                    self._token_expires = parsed
            except Exception:
                # fallback: 23시간 뒤
                self._token_expires = now + timedelta(hours=23)
            log.info("토큰 갱신 완료. 만료: %s", self._token_expires)
            return self._token

    # ---------- 계좌 ----------

    async def get_holdings(self) -> list[dict]:
        """
        kt00018 계좌평가잔고내역요청. 보유 종목 리스트 반환.
        반환 필드 예시(각 원소):
          stk_cd, stk_nm, rmnd_qty(보유수량), trde_able_qty(매매가능수량),
          pur_pric(매입가), cur_prc(현재가), pred_close_pric(전일종가), evltv_prft(평가손익)
        """
        body = {"qry_tp": "1", "dmst_stex_tp": "KRX"}
        data = await self._request("kt00018", "/api/dostk/acnt", body)
        rows = data.get("acnt_evlt_remn_indv_tot", []) or []
        out = []
        for r in rows:
            out.append({
                "stk_cd": _clean_stkcd(r.get("stk_cd", "")),
                "stk_nm": r.get("stk_nm", ""),
                "rmnd_qty": _to_int(r.get("rmnd_qty")),
                "trde_able_qty": _to_int(r.get("trde_able_qty")),
                "pur_pric": _to_price(r.get("pur_pric")),
                "cur_prc": _to_price(r.get("cur_prc")),
                "pred_close_pric": _to_price(r.get("pred_close_pric")),
                "evltv_prft": _to_int(r.get("evltv_prft")),  # 손익은 음수 유지
                "prft_rt": _to_float(r.get("prft_rt")),
            })
        return out

    async def get_pending_orders(self, stk_cd: Optional[str] = None) -> list[dict]:
        """
        ka10075 미체결 조회. 매도만(trde_tp=1) 필터해 반환.
        주요 필드: ord_no, stk_cd, stk_nm, ord_qty, ord_pric, oso_qty(미체결수량), cntr_qty(체결량), trde_tp
        """
        body = {
            "all_stk_tp": "1" if stk_cd else "0",
            "trde_tp": "1",  # 매도만
            "stk_cd": stk_cd or "",
            "stex_tp": "1",  # 1:KRX (모의는 KRX만 지원)
        }
        data = await self._request("ka10075", "/api/dostk/acnt", body)
        rows = data.get("oso", []) or []
        out = []
        for r in rows:
            out.append({
                "ord_no": r.get("ord_no", ""),
                "stk_cd": _clean_stkcd(r.get("stk_cd", "")),
                "stk_nm": r.get("stk_nm", ""),
                "ord_qty": _to_int(r.get("ord_qty")),
                "ord_pric": _to_price(r.get("ord_pric")),
                "oso_qty": _to_int(r.get("oso_qty")),  # 미체결 잔량
                "cntr_qty": _to_int(r.get("cntr_qty")),  # 체결수량
                "trde_tp": r.get("trde_tp", ""),
            })
        return out

    async def get_filled_orders(self, stk_cd: Optional[str] = None) -> list[dict]:
        """
        ka10076 체결요청. 오늘 체결된 주문 이력 반환.
        Body 필드 (공식 명세):
          - stk_cd: 종목코드 (선택)
          - qry_tp: 조회구분 ("0:전체" 또는 "1:종목")
          - sell_tp: 매도수구분 ("0:전체", "1:매도", "2:매수")
          - ord_no: 기준 주문번호 (이거보다 과거 체결된 내역; 빈 문자열 = 오늘 전부)
          - stex_tp: 거래소구분 ("0:통합", "1:KRX", "2:NXT")
        응답 LIST 키: `cntr`
        """
        body = {
            "stk_cd": stk_cd or "",
            "qry_tp": "1" if stk_cd else "0",  # 종목 지정하면 종목필터, 아니면 전체
            "sell_tp": "1",                    # 매도만
            "ord_no": "",                      # 오늘 전체
            "stex_tp": "1",                    # 1:KRX (모의는 KRX만)
        }
        try:
            data = await self._request("ka10076", "/api/dostk/acnt", body)
        except KiwoomAPIError as e:
            log.warning("체결조회 실패 (무시): %s", e)
            return []
        rows = data.get("cntr", []) or []
        out = []
        for r in rows:
            out.append({
                "ord_no": r.get("ord_no", ""),
                "stk_nm": r.get("stk_nm", ""),
                "cntr_qty": _to_int(r.get("cntr_qty")),
                "cntr_pric": _to_price(r.get("cntr_pric")),
                "ord_qty": _to_int(r.get("ord_qty")),
                "ord_pric": _to_price(r.get("ord_pric")),
                "io_tp_nm": r.get("io_tp_nm", ""),  # 주문구분
            })
        return out

    # ---------- 시세 ----------

    async def get_basic_info(self, stk_cd: str) -> dict:
        """
        ka10001 주식기본정보요청. 현재가, 시가, 기준가(전일종가 대용) 등 반환.
        """
        body = {"stk_cd": stk_cd}
        data = await self._request("ka10001", "/api/dostk/stkinfo", body)
        return {
            "stk_cd": data.get("stk_cd", stk_cd),
            "stk_nm": data.get("stk_nm", ""),
            "cur_prc": _to_price(data.get("cur_prc")),
            "open_pric": _to_price(data.get("open_pric")),
            "high_pric": _to_price(data.get("high_pric")),
            "low_pric": _to_price(data.get("low_pric")),
            "base_pric": _to_price(data.get("base_pric")),  # 기준가 = 전일종가
        }

    async def get_orderbook(self, stk_cd: str) -> dict:
        """
        ka10004 주식호가요청. 매도1~10호가, 매수1~10호가 반환.
        매도호가 리스트는 호가1(최우선)~10 순으로, 가격은 부호 있으므로 abs() 처리.
        """
        body = {"stk_cd": stk_cd}
        data = await self._request("ka10004", "/api/dostk/mrkcond", body)
        asks: list[int] = []
        bids: list[int] = []
        # 매도1 = sel_fpr_bid, 매도2~10 = sel_2th_pre_bid ~ sel_10th_pre_bid
        asks.append(_to_int(data.get("sel_fpr_bid")))
        for i in range(2, 11):
            asks.append(_to_int(data.get(f"sel_{i}th_pre_bid")))
        # 매수1 = buy_fpr_bid, 매수2~10 = buy_2th_pre_bid ~ buy_10th_pre_bid
        bids.append(_to_int(data.get("buy_fpr_bid")))
        for i in range(2, 11):
            bids.append(_to_int(data.get(f"buy_{i}th_pre_bid")))
        return {
            "asks": [abs(x) for x in asks if x],  # 매도호가 (낮은->높은)
            "bids": [abs(x) for x in bids if x],  # 매수호가 (높은->낮은)
        }

    async def get_daily_candles(self, stk_cd: str, base_dt: Optional[str] = None) -> list[dict]:
        """
        ka10081 주식일봉차트. 기준일자 포함 과거 일봉 리스트 반환.
        base_dt: YYYYMMDD. 생략 시 오늘.
        """
        if not base_dt:
            base_dt = datetime.now(KST).strftime("%Y%m%d")
        body = {"stk_cd": stk_cd, "base_dt": base_dt, "upd_stkpc_tp": "1"}
        data = await self._request("ka10081", "/api/dostk/chart", body)
        rows = data.get("stk_dt_pole_chart_qry", []) or []
        out = []
        for r in rows:
            out.append({
                "dt": r.get("dt", ""),
                "open": _to_price(r.get("open_pric")),
                "high": _to_price(r.get("high_pric")),
                "low": _to_price(r.get("low_pric")),
                "close": _to_price(r.get("cur_prc")),
                "volume": _to_int(r.get("trde_qty")),
            })
        return out

    async def get_5day_ma(self, stk_cd: str) -> Optional[float]:
        """직전 5거래일(오늘 제외) 종가 평균. 장 마감 전이라 오늘 종가가 미확정이므로 제외."""
        try:
            from datetime import datetime, timezone, timedelta
            KST_TZ = timezone(timedelta(hours=9))
            today_str = datetime.now(KST_TZ).strftime("%Y%m%d")

            candles = await self.get_daily_candles(stk_cd)
            # 최신순으로 옴. 오늘자(dt == today_str)는 제외하고 과거 5거래일 종가
            past_closes = []
            for c in candles:
                if c.get("dt") == today_str:
                    continue  # 오늘은 제외
                if c["close"]:
                    past_closes.append(c["close"])
                if len(past_closes) >= 5:
                    break

            if len(past_closes) < 5:
                return None
            return sum(past_closes) / 5.0
        except Exception as e:
            log.warning("5일선 계산 실패 %s: %s", stk_cd, e)
            return None

    # ---------- 주문 ----------

    async def sell_limit(self, stk_cd: str, qty: int, price: int) -> str:
        """지정가 매도 (trde_tp=0). 주문번호 반환."""
        qty = int(qty or 0)
        price = int(price or 0)
        if qty <= 0:
            raise KiwoomAPIError(f"kt10001 실패: 매도 수량이 0 이하 ({qty})")
        if price <= 0:
            raise KiwoomAPIError(f"kt10001 실패: 지정가가 0 이하 ({price})")
        body = {
            "dmst_stex_tp": "KRX",
            "stk_cd": stk_cd,
            "ord_qty": str(qty),
            "ord_uv": str(price),
            "trde_tp": "0",
            "cond_uv": "",
        }
        data = await self._request("kt10001", "/api/dostk/ordr", body)
        ord_no = data.get("ord_no", "")
        if not ord_no:
            raise KiwoomAPIError(f"kt10001 성공했으나 주문번호 없음: {data}")
        return ord_no

    async def sell_market(self, stk_cd: str, qty: int) -> str:
        """시장가 매도 (trde_tp=3). 주문번호 반환."""
        qty = int(qty or 0)
        if qty <= 0:
            raise KiwoomAPIError(f"kt10001 실패: 매도 수량이 0 이하 ({qty})")
        body = {
            "dmst_stex_tp": "KRX",
            "stk_cd": stk_cd,
            "ord_qty": str(qty),
            "ord_uv": "",
            "trde_tp": "3",
            "cond_uv": "",
        }
        data = await self._request("kt10001", "/api/dostk/ordr", body)
        ord_no = data.get("ord_no", "")
        if not ord_no:
            raise KiwoomAPIError(f"kt10001 성공했으나 주문번호 없음: {data}")
        return ord_no

    async def cancel_order(self, stk_cd: str, orig_ord_no: str, qty: int = 0) -> str:
        """취소 주문 (kt10003). qty=0이면 잔량 전부 취소."""
        if not orig_ord_no:
            raise KiwoomAPIError("kt10003 실패: 원주문번호 누락")
        body = {
            "dmst_stex_tp": "KRX",
            "orig_ord_no": str(orig_ord_no),
            "stk_cd": stk_cd,
            "cncl_qty": str(int(qty or 0)),
        }
        data = await self._request("kt10003", "/api/dostk/ordr", body)
        return data.get("ord_no", "")

    async def modify_order(self, stk_cd: str, orig_ord_no: str, qty: int, new_price: int) -> str:
        """정정 주문 (kt10002)."""
        body = {
            "dmst_stex_tp": "KRX",
            "orig_ord_no": orig_ord_no,
            "stk_cd": stk_cd,
            "mdfy_qty": str(qty),
            "mdfy_uv": str(new_price),
            "mdfy_cond_uv": "",
        }
        data = await self._request("kt10002", "/api/dostk/ordr", body)
        return data.get("ord_no", "")


# ---------- 유틸 ----------

def _to_int(v: Any) -> int:
    """키움 응답을 int로. 빈 값 → 0. 부호 유지."""
    if v is None or v == "":
        return 0
    try:
        s = str(v).strip().replace(",", "")
        return int(float(s))
    except Exception:
        return 0


def _to_price(v: Any) -> int:
    """키움 가격 응답 전용. 상한가/하한가 부호 붙은 경우 절댓값. 주문가/체결가 등에 사용."""
    if v is None or v == "":
        return 0
    try:
        s = str(v).strip().replace(",", "")
        return abs(int(float(s)))
    except Exception:
        return 0


def _to_float(v: Any) -> float:
    """키움 응답을 float로. 수익률 등은 음수도 의미있으므로 부호 유지."""
    if v is None or v == "":
        return 0.0
    try:
        s = str(v).strip().replace(",", "").replace("+", "")
        return float(s)
    except Exception:
        return 0.0


def _clean_stkcd(code: str) -> str:
    """'A005930' -> '005930' 처럼 접두사 제거."""
    code = code.strip()
    if code.startswith(("A", "a")) and len(code) > 6:
        code = code[1:]
    return code


def round_to_tick(price, up: bool = False) -> int:
    """
    가격을 KRX 호가 단위로 반올림. 무조건 int 반환.
    up=True: 올림(sell 지정가가 목표 이상이어야 할 때)
    up=False: 가장 가까운 호가
    """
    # 반드시 int로 먼저 변환 (float 들어와도 안전)
    try:
        price = int(price)
    except (TypeError, ValueError):
        return 0
    if price <= 0:
        return 0
    if price < 2000:
        tick = 1
    elif price < 5000:
        tick = 5
    elif price < 20000:
        tick = 10
    elif price < 50000:
        tick = 50
    elif price < 200000:
        tick = 100
    elif price < 500000:
        tick = 500
    else:
        tick = 1000
    if up:
        return int(((price + tick - 1) // tick) * tick)
    return int(round(price / tick)) * tick


def split_4(total: int) -> list[int]:
    """짝수 total을 4등분. 나머지는 뒤쪽 슬롯에 더 할당.
    예) 10 -> [2,2,3,3], 14 -> [3,3,4,4], 4 -> [1,1,1,1], 8 -> [2,2,2,2]
    """
    base = total // 4
    rem = total % 4
    return [base + (1 if i >= 4 - rem else 0) for i in range(4)]
