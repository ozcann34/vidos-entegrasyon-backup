@echo off
title Vidos Sunucu Temizleyici (DIKKAT)
color 0c
echo ============================================================
echo           !!!! DIKKAT: SUNUCU TEMIZLEME !!!!
echo ============================================================
echo.
echo Bu islem sunucudaki /var/www/vidos/ klasorunun icini SILECEKTIR!
echo.
set /p onay="Devam etmek istiyor musunuz? (evet/hayir): "

if /i "%onay%" neq "evet" (
    echo.
    echo Islem iptal edildi.
    pause
    exit
)

echo.
echo Sunucuya baglaniliyor ve temizlik yapiliyor...
echo (SSH sifresi istenebilir)
echo.

:: SSH ile sunucuya baglanip silme komutunu calistirir
:: NOT: SSH baglanti bilgilerini (user@host) asagida guncellemeniz gerekebilir.
:: Su an varsayilan olarak "root@vidos-server" gibi bir placeholder kullaniliyor.

ssh -t root@host "cd /var/www/vidos && sudo rm -rf *"

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ============================================================
    echo    TEMIZLIK TAMAMLANDI: Sunucu klasoru bosaltildi.
    echo ============================================================
) else (
    echo.
    echo !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    echo    HATA: Sunucuya baglanilamadi veya islem basarisiz!
    echo !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
)

echo.
pause
