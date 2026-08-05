param(
    [switch]$RequireRuntimeData
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root

$required = @(
    'datasets/catalog/source-registry.json',
    'datasets/catalog/source-registry.schema.json',
    'datasets/catalog/source-snapshot.schema.json',
    'datasets/catalog/knowledge-library.schema.json',
    'datasets/recipes/collect_official_problems.py',
    'datasets/recipes/build_knowledge_library.py',
    'datasets/recipes/ingest_mathmodel_full_problems.py',
    'datasets/recipes/ingest_full_problem_archives.py',
    'apps/web/src/data/knowledge-library.json',
    'apps/web/src/legacy/openmathmodel-ui.ts',
    'docs/implementation/data-collection/wave-a-plan.md'
)
foreach ($path in $required) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Missing required file: $path"
    }
}

python -m py_compile datasets/recipes/collect_official_problems.py
if ($LASTEXITCODE -ne 0) { throw 'Python compilation failed' }
python -m py_compile datasets/recipes/build_knowledge_library.py
if ($LASTEXITCODE -ne 0) { throw 'Knowledge library builder compilation failed' }
python -m py_compile datasets/recipes/ingest_mathmodel_full_problems.py
if ($LASTEXITCODE -ne 0) { throw 'MathModel full-problem ingester compilation failed' }
python -m py_compile datasets/recipes/ingest_full_problem_archives.py
if ($LASTEXITCODE -ne 0) { throw 'Full archive problem ingester compilation failed' }
python datasets/recipes/collect_official_problems.py validate
if ($LASTEXITCODE -ne 0) { throw 'Source registry validation failed' }

Get-Content -LiteralPath 'datasets/catalog/source-registry.schema.json' -Raw -Encoding UTF8 | ConvertFrom-Json | Out-Null
Get-Content -LiteralPath 'datasets/catalog/source-snapshot.schema.json' -Raw -Encoding UTF8 | ConvertFrom-Json | Out-Null
Get-Content -LiteralPath 'datasets/catalog/knowledge-library.schema.json' -Raw -Encoding UTF8 | ConvertFrom-Json | Out-Null
$registry = Get-Content -LiteralPath 'datasets/catalog/source-registry.json' -Raw -Encoding UTF8 | ConvertFrom-Json
if ($registry.sources.Count -ne 5) { throw "Expected 5 registered sources, found $($registry.sources.Count)" }
if (($registry.sources.id | Sort-Object -Unique).Count -ne $registry.sources.Count) { throw 'Duplicate source id' }
foreach ($source in $registry.sources) {
    if ($source.license_record.PSObject.Properties.Name.Count -lt 8) { throw "Incomplete LicenseRecord: $($source.id)" }
}

$library = Get-Content -LiteralPath 'apps/web/src/data/knowledge-library.json' -Raw -Encoding UTF8 | ConvertFrom-Json
if ($library.stats.problem_count -ne $library.problems.Count) { throw 'Problem count does not match frontend data' }
if ($library.stats.paper_count -ne $library.papers.Count) { throw 'Paper count does not match frontend data' }
if ($library.problems.Count -ne 86) { throw "Expected 86 complete problems, found $($library.problems.Count)" }
if ($library.papers.Count -lt 906) { throw "Expected at least 906 structured paper records, found $($library.papers.Count)" }
if (($library.problems.id | Sort-Object -Unique).Count -ne $library.problems.Count) { throw 'Duplicate problem id' }
if (($library.papers.id | Sort-Object -Unique).Count -ne $library.papers.Count) { throw 'Duplicate paper id' }
$repositoryPapers = @($library.papers | Where-Object { $_.source_id -eq 'github_zhanwen_mathmodel' })
if ($repositoryPapers.Count -ne 685) { throw "Expected 685 repository paper records, found $($repositoryPapers.Count)" }
$fullProblems = @($library.problems | Where-Object { $_.content_status -eq 'complete' })
if ($fullProblems.Count -ne 86) { throw "Expected every frontend problem to be complete, found $($fullProblems.Count)" }
# Layout-aware extraction excludes page numbers, running heads and footer boilerplate,
# so the published character total sits below the raw text-layer length.
if (($fullProblems | Measure-Object content_character_count -Sum).Sum -lt 425000) { throw 'Complete problem text is unexpectedly short' }
if (($fullProblems | Measure-Object content_block_count -Sum).Sum -lt 2700) { throw 'Complete problem block count is unexpectedly low' }
foreach ($expected in @(@('comap_mcm_icm',30), @('apmcm_problems',19), @('cumcm_official',25), @('github_zhanwen_mathmodel',12))) {
    $actual = @($fullProblems | Where-Object { $_.source_id -eq $expected[0] }).Count
    if ($actual -ne $expected[1]) { throw "Expected $($expected[1]) complete problems for $($expected[0]), found $actual" }
}
foreach ($year in 2023, 2024) {
    foreach ($letter in 'a','b','c','d','e','f') {
        if (-not ($fullProblems.id -contains "cpmcm-$year-$letter")) { throw "Missing complete problem cpmcm-$year-$letter" }
    }
}
$archiveProblems = @($fullProblems | Where-Object { $_.source_id -in @('comap_mcm_icm', 'apmcm_problems', 'cumcm_official') })
if (@($archiveProblems | Where-Object { $_.content_format -ne 'structured_text' }).Count -ne 0) { throw 'PDF problems are not published as structured text' }
if (@($archiveProblems | Where-Object { @($_.content_blocks | Where-Object { $_.type -eq 'page' }).Count -gt 0 }).Count -ne 0) { throw 'PDF page screenshot blocks remain' }
$archiveParagraphs = @($archiveProblems.content_blocks | Where-Object { $_.type -eq 'paragraph' })
if ($archiveParagraphs.Count -lt 1400) { throw "PDF statements lost paragraph structure, found only $($archiveParagraphs.Count) paragraphs" }
$runOnParagraphs = @($archiveParagraphs | Where-Object { $_.text.Length -gt 3000 })
if ($runOnParagraphs.Count -ne 0) { throw "Found $($runOnParagraphs.Count) unsegmented paragraphs over 3000 characters" }
if (@($archiveProblems | Where-Object { @($_.attachments | Where-Object { $_.kind -eq 'problem' -and $_.url -like '/problem-files/*/problem.pdf' }).Count -ne 1 }).Count -ne 0) { throw 'A PDF problem is missing its local original download' }
$cumcmProblems = @($fullProblems | Where-Object { $_.source_id -eq 'cumcm_official' })
if (@($cumcmProblems | Where-Object { $_.category -ne '国赛' }).Count -ne 0) { throw 'CUMCM category must be 国赛' }
foreach ($problem in $archiveProblems) {
    foreach ($attachment in $problem.attachments) {
        $download = Join-Path $root ('apps/web/public' + ($attachment.url -replace '/', [IO.Path]::DirectorySeparatorChar))
        if (-not (Test-Path -LiteralPath $download -PathType Leaf)) { throw "Missing local problem download: $download" }
        if ((Get-Item -LiteralPath $download).Length -ne [long]$attachment.bytes) { throw "Problem download size mismatch: $download" }
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $download).Hash.ToLowerInvariant() -ne $attachment.sha256) { throw "Problem download hash mismatch: $download" }
    }
}
$frontendSource = Get-Content -LiteralPath 'apps/web/src/legacy/openmathmodel-ui.ts' -Raw -Encoding UTF8
if ($frontendSource -notmatch 'import\("\.\./data/knowledge-library\.json\?url"\)') { throw 'Frontend does not lazily load the structured knowledge library' }
if ($frontendSource -notmatch 'completeProblemMarkup' -or $frontendSource -notmatch 'problem-full-content') { throw 'Frontend does not render ordered complete problem blocks' }
if ($frontendSource -match 'problem-completeness-bar' -or $frontendSource -match 'problem-source-ledger' -or $frontendSource -match '数据版本　') { throw 'Removed problem metadata boxes or footer labels are still present' }
if ($frontendSource -match 'block\.type === "page"' -or $frontendSource -match 'problem-page-sheet') { throw 'Frontend still contains page screenshot rendering' }
if ($frontendSource -notmatch 'problem-download-item' -or $frontendSource -notmatch 'download-simple') { throw 'Frontend does not expose local problem downloads' }
python datasets/recipes/ingest_mathmodel_full_problems.py verify
if ($LASTEXITCODE -ne 0) { throw 'Complete MathModel problem verification failed' }
$bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (-not (Test-Path -LiteralPath $bundledPython -PathType Leaf)) { throw "Bundled workspace Python not found: $bundledPython" }
& $bundledPython datasets/recipes/ingest_full_problem_archives.py verify
if ($LASTEXITCODE -ne 0) { throw 'Complete COMAP/APMCM/CUMCM problem verification failed' }
$verificationOutput = 'artifacts/data-collection-wave-a/verification-library.json'
python datasets/recipes/build_knowledge_library.py --output $verificationOutput
if ($LASTEXITCODE -ne 0) { throw 'Knowledge library deterministic rebuild failed' }
$publishedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath 'apps/web/src/data/knowledge-library.json').Hash
$rebuiltHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $verificationOutput).Hash
if ($publishedHash -ne $rebuiltHash) { throw 'Published frontend data differs from deterministic rebuild' }

$runtimeSummary = [ordered]@{ manifests = 0; objects = 0; bytes = 0; discovered_links = 0; errors = 0; orphan_objects = 0; full_problem_documents = 0; full_problem_bytes = 0; full_problem_pages = 0; full_problem_text_blocks = 0; full_problem_figures = 0; full_problem_downloads = 0; full_problem_download_bytes = 0 }
if ($RequireRuntimeData) {
    $manifests = @(Get-ChildItem -LiteralPath 'datasets/raw/snapshots' -Recurse -Filter manifest.json -File)
    if ($manifests.Count -lt 3) { throw "Expected at least 3 runtime manifests, found $($manifests.Count)" }
    $sourceIds = @{}
    $referencedObjects = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($manifestFile in $manifests) {
        $manifest = Get-Content -LiteralPath $manifestFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
        $sourceIds[$manifest.source_id] = $true
        $runtimeSummary.manifests++
        $runtimeSummary.discovered_links += [int]$manifest.summary.discovered_links
        $runtimeSummary.errors += [int]$manifest.summary.failed
        foreach ($record in $manifest.records) {
            $objectPath = Join-Path $root ($record.stored_path -replace '/', [IO.Path]::DirectorySeparatorChar)
            if (-not (Test-Path -LiteralPath $objectPath -PathType Leaf)) { throw "Missing raw object: $objectPath" }
            [void]$referencedObjects.Add([IO.Path]::GetFullPath($objectPath))
            $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $objectPath).Hash.ToLowerInvariant()
            if ($actual -ne $record.sha256) { throw "Object hash mismatch: $objectPath" }
        }
    }
    $repositoryManifest = Get-Content -LiteralPath 'datasets/raw/sources/github/zhanwen-MathModel/source-manifest.json' -Raw -Encoding UTF8 | ConvertFrom-Json
    $sourceIds[$repositoryManifest.source_id] = $true
    $runtimeSummary.manifests++
    $runtimeSummary.full_problem_documents = [int]$repositoryManifest.summary.documents
    $runtimeSummary.full_problem_bytes = [long]$repositoryManifest.summary.bytes
    $archiveProblems = Get-Content -LiteralPath 'datasets/interim/full_problem_sources/problems.json' -Raw -Encoding UTF8 | ConvertFrom-Json
    $sourceIds['cumcm_official'] = $true
    $runtimeSummary.full_problem_pages = [int]$archiveProblems.stats.page_count
    $runtimeSummary.full_problem_text_blocks = [int]$archiveProblems.stats.text_block_count
    $runtimeSummary.full_problem_figures = [int]$archiveProblems.stats.figure_count
    $runtimeSummary.full_problem_downloads = [int]$archiveProblems.stats.attachment_count
    $runtimeSummary.full_problem_download_bytes = [long]$archiveProblems.stats.download_bytes
    foreach ($source in $registry.sources) {
        if (-not $sourceIds.ContainsKey($source.id)) { throw "No runtime manifest for source: $($source.id)" }
    }
    $objects = @(Get-ChildItem -LiteralPath 'datasets/raw/objects' -Recurse -File)
    $runtimeSummary.objects = $objects.Count
    $runtimeSummary.bytes = [long](($objects | Measure-Object Length -Sum).Sum)
    $runtimeSummary.orphan_objects = @($objects | Where-Object { -not $referencedObjects.Contains($_.FullName) }).Count
    if ($runtimeSummary.errors -ne 0) { throw "Runtime manifests report $($runtimeSummary.errors) failed fetches" }
    if ($runtimeSummary.orphan_objects -ne 0) { throw "Found $($runtimeSummary.orphan_objects) raw objects without a manifest reference" }
}

Write-Output ("DATA_COLLECTION_VERIFY_OK " + ($runtimeSummary | ConvertTo-Json -Compress))
Write-Output ("KNOWLEDGE_LIBRARY_VERIFY_OK " + ([ordered]@{ problems = $library.problems.Count; papers = $library.papers.Count; sources = $library.stats.source_count; version = $library.dataset_version } | ConvertTo-Json -Compress))
