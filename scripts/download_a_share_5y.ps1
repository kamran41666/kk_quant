param(
    [string]$StartDate = "20210905",
    [string]$EndDate = "20260905",
    [string]$OutputDir = "data/market_a_share_5y",
    [int]$PageSize = 100,
    [int]$ThrottleLimit = 8,
    [int]$Retries = 3,
    [switch]$RefreshUniverse
)

$ErrorActionPreference = "Stop"
$root = [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $OutputDir))
$rawRoot = Join-Path $root "raw"
$universePath = Join-Path $root "universe.csv"
$manifestPath = Join-Path $root "manifest.json"
New-Item -ItemType Directory -Force -Path $root, $rawRoot | Out-Null

function Invoke-TextWithRetry {
    param([string]$Uri, [int]$Attempts = 3)
    $last = $null
    for ($try = 1; $try -le $Attempts; $try++) {
        try {
            return (Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 30).Content
        } catch {
            $last = $_
            if ($try -lt $Attempts) {
                Start-Sleep -Seconds ([Math]::Min(12, 2 * $try))
            }
        }
    }
    throw $last
}

function Get-Universe {
    if ((Test-Path -LiteralPath $universePath) -and -not $RefreshUniverse) {
        return @(Import-Csv -LiteralPath $universePath)
    }

    $base = "https://82.push2.eastmoney.com/api/qt/clist/get?pn={0}&pz={1}&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281&fltt=2&invt=2&fid=f12&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048&fields=f12,f13,f14,f26"
    $first = (Invoke-TextWithRetry -Uri ($base -f 1, $PageSize) -Attempts $Retries) | ConvertFrom-Json
    if ($null -eq $first.data -or $null -eq $first.data.total) {
        throw "East Money universe response has no total count"
    }
    $total = [int]$first.data.total
    $pages = [Math]::Ceiling($total / $PageSize)
    $rows = New-Object System.Collections.Generic.List[object]
    for ($page = 1; $page -le $pages; $page++) {
        if ($page -eq 1) {
            $payload = $first
        } else {
            $payload = (Invoke-TextWithRetry -Uri ($base -f $page, $PageSize) -Attempts $Retries) | ConvertFrom-Json
        }
        foreach ($item in @($payload.data.diff)) {
            $code = ([string]$item.f12).PadLeft(6, '0')
            if ($code -notmatch '^[0-9]{6}$') { continue }
            $market = if ($code.StartsWith('6') -or $code.StartsWith('9')) { 'SH' } elseif ($code.StartsWith('4') -or $code.StartsWith('8') -or $code.StartsWith('92')) { 'BJ' } else { 'SZ' }
            $rows.Add([pscustomobject]@{
                code = "$code.$market"
                symbol = $code
                market = if ($market -eq 'SH') { 1 } else { 0 }
                exchange = $market
                name = [string]$item.f14
                listed_date = ([string]$item.f26)
            })
        }
        Write-Host ("universe page {0}/{1}" -f $page, $pages)
    }
    $unique = @($rows | Sort-Object code -Unique)
    $unique | Export-Csv -LiteralPath $universePath -NoTypeInformation -Encoding UTF8
    return $unique
}

$universe = @(Get-Universe)
if ($universe.Count -eq 0) { throw "Universe is empty" }
Write-Host ("universe rows={0}" -f $universe.Count)

$modes = @(
    [pscustomobject]@{ Name = "none"; Fqt = 0 },
    [pscustomobject]@{ Name = "qfq"; Fqt = 1 },
    [pscustomobject]@{ Name = "hfq"; Fqt = 2 }
)

$jobs = foreach ($mode in $modes) {
    foreach ($security in $universe) {
        [pscustomobject]@{ mode = $mode.Name; fqt = $mode.Fqt; code = $security.code; symbol = $security.symbol; market = $security.market }
    }
}
$firstYear = [int]$StartDate.Substring(0, 4)
$lastYear = [int]$EndDate.Substring(0, 4)

$results = @($jobs | ForEach-Object -Parallel {
    $job = $_
    $modeDir = Join-Path $using:rawRoot $job.mode
    New-Item -ItemType Directory -Force -Path $modeDir | Out-Null
    $target = Join-Path $modeDir ("{0}.json" -f $job.code)
    if (Test-Path -LiteralPath $target) {
        [pscustomobject]@{ code = $job.code; mode = $job.mode; status = "skipped"; rows = -1; error = $null }
        return
    }

    $secid = "{0}.{1}" -f $job.market, $job.symbol
    $uri = "https://push2his.eastmoney.com/api/qt/stock/kline/get?fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&ut=7eea3edcaed734bea9cbfc24409ed989&klt=101&fqt={0}&secid={1}&beg={2}&end={3}" -f $job.fqt, $secid, $using:StartDate, $using:EndDate
    $last = $null
    for ($try = 1; $try -le $using:Retries; $try++) {
        try {
            $temp = "$target.$PID.$try.tmp"
            & curl.exe --noproxy "*" --silent --show-error --connect-timeout 5 --max-time 10 --output $temp $uri 2>$null
            if ($LASTEXITCODE -ne 0) { throw "curl exit code $LASTEXITCODE" }
            $content = Get-Content -LiteralPath $temp -Raw -Encoding UTF8
            $json = $content | ConvertFrom-Json
            $data = $json.data
            if ($null -eq $data) { throw "response data is null" }
            Move-Item -LiteralPath $temp -Destination $target -Force
            $rows = if ($null -eq $data.klines) { 0 } else { @($data.klines).Count }
            [pscustomobject]@{ code = $job.code; mode = $job.mode; status = "ok"; rows = $rows; error = $null }
            return
        } catch {
            $last = $_.Exception.Message
            if ($temp -and (Test-Path -LiteralPath $temp)) { Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue }
            if ($try -lt $using:Retries) { Start-Sleep -Seconds ([Math]::Min(12, 2 * $try)) }
        }
    }
    # Tencent's annual endpoint is a slower but independent fallback. It
    # returns up to 640 bars per request, so request one calendar year at a
    # time and normalize its 10-column rows to the East Money 11-column shape.
    try {
        $txSymbol = if ($job.market -eq 1) { "sh$($job.symbol)" } elseif ($job.code.EndsWith('.BJ')) { "bj$($job.symbol)" } else { "sz$($job.symbol)" }
        $txAdjust = if ($job.mode -eq "qfq") { "qfq" } elseif ($job.mode -eq "hfq") { "hfq" } else { "" }
        $normalized = New-Object System.Collections.Generic.List[string]
        for ($year = $using:firstYear; $year -le $using:lastYear; $year++) {
            $var = "kline_day$year"
            $param = "$txSymbol,day,$year-01-01,$($year + 1)-12-31,640,$txAdjust"
            $txUri = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?_var=$var&param=$param&r=0.8205512681390605"
            $txTemp = "$target.$PID.tx.$year.tmp"
            & curl.exe --noproxy "*" --silent --show-error --connect-timeout 5 --max-time 10 --output $txTemp $txUri 2>$null
            if ($LASTEXITCODE -ne 0) { throw "tencent curl exit code $LASTEXITCODE" }
            $txText = Get-Content -LiteralPath $txTemp -Raw -Encoding UTF8
            $jsonStart = $txText.IndexOf('{')
            if ($jsonStart -lt 0) { throw "tencent response is not JSON" }
            $txJson = $txText.Substring($jsonStart) | ConvertFrom-Json
            $txSecurity = $txJson.data.PSObject.Properties[$txSymbol].Value
            $key = if ($job.mode -eq "qfq") { "qfqday" } elseif ($job.mode -eq "hfq") { "hfqday" } else { "day" }
            foreach ($row in @($txSecurity.PSObject.Properties[$key].Value)) {
                $p = @($row)
                if ($p.Count -ge 9) {
                    $normalized.Add("$($p[0]),$($p[1]),$($p[2]),$($p[3]),$($p[4]),$($p[5]),$($p[8]),,,,$($p[7])")
                }
            }
            Remove-Item -LiteralPath $txTemp -Force -ErrorAction SilentlyContinue
        }
        if ($normalized.Count -eq 0) { throw "tencent returned no rows" }
        @{ rc = 0; source = "tencent"; data = @{ code = $job.symbol; klines = @($normalized) } } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $target -Encoding UTF8
        [pscustomobject]@{ code = $job.code; mode = $job.mode; status = "ok_fallback"; rows = $normalized.Count; error = $last }
        return
    } catch {
        [pscustomobject]@{ code = $job.code; mode = $job.mode; status = "error"; rows = 0; error = "$last; fallback=$($_.Exception.Message)" }
    }
} -ThrottleLimit $ThrottleLimit)

$failed = @($results | Where-Object status -eq "error")
$counts = $results | Group-Object status | ForEach-Object { [pscustomobject]@{ status = $_.Name; count = $_.Count } }
$manifest = [pscustomobject]@{
    dataset = "a_share_daily_5y"
    source = "eastmoney:push2his"
    source_url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    start_date = $StartDate
    end_date = $EndDate
    adjustment_modes = @("none", "qfq", "hfq")
    universe_count = $universe.Count
    request_count = $jobs.Count
    completed_at = (Get-Date).ToUniversalTime().ToString("o")
    status_counts = $counts
    failed = $failed
}
$manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
Write-Host ("completed requests={0}, failed={1}, manifest={2}" -f $results.Count, $failed.Count, $manifestPath)
if ($failed.Count -gt 0) { exit 2 }
