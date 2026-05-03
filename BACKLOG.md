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

## Nice-to-have

### 10. Backtest auto-run định kỳ
Cron mỗi 24h: chạy backtest history 72h, save snapshot vào `data/backtest_snapshots/YYYY-MM-DD.json`. Frontend show timeline WR.

### 11. WR-by-time-of-day
Phân tích xem có giờ nào WR cao bất thường (ví dụ 8-12 UTC khi US market mở). Nếu có pattern → hiển thị "best time" trên dashboard.

### 12. Anti-correlation pair filter
Nếu vừa LONG BTC và đang xét LONG ETH → block (cùng beta, double exposure). Chỉ allow nếu correlations recently breakdown.
