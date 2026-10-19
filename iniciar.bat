@echo off
cd /d "%~dp0"
title Controle Financeiro Web

echo.
echo ==========================================
echo   Controle Financeiro Web
echo ==========================================
echo.

REM ==========================================================
REM Remove um venv copiado de outro computador ou quebrado.
REM ==========================================================

if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" --version >nul 2>&1

    if errorlevel 1 (
        echo Ambiente virtual antigo ou invalido detectado.
        echo Recriando para este computador...
        rmdir /s /q "venv"
    )
)

REM ==========================================================
REM Se ainda existir a pasta venv mas sem python.exe valido,
REM remove completamente.
REM ==========================================================

if exist "venv" (
    if not exist "venv\Scripts\python.exe" (
        echo Ambiente virtual incompleto detectado.
        echo Removendo...
        rmdir /s /q "venv"
    )
)

REM ==========================================================
REM Criacao do ambiente virtual.
REM Tenta nesta ordem:
REM 1) py -3
REM 2) py
REM 3) python
REM ==========================================================

if not exist "venv\Scripts\python.exe" (

    where py >nul 2>&1
    if not errorlevel 1 (
        py -3 --version >nul 2>&1

        if not errorlevel 1 (
            echo Criando ambiente virtual com py -3...
            py -3 -m venv venv
        ) else (
            echo Criando ambiente virtual com py...
            py -m venv venv
        )
    ) else (
        where python >nul 2>&1

        if errorlevel 1 (
            echo.
            echo ERRO: Python nao foi encontrado neste computador.
            echo.
            echo Instale o Python 3 e marque a opcao:
            echo Add Python to PATH
            echo.
            pause
            exit /b 1
        )

        echo Criando ambiente virtual com python...
        python -m venv venv
    )

    if errorlevel 1 (
        echo.
        echo ERRO: Nao foi possivel criar o ambiente virtual.
        echo.
        echo Teste no Prompt de Comando:
        echo   py --version
        echo   python --version
        echo.
        pause
        exit /b 1
    )
)

REM ==========================================================
REM Confirma se o Python do venv realmente funciona.
REM ==========================================================

"venv\Scripts\python.exe" --version >nul 2>&1

if errorlevel 1 (
    echo.
    echo ERRO: O ambiente virtual foi criado, mas o Python dele nao funciona.
    echo Apague a pasta venv e tente novamente.
    pause
    exit /b 1
)

REM ==========================================================
REM Garante que pip exista e instala dependencias.
REM ==========================================================

echo Verificando pip...
"venv\Scripts\python.exe" -m ensurepip --upgrade >nul 2>&1

echo Verificando dependencias...
"venv\Scripts\python.exe" -m pip install -r requirements.txt

if errorlevel 1 (
    echo.
    echo ERRO ao instalar as dependencias.
    echo Verifique sua conexao com a internet.
    pause
    exit /b 1
)

echo.
echo Iniciando Controle Financeiro...
echo Endereco local: http://127.0.0.1:5000
echo.

start "" "http://127.0.0.1:5000"
"venv\Scripts\python.exe" app.py

pause
