# Source package only: never ship local credentials, databases or virtualenvs.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$releaseRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$releaseFiles = @(
    '.gitignore', 'README.md', 'requirements.txt', 'config.example.json',
    'run.bat', 'setup.bat', 'launch.py', 'main.py', 'batch.py',
    'storage.py', 'downloads.py', 'delivery.py', 'report.py',
    'static/index.html', 'static/workbench.css', 'static/workbench.js',
    'search_engine/AGENTS.md', 'scripts/package_release.ps1'
)
foreach ($releaseFolder in @('search_engine', 'tests')) {
    Get-ChildItem -LiteralPath (Join-Path $releaseRoot $releaseFolder) -File -Filter '*.py' |
        ForEach-Object { $releaseFiles += "$releaseFolder/$($_.Name)" }
}
# Include the owner's license when one has been selected.
foreach ($releaseLicense in @('LICENSE', 'LICENSE.md', 'NOTICE')) {
    if (Test-Path -LiteralPath (Join-Path $releaseRoot $releaseLicense) -PathType Leaf) {
        $releaseFiles += $releaseLicense
    }
}
foreach ($releaseRelative in $releaseFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $releaseRoot $releaseRelative) -PathType Leaf)) {
        throw "Missing release file: $releaseRelative"
    }
}
$releaseDist = Join-Path $releaseRoot 'dist'
New-Item -ItemType Directory -Path $releaseDist -Force | Out-Null
$releaseStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$releaseShortId = [Guid]::NewGuid().ToString('N').Substring(0, 8)
$releaseZip = Join-Path $releaseDist "movie-downloader-assist-$releaseStamp-$releaseShortId.zip"
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$releaseStream = [System.IO.File]::Open($releaseZip, [System.IO.FileMode]::CreateNew)
try {
    $releaseArchive = New-Object System.IO.Compression.ZipArchive($releaseStream, [System.IO.Compression.ZipArchiveMode]::Create, $true)
    try {
        foreach ($releaseRelative in ($releaseFiles | Sort-Object -Unique)) {
            $releaseEntry = "movie-downloader-assist/$releaseRelative"
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($releaseArchive, (Join-Path $releaseRoot $releaseRelative), $releaseEntry) | Out-Null
        }
    } finally { $releaseArchive.Dispose() }
} finally { $releaseStream.Dispose() }
$releaseHash = (Get-FileHash -LiteralPath $releaseZip -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$releaseZip.sha256" -Value "$releaseHash  $([System.IO.Path]::GetFileName($releaseZip))" -Encoding ASCII
Write-Output "Release package: $releaseZip"
Write-Output "SHA256: $releaseHash"
