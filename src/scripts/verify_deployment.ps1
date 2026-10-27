param(
    [Parameter(Mandatory)]
    [uri] $BaseUrl,
    [ValidateRange(1, 600)]
    [int] $TimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'
if ($BaseUrl.Scheme -notin @('http', 'https')) {
    throw 'BaseUrl must use HTTP or HTTPS.'
}
$base = $BaseUrl.GetLeftPart([System.UriPartial]::Authority)

$page = Invoke-WebRequest "$base/?verify=$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())" -TimeoutSec $TimeoutSeconds
if ($page.StatusCode -ne 200 -or
    $page.Headers['Content-Type'] -notmatch 'text/html' -or
    $page.Content -match 'Your container app is running with a Hello World image' -or
    $page.Content -notmatch 'id="messageInput"' -or
    $page.Content -notmatch 'new WebSocket') {
    throw 'The public root does not serve the shopping chat UI.'
}

$healthResponse = Invoke-WebRequest "$base/health" -TimeoutSec $TimeoutSeconds
if ($healthResponse.StatusCode -ne 200 -or
    $healthResponse.Headers['Content-Type'] -notmatch 'application/json' -or
    [string]::IsNullOrWhiteSpace($healthResponse.Content)) {
    throw '/health must return a non-empty JSON response, not just HTTP 200.'
}
$health = $healthResponse.Content | ConvertFrom-Json
if ($health.status -ne 'healthy' -or
    -not $health.timestamp -or
    $health.environment_vars_configured.phi_4_endpoint -ne $true -or
    $health.environment_vars_configured.foundry_endpoint -ne $true -or
    $health.environment_vars_configured.gpt_endpoint -ne $true) {
    throw '/health does not report a healthy, configured shopping app.'
}

$wsUri = [UriBuilder]::new("$base/ws")
$wsUri.Scheme = if ($BaseUrl.Scheme -eq 'https') { 'wss' } else { 'ws' }
$socket = [System.Net.WebSockets.ClientWebSocket]::new()
$timeout = [System.Threading.CancellationTokenSource]::new($TimeoutSeconds * 1000)
try {
    $socket.ConnectAsync($wsUri.Uri, $timeout.Token).GetAwaiter().GetResult() | Out-Null
    $payload = @{
        message = 'Recommend lavender paint for a bedroom.'
        conversation_history = ''
        cart = @()
        has_image = $false
        image_url = ''
    } | ConvertTo-Json -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
    $socket.SendAsync(
        [ArraySegment[byte]]::new($bytes),
        [System.Net.WebSockets.WebSocketMessageType]::Text,
        $true,
        $timeout.Token
    ).GetAwaiter().GetResult() | Out-Null

    $buffer = [byte[]]::new(65536)
    $message = [IO.MemoryStream]::new()
    try {
        do {
            $frame = $socket.ReceiveAsync(
                [ArraySegment[byte]]::new($buffer), $timeout.Token
            ).GetAwaiter().GetResult()
            if ($frame.MessageType -ne [System.Net.WebSockets.WebSocketMessageType]::Text) {
                throw 'The shopping WebSocket closed or returned a non-text response.'
            }
            $message.Write($buffer, 0, $frame.Count)
            if ($message.Length -gt 1048576) {
                throw 'The shopping response exceeded 1 MiB.'
            }
        } while (-not $frame.EndOfMessage)
        $reply = [Text.Encoding]::UTF8.GetString($message.ToArray()) | ConvertFrom-Json
    } finally {
        $message.Dispose()
    }

    if ($reply.agent -ne 'interior_designer' -or
        [string]::IsNullOrWhiteSpace($reply.answer) -or
        -not $reply.products) {
        throw 'The shopping query did not return an interior designer answer with catalog products.'
    }
    $products = @($reply.products | ConvertFrom-Json)
    if ($products.Count -eq 0 -or
        @($products | Where-Object { -not $_.id -or -not $_.name }).Count -gt 0) {
        throw 'The shopping query returned an empty or invalid product list.'
    }
    [pscustomobject]@{
        Url = "$base/"
        HtmlCharacters = $page.Content.Length
        Health = $health.status
        Agent = $reply.agent
        Products = @($products | ForEach-Object { $_.name })
        Answer = $reply.answer
    }
} finally {
    $socket.Dispose()
    $timeout.Dispose()
}
