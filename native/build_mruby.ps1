<#
.SYNOPSIS
Builds native/rpgc_mruby.dll (x64 MSVC) from official mruby 4.0.0 source + native/rpgc_mruby.c.
Uses a temporary portable Ruby build tool in $env:TEMP so zero global PATH changes are needed.
#>
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$nativeDir = Join-Path $repoRoot "native"
$cacheDir = Join-Path $env:TEMP "rpgc_mruby_build"
New-Item -ItemType Directory -Path $cacheDir -Force | Out-Null

$vcvars = Get-ChildItem "C:\Program Files (x86)\Microsoft Visual Studio", "C:\Program Files\Microsoft Visual Studio" -Recurse -Filter "vcvars64.bat" -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $vcvars) {
    throw "Visual Studio vcvars64.bat not found. Please install Visual Studio C++ Build Tools (x64)."
}
Write-Host "[1/4] Using VS Build Tools: $vcvars"

# Ensure build-time ruby.exe in temp
$rubyBin = Get-Command ruby -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
if (-not $rubyBin) {
    $portDir = Join-Path $cacheDir "ruby4"
    $rubyExe = Get-ChildItem $portDir -Recurse -Filter "ruby.exe" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
    if (-not $rubyExe) {
        $riArchive = Join-Path $cacheDir "rubyinstaller-4.0.9-1-x64.7z"
        if (-not (Test-Path $riArchive)) {
            Write-Host "[2/4] Downloading portable Ruby build helper to TEMP..."
            $riUrl = "https://github.com/oneclick/rubyinstaller2/releases/download/RubyInstaller-4.0.9-1/rubyinstaller-4.0.9-1-x64.7z"
            Invoke-WebRequest -Uri $riUrl -OutFile $riArchive -Headers @{"User-Agent"="rpgcreator"}
        }
        New-Item -ItemType Directory -Path $portDir -Force | Out-Null
        $py7z = Join-Path $cacheDir "py7zr_lib"
        if (-not (Test-Path $py7z)) {
            python -m pip install --quiet --target "$py7z" py7zr
        }
        $env:PYTHONPATH = $py7z
        python -c "import py7zr; py7zr.SevenZipFile(r'$riArchive', mode='r').extractall(path=r'$portDir')"
        $env:PYTHONPATH = ""
        $rubyExe = Get-ChildItem $portDir -Recurse -Filter "ruby.exe" | Select-Object -First 1 -ExpandProperty FullName
    }
    $rubyBin = $rubyExe
}
$rubyDir = Split-Path -Parent $rubyBin
Write-Host "[2/4] Build-time Ruby: $rubyBin"

# Fetch mruby 4.0.0 source in temp
$mrubySrc = Join-Path $cacheDir "mruby-4.0.0"
if (-not (Test-Path (Join-Path $mrubySrc "minirake"))) {
    $mrubyTgz = Join-Path $cacheDir "mruby-4.0.0.tar.gz"
    if (-not (Test-Path $mrubyTgz)) {
        Write-Host "[3/4] Downloading mruby 4.0.0 source..."
        Invoke-WebRequest -Uri "https://github.com/mruby/mruby/archive/refs/tags/4.0.0.tar.gz" -OutFile $mrubyTgz -Headers @{"User-Agent"="rpgcreator"}
    }
    tar -xzf $mrubyTgz -C $cacheDir
}

$cfgPath = Join-Path $nativeDir "rpgc_build_config.rb"
$libOut = Join-Path $mrubySrc "build\host\lib\libmruby.lib"
$incDir = Join-Path $mrubySrc "include"
$buildIncDir = Join-Path $mrubySrc "build\host\include"
$dllOut = Join-Path $nativeDir "rpgc_mruby.dll"
$cSrc = Join-Path $nativeDir "rpgc_mruby.c"

Write-Host "[3/4] Building libmruby.lib + rpgc_mruby.dll via MSVC x64..."
$cmd = @"
call "$vcvars" >nul
set "PATH=$rubyDir;%PATH%"
set "MRUBY_CONFIG=$cfgPath"
cd /d "$mrubySrc"
ruby minirake -j4 all
if errorlevel 1 exit /b 1
cl /nologo /O2 /MD /DWIN32 /D_WINDOWS /DMRB_UTF8_STRING /I"$incDir" /I"$buildIncDir" "$cSrc" /link /DLL /OUT:"$dllOut" "$libOut" ws2_32.lib advapi32.lib shell32.lib user32.lib
if errorlevel 1 exit /b 1
"@
$batFile = Join-Path $cacheDir "build_step.bat"
$cmd | Set-Content -Encoding ascii $batFile
cmd /c "`"$batFile`""
if ($LASTEXITCODE -ne 0) {
    throw "mruby native build failed with exit code $LASTEXITCODE"
}
# Clean linker .lib/.exp next to DLL
Remove-Item (Join-Path $nativeDir "rpgc_mruby.lib"), (Join-Path $nativeDir "rpgc_mruby.exp") -ErrorAction SilentlyContinue
Write-Host "[4/4] Built successfully: $dllOut ($((Get-Item $dllOut).Length) bytes)"
