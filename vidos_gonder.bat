@echo off
title Vidos GitHub Gonderici
color 0b
echo ============================================================
echo           VIDOS GITHUB GONDERICI (YARI-OTOMATIK)
echo ============================================================
echo.

:: Klasore git
cd /d "%~dp0"

:: 1. Degisiklikleri Ekle
echo [1/3] Degisiklikler paketleniyor...
git add .

:: 2. Commit Mesaji Al
echo.
set /p msg="Yapilan degisiklikleri kisaca yazin (Bos birakirsaniz tarih yazilir): "
if "%msg%"=="" set msg="Guncelleme - %date% %time%"

echo.
echo [2/3] Kayit olusturuluyor: %msg%
git commit -m "%msg%"

:: 3. GitHub'a Push
echo.
echo [3/3] GitHub'a gonderiliyor...
git push origin main

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ============================================================
    echo    BASARILI: Kodlar GitHub'a ulasti!
    echo ============================================================
    echo.
    echo SIMDI SUNUCUDA (SSH) SUNLARI CALISTIRIN:
    echo 1. cd /var/www/vidos
    echo 2. git pull
    echo 3. sudo systemctl restart vidos
    echo.
) else (
    echo.
    echo !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    echo    HATA: GitHub'a gonderilemedi! 
    echo    (Token veya baglanti sorunu olabilir)
    echo !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
)

echo.
pause
