@echo off
chcp 65001 >nul
title 文档处理工具
cd /d "%~dp0"
rem AI 修复模型缓存放 D 盘(C 盘空间紧)
set "TORCH_HOME=D:\torch-cache"
set "HF_HOME=D:\hf-cache"
echo 正在启动文档处理工具...
echo (首次擦除厂标会下载 AI 修复模型约 200MB，请耐心等待)
".venv\Scripts\pythonw.exe" desktop_app.py
