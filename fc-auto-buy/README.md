# FC Auto Buy

Project riêng cho tool FC Online. Bản hiện tại chỉ theo dõi vùng giá bằng
ảnh/OCR ở chế độ read-only; chưa có thao tác chuột, bàn phím hoặc đặt lệnh.

AutoMua appears to use multiple regions rather than one global price box:
player name, market rows, editable buy price, quantity, success/failure state,
plus pixel/color checks for readiness and reset detection. See
`regions.example.json` for the calibration model.

The maximum price is refreshed by opening the buy-player dialog. See
`flow.md`; the prototype keeps this flow read-only.

## Prototype

```powershell
python -m pip install mss pillow opencv-python pytesseract
python fc-auto-buy/monitor.py --region 900,380,300,80 --interval-ms 40 --tesseract
```

Calibrate regions from the live FC ONLINE window:

```powershell
python fc-auto-buy/calibrate.py
```

The calibration tool only captures and displays the game window. It does not
send mouse or keyboard input to the game.

To monitor only the maximum-price region, use:

```powershell
python fc-auto-buy/watch_max_price.py
```
