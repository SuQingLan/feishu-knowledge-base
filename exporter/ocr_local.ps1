param([Parameter(Mandatory=$true)][string]$Queue, [Parameter(Mandatory=$true)][string]$Output)
# Local Windows OCR only; no upload and no original image modification.
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Await-Result($Operation, $Type) {
    $task = $asTask.MakeGenericMethod($Type).Invoke($null, @($Operation))
    $task.Wait()
    $task.Result
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('zh-Hans-CN'))
$items = Get-Content -LiteralPath $Queue -Raw -Encoding UTF8 | ConvertFrom-Json
$encoding = New-Object System.Text.UTF8Encoding($false)
$writer = New-Object System.IO.StreamWriter($Output, $false, $encoding)
try {
    foreach ($item in $items) {
        $bitmap = $null; $stream = $null
        try {
            $file = Await-Result ([Windows.Storage.StorageFile]::GetFileFromPathAsync($item.path)) ([Windows.Storage.StorageFile])
            $stream = Await-Result ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
            $decoder = Await-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
            $bitmap = Await-Result ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
            $result = Await-Result ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
            $row = @{message_id=$item.message_id; path=$item.path; status='ocr_done'; text=$result.Text; width=$bitmap.PixelWidth; height=$bitmap.PixelHeight}
        } catch {
            $row = @{message_id=$item.message_id; path=$item.path; status='ocr_failed'; error=$_.Exception.Message}
        } finally {
            if ($bitmap) {$bitmap.Dispose()}
            if ($stream) {$stream.Dispose()}
        }
        $writer.WriteLine(($row | ConvertTo-Json -Compress -Depth 5)); $writer.Flush()
    }
} finally {$writer.Dispose()}
