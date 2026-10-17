@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (
  set "PY=py -3"
) else (
  where python >nul 2>&1
  if errorlevel 1 (
    echo Instale Python 3.10 ou superior e tente novamente.
    pause
    exit /b 1
  )
  set "PY=python"
)
:MENU
cls
echo ==================================================
echo  Ghost of Yotei - PC Lab v0.4.0
 echo  AnyPS5 incluso - compatibilidade experimental
 echo ==================================================
echo 1 - Diagnostico do ambiente
echo 2 - Instalar ferramentas e dependencias automaticamente
echo 3 - Compilar apenas relinker + executar testes
echo 4 - Compilar runtime completo (requer dependencias)
echo 5 - Converter executavel para CPU AMD
echo 6 - Converter executavel para CPU Intel
echo 7 - Auditar funcoes e bibliotecas do runtime
echo 8 - Testar jogo com log e monitor de memoria
echo E - Testar modo economico de memoria
echo S - Testar economico + capturar shaders (investigacao)
echo F - Testar desempenho (cache de imagens maior)
echo L - Aplicar correcao local 0.4.0 (backup + compilacao + testes)
echo V - Verificar atualizacoes do AnyPS5
echo U - Atualizar AnyPS5 (backup + compilacao + testes)
echo R - Retornar a versao anterior
echo B - Listar backups
echo 9 - Sair
echo.
choice /c 123456789ESVURBLF /n /m "Escolha uma opcao: "
if errorlevel 17 ( %PY% tools\project.py run --profile desempenho & pause & goto MENU )
if errorlevel 16 ( %PY% tools\project.py apply-lab-update --jobs 4 & pause & goto MENU )
if errorlevel 15 ( %PY% tools\project.py backups & pause & goto MENU )
if errorlevel 14 ( %PY% tools\project.py rollback & pause & goto MENU )
if errorlevel 13 ( %PY% tools\project.py update --jobs 4 & pause & goto MENU )
if errorlevel 12 ( %PY% tools\project.py check-updates & pause & goto MENU )
if errorlevel 11 ( %PY% tools\project.py run --profile economico --dump-shaders & pause & goto MENU )
if errorlevel 10 ( %PY% tools\project.py run --profile economico & pause & goto MENU )
if errorlevel 9 goto END
if errorlevel 8 ( %PY% tools\project.py run & pause & goto MENU )
if errorlevel 7 ( %PY% tools\project.py audit & pause & goto MENU )
if errorlevel 6 ( %PY% tools\project.py relink --cpu intel & pause & goto MENU )
if errorlevel 5 ( %PY% tools\project.py relink --cpu amd & pause & goto MENU )
if errorlevel 4 ( %PY% tools\project.py build --jobs 4 & pause & goto MENU )
if errorlevel 3 ( %PY% tools\project.py build --relinker-only --tests --jobs 4 & pause & goto MENU )
if errorlevel 2 ( %PY% tools\project.py setup & pause & goto MENU )
if errorlevel 1 ( %PY% tools\project.py status & pause & goto MENU )
:END
endlocal
