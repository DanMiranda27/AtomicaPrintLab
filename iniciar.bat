@echo off
setlocal
cd /d "%~dp0"
title Atomica Print Lab

echo ============================================
echo   Atomica Print Lab
echo ============================================
echo.

rem ---------- 1. Localizar o Python ----------
set "PY="
py -3.12 --version >nul 2>&1 && set "PY=py -3.12"
if not defined PY py -3.13 --version >nul 2>&1 && set "PY=py -3.13"
if not defined PY py -3.11 --version >nul 2>&1 && set "PY=py -3.11"
if not defined PY (
    python --version >nul 2>&1 && set "PY=python"
)
if not defined PY goto sem_python

%PY% -c "import sys; sys.exit(0 if (sys.version_info >= (3,11) and sys.version_info < (3,14)) else 1)"
if errorlevel 1 goto python_antigo

rem ---------- 2. Ambiente virtual (so na primeira vez) ----------
if exist ".venv\Scripts\python.exe" goto venv_ok
echo Criando ambiente virtual, so na primeira vez...
%PY% -m venv .venv
if errorlevel 1 goto erro_venv
:venv_ok

rem ---------- 3. Dependencias (so na primeira vez) ----------
if exist ".venv\.dependencias_ok" goto deps_ok
echo Instalando dependencias, pode levar alguns minutos...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto erro_deps
echo ok>".venv\.dependencias_ok"
:deps_ok

rem ---------- 3b. Garantir Streamlit atual (corrige timeout do uploader nativo no Android) ----------
".venv\Scripts\python.exe" -c "import streamlit as st; v=tuple(map(int,st.__version__.split('.')[:2])); assert v >= (1,67) and hasattr(st.components, 'v2')" >nul 2>&1
if errorlevel 1 (
    echo Atualizando Streamlit para os componentes visuais e a estabilidade do uploader nativo em celulares...
    ".venv\Scripts\python.exe" -m pip install --upgrade "streamlit>=1.67.0"
    if errorlevel 1 goto erro_deps
)

rem ---------- 3c. Dependencia do recebimento por QR Code ----------
".venv\Scripts\python.exe" -c "import qrcode" >nul 2>&1
if errorlevel 1 (
    echo Instalando a dependencia do envio por QR Code...
    ".venv\Scripts\python.exe" -m pip install "qrcode[pil]>=7.4"
    if errorlevel 1 goto erro_deps
)


rem ---------- 3d. Removedor de fundos com IA (backend CPU) ----------
".venv\Scripts\python.exe" -c "import rembg, onnxruntime" >nul 2>&1
if errorlevel 1 (
    echo Instalando o removedor de fundos com IA...
    ".venv\Scripts\python.exe" -m pip install "rembg[cpu]>=2.0.87"
    if errorlevel 1 goto erro_deps
)

rem ---------- 3e. Leitura de PDFs de projeto de versoes antigas ----------
".venv\Scripts\python.exe" -c "import pypdf; assert int(pypdf.__version__.split('.')[0]) >= 5" >nul 2>&1
if errorlevel 1 (
    echo Instalando a dependencia para ler projetos PDF de versoes anteriores...
    ".venv\Scripts\python.exe" -m pip install "pypdf>=5.0"
    if errorlevel 1 goto erro_deps
)

rem ---------- 4. Evita o pedido de e-mail do Streamlit na 1a execucao ----------
if exist "%USERPROFILE%\.streamlit\credentials.toml" goto cred_ok
if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit"
> "%USERPROFILE%\.streamlit\credentials.toml" (
    echo [general]
    echo email = ""
)
:cred_ok

rem ---------- 5. Iniciar o app ----------
echo.
echo Abrindo o app no navegador: http://localhost:8501
echo Para encerrar, feche esta janela ou pressione Ctrl+C.
echo.
".venv\Scripts\python.exe" -m streamlit run app.py
if errorlevel 1 pause
exit /b

:sem_python
echo [ERRO] Python nao foi encontrado neste computador.
echo.
echo Instale o Python 3.11, 3.12 ou 3.13 em https://www.python.org/downloads/
echo e marque a opcao "Add python.exe to PATH" no instalador.
echo Depois execute este arquivo novamente.
echo.
pause
exit /b 1

:python_antigo
echo [ERRO] E necessario o Python 3.11, 3.12 ou 3.13.
echo Instale uma versao mais recente em https://www.python.org/downloads/
echo.
pause
exit /b 1

:erro_venv
echo [ERRO] Nao foi possivel criar o ambiente virtual.
echo.
pause
exit /b 1

:erro_deps
echo [ERRO] Falha ao instalar as dependencias. Verifique a conexao com a internet
echo e execute este arquivo novamente.
echo.
pause
exit /b 1
