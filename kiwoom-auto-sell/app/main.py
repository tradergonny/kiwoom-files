"""
FastAPI 서버. 웹 UI + REST API 제공.
시작 시:
  - DB 초기화
  - 엔진 백그라운드 태스크 시작 (10초마다 tick)
"""
from __future__ import annotations

import logging
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db
from .kiwoom import KiwoomClient, KiwoomAPIError
from .engine import AutoSellEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("main")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# --- 싱글톤 클라이언트 ---

_client: Optional[KiwoomClient] = None


def _build_client_from_db() -> Optional[KiwoomClient]:
    appkey = db.config_get("appkey")
    secretkey = db.config_get("secretkey")
    if not appkey or not secretkey:
        return None
    is_mock = db.config_get("is_mock", "1") == "1"
    return KiwoomClient(appkey, secretkey, is_mock=is_mock)


def _refresh_client():
    global _client
    _client = _build_client_from_db()


def _client_factory() -> Optional[KiwoomClient]:
    return _client


engine = AutoSellEngine(client_factory=_client_factory)


# --- 앱 lifecycle ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    _refresh_client()
    await engine.start(interval_sec=10)
    log.info("서버 시작 완료")
    try:
        yield
    finally:
        await engine.stop()
        log.info("서버 종료")


app = FastAPI(title="키움 자동매도", lifespan=lifespan)

# 정적 파일
static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ---------- 라우트 ----------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ----- 설정 -----

class ConfigIn(BaseModel):
    appkey: Optional[str] = None      # None/빈 문자열이면 기존 값 유지
    secretkey: Optional[str] = None   # None/빈 문자열이면 기존 값 유지
    is_mock: bool = True


@app.get("/api/config")
async def api_get_config():
    return {
        "configured": bool(db.config_get("appkey") and db.config_get("secretkey")),
        "is_mock": db.config_get("is_mock", "1") == "1",
        "appkey_masked": _mask(db.config_get("appkey", "")),
    }


@app.post("/api/config")
async def api_save_config(cfg: ConfigIn):
    # appkey/secretkey는 빈 값이면 기존 값 유지 (수정 편의성)
    if cfg.appkey and cfg.appkey.strip():
        appkey = cfg.appkey.strip()
        if len(appkey) < 10:
            raise HTTPException(400, "앱키가 너무 짧습니다 (10자 이상)")
        db.config_set("appkey", appkey)
    if cfg.secretkey and cfg.secretkey.strip():
        secretkey = cfg.secretkey.strip()
        if len(secretkey) < 10:
            raise HTTPException(400, "시크릿키가 너무 짧습니다 (10자 이상)")
        db.config_set("secretkey", secretkey)
    # 처음 설정하는데 둘 다 비어있으면 거부
    if not db.config_get("appkey") or not db.config_get("secretkey"):
        raise HTTPException(400, "앱키와 시크릿키를 모두 입력해주세요")
    db.config_set("is_mock", "1" if cfg.is_mock else "0")
    _refresh_client()
    db.log_activity("config_saved", "", f"모드={'모의' if cfg.is_mock else '실전'}")
    return {"ok": True}


@app.post("/api/toggle-mode")
async def api_toggle_mode():
    cur = db.config_get("is_mock", "1") == "1"
    new_val = "0" if cur else "1"
    db.config_set("is_mock", new_val)
    _refresh_client()
    db.log_activity("mode_toggled", "", f"모드 전환 → {'모의' if new_val == '1' else '실전'}")
    return {"is_mock": new_val == "1"}


@app.post("/api/test-connection")
async def api_test_connection():
    cli = _client_factory()
    if not cli:
        raise HTTPException(400, "API 키가 설정되지 않았습니다")
    try:
        token = await cli.get_token()
        return {"ok": True, "token_preview": token[:20] + "...",
                "host": cli.host, "mode": "모의" if cli.is_mock else "실전"}
    except KiwoomAPIError as e:
        from .engine import _is_auth_error
        msg = str(e)
        mode_now = "모의" if cli.is_mock else "실전"
        mode_other = "실전" if cli.is_mock else "모의"
        # 실전/모의 불일치
        if "8030" in msg or "8031" in msg or "투자구분" in msg:
            raise HTTPException(400,
                f"⚠️ 앱키 종류 불일치! 현재 [{mode_now}] 모드인데 발급받은 앱키는 [{mode_other}]용입니다.\n\n"
                f"해결: 키움 OpenAPI 포털에서 {mode_now}용 앱키로 재발급하거나, "
                f"UI에서 {mode_other} 모드로 전환하세요.\n\n"
                f"(원본 에러: {msg})")
        # IP 미등록 (가장 흔한 에러!)
        if "8010" in msg or "8050" in msg or "단말기" in msg or "IP" in msg:
            raise HTTPException(400,
                f"🌐 서버 IP가 키움에 등록되지 않았거나 다른 IP에서 토큰을 발급받았어요.\n\n"
                f"해결: 서버 터미널에서 `curl ifconfig.me` 명령어로 IP 확인 후, "
                f"키움 OpenAPI 포털 → 'IP 등록' 페이지에 추가해주세요.\n\n"
                f"(원본 에러: {msg})")
        # 앱키/시크릿 검증 실패
        if "8001" in msg or "8002" in msg:
            raise HTTPException(400,
                f"❌ 앱키 또는 시크릿키가 올바르지 않습니다. 복사할 때 앞뒤 공백이 포함됐거나 오타가 있을 수 있어요.\n\n"
                f"(원본 에러: {msg})")
        # 일반 에러
        if _is_auth_error(e):
            raise HTTPException(400,
                f"🔐 인증 관련 오류입니다. 앱키/시크릿키를 재확인하고 재발급해 보세요.\n\n{msg}")
        raise HTTPException(400, f"연결 실패: {msg}")


# ----- 보유종목 -----

@app.get("/api/holdings")
async def api_holdings():
    cli = _client_factory()
    if not cli:
        raise HTTPException(400, "API 키가 설정되지 않았습니다")
    try:
        holdings = await cli.get_holdings()
        # 보유 0주 종목 제외 (최근 전량 매도 후 잔여 레코드 등)
        holdings = [h for h in holdings if (h.get("rmnd_qty") or 0) > 0]
        return {"holdings": holdings}
    except KiwoomAPIError as e:
        raise HTTPException(502, f"조회 실패: {e}")


@app.get("/api/orderbook/{stock_code}")
async def api_orderbook(stock_code: str):
    """종목 호가 + 현재가. 목표가 입력 시 참고용."""
    cli = _client_factory()
    if not cli:
        raise HTTPException(400, "API 키가 설정되지 않았습니다")
    try:
        info = await cli.get_basic_info(stock_code)
        ob = await cli.get_orderbook(stock_code)
        return {
            "stk_cd": stock_code,
            "stk_nm": info.get("stk_nm", ""),
            "cur_prc": info.get("cur_prc", 0),
            "open_pric": info.get("open_pric", 0),
            "high_pric": info.get("high_pric", 0),
            "low_pric": info.get("low_pric", 0),
            "base_pric": info.get("base_pric", 0),
            "asks": ob.get("asks", []),  # 매도호가 (낮은→높은)
            "bids": ob.get("bids", []),  # 매수호가 (높은→낮은)
        }
    except KiwoomAPIError as e:
        raise HTTPException(502, f"조회 실패: {e}")


# ----- 전략 -----

class StrategyIn(BaseModel):
    stock_code: str
    stock_name: str = ""
    strategy_type: str  # day | target | swing1 | swing2 | swing3
    holding_qty: int    # 보유 수량 (홀짝 조정 전 원본)
    params: dict = {}


@app.get("/api/strategies")
async def api_list_strategies():
    out = []
    for s in db.list_strategies():
        s["slots"] = db.get_slots(s["stock_code"])
        out.append(s)
    return {"strategies": out}


@app.post("/api/strategies")
async def api_set_strategy(s: StrategyIn):
    if s.strategy_type not in ("day", "target", "swing1", "swing2", "swing3"):
        raise HTTPException(400, "잘못된 전략 타입")
    if s.holding_qty < 4:
        raise HTTPException(400, "보유 수량은 4주 이상이어야 합니다 (4등분 필요)")

    # 홀수면 1주 제외하고 짝수화
    reserved = 1 if s.holding_qty % 2 else 0
    total = s.holding_qty - reserved
    if total < 4:
        raise HTTPException(400, "짝수화 후 수량이 4주 미만입니다")

    # 기존 전략 있으면 주문 전부 취소 후 슬롯 삭제
    existing = db.get_strategy(s.stock_code)
    if existing:
        # 🔒 먼저 기존 전략 cancelled로 (엔진 개입 방지)
        db.set_strategy_state(s.stock_code, "cancelled")
        await engine.cancel_all_for_stock(s.stock_code)
        db.delete_slots(s.stock_code)

    # 전략별 파라미터 검증
    params = dict(s.params or {})
    if s.strategy_type == "day":
        # Case B 기본값
        if "case_b_pcts" not in params:
            params["case_b_pcts"] = [4.5, 5.2, 5.5, 5.8]
    elif s.strategy_type == "target":
        tp = int(params.get("target_price", 0))
        if tp <= 0:
            raise HTTPException(400, "목표가를 입력해 주세요")
    elif s.strategy_type == "swing1":
        params.setdefault("gain_pct", 10.0)
    elif s.strategy_type == "swing2":
        tp = int(params.get("trigger_price", 0))
        if tp <= 0:
            raise HTTPException(400, "기준가를 입력해 주세요")
    elif s.strategy_type == "swing3":
        params.setdefault("drop_pct", 6.0)

    db.upsert_strategy(
        stock_code=s.stock_code,
        stock_name=s.stock_name,
        strategy_type=s.strategy_type,
        params=params,
        total_qty=total,
        reserved_qty=reserved,
    )
    db.log_activity(
        "strategy_set", s.stock_code,
        f"{s.strategy_type} 설정 (총 {s.holding_qty}주 → 대상 {total}주, 예비 {reserved}주) "
        f"params={params}"
    )
    return {"ok": True, "total_qty": total, "reserved_qty": reserved}


@app.post("/api/strategies/{stock_code}/cancel")
async def api_cancel_strategy(stock_code: str):
    existing = db.get_strategy(stock_code)
    if not existing:
        raise HTTPException(404, "없는 전략")
    # 🔒 먼저 전략 상태를 cancelled로 변경 (엔진이 새 주문 발사 못하게)
    db.set_strategy_state(stock_code, "cancelled")
    # 그 다음 기존 주문 취소
    n = await engine.cancel_all_for_stock(stock_code)
    db.delete_strategy(stock_code)
    db.delete_slots(stock_code)
    db.log_activity("strategy_cancelled", stock_code, f"전략 해제 및 주문 {n}건 취소")
    return {"ok": True, "cancelled_orders": n}


@app.post("/api/strategies/{stock_code}/retry-errors")
async def api_retry_errored_slots(stock_code: str):
    """에러 상태로 박힌 슬롯들을 pending으로 되돌려 재시도하게 함."""
    existing = db.get_strategy(stock_code)
    if not existing:
        raise HTTPException(404, "없는 전략")
    n = 0
    for s in db.get_slots(stock_code):
        if s["status"] == "error":
            db.update_slot(s["id"], status="pending",
                           notes=(s["notes"] or "") + " | 사용자 재시도")
            n += 1
    db.log_activity("slots_retry", stock_code, f"에러 슬롯 {n}개 재시도 예약")
    return {"ok": True, "retried": n}


@app.post("/api/emergency-stop")
async def api_emergency_stop():
    cli = _client_factory()
    if not cli:
        raise HTTPException(400, "API 키가 설정되지 않았습니다")
    # 🔒 1단계: 모든 전략을 먼저 cancelled로 (엔진 재주문 원천 차단)
    active_strategies = list(db.list_strategies(active_only=True))
    for s in active_strategies:
        try:
            db.set_strategy_state(s["stock_code"], "cancelled")
        except Exception as e:
            log.error("전략 비활성화 실패 %s: %s", s["stock_code"], e)

    # 2단계: 그 다음 기존 주문들 취소
    total = 0
    errors = []
    for s in active_strategies:
        try:
            n = await engine.cancel_all_for_stock(s["stock_code"])
            total += n
        except Exception as e:
            errors.append(f"{s['stock_code']}: {e}")
    db.log_activity("emergency_stop", "",
                    f"🛑 응급정지 - 전략 {len(active_strategies)}개 비활성화, 주문 {total}건 취소 (오류 {len(errors)}건)",
                    level="WARN")
    return {"ok": True, "cancelled": total, "errors": errors}


# ----- 기타 -----

@app.get("/api/logs")
async def api_logs(limit: int = 200):
    return {"logs": db.recent_logs(limit=limit)}


@app.get("/api/status")
async def api_status():
    from datetime import datetime, timedelta
    from .kiwoom import KST
    from .engine import is_market_open_day
    now = datetime.now(KST)

    # 시장 상태 계산
    today_open = is_market_open_day(now)
    market_open_now = (today_open
                       and now.time().hour * 60 + now.time().minute >= 9 * 60
                       and now.time().hour * 60 + now.time().minute < 15 * 60 + 30)

    # 다음 장 시작 시각 계산
    next_open = None
    if market_open_now:
        pass  # 지금 열려있음
    else:
        # 오늘 열릴 예정이고 아직 9시 전이면 오늘 9시, 아니면 다음 영업일 9시
        candidate = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if candidate <= now or not today_open:
            # 다음 영업일 찾기
            candidate = candidate + timedelta(days=1)
            for _ in range(14):  # 최대 2주
                if is_market_open_day(candidate):
                    break
                candidate += timedelta(days=1)
        next_open = candidate.isoformat()

    # 상태 메시지
    if market_open_now:
        status_msg = "🟢 장 진행중"
    elif not today_open:
        status_msg = "📅 오늘 휴장 (주말 또는 공휴일)"
    elif now.time().hour < 9:
        status_msg = f"⏰ 장 시작 대기중 ({9 - now.time().hour}시간 뒤 개장)"
    else:
        status_msg = "🔚 장 종료됨 (다음 영업일 9시 개장)"

    # 토큰 상태 확인
    token_status = "없음"
    token_valid = False
    if _client:
        if _client._token and _client._token_expires:
            remaining = (_client._token_expires - now).total_seconds()
            if remaining > 300:
                token_status = f"정상 (만료 {int(remaining/3600)}시간 {int((remaining%3600)/60)}분 후)"
                token_valid = True
            elif remaining > 0:
                token_status = f"⚠️ 곧 만료 ({int(remaining/60)}분 남음)"
                token_valid = True
            else:
                token_status = "❌ 만료됨"
        else:
            token_status = "미발급"

    # 오늘 체결 요약 (날짜 필터링)
    today_summary = {"filled_count": 0, "filled_qty": 0, "pending_count": 0, "error_count": 0}
    today_str = now.strftime("%Y-%m-%d")
    try:
        for s in db.list_strategies():
            for sl in db.get_slots(s["stock_code"]):
                status = sl.get("status")
                # 체결은 오늘 filled_at 인 것만
                if status == "filled":
                    filled_at = sl.get("filled_at") or ""
                    if filled_at.startswith(today_str):
                        today_summary["filled_count"] += 1
                        today_summary["filled_qty"] += sl.get("filled_qty") or sl.get("qty") or 0
                # 대기 중인 것은 활성 전략의 것만
                elif status == "pending" and s.get("state") == "active":
                    today_summary["pending_count"] += 1
                elif status == "error" and s.get("state") == "active":
                    today_summary["error_count"] += 1
    except Exception:
        pass

    return {
        "now_kst": now.isoformat(timespec="seconds"),
        "weekday": now.weekday(),
        "market_open": market_open_now,
        "today_is_holiday": not today_open,
        "status_msg": status_msg,
        "next_market_open": next_open,
        "engine_running": engine._running,
        "client_configured": _client is not None,
        "token_status": token_status,
        "token_valid": token_valid,
        "today_summary": today_summary,
    }


def _mask(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 8:
        return "****"
    return s[:4] + "****" + s[-4:]
