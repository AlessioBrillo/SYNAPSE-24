# SYNAPSE-24 ESP-IDF v5.5.5 download helper (flaky-network friendly).
# Tries the Espressif asset CDN first, falls back to codeload (GitHub).
# curl -C - resumes a partial file, so retries make forward progress.
$ErrorActionPreference = 'Continue'  # curl stderr on a dropped link is EXPECTED here; retry, don't die
$espDir = Join-Path $env:USERPROFILE 'esp'
$dest = Join-Path $espDir 'esp-idf-v5.5.5.zip'
$log = Join-Path $espDir 'download.log'
New-Item -ItemType Directory -Force -Path $espDir | Out-Null
"" | Out-File $log

$urls = @(
  'https://dl.espressif.com/github_assets/espressif/esp-idf/releases/download/v5.5.5/esp-idf-v5.5.5.zip',
  'https://codeload.github.com/espressif/esp-idf/zip/refs/tags/v5.5.5'
)

function Test-ValidZip([string]$path) {
  if (-not (Test-Path $path)) { return $false }
  if ((Get-Item $path).Length -lt 10MB) { return $false }  # a v5.5.x zip is >100MB
  $fs = [System.IO.File]::OpenRead($path)
  try {
    $buf = New-Object byte[] 4
    $fs.Read($buf, 0, 4) | Out-Null
    return ($buf[0] -eq 0x50 -and $buf[1] -eq 0x4B)  # 'PK' local file header
  } finally { $fs.Dispose() }
}

foreach ($u in $urls) {
  for ($i = 1; $i -le 200; $i++) {
    curl.exe -sS -L -C - -o $dest $u 2>&1 | Out-File -Append $log
    $rc = $LASTEXITCODE
    if ($rc -eq 0 -and (Test-ValidZip $dest)) {
      $out = (Get-Item $dest).Length
      "SUCCESS url=$u attempts=$i bytes=$out" | Out-File -Append $log
      Write-Output "SUCCESS url=$u attempts=$i bytes=$out"
      exit 0
    }
    $partial = 0
    if (Test-Path $dest) { $partial = (Get-Item $dest).Length }
    "attempt=$i rc=$rc url=$u partial_bytes=$partial" | Out-File -Append $log
    Start-Sleep -Seconds 3
  }
}
Write-Output 'FAILED all attempts'
exit 1
