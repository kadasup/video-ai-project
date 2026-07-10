@echo off
rem VideoAI semantic search server (port 5001)
cd /d D:\VideoAI\scripts
"C:\Users\kevin\AppData\Local\Python\bin\python.exe" -X utf8 search_app.py >> "D:\VideoAI\logs\search-server.log" 2>&1
