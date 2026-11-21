@echo off
rem start-link.cmd — поднимает мост к 1С: PowerShell COM + SSH на hardpro-llm.
rem Ставится в планировщик при входе в систему: install-link.ps1
cd /d "%~dp0"
start "" /b "D:\Users\user\AppData\Local\Microsoft\WinGet\Packages\OpenJS.NodeJS.LTS_Microsoft.Winget.Source_8wekyb3d8bbwe\node-v24.19.0-win-x64\node.exe" "E:\Work\Tools\onec-bridge\link.mjs" >> "E:\Work\Tools\onec-bridge\link.log" 2>&1
