# CryptoDesk — Backlog

Ý tưởng đã nghiên cứu, chưa triển khai. Xếp theo impact ước tính.

## Đã làm (xong, ghi để tham khảo)

- ✅ **SHORT booster funding** — `dashboard/{fam,swing_h1,range}_engine.py`. Funding > +0.05% +1 score; > +0.10% +2 score và auto-upgrade confidence MEDIUM → HIGH. Backed by case CRCL +3.57R (funding +0.16%) và backtest setup `fund_pos_oi_up` WR=90%.
- ✅ **Watchlist auto-add funding spike** — `_auto_add_funding_spike_watchlist()` trong `main.py`. Quét top-volume futures mỗi 30 phút, add coin có |funding| > 0.05% với algo RANGE_SCALP. API manual: `POST /api/watchlist/funding-spike-scan`.
- ✅ **Filter LONG**: oi_change > 8% / funding < -0.01% / rr > 2.5 → block (chỉ LONG, SHORT-RR cao vẫn cho qua).
- ✅ **Cooldown 12h**: (symbol, direction) đã LOSS ≥2 lần → block.
- ✅ **Force-close timeout backtest**: 8h/12h/24h/72h theo strategy.
- ✅ **Confidence buckets** trong export backtest JSON + `confidence_numeric`.
- ✅ **Exhaustion candle detector (SHORT only)** — `core/indicators.py:detect_exhaustion_short`. Tích hợp vào `range_engine` + `swing_h1_engine`. Verify backtest 2026-04-30: precision 71%, recall 20% (5/25 SHORT-WIN: CRCL/OPG/BASED/ROBO/MON). Threshold: body ≥ 1.5×ATR(14), vol ≥ 1.5×avg(20), close ≥ 50% từ đáy nến. Guard: chỉ trigger khi engine đang ra SHORT (tránh spurious LONG-LOSS).
- ✅ **Save history cho watchlist + position reversal** — `_check_watchlist_alert` + `_check_position_reversal` giờ save history với `source` tag. Bypass dedup vì cooldown đã handled ở caller. Schema thêm `source`, `algo`, `alert_type` (commit aa2e881).
- ✅ **BTC counter-trend block decouple-aware** — `dashboard/swing_h1_engine.py` PATCH A. Trước: hard block SHORT/LONG khi BTC ngược chiều → bỏ lỡ alt decoupled. Sau: nếu alt structure mạnh (score≥4) hoặc spread alt-BTC 24h ≥ 2pp ngược chiều → giữ direction, hạ confidence 1 bậc. Bằng chứng case ARB 28/4-3/5: BTC +0.98%, ARB -6.16%, spread -7.14pp; 25/123 H1 candles "BTC up + ARB down". Verify ARB live: trigger "Alt decoupled bearish: -2.7% vs BTC -0.1% (-2.6pp) — giữ SHORT, conf hạ MEDIUM" ✅ (commit 92c49ff).
- ✅ **Funding filter universal (3/5)** — `swing_h1_engine`, `range_engine`, `fam_engine` đều có PATCH K block LONG khi funding < -0.01% (data backtest 168h: WR=26%, sumR=-6.94R trên 57 signals). Trước đó filter chỉ áp dụng ở `_should_block_signal` (market_scan layer), không ở dashboard analyze API → signal APT hôm 3/5 (funding -0.0211%) lọt qua filter và ra HIGH confidence sai.
- ❌ **Verify 4 fix khác — 3/4 ngược intuition (3/5)**: Sau case APT, em đề xuất 4 fix thêm. Verify trên 120-340 closed signals:
  - **#2 Hammer + volume**: Trực giác "hammer cần vol cao". Data: hammer LONG vol<0.7× WR=67%, vol≥1.5× WR=50% — NGƯỢC. Skip.
  - **#3 Range vs trending**: Trực giác "RANGE_SCALP cần sideways thực sự". Data: flush 2-3% WR=62%, sideways<1% WR=36% — NGƯỢC. Skip.
  - **#5 M15 desc → block LONG**: Trực giác "LONG counter momentum M15". Data: M15 desc WR=57%, asc 44% — NGƯỢC. Skip.
  - **#4 SL ATR floor**: Sample ATR field missing trong export. sl_pct buckets WR fairly equal (~38%) — chưa thấy pattern rõ. Skip.
  - Bài học: trader real pattern (mean reversion, exhaustion) ngược "trend continuation logic". Luôn verify ≥50 samples trước build.

- ✅ **Entry optimal — backtest fill rate verify** (Stage 0+1+2). Trước: backtest dùng giá market thời điểm signal làm entry → khác hoàn toàn cách user dùng limit order tại entry_opt. Sau:
  - Stage 0: `_save_signal_to_history` lưu `entry_opt`, `entry_opt_label`, `entry_opt_rr`. Frontend backtest export thêm các fields này + `bt_used_entry`, `bt_fill_candles`.
  - Stage 1: `backtest_signal` swap entry → entry_opt nếu có (chênh ≥0.05% và đúng phía). SL/TP giữ key levels, recompute sl_pct/tp1_pct từ entry mới. Status mới `EXPIRED` khi không chạm trong 8h.
  - Stage 2: build entry_opt cho `swing_h1_engine` (Fib 0.5/0.618/0.382 H1 + MA34 H1 + MA89 H1) và `range_engine` (range bottom/top với 0.5% buffer). fam_engine đã có sẵn.
  - Verify ARB live: SWING_H1 cho RR thị trường = 0.9 nhưng entry_opt RR = 4.04 (Fib 0.618 H1) — chênh lệch khổng lồ chứng minh entry_opt có giá trị.
  - Sau 1-2 ngày live có data fill rate sẽ verify được engine recommend hợp lý không.
- ✅ **REVISE filter LONG (5/3) dựa data 168h backtest 500 signals** — `_should_block_signal`. Phát hiện 2 filter cũ sai sau 7 ngày live:
  - **OI threshold 8% → 10%**: bucket OI 8-10% có WR=89%, sumR=+18R (8W/1L) — vùng vàng bị filter cũ chặn nhầm. Vùng OI≥10% mới thực sự xấu (WR=22%, -12.5R).
  - **BỎ filter `LONG rr > 2.5`**: data 7 ngày cho thấy LONG RR≥3 có WR=42%, sumR=+57R (53 signals) — ngược hoàn toàn dự đoán cũ. RR cao trong LONG thực ra là TỐT.
  - Funding<-0.01% giữ (WR=26%, sumR=-6.9R trên 57 signals — confirm đúng).
  - Tổng hệ thống cải thiện: WR 30% → 37%, total R -0.34 → +94.63, expectancy 0 → +0.28.

---

## Chưa làm — ưu tiên cao

### REVERSAL engine — RSI 33 không phải oversold
**Bug**: `reversal_engine` ra "đang theo dõi LONG bounce" khi RSI H1 = 33, nhưng trong downtrend mạnh RSI có thể đi 25-30 hàng tuần.

**Fix**: 
- Threshold oversold: RSI < 28 thay vì RSI < 35
- Cần thêm điều kiện: cấu trúc H4 structure không phải DOWNTREND mới được ra LONG bias
- Nếu cấu trúc downtrend rõ → RSI low chỉ là continuation, không phải reversal

---

### A. PARTIAL CUT khi giá gần support (smart_action v3)
**Vấn đề**: Hiện tại smart_action chỉ có 2 tier rõ — "CẮT NGAY" hoặc "HOLD". Khi giá đang lỗ và cách swing-low/support recent < 1.5× ATR, cắt 100% bỏ lỡ cơ hội bounce.

**Case test**: ARB 2026-05-01 — system khuyên CẮT NGAY ở 0.1244, giá test 0.1232 (cách 1%) rồi bounce lên 0.1264. Nếu khuyến nghị "PARTIAL CUT 50%" thì:
- Cắt 50% mất -0.8% (thay vì -1.6% full)
- Hold 50% với SL chặt 0.1228 → bounce ăn được +0.8% trên phần giữ
- Net P&L = ~0% thay vì -1.6%

**Logic đề xuất**:
```
khoảng_cách_đến_support = (entry_price - swing_low_recent) / ATR_h1

if khoảng_cách < 1.5 × ATR và cấu_trúc_chưa_gãy_xa:
    action = "PARTIAL_CUT_50"
    SL_phần_còn_lại = swing_low - 0.3 × ATR
elif cấu_trúc_gãy_rõ và xa_support (≥ 2× ATR):
    action = "CẮT NGAY"
```

**Verify trước khi build**: Em chạy backtest 72h, đếm các LONG-LOSS đã từng "test support gần rồi vẫn break" vs "test support rồi bounce". Threshold 1.5× ATR có tối ưu không?

**Điểm gắn**: `main.py` `smart_action` block (search "smart_action" trong main.py).

---

### B. Failed breakdown detector (ngược của exhaustion)
**Mục tiêu**: Detect cú "test support thất bại" — tức là pump ngược lên — để **flip recommendation** từ CẮT thành HOLD khi đang LONG dở.

**Logic**: trên nến M15 hoặc H1:
- Lower wick ≥ 50% range nến (≥ retrace_min)
- Volume ≥ 1.2× avg 20 nến
- Close ≥ open (nến xanh) HOẶC close > giá mở của cây trước
- Xảy ra ở vùng cách swing_low recent < 0.5× ATR

→ Đây chính là pattern ARB ở 0.1232 vừa rồi (nếu detect kịp).

**Use-case**:
1. **Khi đang LONG lỗ**: nếu detect failed breakdown ở support gần → smart_action chuyển từ "CẮT NGAY" sang "HOLD + trail SL" hoặc "tighten SL chặt"
2. **Khi không có lệnh**: tạo LONG signal MEDIUM-HIGH cho coin có pattern này (mirror của exhaustion-short)

**Verify trước khi build**: 
- Trên các LONG-WIN trong backtest, bao nhiêu cú có failed breakdown wick trước entry?
- Trên LONG-LOSS đã thua, có bao nhiêu cú có wick "fake bounce" rồi vẫn rớt? (false positive)

**Điểm gắn**: 
- `core/indicators.py:detect_failed_breakdown` (mirror của `detect_exhaustion_short`)
- `main.py:smart_action` flip logic
- Tích hợp vào `range_engine` cho LONG signal (boost score như SHORT exhaustion)

---

### 2. Failed breakout 24h (strict) — đã verify, **đã loại bỏ**
Verify cho thấy chỉ 1/25 SHORT-WIN trigger (CRCL). Quá ngặt, recall 4%. Đã thay bằng "Exhaustion candle" (mục đã làm ở trên).
**Mục tiêu**: Bắt cú "blow-off top" kiểu CRCL — nến H1 break high gần rồi đóng dưới mở cửa với volume cao.

**Logic**:
- Tính `high_24h` từ 24 nến H1 trước
- Nến H1 hiện tại: `high > high_24h` (true break) **VÀ** `close < open` (đảo chiều) **VÀ** body ≥ 1.5× ATR H1 trung bình 20 nến
- Volume nến đó ≥ 1.5× avg 20 nến
- → Tạo SHORT signal HIGH confidence

**Điểm gắn**: thêm logic vào `reversal_engine.py` hoặc helper trong `core/indicators.py` rồi gọi từ `range_engine` / `swing_h1_engine` để boost score.

**Effort**: ~50 dòng code + test bằng cách backtest lại CRCL/PNUT/TAO trong file backtest 2026-04-30 xem có detect được không.

---

### 3. Long-trap unwinding detector
**Mục tiêu**: Phát hiện sớm setup "longs đang trapped" trước khi flush — cho SHORT entry tốt hơn.

**Logic**:
- OI tăng > 5% trong 4h gần (từ `fetch_oi_change(symbol, "1h", 5)`)
- Giá trong 4h đó: range < 1.5% (đi ngang, không break up)
- Funding > 0
- → Nếu nến H1 cuối có wick trên dài hoặc close < open → SHORT signal ngay

**Điểm gắn**: helper `_long_trap_setup(df_h1, oi_change, funding)` trong `core/indicators.py`, gọi từ `swing_h1_engine`.

**Effort**: ~30 dòng. Cần verify bằng backtest GIGGLEUSDT (OI +24%, funding +0.005%, +4.11R).

---

### 4. Volume exhaustion divergence
**Mục tiêu**: Bắt SHORT khi buyer cạn — giá tạo new high nhưng volume + RSI yếu dần.

**Logic**:
- Giá H1 tạo new high của 24h
- Volume nến tạo new high < average volume 20 nến trước
- RSI H1 phân kỳ giảm: RSI(now) < RSI(prev_high) trong khi price(now) > price(prev_high)
- → SHORT signal MEDIUM-HIGH

**Effort**: ~60 dòng. Cần utility tìm prev_high (đã có `find_swing_points` trong `core/indicators.py`).

**Trade-off**: Pattern khó detect đúng vì RSI divergence thường có nhiều false positive. Cần backtest kỹ.

---

## Chưa làm — ưu tiên trung

### 6. Per-strategy confidence threshold
Hiện `confidence == "HIGH"` áp chung. Backtest cho thấy:
- RANGE_SCALP HIGH: WR thấp hơn SWING_H1 HIGH
- SWING_H1 HIGH: ổn định hơn

→ Cho phép cấu hình threshold riêng (ví dụ RANGE_SCALP cần score ≥ 7 mới HIGH thay vì 6).

### 7. Funding momentum (delta)
Hiện chỉ check funding tuyệt đối. Có thể thêm: funding tăng từ 0.02% → 0.10% trong 24h = signal mạnh hơn so với funding ổn định ở 0.10%.

**Cần**: Lưu lịch sử funding theo giờ trong file riêng, hoặc dùng Binance API `/fapi/v1/fundingRate?symbol=X&limit=N`.

### 8. Symbol-specific historical WR
Mỗi symbol có WR riêng theo strategy. Sau ≥10 signals của (symbol, strategy), lưu `historical_wr` và dùng để adjust score:
- WR > 60% → +1 score
- WR < 30% → -1 score

**Effort**: Trung bình, cần lưu state riêng + cập nhật khi backtest chạy.

### 9. Telegram alert template phân loại
Hiện alert HIGH chung 1 template. Có thể chia:
- **🚀 PRIME**: HIGH + funding spike + RR cao → ưu tiên hành động
- **✅ STANDARD**: HIGH thường
- **🟡 WATCH**: MEDIUM, có context tốt nhưng chưa đủ trigger

→ User dễ phân biệt và phản ứng nhanh hơn.

---

## Setup persistence tracking + Live re-validation

**Vấn đề** (case SNX 5/5/2026 12:04 → 12:24): cùng strategy SWING_H4, snapshot HIGH conf 7/7 conditions → 20 phút sau live LOW conf 3/7. Trader không biết: lệnh snapshot có còn valid không? Conditions nào đã expire? Có nên execute hay cancel?

**Root cause**:
- Snapshot moment có conditions ngắn hạn (Bullish Engulfing H1, vol spike) — chỉ valid trong cây đó
- 20 phút sau cây mới đóng → các conditions ngắn hạn expire → score sụt
- Engine không track historical setup, mỗi lần phân tích là "fresh evaluation"
- D1+H4 bias vẫn LONG (3 conditions còn) — trend chính không đổi, nhưng score gộp giảm mạnh

**Đề xuất**:
1. **Conditions list explicit trong snapshot** — lưu list 7 conditions với timestamp:
   ```
   [
     {cond: "D1 LONG", expires: "PERSISTENT"},
     {cond: "BullEng H1", expires: "2026-05-05T01:00:00", expired: true},
     {cond: "Vol 1.5x avg", expires: "NEXT_CANDLE", expired: true},
     ...
   ]
   ```
2. **Live re-validation endpoint** `/api/signal/revalidate` — input: snapshot signal + current time → output:
   - `still_valid: bool`
   - `conditions_changed: [{cond, was, now}]`
   - `price_drift_pct` từ entry
   - `recommendation`: "EXECUTE" | "WAIT_PULLBACK" | "CANCEL"
3. **UI snapshot card** thêm nút "🔄 Re-validate now" — hiển thị diff với màu sắc:
   - 🟢 Conditions giữ nguyên + giá quanh entry → EXECUTE
   - 🟡 Mất 2-3 conditions ngắn hạn nhưng trend còn → WAIT_PULLBACK
   - 🔴 Mất D1/H4 bias hoặc giá > 2% từ entry → CANCEL

**Verify trước khi build**: chạy 50 historical signals, đếm % case "snapshot HIGH 20 phút sau LOW" có outcome khác nhau (hit TP vs hit SL). Nếu re-validation đúng > 70% → triển khai.

**Điểm gắn**: `_save_signal_to_history` thêm field `conditions_with_meta`, endpoint mới trong `main.py`, UI snapshot drawer.

---

## Nice-to-have

### 10. Backtest auto-run định kỳ
Cron mỗi 24h: chạy backtest history 72h, save snapshot vào `data/backtest_snapshots/YYYY-MM-DD.json`. Frontend show timeline WR.

### 11. WR-by-time-of-day
Phân tích xem có giờ nào WR cao bất thường (ví dụ 8-12 UTC khi US market mở). Nếu có pattern → hiển thị "best time" trên dashboard.

### 12. Anti-correlation pair filter
Nếu vừa LONG BTC và đang xét LONG ETH → block (cùng beta, double exposure). Chỉ allow nếu correlations recently breakdown.
