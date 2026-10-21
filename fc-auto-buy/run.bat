@echo off
cd /d "%~dp0.."
set "PRICE="
set "DELAY="
set /p PRICE=Nhap gia moc BP (bo trong = lay gia max lan dau): 
set /p SPEED=Chon toc do [1=an toan, 2=nhanh, 3=turbo] (mac dinh 1): 
if "%SPEED%"=="2" set DELAY=100
if "%SPEED%"=="3" set DELAY=60
if "%DELAY%"=="" set DELAY=250
set /p CUSTOM_DELAY=Delay giua vong (ms, bo trong = dung toc do): 
if not "%CUSTOM_DELAY%"=="" set DELAY=%CUSTOM_DELAY%
if not "%PRICE%"=="" goto explicit
set "LOG=%TEMP%\fc_auto_buy_learn.txt"
"C:\Users\user\AppData\Local\Programs\Python\Python310\python.exe" -u fc-auto-buy\flow_probe.py --open-dialog --learn-only > "%LOG%"
type "%LOG%"
for /f "tokens=2 delims==" %%A in ('findstr "learned_price_bp=" "%LOG%"') do set "PRICE=%%A"
if "%PRICE%"=="" (echo Khong doc duoc gia max.& exit /b 1)
echo Gia moc lan dau: %PRICE% BP
:explicit
"C:\Users\user\AppData\Local\Programs\Python\Python310\python.exe" -u fc-auto-buy\loop_test.py --baseline-bp %PRICE% --count 1000 --delay-ms %DELAY%
