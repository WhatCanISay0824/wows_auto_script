@echo off
chcp 65001 >nul
rem Use py -3.14; local 'python' is a Windows Store stub.
py -3.14 -m src.tools.%*
