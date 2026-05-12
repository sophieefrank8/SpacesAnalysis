@echo off
:: CoStar Weekly Export
:: Double-click this file after dropping PDFs into the inbox folder.

cd /d "%~dp0.."
echo.
echo  CoStar Weekly Export
echo  ==============================
echo  Drop PDFs into LandlordData\inbox\
echo  then press any key to run...
echo.
pause

python tools\costar_weekly_export.py

echo.
pause
