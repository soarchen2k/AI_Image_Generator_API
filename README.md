# AI 图片与 ComfyUI

这里存放供 Codex 调用 ComfyUI API 的 API Format 工作流和客户端。生成结果默认写入运行命令时的**当前工作目录**；每开一个新主题，就在工作目录下建一个主题子目录 `<当前工作目录>\<主题>\`，并把 `COMFYUI_OUTPUT_DIR` 指向它。`Z:\TEMP` 只用于临时文件。

## 独立生图 API

项目提供一个同步 HTTP API，将请求参数写入已检查过的 Qwen Image 2.1 API Format 工作流，再通过 `tools/comfyui_client.py` 的提交、等待和下载逻辑调用 ComfyUI。服务默认只监听本机 `127.0.0.1:8190`，不需要额外 Python 套件。

在项目目录启动服务：

    python tools\comfyui_image_api.py

生图入口为 `POST http://127.0.0.1:8190/v1/images/generations`。它会等待任务完成后返回图片，默认使用 `b64_json`；可改为 `response_format: "url"`，再通过返回的本机图片地址读取文件。`GET /health` 查看进程状态，`GET /v1/models` 查看支持的模型。

请求必须提供非空 `prompt`。其余参数均可省略，省略时使用工作流当前值。可调用参数如下：

- 尺寸：`aspect_ratio`、`megapixels`、`multiple`、`resolution`
- 数量与采样：`n` 或 `batch_size`、`seed`、`steps`、`cfg`、`sampler_name`、`scheduler`、`denoise`
- 模型加载：`unet_name`、`weight_dtype`、`clip_name`、`clip_type`、`clip_device`、`vae_name`
- 提示词：`prompt`、`negative_prompt`
- 输出：`filename_prefix`、`format`、`bit_depth`、`input_color_space`
- 通用：`model`（固定为 `qwen-image-2.1`）、`response_format`（`b64_json` 或 `url`）

组合尺寸由宽高比、百万像素和倍数计算，不能直接设置 `width` / `height`。`n` 是 `batch_size` 的简写，两者不能同时提供。`seed: -1` 表示随机种子。ComfyUI 会提供模型选项、数值范围和输出格式的可选项，API 会据此校验请求；不同输出格式支持的位深和色彩空间也不同。

PowerShell 请求示例：

    $body = @{ prompt = "A watercolor lighthouse on a rocky coast"; aspect_ratio = "3:2 (Photo)"; megapixels = 1; steps = 25; seed = -1 } | ConvertTo-Json
    $result = Invoke-RestMethod -Uri "http://127.0.0.1:8190/v1/images/generations" -Method Post -ContentType "application/json" -Body $body
    $result.data[0].b64_json

响应中的 `data` 数组包含文件名及 base64 图片内容；设置 `response_format` 为 `url` 后，每项改为返回可读取图片的 URL。此 API 调用会实际提交生图任务，ComfyUI 结果写入 `COMFYUI_OUTPUT_DIR` 指定的目录；未设置时写入服务进程的当前工作目录。生图前请把它指向本次主题的子目录（见下节）。

服务配置可放在被 `.gitignore` 忽略的 `config/comfyui.env` 中。默认只绑定本机；需要局域网访问时，把 `COMFYUI_IMAGE_API_HOST` 设为可访问的本机 IPv4 地址或 `0.0.0.0`，并设置 `COMFYUI_IMAGE_API_TOKEN`。外部请求使用 `Authorization: Bearer <token>`。绑定 `0.0.0.0` 且需要 URL 响应时，还要设置 `COMFYUI_IMAGE_API_PUBLIC_URL` 为客户端可访问的 HTTP(S) 地址。不要提交真实 token。

### 参数详细说明

请求使用 JSON 对象，最多 1 MiB。`prompt` 是必填的非空字符串，确保不会误用工作流里保存的示例提示词。未提供的其他字段保留工作流默认值。除 `model` 和 `response_format` 外，所有参数对应当前 `workflows/txt2img/qwen-image-2.1.json` 中已核对的可编辑输入；链接输入和由尺寸节点计算出的 `width` / `height` 不接受直接覆盖。

| 参数 | 类型或取值 | 用途 |
| --- | --- | --- |
| `prompt` | 字符串，必填 | 正向提示词 |
| `negative_prompt` | 字符串 | 负向提示词 |
| `model` | `qwen-image-2.1` | 唯一模型标识；其他值返回 400 |
| `response_format` | `b64_json` 或 `url`，默认 `b64_json` | 选择 base64 图片或图片 URL |
| `aspect_ratio` | 当前 ComfyUI 节点提供的选项 | 宽高比 |
| `megapixels` | 数字；当前节点范围 0.1–16 | 总像素规模，单位百万像素 |
| `multiple` | 整数；当前节点范围 8–128、步长 4 | 生成宽高对齐倍数 |
| `resolution` | 整数；当前节点范围 0–4096、步长 32 | Qwen 文本编码节点分辨率 |
| `n` / `batch_size` | 整数；当前节点范围 1–4096 | 批量生成张数；两者是别名，不能同时传 |
| `seed` | 0 至 18446744073709551615 的整数；`-1` 表示随机 | 随机种子 |
| `steps` | 整数；当前节点范围 1–10000 | 采样步数 |
| `cfg` | 数字；当前节点范围 0–100 | CFG 引导强度 |
| `sampler_name` | 当前 ComfyUI 节点提供的选项 | 采样器 |
| `scheduler` | 当前 ComfyUI 节点提供的选项 | 调度器 |
| `denoise` | 0–1 的数字 | 去噪强度 |
| `unet_name`、`clip_name`、`vae_name` | 当前已安装模型选项 | 选择 UNet、CLIP 和 VAE |
| `weight_dtype`、`clip_type`、`clip_device` | 当前 ComfyUI 节点提供的选项 | 模型精度、CLIP 类型和设备 |
| `filename_prefix` | 1–128 字符的安全文件名前缀 | 输出文件名前缀；不能包含路径或 Windows 保留字符 |
| `format` | 当前 SaveImageAdvanced 节点提供的选项 | 输出格式 |
| `bit_depth`、`input_color_space` | 随 `format` 变化的选项 | 位深和输入色彩空间；格式不支持时会返回 400 |

实际可选模型、采样器、调度器、数值范围和格式子选项通过 ComfyUI 的节点定义获取，并缓存 5 分钟；选项随 ComfyUI 安装变化时以运行时定义为准。

### 请求与响应

PowerShell 示例：

    $body = @{ prompt = "A watercolor lighthouse on a rocky coast"; aspect_ratio = "3:2 (Photo)"; megapixels = 1; steps = 25; seed = -1; response_format = "b64_json" } | ConvertTo-Json
    $result = Invoke-RestMethod -Uri "http://127.0.0.1:8190/v1/images/generations" -Method Post -ContentType "application/json" -Body $body
    $result.data[0].b64_json

成功响应包含 `created`、`model`、实际使用的 `seed`、任务 `id`（ComfyUI 有返回时）和图片数组 `data`。`b64_json` 模式每项含 `filename` 与 base64 字符串；`url` 模式每项含 `filename` 与 `url`。URL 通过 `GET /v1/images/{filename}` 读取，仍遵守 API Bearer token 鉴权。

`GET /health` 只表示包装服务进程可响应；`GET /v1/models` 列出此服务支持的模型。生成请求是同步的，默认最多等待 1800 秒。常见错误：400 参数或 JSON 无效，401 缺少或错误的 API token，422 ComfyUI 拒绝工作流，429 另一项 API 生图正在运行，502 ComfyUI 执行失败，503 无法连接 ComfyUI，504 等待超时。收到 504 时任务可能仍在 ComfyUI 队列运行，先查看队列，不要自动重复提交。

### 服务启停与模型调用生命周期

只在用户明确要求生图、且已按项目规则说明工作流、参数修改、目标 ComfyUI 地址和本次主题的输出目录后启动包装 API。只做状态检查、工作流检查或文档工作时不要启动。服务默认读取 `config/comfyui.env`，只监听 `127.0.0.1:8190`。

模型启动 API 前先检查默认端口。若端口已占用，不要另启实例，也不要停止来源不明的进程：

    $listener = Get-NetTCPConnection -State Listen -LocalPort 8190 -ErrorAction SilentlyContinue
    if ($listener) { throw "Port 8190 is already in use; inspect its owner before proceeding." }

端口空闲时，在项目根目录运行以下 PowerShell 命令。它在隐藏窗口启动服务，并把 PID 记在项目外的临时目录，便于模型跨命令调用后准确关闭。启动前先把 `COMFYUI_OUTPUT_DIR` 设成本次主题的输出目录（绝对路径），`Start-Process` 启动的子进程会继承该变量；不设置则按服务进程的工作目录输出：

    $env:COMFYUI_OUTPUT_DIR = "<当前工作目录>\<主题>"
    $pidFile = "Z:\TEMP\comfyui\image-api.pid"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $pidFile) | Out-Null
    $apiProcess = Start-Process -FilePath (Get-Command python).Source -ArgumentList "tools\comfyui_image_api.py" -WorkingDirectory (Get-Location).Path -WindowStyle Hidden -PassThru
    $apiPid = $apiProcess.Id
    Set-Content -LiteralPath $pidFile -Value $apiPid -NoNewline
    Get-NetTCPConnection -State Listen -LocalPort 8190

等到本机端口开始监听后再发送生图请求。完成当前任务的所有 API 请求后，读取 PID 文件并确认它确实属于本项目的 API，再停止：

    $pidFile = "Z:\TEMP\comfyui\image-api.pid"
    $apiPid = [int](Get-Content -LiteralPath $pidFile)
    $apiProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $apiPid"
    if (-not $apiProcess -or $apiProcess.CommandLine -notmatch "comfyui_image_api\.py") { throw "PID does not match the ComfyUI image API." }
    Stop-Process -Id $apiPid
    Remove-Item -LiteralPath $pidFile
    Get-NetTCPConnection -State Listen -LocalPort 8190 -ErrorAction SilentlyContinue

确认该 PID 已退出、端口已释放。不要用按进程名结束所有 Python 的方式停止服务。若服务在前台运行，回到启动它的终端按 Ctrl+C。API 超时后不要盲目重提；记录 ComfyUI 队列状态，结束本次调用后仍要关闭本任务启动的包装服务。ComfyUI 主服务由用户管理，关闭包装 API 不会关闭 ComfyUI。

默认只允许本机访问。需要局域网访问时，在私有的 `config/comfyui.env` 配置 `COMFYUI_IMAGE_API_HOST`、独立的 `COMFYUI_IMAGE_API_TOKEN`；外部请求使用 `Authorization: Bearer <token>`。若绑定 `0.0.0.0` 并使用 URL 响应，还要配置客户端可访问的 `COMFYUI_IMAGE_API_PUBLIC_URL`。不要将 token 放入工作流、提示词、日志或提交。

## 快速开始

1. 将 `config/comfyui.example.env` 复制为 `config/comfyui.env`。
2. 按实际部署设置 `COMFYUI_URL`（当前 ComfyUI 主机示例为 `http://192.168.3.75:8188`）、可选的 `COMFYUI_V2_URL`、可选的 `COMFYUI_API_TOKEN` 和可选的 `COMFYUI_OUTPUT_DIR`（留空则用运行命令时的当前工作目录）。
3. 在 ComfyUI 中通过 `File → Export Workflow (API)` 导出 JSON，放入相应的 `workflows` 子目录。
4. 在项目目录运行：

```powershell
python tools\comfyui_client.py --env-file config\comfyui.env status
python tools\comfyui_client.py inspect workflows\txt2img\flux.json
$env:COMFYUI_OUTPUT_DIR = "<当前工作目录>\<主题>"
python tools\comfyui_client.py run workflows\txt2img\flux.json
```

`run` 支持 `--api auto|v2|classic`。`auto` 先尝试 v2；只有提交端点返回 404/405 才会改用 Classic API。请只在明确希望执行生成时运行该命令。

生成前先把 `COMFYUI_OUTPUT_DIR` 设为本次主题的输出目录（绝对路径），例如 `$env:COMFYUI_OUTPUT_DIR = "<当前工作目录>\<主题>"`；不设置则写入运行命令时的当前工作目录。

## 输出目录与临时文件

- 出图根目录是运行命令时的当前工作目录（通常就是本项目目录），生成结果放在它的主题子目录 `<当前工作目录>\<主题>\`。
- 每开一个新主题就新建一个主题子目录，同一主题复用；目录名简短可读，不含 Windows 保留字符与保留名。
- 输出位置优先级：进程环境变量 `COMFYUI_OUTPUT_DIR` > `config/comfyui.env` 中的同名项 > 运行时的当前工作目录。
- `Z:\TEMP` 只放临时文件：一次性脚本、测试、调试文件、日志、PID 文件、中间产物和临时下载。出图成品不留在这里。
- 不要在 `workflows/`、`tools/`、`config/` 等源文件目录里创建缓存、临时下载、测试结果或运行日志。

客户端只依赖 Python 标准库。`status`、`inspect`、`run` 均输出 JSON，便于查看或由其他工具处理。
