param([string]$Sdk = $env:ANDROID_HOME)
$ErrorActionPreference = 'Stop'
function Run-Tool([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Build step failed: $Program" }
}
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$out = Join-Path $root 'dist/notification-bridge'
$work = Join-Path $out ([guid]::NewGuid().ToString('N'))
$classes = Join-Path $work 'classes'
$dex = Join-Path $work 'dex'
New-Item -ItemType Directory -Force -Path $out,$work,$classes,$dex | Out-Null
$bt = Join-Path $Sdk 'build-tools/36.0.0'
$androidJar = Join-Path $Sdk 'platforms/android-36/android.jar'
$sources = @(Get-ChildItem (Join-Path $PSScriptRoot 'src') -Filter '*.java' -Recurse | ForEach-Object FullName)
Run-Tool 'javac' (@('-encoding','UTF-8','--release','8','-classpath',$androidJar,'-d',$classes) + $sources)
$jar = Join-Path $work 'classes.jar'
Run-Tool 'jar' @('cf',$jar,'-C',$classes,'.')
Run-Tool (Join-Path $bt 'd8.bat') @('--lib',$androidJar,'--min-api','28','--output',$dex,$jar)
$unsigned = Join-Path $work 'unsigned.apk'
Run-Tool (Join-Path $bt 'aapt2.exe') @('link','-I',$androidJar,'--manifest',(Join-Path $PSScriptRoot 'AndroidManifest.xml'),'-o',$unsigned)
Run-Tool 'jar' @('uf',$unsigned,'-C',$dex,'classes.dex')
$aligned = Join-Path $work 'aligned.apk'
Run-Tool (Join-Path $bt 'zipalign.exe') @('-f','4',$unsigned,$aligned)
$keystore = Join-Path $out 'local-development.p12'
if (-not (Test-Path -LiteralPath $keystore)) {
    Run-Tool 'keytool' @('-genkeypair','-keystore',$keystore,'-storepass','android','-keypass','android','-alias','echo-local','-dname','CN=Echo Local Development','-keyalg','RSA','-validity','3650')
}
$apk = Join-Path $out 'echo-notification-bridge.apk'
Run-Tool (Join-Path $bt 'apksigner.bat') @('sign','--ks',$keystore,'--ks-pass','pass:android','--out',$apk,$aligned)
Run-Tool (Join-Path $bt 'apksigner.bat') @('verify',$apk)
Write-Output "Built: $apk"
