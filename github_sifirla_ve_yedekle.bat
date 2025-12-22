@echo off
title GitHub Temizleme ve Yedek Yukleme
color 0c
echo ============================================================
echo   DIKKAT: GITHUB'I TAMAMEN SIFIRLAYIP SADECE YEDEGI YUKLER
echo ============================================================
echo.
echo Bu islem:
echo 1. Ana dizindeki tum dosyalari ve Git gecmisini temizler.
echo 2. 19.12.2025 klasorundeki kodlari ana dizine tasiyip GitHub'a yukler.
echo.
set /p onay="Onayliyor musunuz? (evet/hayir): "

if /i "%onay%" neq "evet" (
    echo Islem iptal edildi.
    pause
    exit
)

:: Git ana dizinine git
cd /d "%~dp0"

echo.
echo [1/4] Yedek dosyalar gecici olarak korunuyor...
if exist "_temp_vidos" rd /s /q "_temp_vidos"
mkdir _temp_vidos
xcopy "Vidos Entegrasyon 19.12.2025\*.*" "_temp_vidos" /E /Y /I /H

echo.
echo [2/4] Ana dizin ve Git indeksi temizleniyor...
:: Git'teki her seyi sil
git rm -rf .
:: Kalan dosyalari temizle (.git ve bat haric)
for /d %%i in (*) do if /i "%%i" neq "_temp_vidos" if /i "%%i" neq ".git" rd /s /q "%%i"
for %%i in (*) do if /i "%%i" neq "github_sifirla_ve_yedekle.bat" del /q "%%i"

echo.
echo [3/4] Yedek kodlar ana dizine yerlestiriliyor...
xcopy "_temp_vidos\*.*" "." /E /Y /I /H
rd /s /q _temp_vidos

echo.
echo [4/4] GitHub'a gonderiliyor (FORCE PUSH)...
git add .
git commit -m "Repository Cleaned - Only Backup Version Loaded"
git push origin main --force

echo.
echo ============================================================
echo   BASARILI: GitHub artik tertemiz ve sadece yedek kodlari iceriyor!
echo ============================================================
pause
