"""
전략 실행 엔진. 10초 간격으로 tick() 호출.
전략별 상태 머신:
  - day: 시초가 4% 기준 분기
  - target: 목표가 분할 매도
  - swing1/2/3: 스윙 매도

공통 규칙:
  - 매 tick: 'ordered' 상태 슬롯들의 체결 여부 갱신
  - 트리거 시각 도달 + 조건 만족 시 주문 발사
  - 지정가 미체결 시 fallback_deadline 뒤 시장가 전환
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from typing import Optional, Callable

from . import db
from .kiwoom import KiwoomClient, KiwoomAPIError, KST, round_to_tick, split_4
from .holidays import is_market_holiday

log = logging.getLogger("engine")

# 장 시간
MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(15, 30)

# 단타 Case A: 슬롯별 발사 시각
DAY_A_TIMES = [time(9, 0), time(9, 5), time(9, 10), time(9, 15)]

# 단타 Case B 오후 fallback (미체결 잔여분 4등분)
DAY_B_FALLBACK_TIMES = [time(12, 0), time(13, 0), time(14, 0), time(14, 30)]

# 목표가: 첫 슬롯 체결 후 후속 간격(분)
TARGET_FOLLOWUP_INTERVAL_MIN = 5

# 목표가: 당일 미체결시 종가 매도 시각
TARGET_CLOSE_TIME = time(15, 20)

# 스윙 공통 발사 시각
SWING_CLOSE_TIME = time(15, 15)
SWING_V2_CHECK_TIME = time(15, 10)
SWING_V3_CHECK_TIME = time(15, 5)

# 지정가 미체결 시 시장가 전환 대기(분) - 단타 Case A 용
LIMIT_FALLBACK_MINUTES = 5

# API 호출 간 최소 간격(초) - 키움 초당 요청 제한 회피
API_CALL_INTERVAL_SEC = 0.35

# 기준가(전일종가) 조회 실패 시 최대 재시도 횟수. 이후엔 전략 에러로 마킹.
MAX_INIT_RETRY = 30  # tick이 10초 간격이면 약 5분 재시도


def is_market_open_day(d: datetime) -> bool:
    """해당 날짜가 장이 열리는 날인지. 주말/공휴일이면 False."""
    return not is_market_holiday(d.date())


ClientFactory = Callable[[], Optional[KiwoomClient]]


class AutoSellEngine:
    def __init__(self, client_factory: ClientFactory):
        self.client_factory = client_factory
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_api_call = 0.0  # API 요청 제한용
        self._throttle_lock = asyncio.Lock()  # 🔒 throttle 동시성 보호
        self._price_cache: dict = {}  # tick 내 가격 캐시 (종목코드→(현재가, 캐시시각))

    async def _throttle(self):
        """API 호출 직전 호출하면 초당 요청 제한 회피. 동시성 안전."""
        import time as _time
        async with self._throttle_lock:
            now = _time.monotonic()
            elapsed = now - self._last_api_call
            if elapsed < API_CALL_INTERVAL_SEC:
                await asyncio.sleep(API_CALL_INTERVAL_SEC - elapsed)
            self._last_api_call = _time.monotonic()

    async def _get_cached_price(self, cli: KiwoomClient, stk: str) -> int:
        """현재 tick 내에서는 같은 종목 가격을 한 번만 조회. 조회 실패(0)는 캐시 안 함."""
        import time as _time
        now = _time.monotonic()
        if stk in self._price_cache:
            price, ts = self._price_cache[stk]
            if now - ts < 8.0:  # 8초 이내 캐시 재사용
                return price
        await self._throttle()
        try:
            info = await cli.get_basic_info(stk)
            price = int(info.get("cur_prc") or 0)
        except Exception as e:
            log.warning("가격 조회 실패 %s: %s", stk, e)
            price = 0
        # 조회 실패(0)는 캐시하지 않음 → 다음 호출에서 재시도
        if price > 0:
            self._price_cache[stk] = (price, now)
        return price

    # ---------- lifecycle ----------

    async def start(self, interval_sec: int = 10):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(interval_sec))
        log.info("엔진 시작 (interval=%ds)", interval_sec)

    async def stop(self):
        """엔진 정지. 진행 중인 tick이 있으면 최대 5초까지 완료 대기."""
        self._running = False
        if self._task:
            try:
                # tick이 진행 중이면 최대 5초 기다림
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("tick 5초 내 종료 안됨 → 강제 취소")
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self, interval: int):
        consecutive_failures = 0
        while self._running:
            try:
                await self.tick()
                if consecutive_failures > 0:
                    try:
                        db.log_activity("engine_recovered", "",
                                        f"엔진 복구됨 (연속 실패 {consecutive_failures}회 뒤)")
                    except Exception:
                        pass
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                log.exception("tick 예외: %s", e)
                # 로그 자체가 실패해도 엔진은 살아남아야 함
                try:
                    if consecutive_failures == 3:
                        db.log_activity("engine_degraded", "",
                                        f"⚠️ 엔진 3회 연속 실패: {e} (키 만료/네트워크 확인 필요)",
                                        level="ERROR")
                    elif consecutive_failures == 10:
                        db.log_activity("engine_critical", "",
                                        f"🚨 엔진 10회 연속 실패: {e}",
                                        level="ERROR")
                    else:
                        db.log_activity("tick_exception", "", str(e), level="WARN")
                except Exception:
                    log.exception("로그 기록 자체도 실패")
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise  # 정상 종료 신호는 전파

    # ---------- 메인 tick ----------

    async def tick(self):
        cli = self.client_factory()
        if cli is None:
            return
        now = datetime.now(KST)

        # 주말/공휴일 스킵
        if not is_market_open_day(now):
            return

        # 장 마감 후 30분까지는 체결 동기화 위해 tick 계속 돌림
        # (15:30~16:00 정도는 동시호가 체결 후 상태 업데이트 필요)
        cur_min = now.time().hour * 60 + now.time().minute
        if cur_min >= (15 * 60 + 60):  # 16:00 이후
            return

        # 매 tick마다 가격 캐시 초기화 (신선한 가격 사용)
        self._price_cache.clear()

        # 장 시작 전이면 sync 스킵 (API 낭비 방지)
        # 단, 장 시작 10분 전부터는 시작 (초기 토큰 발급, 시세 준비)
        if now.time() < time(8, 50):
            return

        # 🔒 전날 남은 활성 전략 정리: 이미 슬롯이 생성되어 실행된 단타만 정리
        # (initialized=1 이면 어제 실행됐던 것 → 정리)
        # (initialized=0 이면 어제 저녁/밤에 예약해둔 것 → 살려둬야 함!)
        today_str = now.strftime("%Y-%m-%d")
        for s in db.list_strategies(active_only=True):
            created = (s.get("created_at") or "")[:10]
            # 단타 + 오늘 생성이 아님 + 이미 초기화됨(실행됐음) → 완료 처리
            if (s["strategy_type"] == "day"
                    and created and created < today_str
                    and s.get("initialized")):
                db.set_strategy_state(s["stock_code"], "completed")
                db.log_activity("strategy_stale", s["stock_code"],
                                f"단타 전략이 당일이 아님 (생성일 {created}) → 자동 종료",
                                level="WARN")

        # 1) 주문 상태 동기화 (미체결 조회)
        try:
            await self._sync_order_status(cli)
        except KiwoomAPIError as e:
            log.warning("미체결 조회 실패: %s", e)

        # 2) 활성 전략 순회
        for s in db.list_strategies(active_only=True):
            stk = s["stock_code"]
            try:
                t = s["strategy_type"]
                if t == "day":
                    await self._handle_day(cli, s, now)
                elif t == "target":
                    await self._handle_target(cli, s, now)
                elif t == "swing1":
                    await self._handle_swing1(cli, s, now)
                elif t == "swing2":
                    await self._handle_swing2(cli, s, now)
                elif t == "swing3":
                    await self._handle_swing3(cli, s, now)
                self._maybe_complete(stk)
            except KiwoomAPIError as e:
                log.warning("[%s] API 오류: %s", stk, e)
                db.log_activity("api_error", stk, str(e), level="WARN")
            except Exception as e:
                log.exception("[%s] 전략 처리 예외", stk)
                db.log_activity("handler_exception", stk, str(e), level="ERROR")

    # ---------- 주문 상태 동기화 ----------

    async def _sync_order_status(self, cli: KiwoomClient):
        """
        'ordered' 상태 슬롯들의 체결 상태를 정확히 갱신.
        - ka10075 미체결 조회: 아직 남은 수량 확인
        - ka10076 체결 조회: 실제 체결된 수량 확인
        전체 체결(oso_qty=0) → filled
        부분 체결(oso_qty>0 & cntr_qty>0) → ordered 유지 + filled_qty 업데이트
        미체결(cntr_qty=0) → 그대로 ordered
        목록에서 완전히 없어짐 → 체결이력으로 검증 후 filled or cancelled
        """
        all_ordered = db.get_all_active_ordered_slots()
        if not all_ordered:
            return

        # 오늘 주문된 것만 sync (어제 주문은 이미 처리된 것으로 간주)
        today_str = datetime.now(KST).strftime("%Y-%m-%d")
        ordered = [s for s in all_ordered
                   if (s.get("ordered_at") or "").startswith(today_str)]
        if not ordered:
            return

        # 미체결/체결 목록 동시에 조회 (단일 호출로 요청량 최소화)
        try:
            pending = await cli.get_pending_orders()
        except KiwoomAPIError as e:
            log.warning("미체결 조회 실패 (무시): %s", e)
            return
        pending_map = {p["ord_no"]: p for p in pending if p.get("ord_no")}

        # 체결 이력 (있으면 매칭, 없어도 무시)
        filled_map = {}
        try:
            filled_hist = await cli.get_filled_orders()
            filled_map = {f["ord_no"]: f for f in filled_hist if f.get("ord_no")}
        except Exception:
            pass  # ka10076 실패해도 ka10075만으로 동작

        for slot in ordered:
            ord_no = slot.get("order_no") or ""
            if not ord_no:
                continue
            p = pending_map.get(ord_no)
            f = filled_map.get(ord_no)
            slot_qty = slot.get("qty") or 0
            prev_filled = slot.get("filled_qty") or 0

            if p:
                # 아직 미체결 리스트에 있음
                oso = p.get("oso_qty") or 0
                cntr = p.get("cntr_qty") or 0
                if cntr > prev_filled:
                    # 부분 체결 발생
                    db.update_slot(slot["id"], filled_qty=cntr)
                    db.log_activity(
                        "order_partial_fill", slot["stock_code"],
                        f"슬롯 {slot['slot_index']} 부분체결 {cntr}/{slot_qty}주"
                    )
                if oso == 0:
                    # 전량 체결 완료
                    db.update_slot(
                        slot["id"],
                        status="filled",
                        filled_qty=slot_qty,
                        filled_at=datetime.now(KST).isoformat(timespec="seconds"),
                    )
                    db.log_activity("order_filled", slot["stock_code"],
                                    f"슬롯 {slot['slot_index']} 체결 완료 {slot_qty}주 (주문 {ord_no})")
            else:
                # 미체결 목록에서 사라짐 → 체결 또는 취소
                if f and (f.get("cntr_qty") or 0) > 0:
                    # 체결이력에서 확인됨 → 체결
                    cntr = f.get("cntr_qty") or slot_qty
                    price = f.get("cntr_pric") or slot.get("order_price") or 0
                    db.update_slot(
                        slot["id"],
                        status="filled",
                        filled_qty=cntr,
                        filled_price=price,
                        filled_at=datetime.now(KST).isoformat(timespec="seconds"),
                    )
                    db.log_activity("order_filled", slot["stock_code"],
                                    f"슬롯 {slot['slot_index']} 체결 완료 {cntr}주 @ {price}원 (주문 {ord_no})")
                elif f is None and not filled_map:
                    # 체결이력 API가 실패/비어있음 → 보수적으로 filled 간주 (기존 동작 유지)
                    # 주의: 사용자가 HTS에서 취소했으면 여기서 오탐 가능
                    db.update_slot(
                        slot["id"],
                        status="filled",
                        filled_qty=slot_qty,
                        filled_at=datetime.now(KST).isoformat(timespec="seconds"),
                    )
                    db.log_activity(
                        "order_filled", slot["stock_code"],
                        f"슬롯 {slot['slot_index']} 체결 추정 {slot_qty}주 (주문 {ord_no}, 체결이력 미확인)",
                        level="INFO",
                    )
                else:
                    # 체결이력에 없음 → 사용자가 수동 취소했을 가능성
                    db.update_slot(
                        slot["id"],
                        status="cancelled",
                        notes=(slot.get("notes") or "") + " | 외부 취소 감지",
                    )
                    db.log_activity(
                        "order_external_cancel", slot["stock_code"],
                        f"슬롯 {slot['slot_index']} 외부에서 취소됨 (주문 {ord_no})",
                        level="WARN",
                    )

    # ---------- 단타 (day) ----------

    async def _handle_day(self, cli: KiwoomClient, strat: dict, now: datetime):
        stk = strat["stock_code"]

        # 장 시작 전이면 대기
        if now.time() < MARKET_OPEN:
            return

        # 초기화(슬롯 생성) 필요한지
        if not strat["initialized"]:
            ok = await self._init_day_slots(cli, strat, now)
            if ok:
                db.mark_strategy_initialized(stk)
                # 방금 초기화했으면 이번 tick에 슬롯 발사도 하도록 state 새로 읽기
                strat = db.get_strategy(stk) or strat
            else:
                return  # 초기화 실패 시 다음 tick에 재시도

        # Case B 오후 fallback 생성 필요한지 (12:00 이후, 아직 phase2_init 안됐고, 미체결 잔여 있음)
        params = strat["params"]
        if params.get("case") == "B" and not params.get("phase2_initialized") and now.time() >= DAY_B_FALLBACK_TIMES[0]:
            await self._init_day_b_fallback(cli, strat, now)

        # 모든 슬롯 처리
        for slot in db.get_slots(stk):
            await self._process_day_slot(cli, strat, slot, now)

    async def _init_day_slots(self, cli: KiwoomClient, strat: dict, now: datetime) -> bool:
        stk = strat["stock_code"]
        total = strat["total_qty"]
        if total <= 0:
            return True
        qtys = split_4(total)
        params = dict(strat["params"])

        # 전일종가(base_pric)와 시가 필요
        await self._throttle()
        info = await cli.get_basic_info(stk)
        base_pric = info["base_pric"] or 0
        open_pric = info["open_pric"] or 0

        # 전일종가 0원 대응: 일봉 API로 직접 조회
        if base_pric == 0:
            try:
                await self._throttle()
                candles = await cli.get_daily_candles(stk)
                # 오늘 제외, 가장 최근 거래일 종가
                today_str = now.strftime("%Y%m%d")
                for c in candles:
                    if c.get("dt") != today_str and c.get("close"):
                        base_pric = c["close"]
                        db.log_activity("init_fallback", stk,
                                        f"전일종가 0 → 일봉에서 {base_pric}원 조회", level="INFO")
                        break
            except Exception as e:
                log.warning("일봉 폴백 실패 %s: %s", stk, e)

        # 그래도 0이면 10회까지만 연기하고 이후엔 포기
        retry_count = int(params.get("init_retry", 0))
        if base_pric == 0:
            if retry_count >= 10:
                # 포기: 현재가 기준으로라도 진행
                cur = info.get("cur_prc") or 0
                if cur > 0:
                    base_pric = cur
                    db.log_activity("init_fallback", stk,
                                    f"전일종가 조회 10회 실패 → 현재가 {cur}원 기준으로 진행",
                                    level="WARN")
                else:
                    # 이 종목은 포기
                    db.log_activity("init_give_up", stk,
                                    "전일종가/현재가 모두 0 - 전략 실행 불가",
                                    level="ERROR")
                    db.set_strategy_state(stk, "cancelled")
                    return True  # 더 이상 시도 안함
            else:
                params["init_retry"] = retry_count + 1
                import json as _json
                with db._lock, db._conn() as c:
                    c.execute("UPDATE strategies SET params=? WHERE stock_code=?",
                              (_json.dumps(params, ensure_ascii=False), stk))
                db.log_activity("init_skip", stk,
                                f"전일종가 0 - 재시도 예정 ({retry_count+1}/10)",
                                level="WARN")
                return False  # 재시도

        # 시가(open_pric)가 0이면 아직 장 시작 전이거나 시가 확정 전 → 재시도
        if open_pric == 0 and now.time() < time(9, 3):
            # 9:03 전에는 시가 아직 형성 중일 수 있음 → 대기
            db.log_activity("init_skip", stk,
                            "시가 형성 전 - 잠시 대기", level="INFO")
            return False  # 재시도 (initialized 안 찍음)

        # 시가 4% 이상 여부
        open_gain_pct = ((open_pric - base_pric) / base_pric * 100.0) if base_pric and open_pric else 0.0
        case = "A" if open_gain_pct >= 4.0 else "B"
        params["case"] = case
        params["base_pric"] = base_pric
        params["open_pric"] = open_pric
        params["open_gain_pct"] = round(open_gain_pct, 2)

        today = now.date()

        if case == "A":
            # 4 슬롯. slot0 = 시초 시장가, slot1~3 = 9:05/9:10/9:15 현재가 지정가
            for i in range(4):
                sched_t = DAY_A_TIMES[i]
                sched_dt = datetime.combine(today, sched_t).replace(tzinfo=KST)
                # 지나갔으면 now로 - 늦은 시작 대응
                if sched_dt < now:
                    sched_dt = now
                db.add_slot(
                    stock_code=stk,
                    slot_index=i,
                    qty=qtys[i],
                    order_tp=("market" if i == 0 else "limit"),
                    scheduled_time=sched_dt.isoformat(),
                    notes=f"단타A slot{i}",
                )
            db.log_activity("day_init", stk, f"Case A (시가 {open_gain_pct:.2f}%) 4개 슬롯 생성")
        else:
            # Case B: 4개 지정가 주문을 즉시 (now에) 걸어둔다
            pct_thresholds = params.get("case_b_pcts", [4.5, 5.2, 5.5, 5.8])
            for i in range(4):
                target_price = base_pric * (1 + pct_thresholds[i] / 100.0)
                limit_price = round_to_tick(int(target_price), up=True)
                db.add_slot(
                    stock_code=stk,
                    slot_index=i,
                    qty=qtys[i],
                    order_tp="limit",
                    order_price=limit_price,
                    scheduled_time=now.isoformat(),
                    notes=f"단타B slot{i} 목표 +{pct_thresholds[i]}% = {limit_price}원",
                )
            params["case_b_pcts"] = pct_thresholds
            db.log_activity("day_init", stk, f"Case B (시가 {open_gain_pct:.2f}%) 4개 지정가 슬롯 생성")

        # 저장
        import json as _json
        with db._lock, db._conn() as c:
            c.execute(
                "UPDATE strategies SET params=? WHERE stock_code=?",
                (_json.dumps(params, ensure_ascii=False), stk),
            )
        return True

    async def _process_day_slot(self, cli: KiwoomClient, strat: dict, slot: dict, now: datetime):
        """단타의 각 슬롯 처리."""
        stk = strat["stock_code"]

        # pending → 발사 시각 도달했으면 주문
        if slot["status"] == "pending":
            sched = _parse_iso(slot["scheduled_time"])
            if sched and now >= sched:
                await self._fire_slot_day(cli, strat, slot, now)
            return

        # ordered → fallback_deadline 도달했으면 취소 후 시장가 재주문
        if slot["status"] == "ordered":
            dl = _parse_iso(slot.get("fallback_deadline"))
            if dl and now >= dl:
                await self._fallback_to_market(cli, stk, slot)
            return

    async def _fire_slot_day(self, cli: KiwoomClient, strat: dict, slot: dict, now: datetime):
        """단타 슬롯 발사(주문 전송)."""
        stk = strat["stock_code"]
        qty = slot["qty"]
        if qty <= 0:
            db.update_slot(slot["id"], status="cancelled",
                           notes=(slot.get("notes") or "") + " | qty=0")
            return

        # 🔒 중복 주문 방지: 같은 tick 재진입이나 경쟁 조건 대비
        # 발사 직전에 slot 상태를 재확인
        cur_slot = next((s for s in db.get_slots(stk) if s["id"] == slot["id"]), None)
        if not cur_slot or cur_slot["status"] != "pending":
            return  # 이미 발사됐거나 취소됨

        try:
            if slot["order_tp"] == "market":
                await self._throttle()
                ord_no = await cli.sell_market(stk, qty)
                try:
                    db.update_slot(
                        slot["id"],
                        status="ordered",
                        order_no=ord_no,
                        ordered_at=datetime.now(KST).isoformat(timespec="seconds"),
                    )
                    db.log_activity("order_market", stk,
                                    f"슬롯 {slot['slot_index']} 시장가 매도 {qty}주 → 주문번호 {ord_no}")
                except Exception as db_e:
                    # 🚨 주문 성공했는데 DB 저장 실패 → 중복 방지 위해 즉시 취소 시도
                    log.error("DB 저장 실패 → 주문 취소 시도: %s", db_e)
                    try:
                        await self._throttle()
                        await cli.cancel_order(stk, ord_no, 0)
                    except Exception:
                        pass
                    db.log_activity("db_error", stk,
                                    f"슬롯 {slot['slot_index']} DB 저장 실패 (주문 {ord_no} 취소 시도): {db_e}",
                                    level="ERROR")
                    raise
            else:
                # 지정가: 현재가 조회 (캐시+throttle)
                price = slot.get("order_price")
                if not price:
                    price = await self._get_cached_price(cli, stk)
                if not price or int(price) <= 0:
                    db.log_activity("order_skip", stk,
                                    f"슬롯 {slot['slot_index']} 가격 조회 실패 (0원)", level="WARN")
                    return
                # 반드시 정수 + 호가 단위 보정
                price = round_to_tick(int(price), up=False)
                if price <= 0:
                    return

                await self._throttle()
                ord_no = await cli.sell_limit(stk, qty, price)
                # Case A 지정가는 5분 뒤 시장가 fallback
                fb = None
                if strat["params"].get("case") == "A" and slot["order_tp"] == "limit":
                    fb = (now + timedelta(minutes=LIMIT_FALLBACK_MINUTES)).isoformat()
                try:
                    db.update_slot(
                        slot["id"],
                        status="ordered",
                        order_no=ord_no,
                        order_price=price,
                        ordered_at=datetime.now(KST).isoformat(timespec="seconds"),
                        fallback_deadline=fb,
                    )
                    db.log_activity("order_limit", stk,
                                    f"슬롯 {slot['slot_index']} 지정가 {price}원 × {qty}주 "
                                    f"→ 주문번호 {ord_no}" + (f" (fallback {fb})" if fb else ""))
                except Exception as db_e:
                    # 🚨 주문 성공 후 DB 저장 실패 → 중복 방지 위해 즉시 취소
                    log.error("DB 저장 실패 → 주문 취소 시도: %s", db_e)
                    try:
                        await self._throttle()
                        await cli.cancel_order(stk, ord_no, 0)
                    except Exception:
                        pass
                    db.log_activity("db_error", stk,
                                    f"슬롯 {slot['slot_index']} DB 저장 실패 (주문 {ord_no} 취소 시도): {db_e}",
                                    level="ERROR")
                    raise
        except KiwoomAPIError as e:
            if _is_retryable(e):
                db.log_activity("order_retry", stk,
                                f"슬롯 {slot['slot_index']} 재시도 예정: {e}", level="WARN")
            else:
                db.update_slot(slot["id"], status="error",
                               notes=f"{slot.get('notes') or ''} | {e}")
                db.log_activity("order_error", stk,
                                f"슬롯 {slot['slot_index']} 주문 실패: {e}", level="ERROR")

    async def _fallback_to_market(self, cli: KiwoomClient, stk: str, slot: dict):
        """지정가 미체결 → 취소 후 시장가 재주문."""
        try:
            if slot.get("order_no"):
                await self._throttle()
                await cli.cancel_order(stk, slot["order_no"], 0)
                db.log_activity("order_cancel", stk,
                                f"슬롯 {slot['slot_index']} 지정가 취소 (원주문 {slot['order_no']})")
        except KiwoomAPIError as e:
            db.log_activity("cancel_error", stk, f"슬롯 {slot['slot_index']} 취소 실패: {e}",
                            level="WARN")

        # 잠깐 대기 후 시장가
        await asyncio.sleep(0.3)
        try:
            await self._throttle()
            ord_no = await cli.sell_market(stk, slot["qty"])
            db.update_slot(
                slot["id"],
                status="ordered",
                order_tp="market",
                order_no=ord_no,
                order_price=None,
                ordered_at=datetime.now(KST).isoformat(timespec="seconds"),
                fallback_deadline=None,
                notes=(slot["notes"] or "") + " | fallback→market",
            )
            db.log_activity("order_market", stk,
                            f"슬롯 {slot['slot_index']} 시장가 fallback 주문 → {ord_no}")
        except KiwoomAPIError as e:
            db.update_slot(slot["id"], status="error",
                           notes=(slot["notes"] or "") + f" | fallback 실패 {e}")
            db.log_activity("order_error", stk,
                            f"슬롯 {slot['slot_index']} 시장가 fallback 실패: {e}",
                            level="ERROR")

    async def _init_day_b_fallback(self, cli: KiwoomClient, strat: dict, now: datetime):
        """단타 Case B: 12:00 이후 미체결 잔여분을 4등분해 12/13/14/14:30에 시장가."""
        import json as _json
        stk = strat["stock_code"]
        slots = db.get_slots(stk)

        # 미체결(ordered) 슬롯들 취소
        remaining = 0
        for s in slots:
            if s["status"] == "ordered" and s["order_no"]:
                try:
                    await self._throttle()
                    await cli.cancel_order(stk, s["order_no"], 0)
                    db.update_slot(s["id"], status="cancelled",
                                   notes=(s["notes"] or "") + " | 오후 fallback 취소")
                    remaining += s["qty"]
                except KiwoomAPIError as e:
                    db.log_activity("cancel_error", stk, f"{s['order_no']} 취소 실패: {e}",
                                    level="WARN")
            elif s["status"] == "pending":
                db.update_slot(s["id"], status="cancelled",
                               notes=(s["notes"] or "") + " | 오후 fallback 취소")
                remaining += s["qty"]

        if remaining <= 0:
            # 더 이상 남은 수량 없으면 종료
            params = dict(strat["params"])
            params["phase2_initialized"] = True
            with db._lock, db._conn() as c:
                c.execute("UPDATE strategies SET params=? WHERE stock_code=?",
                          (_json.dumps(params, ensure_ascii=False), stk))
            return

        # 잔여분 4등분 후 새 슬롯 4개 생성 (slot_index 4~7)
        qtys = split_4(remaining) if remaining >= 4 else [remaining, 0, 0, 0]
        today = now.date()
        for i, t_ in enumerate(DAY_B_FALLBACK_TIMES):
            sched_dt = datetime.combine(today, t_).replace(tzinfo=KST)
            if sched_dt < now:
                sched_dt = now
            q = qtys[i] if i < len(qtys) else 0
            if q <= 0:
                continue
            db.add_slot(
                stock_code=stk,
                slot_index=4 + i,
                qty=q,
                order_tp="market",
                scheduled_time=sched_dt.isoformat(),
                notes=f"단타B 오후 fallback slot{4+i} 시장가",
            )

        params = dict(strat["params"])
        params["phase2_initialized"] = True
        with db._lock, db._conn() as c:
            c.execute("UPDATE strategies SET params=? WHERE stock_code=?",
                      (_json.dumps(params, ensure_ascii=False), stk))
        db.log_activity("day_b_fallback_init", stk,
                        f"오후 fallback {remaining}주 4등분 시장가 슬롯 생성")

    # ---------- 목표가 (target) ----------

    async def _handle_target(self, cli: KiwoomClient, strat: dict, now: datetime):
        stk = strat["stock_code"]
        if now.time() < MARKET_OPEN:
            return

        # 초기화: 첫 슬롯 생성
        if not strat["initialized"]:
            ok = await self._init_target_first_slot(cli, strat, now)
            if not ok:
                return  # 재시도 대기
            db.mark_strategy_initialized(stk)

        slots = db.get_slots(stk)

        # 첫 슬롯(index 0)이 체결되면 후속 슬롯 1,2,3 생성
        first = next((s for s in slots if s["slot_index"] == 0), None)
        if first and first["status"] == "filled":
            params = strat["params"]
            if not params.get("followups_initialized"):
                await self._init_target_followups(strat, first, now)

        # 종가 fallback: TARGET_CLOSE_TIME 이후 미체결 전부 시장가로
        if now.time() >= TARGET_CLOSE_TIME:
            params = strat["params"]
            if not params.get("close_fallback_done"):
                await self._target_close_fallback(cli, strat, now)

        # 각 슬롯 처리
        for slot in db.get_slots(stk):
            await self._process_target_slot(cli, strat, slot, now)

    async def _init_target_first_slot(self, cli: KiwoomClient, strat: dict, now: datetime) -> bool:
        stk = strat["stock_code"]
        total = strat["total_qty"]
        qtys = split_4(total)
        target_price = int(strat["params"].get("target_price", 0))
        if target_price <= 0:
            db.log_activity("init_skip", stk, "목표가 미설정", level="WARN")
            return False  # 초기화 실패

        limit_price = round_to_tick(target_price, up=False)
        db.add_slot(
            stock_code=stk,
            slot_index=0,
            qty=qtys[0],
            order_tp="limit",
            order_price=limit_price,
            scheduled_time=now.isoformat(),
            notes=f"목표가 근접 지정가 {limit_price}원",
        )
        # 나머지 슬롯의 수량만 메모(실제 생성은 첫 체결 후)
        import json as _json
        params = dict(strat["params"])
        params["followup_qtys"] = qtys[1:]
        with db._lock, db._conn() as c:
            c.execute("UPDATE strategies SET params=? WHERE stock_code=?",
                      (_json.dumps(params, ensure_ascii=False), stk))
        db.log_activity("target_init", stk, f"1차 지정가 {limit_price}원 × {qtys[0]}주")
        return True

    async def _init_target_followups(self, strat: dict, first_slot: dict, now: datetime):
        """첫 슬롯 체결 후 5분 간격으로 slot 1,2,3 예약."""
        import json as _json
        stk = strat["stock_code"]
        params = dict(strat["params"])
        followup_qtys = params.get("followup_qtys") or [0, 0, 0]

        fill_time = _parse_iso(first_slot.get("filled_at")) or now
        for i in range(3):
            sched = fill_time + timedelta(minutes=TARGET_FOLLOWUP_INTERVAL_MIN * (i + 1))
            db.add_slot(
                stock_code=stk,
                slot_index=1 + i,
                qty=followup_qtys[i] if i < len(followup_qtys) else 0,
                order_tp="limit",
                scheduled_time=sched.isoformat(),
                notes=f"목표가 후속 slot{1+i} (체결 +{TARGET_FOLLOWUP_INTERVAL_MIN*(i+1)}분, 현재가 지정가)",
            )
        params["followups_initialized"] = True
        with db._lock, db._conn() as c:
            c.execute("UPDATE strategies SET params=? WHERE stock_code=?",
                      (_json.dumps(params, ensure_ascii=False), stk))
        db.log_activity("target_followup_init", stk, "첫 슬롯 체결 → 후속 3개 슬롯 예약")

    async def _process_target_slot(self, cli: KiwoomClient, strat: dict, slot: dict, now: datetime):
        if slot["status"] != "pending":
            return
        sched = _parse_iso(slot["scheduled_time"])
        if not sched or now < sched:
            return

        stk = strat["stock_code"]
        qty = slot["qty"]
        if qty <= 0:
            db.update_slot(slot["id"], status="cancelled", notes=(slot["notes"] or "") + " | qty=0")
            return

        # 🔒 중복 주문 방지: 같은 tick 재진입 대비
        cur_slot = next((s for s in db.get_slots(stk) if s["id"] == slot["id"]), None)
        if not cur_slot or cur_slot["status"] != "pending":
            return  # 이미 발사됐거나 취소됨

        try:
            price = slot.get("order_price")
            if not price:
                # 현재가 조회 (캐시 + throttle 자동 적용)
                price = await self._get_cached_price(cli, stk)
            if not price or int(price) <= 0:
                db.log_activity("order_skip", stk, f"슬롯 {slot['slot_index']} 가격조회 실패 (0원)",
                                level="WARN")
                return
            # 반드시 정수 + 호가 단위 보정
            price = round_to_tick(int(price), up=False)
            if price <= 0:
                db.log_activity("order_skip", stk, f"슬롯 {slot['slot_index']} 가격 0원 이하",
                                level="WARN")
                return
            await self._throttle()  # 주문 직전 속도 제어
            ord_no = await cli.sell_limit(stk, qty, price)
            try:
                db.update_slot(
                    slot["id"],
                    status="ordered",
                    order_no=ord_no,
                    order_price=price,
                    ordered_at=datetime.now(KST).isoformat(timespec="seconds"),
                )
                db.log_activity("order_limit", stk,
                                f"목표가 슬롯 {slot['slot_index']} 지정가 {price}원 × {qty}주 → {ord_no}")
            except Exception as db_e:
                # 🚨 주문 성공 후 DB 저장 실패 → 중복 방지 위해 즉시 취소
                log.error("DB 저장 실패 → 주문 취소 시도: %s", db_e)
                try:
                    await self._throttle()
                    await cli.cancel_order(stk, ord_no, 0)
                except Exception:
                    pass
                db.log_activity("db_error", stk,
                                f"목표가 슬롯 {slot['slot_index']} DB 저장 실패 (주문 {ord_no} 취소 시도): {db_e}",
                                level="ERROR")
                raise
        except KiwoomAPIError as e:
            if _is_retryable(e):
                # 속도 제한 등 일시적 에러: 상태 유지, 다음 tick에 재시도
                db.log_activity("order_retry", stk,
                                f"슬롯 {slot['slot_index']} 재시도 예정: {e}", level="WARN")
            else:
                db.update_slot(slot["id"], status="error", notes=(slot["notes"] or "") + f" | {e}")
                db.log_activity("order_error", stk, f"슬롯 {slot['slot_index']} 주문 실패: {e}",
                                level="ERROR")

    async def _target_close_fallback(self, cli: KiwoomClient, strat: dict, now: datetime):
        """15:20 이후 미체결 전부 취소하고 시장가 한방."""
        import json as _json
        stk = strat["stock_code"]
        remaining = 0
        for s in db.get_slots(stk):
            if s["status"] == "ordered" and s["order_no"]:
                try:
                    await self._throttle()
                    await cli.cancel_order(stk, s["order_no"], 0)
                    db.update_slot(s["id"], status="cancelled",
                                   notes=(s["notes"] or "") + " | 종가 fallback")
                    remaining += s["qty"]
                except KiwoomAPIError as e:
                    db.log_activity("cancel_error", stk, str(e), level="WARN")
            elif s["status"] == "pending":
                db.update_slot(s["id"], status="cancelled",
                               notes=(s["notes"] or "") + " | 종가 fallback")
                remaining += s["qty"]

        if remaining > 0:
            try:
                await self._throttle()
                ord_no = await cli.sell_market(stk, remaining)
                db.add_slot(
                    stock_code=stk,
                    slot_index=99,
                    qty=remaining,
                    order_tp="market",
                    scheduled_time=now.isoformat(),
                    notes=f"종가 fallback 시장가 {remaining}주 → {ord_no}",
                )
                # 그 슬롯을 바로 ordered로
                new_slots = db.get_slots(stk)
                ns = next((x for x in new_slots if x["slot_index"] == 99), None)
                if ns:
                    db.update_slot(ns["id"], status="ordered", order_no=ord_no,
                                   ordered_at=datetime.now(KST).isoformat(timespec="seconds"))
                db.log_activity("target_close_fallback", stk,
                                f"종가 fallback 시장가 {remaining}주 → {ord_no}")
            except KiwoomAPIError as e:
                db.log_activity("order_error", stk, f"종가 fallback 실패: {e}", level="ERROR")

        params = dict(strat["params"])
        params["close_fallback_done"] = True
        with db._lock, db._conn() as c:
            c.execute("UPDATE strategies SET params=? WHERE stock_code=?",
                      (_json.dumps(params, ensure_ascii=False), stk))

    # ---------- 스윙 v1 ----------

    async def _handle_swing1(self, cli: KiwoomClient, strat: dict, now: datetime):
        """절반: +10% 도달시 매도1호가. 체결되면 나머지 절반 15:15에 현재가 지정가.
           15:15까지 미체결은 전량 취소."""
        import json as _json
        stk = strat["stock_code"]
        if now.time() < MARKET_OPEN:
            return

        # 초기화
        if not strat["initialized"]:
            total = strat["total_qty"]
            half = total // 2
            rest = total - half
            # slot 0: 첫 절반, 트리거 = 가격 +10% 도달
            db.add_slot(
                stock_code=stk,
                slot_index=0,
                qty=half,
                order_tp="limit",
                scheduled_time=now.isoformat(),  # 매 tick 트리거 확인
                trigger={"type": "gain_above", "pct": float(strat["params"].get("gain_pct", 10.0))},
                notes="스윙1 절반 - +10% 도달시 매도1호가",
            )
            # slot 1: 나머지 절반. 트리거 = slot0 체결 & 15:15
            db.add_slot(
                stock_code=stk,
                slot_index=1,
                qty=rest,
                order_tp="limit",
                scheduled_time=datetime.combine(now.date(), SWING_CLOSE_TIME).replace(tzinfo=KST).isoformat(),
                trigger={"type": "after_prev_filled_and_time", "time": "15:15"},
                notes="스윙1 나머지 절반 - slot0 체결 후 15:15 현재가 지정가",
            )
            db.mark_strategy_initialized(stk)
            db.log_activity("swing1_init", stk, f"절반 {half}주(+10%호가) + 절반 {rest}주(15:15)")

        # slot 0 트리거 평가
        slots = db.get_slots(stk)
        s0 = next((x for x in slots if x["slot_index"] == 0), None)
        s1 = next((x for x in slots if x["slot_index"] == 1), None)

        # 15:15 이후 미체결은 전체 취소 (사용자 요구: 15:15까지 안되면 전부 취소)
        if now.time() >= SWING_CLOSE_TIME and s0 and s0["status"] == "pending":
            db.update_slot(s0["id"], status="cancelled",
                           notes=(s0["notes"] or "") + " | 15:15 미트리거, 취소")
            if s1 and s1["status"] == "pending":
                db.update_slot(s1["id"], status="cancelled",
                               notes=(s1["notes"] or "") + " | 15:15 전체 취소")
            db.log_activity("swing1_cancel_all", stk, "15:15까지 트리거 실패 - 전체 취소")
            return

        # 기존 ordered가 15:15까지 체결 안되면 취소 (slot0)
        if now.time() >= SWING_CLOSE_TIME and s0 and s0["status"] == "ordered" and s0.get("order_no"):
            try:
                await self._throttle()
                await cli.cancel_order(stk, s0["order_no"], 0)
                db.update_slot(s0["id"], status="cancelled",
                               notes=(s0["notes"] or "") + " | 15:15 미체결 취소")
                if s1 and s1["status"] == "pending":
                    db.update_slot(s1["id"], status="cancelled",
                                   notes=(s1["notes"] or "") + " | slot0 미체결로 취소")
                db.log_activity("swing1_cancel_all", stk, "slot0 미체결 - 전체 취소")
            except KiwoomAPIError as e:
                db.log_activity("cancel_error", stk, str(e), level="WARN")
            return

        # slot 0 트리거 평가 (pending일 때만)
        if s0 and s0["status"] == "pending":
            gain_pct = float(s0["trigger"].get("pct", 10.0))
            base = int(strat["params"].get("base_pric", 0))
            if not base:
                # 전일종가 없으면 조회해서 저장
                try:
                    await self._throttle()
                    info = await cli.get_basic_info(stk)
                    base = info.get("base_pric") or 0
                    if base:
                        params = dict(strat["params"])
                        params["base_pric"] = base
                        with db._lock, db._conn() as c:
                            c.execute("UPDATE strategies SET params=? WHERE stock_code=?",
                                      (_json.dumps(params, ensure_ascii=False), stk))
                except Exception as e:
                    log.warning("스윙1 base 조회 실패: %s", e)
                    return
            if base:
                cur = await self._get_cached_price(cli, stk)
                if cur and cur >= base * (1 + gain_pct / 100.0):
                    # 매도1호가 조회 후 발사
                    try:
                        await self._throttle()
                        ob = await cli.get_orderbook(stk)
                        ask1 = ob["asks"][0] if ob["asks"] else cur
                        price = round_to_tick(int(ask1), up=False)
                        if price <= 0:
                            return
                        await self._throttle()
                        ord_no = await cli.sell_limit(stk, s0["qty"], price)
                        db.update_slot(
                            s0["id"],
                            status="ordered",
                            order_no=ord_no,
                            order_price=price,
                            ordered_at=datetime.now(KST).isoformat(timespec="seconds"),
                        )
                        db.log_activity("order_limit", stk,
                                        f"스윙1 slot0 +{gain_pct}% 도달, 매도1호가 {price}원 × {s0['qty']}주 → {ord_no}")
                    except KiwoomAPIError as e:
                        if _is_retryable(e):
                            db.log_activity("order_retry", stk, f"스윙1 재시도 예정: {e}", level="WARN")
                        else:
                            db.log_activity("order_error", stk, f"스윙1 slot0 주문 실패: {e}",
                                        level="ERROR")

        # slot 1: slot0 체결 확인 + 15:15 도달
        if s1 and s1["status"] == "pending":
            s0_now = next((x for x in db.get_slots(stk) if x["slot_index"] == 0), None)
            if s0_now and s0_now["status"] == "filled" and now.time() >= SWING_CLOSE_TIME:
                try:
                    cur = await self._get_cached_price(cli, stk)
                    if not cur:
                        return
                    price = round_to_tick(int(cur), up=False)
                    if not price:
                        return
                    await self._throttle()
                    ord_no = await cli.sell_limit(stk, s1["qty"], price)
                    db.update_slot(
                        s1["id"],
                        status="ordered",
                        order_no=ord_no,
                        order_price=price,
                        ordered_at=datetime.now(KST).isoformat(timespec="seconds"),
                    )
                    db.log_activity("order_limit", stk,
                                    f"스윙1 slot1 15:15 현재가 {price}원 × {s1['qty']}주 → {ord_no}")
                except KiwoomAPIError as e:
                    db.log_activity("order_error", stk, f"스윙1 slot1 주문 실패: {e}", level="ERROR")

    # ---------- 스윙 v2 ----------

    async def _handle_swing2(self, cli: KiwoomClient, strat: dict, now: datetime):
        """15:10 체크: 현재가 > 기준가면 15:15에 절반 현재가 지정가."""
        import json as _json
        stk = strat["stock_code"]
        if now.time() < MARKET_OPEN:
            return

        if not strat["initialized"] and now.time() >= SWING_V2_CHECK_TIME:
            trigger_price = int(strat["params"].get("trigger_price", 0))
            if trigger_price <= 0:
                db.log_activity("init_skip", stk, "스윙2 기준가 미설정", level="WARN")
                db.mark_strategy_initialized(stk)
                return
            info = None
            cur = await self._get_cached_price(cli, stk)
            # 현재가 조회 실패 시 재시도
            if not cur:
                db.log_activity("swing2_retry", stk, "현재가 조회 실패 - 재시도 대기", level="WARN")
                return
            params = dict(strat["params"])
            params["check_cur_prc"] = cur
            if cur > trigger_price:
                half = strat["total_qty"] // 2
                sched = datetime.combine(now.date(), SWING_CLOSE_TIME).replace(tzinfo=KST)
                if sched < now:
                    sched = now
                db.add_slot(
                    stock_code=stk,
                    slot_index=0,
                    qty=half,
                    order_tp="limit",
                    scheduled_time=sched.isoformat(),
                    notes=f"스윙2 15:15 현재가 지정가 (체크시 {cur} > 기준 {trigger_price})",
                )
                db.log_activity("swing2_trigger", stk,
                                f"15:10 체크: 현재가 {cur} > 기준 {trigger_price} → 15:15 절반 매도 예약")
            else:
                db.log_activity("swing2_skip", stk,
                                f"15:10 체크: 현재가 {cur} ≤ 기준 {trigger_price} → 스킵")
            with db._lock, db._conn() as c:
                c.execute("UPDATE strategies SET params=? WHERE stock_code=?",
                          (_json.dumps(params, ensure_ascii=False), stk))
            db.mark_strategy_initialized(stk)

        # 슬롯 발사 처리
        for slot in db.get_slots(stk):
            if slot["status"] == "pending":
                sched = _parse_iso(slot["scheduled_time"])
                if sched and now >= sched:
                    try:
                        cur = await self._get_cached_price(cli, stk)
                        if not cur:
                            continue
                        price = round_to_tick(int(cur), up=False)
                        if not price:
                            continue
                        await self._throttle()
                        ord_no = await cli.sell_limit(stk, slot["qty"], price)
                        db.update_slot(
                            slot["id"],
                            status="ordered",
                            order_no=ord_no,
                            order_price=price,
                            ordered_at=datetime.now(KST).isoformat(timespec="seconds"),
                        )
                        db.log_activity("order_limit", stk,
                                        f"스윙2 slot0 {price}원 × {slot['qty']}주 → {ord_no}")
                    except KiwoomAPIError as e:
                        db.log_activity("order_error", stk, str(e), level="ERROR")

    # ---------- 스윙 v3 ----------

    async def _handle_swing3(self, cli: KiwoomClient, strat: dict, now: datetime):
        """15:05 체크: 5일선 대비 -6% 이하면 15:15에 전량 현재가 지정가."""
        import json as _json
        stk = strat["stock_code"]
        if now.time() < MARKET_OPEN:
            return

        if not strat["initialized"] and now.time() >= SWING_V3_CHECK_TIME:
            drop_pct = float(strat["params"].get("drop_pct", 6.0))
            await self._throttle()
            ma5 = await cli.get_5day_ma(stk)
            cur = await self._get_cached_price(cli, stk)

            # ma5나 cur이 없으면 초기화 연기 (다음 tick에 재시도)
            if not ma5 or not cur:
                db.log_activity("swing3_retry", stk,
                                f"5일선 또는 현재가 조회 실패 (5일선 {ma5}, 현재가 {cur}) - 재시도 대기",
                                level="WARN")
                return  # mark_strategy_initialized 안 함 → 다음 tick에 재시도

            params = dict(strat["params"])
            params["check_ma5"] = ma5
            params["check_cur_prc"] = cur

            if cur <= ma5 * (1 - drop_pct / 100.0):
                # 전량 15:15 현재가 지정가
                sched = datetime.combine(now.date(), SWING_CLOSE_TIME).replace(tzinfo=KST)
                if sched < now:
                    sched = now
                db.add_slot(
                    stock_code=stk,
                    slot_index=0,
                    qty=strat["total_qty"],
                    order_tp="limit",
                    scheduled_time=sched.isoformat(),
                    notes=f"스윙3 전량 15:15 현재가 지정가 (5일선 {ma5:.0f}, 현재 {cur}, -{drop_pct}% 이탈)",
                )
                db.log_activity("swing3_trigger", stk,
                                f"5일선 {ma5:.0f} 대비 현재가 {cur} 이탈 → 15:15 전량 매도 예약")
            else:
                db.log_activity("swing3_skip", stk,
                                f"5일선 이탈 조건 미달 (5일선 {ma5:.0f}, 현재 {cur})")
            with db._lock, db._conn() as c:
                c.execute("UPDATE strategies SET params=? WHERE stock_code=?",
                          (_json.dumps(params, ensure_ascii=False), stk))
            db.mark_strategy_initialized(stk)

        # 슬롯 발사 처리 (swing2와 동일)
        for slot in db.get_slots(stk):
            if slot["status"] == "pending":
                sched = _parse_iso(slot["scheduled_time"])
                if sched and now >= sched:
                    try:
                        cur = await self._get_cached_price(cli, stk)
                        if not cur:
                            continue
                        price = round_to_tick(int(cur), up=False)
                        if not price:
                            continue
                        await self._throttle()
                        ord_no = await cli.sell_limit(stk, slot["qty"], price)
                        db.update_slot(
                            slot["id"],
                            status="ordered",
                            order_no=ord_no,
                            order_price=price,
                            ordered_at=datetime.now(KST).isoformat(timespec="seconds"),
                        )
                        db.log_activity("order_limit", stk,
                                        f"스윙3 slot0 {price}원 × {slot['qty']}주 → {ord_no}")
                    except KiwoomAPIError as e:
                        db.log_activity("order_error", stk, str(e), level="ERROR")

    # ---------- 공통 ----------

    def _maybe_complete(self, stock_code: str):
        slots = db.get_slots(stock_code)
        if not slots:
            return
        if all(s["status"] in ("filled", "cancelled", "error") for s in slots):
            db.set_strategy_state(stock_code, "completed")
            db.log_activity("strategy_completed", stock_code,
                            f"슬롯 {len(slots)}개 모두 종료")

    async def cancel_all_for_stock(self, stock_code: str) -> int:
        """종목의 진행중 주문 전부 취소. 취소된 슬롯 수 반환."""
        cli = self.client_factory()
        if not cli:
            return 0
        cancelled = 0
        for s in db.get_slots(stock_code):
            if s["status"] == "ordered" and s.get("order_no"):
                try:
                    await self._throttle()
                    await cli.cancel_order(stock_code, s["order_no"], 0)
                    db.update_slot(s["id"], status="cancelled",
                                   notes=(s["notes"] or "") + " | 사용자 취소")
                    cancelled += 1
                except KiwoomAPIError as e:
                    db.log_activity("cancel_error", stock_code, str(e), level="WARN")
            elif s["status"] == "pending":
                db.update_slot(s["id"], status="cancelled",
                               notes=(s["notes"] or "") + " | 사용자 취소")
                cancelled += 1
        db.log_activity("cancel_all", stock_code, f"종목 주문 전체 취소 ({cancelled}건)")
        return cancelled


# ---------- 유틸 ----------

def _is_retryable(e: KiwoomAPIError) -> bool:
    """속도 제한, 일시적 네트워크 에러 등은 재시도 대상.
    키움 에러 코드 참고:
      1700 - 허용된 요청 개수 초과 (재시도)
      1687 - 재귀 호출 제한 (재시도)
      1999 - 예기치 못한 에러 (재시도 1회 정도)
    """
    msg = str(e).lower()
    # return_code 직접 체크
    rc = getattr(e, "return_code", None)
    if rc in (1700, 1687, 1999):
        return True
    retry_keywords = [
        "허용된 요청", "요청 개수", "rate", "limit", "1700", "1687",  # 속도 제한
        "timeout", "시간 초과",                                       # 타임아웃
        "500", "502", "503", "504",                                    # 서버 일시 오류
        "재귀 호출",                                                   # 재귀 호출 제한
    ]
    return any(k in msg for k in retry_keywords)


def _is_auth_error(e: KiwoomAPIError) -> bool:
    """앱키/시크릿/토큰 관련 에러. 사용자 조치 필요."""
    msg = str(e).lower()
    rc = getattr(e, "return_code", None)
    if rc and 8000 <= rc < 8100:
        return True
    auth_kw = ["appkey", "secretkey", "token", "grant_type", "8030", "8031",
               "실전/모의", "투자구분"]
    return any(k in msg for k in auth_kw)


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt
    except Exception:
        return None
