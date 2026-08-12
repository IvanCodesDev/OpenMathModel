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
    'datasets/recipes/stage_mathorcup_archives.py',
    'datasets/recipes/discover_electric_cup.py',
    'datasets/recipes/stage_huashu_cup_archive.py',
    'datasets/recipes/image_guard.py',
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
python -m py_compile datasets/recipes/stage_mathorcup_archives.py
if ($LASTEXITCODE -ne 0) { throw 'MathorCup staging recipe compilation failed' }
python -m py_compile datasets/recipes/discover_electric_cup.py
if ($LASTEXITCODE -ne 0) { throw 'Electric Cup discovery recipe compilation failed' }
python -m py_compile datasets/recipes/stage_huashu_cup_archive.py
if ($LASTEXITCODE -ne 0) { throw 'Huashu Cup staging recipe compilation failed' }
python -m py_compile datasets/recipes/image_guard.py
if ($LASTEXITCODE -ne 0) { throw 'Image guard compilation failed' }
python datasets/recipes/collect_official_problems.py validate
if ($LASTEXITCODE -ne 0) { throw 'Source registry validation failed' }

Get-Content -LiteralPath 'datasets/catalog/source-registry.schema.json' -Raw -Encoding UTF8 | ConvertFrom-Json | Out-Null
Get-Content -LiteralPath 'datasets/catalog/source-snapshot.schema.json' -Raw -Encoding UTF8 | ConvertFrom-Json | Out-Null
Get-Content -LiteralPath 'datasets/catalog/knowledge-library.schema.json' -Raw -Encoding UTF8 | ConvertFrom-Json | Out-Null
$registry = Get-Content -LiteralPath 'datasets/catalog/source-registry.json' -Raw -Encoding UTF8 | ConvertFrom-Json
if ($registry.sources.Count -ne 8) { throw "Expected 8 registered sources, found $($registry.sources.Count)" }
if (($registry.sources.id | Sort-Object -Unique).Count -ne $registry.sources.Count) { throw 'Duplicate source id' }
foreach ($source in $registry.sources) {
    if ($source.license_record.PSObject.Properties.Name.Count -lt 8) { throw "Incomplete LicenseRecord: $($source.id)" }
}

$library = Get-Content -LiteralPath 'apps/web/src/data/knowledge-library.json' -Raw -Encoding UTF8 | ConvertFrom-Json
if ($library.stats.problem_count -ne $library.problems.Count) { throw 'Problem count does not match frontend data' }
if ($library.stats.paper_count -ne $library.papers.Count) { throw 'Paper count does not match frontend data' }
if ($library.problems.Count -ne 169) { throw "Expected 169 complete problems, found $($library.problems.Count)" }
if ($library.papers.Count -lt 906) { throw "Expected at least 906 structured paper records, found $($library.papers.Count)" }
if (($library.problems.id | Sort-Object -Unique).Count -ne $library.problems.Count) { throw 'Duplicate problem id' }
if (($library.papers.id | Sort-Object -Unique).Count -ne $library.papers.Count) { throw 'Duplicate paper id' }
$repositoryPapers = @($library.papers | Where-Object { $_.source_id -eq 'github_zhanwen_mathmodel' })
if ($repositoryPapers.Count -ne 685) { throw "Expected 685 repository paper records, found $($repositoryPapers.Count)" }
$fullProblems = @($library.problems | Where-Object { $_.content_status -eq 'complete' })
if ($fullProblems.Count -ne 169) { throw "Expected every frontend problem to be complete, found $($fullProblems.Count)" }
# Layout-aware extraction excludes page numbers, running heads and footer boilerplate,
# so the published character total sits below the raw text-layer length.
if (($fullProblems | Measure-Object content_character_count -Sum).Sum -lt 555000) { throw 'Complete problem text is unexpectedly short' }
if (($fullProblems | Measure-Object content_block_count -Sum).Sum -lt 4350) { throw 'Complete problem block count is unexpectedly low' }
foreach ($expected in @(@('comap_mcm_icm',44), @('apmcm_problems',32), @('cumcm_official',51), @('mathorcup_official',9), @('huashu_cup_official',21), @('github_zhanwen_mathmodel',12))) {
    $actual = @($fullProblems | Where-Object { $_.source_id -eq $expected[0] }).Count
    if ($actual -ne $expected[1]) { throw "Expected $($expected[1]) complete problems for $($expected[0]), found $actual" }
}
foreach ($year in 2023, 2024) {
    foreach ($letter in 'a','b','c','d','e','f') {
        if (-not ($fullProblems.id -contains "cpmcm-$year-$letter")) { throw "Missing complete problem cpmcm-$year-$letter" }
    }
}
$archiveProblems = @($fullProblems | Where-Object { $_.source_id -in @('comap_mcm_icm', 'apmcm_problems', 'cumcm_official', 'mathorcup_official', 'huashu_cup_official') })
if (@($archiveProblems | Where-Object { $_.content_format -ne 'structured_text' }).Count -ne 0) { throw 'PDF problems are not published as structured text' }
if (@($archiveProblems | Where-Object { @($_.content_blocks | Where-Object { $_.type -eq 'page' }).Count -gt 0 }).Count -ne 0) { throw 'PDF page screenshot blocks remain' }
# Paragraphs alone are not a stable measure of structure: text legitimately moves
# between block types as extraction improves. Folding Symbol-font bullets to "•"
# reclassifies their lines as list items, a recovered table grid absorbs cells that
# used to be loose paragraphs, and a rejoined stacked fraction merges three blocks
# into one. What must not happen is a statement flattening into one blob, so the
# gate counts every text-bearing block instead of only the paragraph ones.
$archiveParagraphs = @($archiveProblems.content_blocks | Where-Object { $_.type -eq 'paragraph' })
$archiveTextBlocks = @($archiveProblems.content_blocks | Where-Object { $_.type -in @('paragraph', 'list_item', 'table') })
if ($archiveTextBlocks.Count -lt 2580) { throw "PDF statements lost text structure, found only $($archiveTextBlocks.Count) paragraph/list/table blocks" }
if ($archiveParagraphs.Count -lt 1980) { throw "PDF statements lost paragraph structure, found only $($archiveParagraphs.Count) paragraphs" }
$runOnParagraphs = @($archiveParagraphs | Where-Object { $_.text.Length -gt 3000 })
if ($runOnParagraphs.Count -ne 0) { throw "Found $($runOnParagraphs.Count) unsegmented paragraphs over 3000 characters" }
if (@($archiveProblems | Where-Object { @($_.attachments | Where-Object { $_.kind -eq 'problem' -and $_.url -like '/problem-files/*/problem.pdf' }).Count -ne 1 }).Count -ne 0) { throw 'A PDF problem is missing its local original download' }
$cumcmProblems = @($fullProblems | Where-Object { $_.source_id -eq 'cumcm_official' })
if (@($cumcmProblems | Where-Object { $_.category -ne '国赛' }).Count -ne 0) { throw 'CUMCM category must be 国赛' }
# 2015-2025 must all be present, and with the letters the competition actually ran
# that year -- an E problem only appears from 2019 on. Counting alone would let a
# dropped legacy year hide behind a duplicate elsewhere.
foreach ($year in 2015..2025) {
    $letters = if ($year -le 2018) { @('a','b','c','d') } else { @('a','b','c','d','e') }
    foreach ($letter in $letters) {
        if (-not ($cumcmProblems.id -contains "cumcm-$year-$letter")) { throw "Missing complete problem cumcm-$year-$letter" }
    }
}
$apmcmProblems = @($fullProblems | Where-Object { $_.source_id -eq 'apmcm_problems' })
if (@($apmcmProblems | Where-Object { $_.category -ne '亚太赛' }).Count -ne 0) { throw 'APMCM category must be 亚太赛' }
# 2019, 2020 and 2025 are absent on purpose: those years publish statements only
# through saikr/baidu/modelers mirrors, so the official-domain-only rule leaves
# nothing to collect. 2017 and 2018 really did run just A and B.
$apmcmExpected = @(
    'apmcm-2015-a','apmcm-2015-b','apmcm-2015-c',
    'apmcm-2016-a','apmcm-2016-b','apmcm-2016-c',
    'apmcm-2017-a','apmcm-2017-b',
    'apmcm-2018-a','apmcm-2018-b',
    'apmcm-2021-a','apmcm-2021-b','apmcm-2021-c',
    'apmcm-2022-a','apmcm-2022-b','apmcm-2022-c','apmcm-2022-jan-d','apmcm-2022-jan-e',
    'apmcm-2023-a','apmcm-2023-b','apmcm-2023-c','apmcm-2023-wuyue',
    'apmcm-2024-a','apmcm-2024-b','apmcm-2024-c','apmcm-2024-d',
    'apmcm-2024-cn-a','apmcm-2024-cn-b','apmcm-2024-cn-c',
    'apmcm-2026-cn-a','apmcm-2026-cn-b','apmcm-2026-cn-c'
)
foreach ($id in $apmcmExpected) {
    if (-not ($apmcmProblems.id -contains $id)) { throw "Missing complete problem $id" }
}
if ($apmcmProblems.Count -ne $apmcmExpected.Count) { throw "Unexpected APMCM problem outside the pinned set: found $($apmcmProblems.Count), pinned $($apmcmExpected.Count)" }
$mathorcupProblems = @($fullProblems | Where-Object { $_.source_id -eq 'mathorcup_official' })
if (@($mathorcupProblems | Where-Object { $_.category -ne 'MathorCup' }).Count -ne 0) { throw 'MathorCup category must be MathorCup' }
$mathorcupExpected = @(
    'mathorcup-2023-a','mathorcup-2023-b','mathorcup-2023-c','mathorcup-2023-d',
    'mathorcup-2026-a','mathorcup-2026-b','mathorcup-2026-c','mathorcup-2026-d','mathorcup-2026-e'
)
foreach ($id in $mathorcupExpected) {
    if (-not ($mathorcupProblems.id -contains $id)) { throw "Missing complete problem $id" }
}
if ($mathorcupProblems.Count -ne $mathorcupExpected.Count) { throw "Unexpected MathorCup problem outside the pinned set" }
# The original 2026 D text said 满载率 where the correction says 满容率. The
# corrected official archive must win for both rendered text and attachments.
$mathorcupD = $mathorcupProblems | Where-Object { $_.id -eq 'mathorcup-2026-d' }
if (($mathorcupD.content_blocks.text -join "`n") -notmatch '货物满容率') { throw 'MathorCup 2026 D correction was not applied' }
$comapProblems = @($fullProblems | Where-Object { $_.source_id -eq 'comap_mcm_icm' })
if (@($comapProblems | Where-Object { $_.category -ne '美赛' }).Count -ne 0) { throw 'COMAP category must be 美赛' }
# 2018, 2019 and 2020 are absent on purpose: their official year indexes on
# contest.comap.org serve no PDFs at all, so there is nothing to collect under the
# official-domain-only rule. 2015 published only ICM C and D there.
$comapExpected = @(
    'comap-2015-icm-c','comap-2015-icm-d',
    'comap-2016-mcm-a','comap-2016-mcm-b','comap-2016-mcm-c',
    'comap-2016-icm-d','comap-2016-icm-e','comap-2016-icm-f',
    'comap-2017-mcm-a','comap-2017-mcm-b','comap-2017-mcm-c',
    'comap-2017-icm-d','comap-2017-icm-e','comap-2017-icm-f'
)
foreach ($id in $comapExpected) {
    if (-not ($comapProblems.id -contains $id)) { throw "Missing complete problem $id" }
}
# Bulk data sets over the mirror threshold are published as a link to the official
# page instead of a local copy, so there is no file to hash for those. What has to
# hold is that the link is absolute and on an official host -- the frontend keys
# off the leading "/" to decide download vs. outbound link, so a relative URL here
# would render as a download and 404.
$officialHosts = @('comap.org', 'mcm.edu.cn', 'apmcm.org', 'cmathc.org.cn', 'acge.org.cn', 'mathorcup.org', 'saikr.com')
foreach ($problem in $archiveProblems) {
    foreach ($attachment in $problem.attachments) {
        if ($attachment.external) {
            $uri = $null
            if (-not [Uri]::TryCreate($attachment.url, [UriKind]::Absolute, [ref]$uri)) { throw "Link-only attachment is not an absolute URL: $($attachment.url)" }
            if ($uri.Scheme -ne 'https') { throw "Link-only attachment is not https: $($attachment.url)" }
            # Not $host: that is a PowerShell automatic variable and read-only.
            $linkHost = $uri.Host.ToLowerInvariant()
            if (-not ($officialHosts | Where-Object { $linkHost -eq $_ -or $linkHost.EndsWith(".$_") })) { throw "Link-only attachment points off official domains: $($attachment.url)" }
            continue
        }
        $download = Join-Path $root ('apps/web/public' + ($attachment.url -replace '/', [IO.Path]::DirectorySeparatorChar))
        if (-not (Test-Path -LiteralPath $download -PathType Leaf)) { throw "Missing local problem download: $download" }
        if ((Get-Item -LiteralPath $download).Length -ne [long]$attachment.bytes) { throw "Problem download size mismatch: $download" }
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $download).Hash.ToLowerInvariant() -ne $attachment.sha256) { throw "Problem download hash mismatch: $download" }
    }
}
# CPMCM text is extracted from a community repository, but nothing the reader can
# click may point there. The published landing page is the organiser's own 开赛公告 on
# cpipc.acge.org.cn, and the mirrored .docx originals stay in source_documents -- an
# unrendered provenance field -- instead of being offered as downloads.
$cpmcmProblems = @($fullProblems | Where-Object { $_.source_id -eq 'github_zhanwen_mathmodel' })
if ($cpmcmProblems.Count -ne 12) { throw "Expected 12 CPMCM problems, found $($cpmcmProblems.Count)" }
foreach ($problem in $cpmcmProblems) {
    if ($problem.category -ne '研究生赛') { throw "CPMCM category must be 研究生赛: $($problem.id)" }
    $uri = $null
    if (-not [Uri]::TryCreate($problem.source_url, [UriKind]::Absolute, [ref]$uri)) { throw "CPMCM source_url is not absolute: $($problem.id)" }
    if ($uri.Host.ToLowerInvariant() -ne 'cpipc.acge.org.cn') { throw "CPMCM source_url is not on the official host: $($problem.id) -> $($problem.source_url)" }
    if (@($problem.attachments).Count -ne 0) { throw "CPMCM problems must publish no attachments: $($problem.id)" }
}
# Nothing rendered anywhere may carry a community-repository link. Paper records keep
# their repository URLs as collection metadata, but the frontend gates those behind
# isOfficialSourceUrl, so the invariant is asserted on the fields the UI turns into
# links without any further filtering.
foreach ($problem in $library.problems) {
    if ($problem.source_url -match 'github\.com|githubusercontent\.com') { throw "Rendered problem source_url points at a community repository: $($problem.id)" }
    foreach ($attachment in $problem.attachments) {
        if ($attachment.url -match 'github\.com|githubusercontent\.com') { throw "Rendered attachment points at a community repository: $($problem.id)" }
    }
}

$frontendSource = Get-Content -LiteralPath 'apps/web/src/legacy/openmathmodel-ui.ts' -Raw -Encoding UTF8
if ($frontendSource -notmatch 'import\("\.\./data/knowledge-library\.json\?url"\)') { throw 'Frontend does not lazily load the structured knowledge library' }
if ($frontendSource -notmatch 'completeProblemMarkup' -or $frontendSource -notmatch 'problem-full-content') { throw 'Frontend does not render ordered complete problem blocks' }
if ($frontendSource -match 'problem-completeness-bar' -or $frontendSource -match 'problem-source-ledger' -or $frontendSource -match '数据版本　') { throw 'Removed problem metadata boxes or footer labels are still present' }
if ($frontendSource -match 'block\.type === "page"' -or $frontendSource -match 'problem-page-sheet') { throw 'Frontend still contains page screenshot rendering' }
if ($frontendSource -notmatch 'problem-download-link' -or $frontendSource -notmatch 'local \? "download"') { throw 'Frontend does not expose local problem downloads' }
# The attachment renderer must filter as well as the source button does: only a local
# mirror under /problem-files or an organiser-domain original may become a link. Without
# this filter a community-repository .docx would render as a live download.
if ($frontendSource -notmatch 'linkableAttachments') { throw 'Frontend attachment renderer is not filtered by host' }
if ($frontendSource -notmatch 'OFFICIAL_SOURCE_HOSTS' -or $frontendSource -notmatch '"acge\.org\.cn"' -or $frontendSource -notmatch '"mathorcup\.org"' -or $frontendSource -notmatch '"saikr\.com"') { throw 'Frontend allowlist does not cover all organiser domains' }
if ($frontendSource -match 'OFFICIAL_SOURCE_HOSTS\s*=\s*\[[^\]]*github') { throw 'Frontend allowlist admits a community repository host' }
# Both ingesters open images (Pillow), and the archive one also parses PDFs, so
# both need the bundled interpreter -- the ambient `python` on this workspace has
# neither Pillow nor pdfplumber. The py_compile calls above are fine on ambient
# Python because compiling a module does not execute its imports.
$bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (-not (Test-Path -LiteralPath $bundledPython -PathType Leaf)) { throw "Bundled workspace Python not found: $bundledPython" }
& $bundledPython datasets/recipes/ingest_mathmodel_full_problems.py verify
if ($LASTEXITCODE -ne 0) { throw 'Complete MathModel problem verification failed' }
& $bundledPython datasets/recipes/stage_mathorcup_archives.py verify
if ($LASTEXITCODE -ne 0) { throw 'MathorCup archive staging verification failed' }
& $bundledPython datasets/recipes/discover_electric_cup.py verify
if ($LASTEXITCODE -ne 0) { throw 'Electric Cup discovery verification failed' }
& $bundledPython datasets/recipes/stage_huashu_cup_archive.py verify
if ($LASTEXITCODE -ne 0) { throw 'Huashu Cup archive staging verification failed' }
& $bundledPython datasets/recipes/ingest_full_problem_archives.py verify
if ($LASTEXITCODE -ne 0) { throw 'Complete COMAP/APMCM/CUMCM/MathorCup problem verification failed' }
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
    $sourceIds['electric_cup_official'] = $true
    $sourceIds['huashu_cup_official'] = $true
    $runtimeSummary.manifests++
    $runtimeSummary.manifests++
    $runtimeSummary.full_problem_pages = [int]$archiveProblems.stats.page_count
    $runtimeSummary.full_problem_text_blocks = [int]$archiveProblems.stats.text_block_count
    $runtimeSummary.full_problem_figures = [int]$archiveProblems.stats.figure_count
    $runtimeSummary.full_problem_downloads = [int]$archiveProblems.stats.attachment_count
    $runtimeSummary.full_problem_download_bytes = [long]$archiveProblems.stats.download_bytes
    foreach ($source in $registry.sources | Where-Object { $_.id -notin @('mathorcup_official', 'electric_cup_official', 'huashu_cup_official') }) {
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
