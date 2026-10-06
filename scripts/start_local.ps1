# Opens 3 PowerShell windows: action server, Rasa server, Streamlit UI.
# Run from the project folder:  .\scripts\start_local.ps1
# Expects virtual environments  .venv-rasa  (bot)  and  .venv-ui  (Streamlit).
$root = Split-Path -Parent $PSScriptRoot
Start-Process powershell -ArgumentList "-NoExit","-Command","cd '$root'; .\.venv-rasa\Scripts\Activate.ps1; rasa run actions"
Start-Sleep -Seconds 3
Start-Process powershell -ArgumentList "-NoExit","-Command","cd '$root'; .\.venv-rasa\Scripts\Activate.ps1; rasa run --enable-api --cors '*'"
Start-Sleep -Seconds 3
Start-Process powershell -ArgumentList "-NoExit","-Command","cd '$root'; .\.venv-ui\Scripts\Activate.ps1; streamlit run frontend/app.py"
