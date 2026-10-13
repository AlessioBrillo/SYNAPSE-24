# SYNAPSE-24 ESP-IDF submodule fetch (only what the esp32s3 / NimBLE Band
# build needs), at the exact SHAs pinned by the v5.5.5 tree.
#
# The GitHub release ZIP does not contain submodules. This script materialises
# each required submodule at its pinned commit using a depth-1 fetch by SHA
# (GitHub allows fetching reachable SHAs), with retries for flaky links.
#
# Usage (after expanding the v5.5.5 archive into %USERPROFILE%\esp\esp-idf):
#   powershell -ExecutionPolicy Bypass -File scripts\tools\fetch_idf_submodules.ps1

$ErrorActionPreference = 'Stop'
$idf = Join-Path $env:USERPROFILE 'esp\esp-idf'
if (-not (Test-Path (Join-Path $idf 'CMakeLists.txt'))) {
  Write-Output "ERROR: $idf is not an ESP-IDF checkout"
  exit 1
}

# path -> (url, pinned SHA from the v5.5.5 tree)
$submodules = [ordered]@{
  'components\bt\host\nimble\nimble'                                   = @('https://github.com/espressif/esp-nimble.git',       '685675c0128deafdd201c9eb82e61d227364646c')
  'components\bt\controller\lib_esp32c3_family'                        = @('https://github.com/espressif/esp32c3-bt-lib.git',  '0a08c4b32f3666003080b662a1a61794da24ff0f')
  'components\esp_wifi\lib'                                            = @('https://github.com/espressif/esp32-wifi-lib.git',  '22bb3517b08760ed43b889013f8a122db0d46da0')
  'components\esp_phy\lib'                                             = @('https://github.com/espressif/esp-phy-lib.git',     '59c1234e929212aec0fdda75769b759951235536')
  'components\esp_coex\lib'                                            = @('https://github.com/espressif/esp-coex-lib.git',    '79e618f033e926d134decc6d95bb1b1850bd032c')
  'components\lwip\lwip'                                               = @('https://github.com/espressif/esp-lwip.git',        'fd432e4ee2cfb7f7f1c7eb7227e0173412e7b84e')
  'components\mbedtls\mbedtls'                                         = @('https://github.com/espressif/mbedtls.git',         '9d669eadb1955d348986b9280156710aaaadf79f')
  'components\heap\tlsf'                                               = @('https://github.com/espressif/tlsf.git',            '2867f6883a12920b1969ff9624c0ab0e4185c2ce')
  'components\json\cJSON'                                              = @('https://github.com/DaveGamble/cJSON.git',          'b2890c8d76bbb64e710585ebc0a917196b9c67e7')
  'components\bootloader\subproject\components\micro-ecc\micro-ecc'    = @('https://github.com/kmackay/micro-ecc.git',         '24c60e243580c7868f4334a1ba3123481fe1aa48')
}

$failed = @()
foreach ($rel in $submodules.Keys) {
  $url, $sha = $submodules[$rel]
  $path = Join-Path $idf $rel
  if (-not (Test-Path (Join-Path $path '.git'))) {
    if (Test-Path $path) { Remove-Item -Recurse -Force $path }
    git init -q $path
    git -C $path remote add origin $url
  }
  $ok = $false
  for ($i = 1; $i -le 20 -and -not $ok; $i++) {
    git -C $path fetch -q --depth 1 origin $sha 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { $ok = $true }
    else {
      # Fallback for REPOS where fetch-by-SHA is refused: take the branch tip
      # and deepen until the pinned commit is inside the fetched window.
      git -C $path fetch -q --depth 200 origin master 2>&1 | Out-Null
      if ($LASTEXITCODE -eq 0 -and (git -C $path cat-file -e "$sha^{commit}" 2>$null)) { $ok = $true }
    }
    if (-not $ok) { Start-Sleep -Seconds 3 }
  }
  if ($ok) {
    git -C $path checkout -q --detach $sha
    $head = (git -C $path rev-parse HEAD).Trim()
    $match = if ($head -eq $sha) { 'PINNED-OK' } else { "MISMATCH ($head)" }
    Write-Output ("{0,-12} {1}" -f $match, $rel)
    if ($head -ne $sha) { $failed += $rel }
  } else {
    Write-Output ("{0,-12} {1}" -f 'FETCH-FAIL', $rel)
    $failed += $rel
  }
}

if ($failed.Count -gt 0) {
  Write-Output "FAILED submodules: $($failed -join ', ')"
  exit 1
}
Write-Output 'ALL PINNED SUBMODULES OK'
exit 0
