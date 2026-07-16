$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

Write-Host 'Installing Python dependencies...'
python -m pip install -r requirements.txt

if (!(Test-Path 'bin\Release\whisper-cli.exe')) {
    Write-Host 'Downloading whisper.cpp Windows binaries...'
    Invoke-WebRequest -Uri 'https://github.com/ggml-org/whisper.cpp/releases/download/v1.9.1/whisper-bin-x64.zip' -OutFile 'whisper-bin.zip'
    Expand-Archive -Force 'whisper-bin.zip' 'bin'
    Remove-Item 'whisper-bin.zip'
}

if (!(Test-Path 'models\ggml-base.en.bin')) {
    New-Item -ItemType Directory -Force 'models' | Out-Null
    Write-Host 'Downloading the base.en Whisper model (~142 MB)...'
    Invoke-WebRequest -Uri 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin?download=true' -OutFile 'models\ggml-base.en.bin'
}

Write-Host ''
Write-Host 'Setup complete.' -ForegroundColor Green
Write-Host 'Run: python app.py'
Write-Host 'Optional: python app.py --list-devices'
