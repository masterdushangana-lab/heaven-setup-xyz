@echo off
title AnimeHeaven 5-Second Realtime Silent Watcher
color 0b
echo ==========================================================
echo   Starting AnimeHeaven 5-Second Realtime Silent Watcher
echo ==========================================================
echo.

:: Configure your GitHub Repository and Personal Access Token here
set GITHUB_REPO=masterdushangana-lab/heaven-setup-xyz
set GITHUB_TOKEN=YOUR_GITHUB_FINE_GRAINED_TOKEN_HERE

python watcher_5s.py
pause
