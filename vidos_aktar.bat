@echo off
title Vidos Git Aktarici
color 0a
echo ============================================================
echo           VIDOS GIT AKTARICI (YENI GUNCELLEMELER)
echo ============================================================
echo.

:: Klasore git
cd /d "%~dp0"

:: 1. Degisiklikleri Ekle
echo [1/3] Degisiklikler paketleniyor...
git add .

:: 2. Commit Mesaji
echo.
set /p msg="Yapilan degisiklikleri yazin (Bos birakirsaiz varsayilan yazilir): "
if "%msg%"=="" set msg="Vidos Guncelleme - %date% %time%"

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
    echo    BASARILI: Guncellemeler gonderildi!
    echo ============================================================
    echo.
    echo SUNUCUDA CALISTIRILACAK KOMUTLAR:
    echo 1. cd /var/www/vidos
    echo 2. git pull
    echo 3. sudo systemctl restart vidos
    echo.
) else (
    echo.
    echo !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    echo    HATA: Gonderim sirasinda bir sorun olustu!
    echo !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
)

echo.
pause
