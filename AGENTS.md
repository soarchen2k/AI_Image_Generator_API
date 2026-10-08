# ComfyUI 项目协作约定

本项目由 Codex 读取 API Format 工作流，并通过 `tools/comfyui_client.py` 调用 ComfyUI Classic API 或 Comfy API v2。

## 项目布局

- `workflows/txt2img/`：文生图工作流
- `workflows/img2img/`：图生图工作流
- `workflows/controlnet/`：ControlNet 工作流
- `workflows/inpaint/`：局部重绘工作流
- `workflows/upscale/`：放大与修复工作流
- `config/comfyui.example.env`：连接配置示例；复制为 `config/comfyui.env` 后填写本机值
- `tools/comfyui_client.py`：无需第三方 Python 套件的命令行客户端
- `tools/comfyui_image_api.py`：将文生图 API Format 工作流封装为本地 HTTP API

## 输出目录与主题子目录

1. 出图的根目录是**当前工作目录**（本次会话用户选定的工作目录，通常即本项目目录）。生成结果保存在它的主题子目录里：`<当前工作目录>\<主题>\`。
2. 每开始一个新主题，就在当前工作目录下新建一个主题子目录；同一主题的后续生成复用该目录，不要重复新建，也不要再套 `output`、时间戳等额外层级。
3. 主题目录名由主题内容概括而来，简短、可读，用中文或英文短语；不得含 Windows 保留字符 `\ / : * ? " < > |`、不得以空格结尾，也不得使用 `CON`、`NUL`、`PRN`、`AUX`、`COM1`–`LPT9` 等保留名。目录已存在就直接复用。
4. 用 `COMFYUI_OUTPUT_DIR` 指向主题目录：运行 `tools\comfyui_client.py` 或启动包装 API 之前先设置该变量，值为**绝对路径**，例如 `$env:COMFYUI_OUTPUT_DIR = "C:\Users\soarc\OneDrive\相册\AI 图片\<主题>"`。客户端与包装 API 都按这个变量决定下载和落盘位置；进程环境变量优先于 `config\comfyui.env`，所以可以和 `--env-file` 同时使用。
5. 只有用户明确指定其他位置时才改变这个约定。若成品在别处（例如 `Z:\TEMP`）生成，要把它移动到对应的主题子目录归档，不要留在临时目录。

## 工作流规则

1. 自动化调用使用 ComfyUI 的 **API Format** JSON。请在 ComfyUI 中选择 `File → Export Workflow (API)`，不要把普通画布保存格式当成 API 工作流提交。
2. 使用前先 `inspect` 工作流，确认节点 ID、节点类型、输入字段以及目标输出节点；只修改用户指定的输入。
3. 不要猜测节点 ID、模型文件名、输入图片路径或节点扩展是否已安装。信息不足时，先说明缺项。
4. 用户要求生成时才运行 `run`。提交前说明将使用的工作流、修改项、目标 ComfyUI 地址以及本次主题的输出目录；结果下载到当前工作目录下本次主题的子目录（见「输出目录与主题子目录」）。
5. 不要把 API token、凭据或私有配置写入工作流、命令输出或提交内容。生成结果只写入当前工作目录下的主题子目录，不要写进 `workflows/`、`tools/`、`config/` 等项目源文件目录，也不要留在临时目录。
6. 生图 HTTP API 默认只监听本机；绑定到局域网地址前必须配置独立的 `COMFYUI_IMAGE_API_TOKEN`。API 只接受工作流中已核对的输入，不记录提示词或图片文件名。

## 文生图 HTTP API 使用与启停

1. 只有用户明确要求生图时才调用 `POST /v1/images/generations`；检查、说明、整理工作流时不启动服务。提交前先说明工作流、请求参数和目标 ComfyUI 地址。此接口只使用 `workflows/txt2img/qwen-image-2.1.json`。
2. API 默认地址为 `http://127.0.0.1:8190`，路由为 `POST /v1/images/generations`。请求体为 JSON，必须带非空 `prompt`。未提供的其他字段保留工作流默认值；每个生图任务都要显式提供用户要求的提示词，不能使用工作流内保存的示例提示词。
3. 可调用字段：`negative_prompt`、`aspect_ratio`、`megapixels`、`multiple`、`resolution`、`n`、`batch_size`、`seed`、`steps`、`cfg`、`sampler_name`、`scheduler`、`denoise`、`unet_name`、`weight_dtype`、`clip_name`、`clip_type`、`clip_device`、`vae_name`、`filename_prefix`、`format`、`bit_depth`、`input_color_space`、`model`、`response_format`。`n` 与 `batch_size` 不能同时传；`seed=-1` 表示随机；`model` 固定为 `qwen-image-2.1`；`response_format` 为 `b64_json`（默认）或 `url`。`width` / `height` 是链接计算值，不可直接设置。选项和数值边界以 ComfyUI 当前节点定义为准，格式子选项必须匹配所选 `format`。
4. 服务默认读取 `config/comfyui.env`，在本机 `127.0.0.1:8190` 监听。仅在用户授权的生图任务开始时检查端口；端口空闲才启动。用 PowerShell `Start-Process` 启动时必须带 `-WindowStyle Hidden -PassThru`，把确切 PID 记录到 `Z:\TEMP\comfyui\image-api.pid`，等端口监听后才发送请求。详细命令见 `README.md`。
5. 一项任务内需要多次请求时复用该实例；所有请求完成后，从 PID 文件读取并核对进程命令行确实属于本项目 API，再停止它并删除该 PID 文件，确认进程退出和端口释放。前台启动则在原终端按 Ctrl+C。不得按 `python` 进程名批量结束进程；若端口原本已被占用，不重复启动，也不停止来源不明的实例。
6. API 生图是同步请求，默认最长等待 1800 秒；结果写入 `COMFYUI_OUTPUT_DIR` 指定的目录（生图前把它指向当前工作目录下本次主题的子目录），响应默认包含 base64 图片。504 表示等待超时且 ComfyUI 任务可能仍在运行；先检查 ComfyUI 队列，不得盲目重提。结束本次调用后关闭由本任务启动的包装 API；关闭包装 API 不会关闭 ComfyUI 主服务。
7. 默认仅本机可访问。非回环绑定必须配置独立的 `COMFYUI_IMAGE_API_TOKEN`；客户端使用 `Authorization: Bearer <token>`。绑定 `0.0.0.0` 并返回 URL 时还需配置 `COMFYUI_IMAGE_API_PUBLIC_URL`。不要输出或提交 token。

8. 数值边界以当前已检查的节点定义为准：megapixels 为 0.1–16；multiple 为 8–128、步长 4；resolution 为 0–4096、步长 32；n / batch_size 为 1–4096；seed 为 0–18446744073709551615（另接受 -1 随机）；steps 为 1–10000；cfg 为 0–100；denoise 为 0–1。采样器、调度器、模型加载选项、宽高比及格式子选项从 ComfyUI 节点定义动态获取；收到 400 时应按错误字段修正请求，不要猜选项。
9. 请求示例：{"prompt":"A watercolor lighthouse on a rocky coast","aspect_ratio":"3:2 (Photo)","megapixels":1,"steps":25,"seed":-1}。发送 Content-Type: application/json；配置了 COMFYUI_IMAGE_API_TOKEN 时加 Authorization: Bearer <token>。
10. 成功响应为 JSON，包含 created、model、实际 seed、可选任务 id 和 data 图片数组。默认每项返回 filename 与 b64_json；传 response_format=url 后每项返回 filename 与 url，用 GET /v1/images/{filename} 下载。GET /health 只检查包装服务进程，GET /v1/models 列出支持模型。
11. 常见状态码：400 请求无效，401 token 无效，422 ComfyUI 拒绝工作流，429 API 正在处理另一项生图，502 生成失败，503 ComfyUI 不可达，504 等待超时。504 后 ComfyUI 任务可能仍在队列中；检查队列且不要盲目重提。

## 临时文件

- `Z:\TEMP` 只作为临时文件的输出目录：一次性脚本、测试、调试文件、日志、PID 文件、中间产物和临时下载都放这里，用完即弃。
- 出图成品不留在 `Z:\TEMP`；生成结果按「输出目录与主题子目录」的约定落到当前工作目录的主题子目录。
- 不要在项目源文件目录（`workflows/`、`tools/`、`config/`）中创建缓存、临时下载、测试结果或运行日志。

## API 与配置

- `COMFYUI_URL` 是 Classic API 地址，默认 `http://192.168.3.75:8188`。
- `COMFYUI_V2_URL` 可指向 v2 网关；未设置时与 `COMFYUI_URL` 相同。自托管 Comfy API v2 代理示例地址是 `http://127.0.0.1:8189`。
- `COMFYUI_API_TOKEN` 可选；设置后以 Bearer token 发送。
- `--api auto` 先提交 v2；只有 v2 提交响应为 HTTP 404/405 时才回退 Classic。不得因认证失败、工作流校验失败、服务器错误或请求结果未知而自动重提。
- `.env` 本地配置文件不得提交；`config/comfyui.env` 已列入 `.gitignore`。

## 常用命令

```powershell
python tools\comfyui_client.py status
python tools\comfyui_client.py inspect workflows\txt2img\flux.json
$env:COMFYUI_OUTPUT_DIR = "<当前工作目录>\<主题>"
python tools\comfyui_client.py run workflows\txt2img\flux.json
```

需要加载本地配置文件时：

```powershell
python tools\comfyui_client.py --env-file config\comfyui.env status
$env:COMFYUI_OUTPUT_DIR = "<当前工作目录>\<主题>"
python tools\comfyui_client.py --env-file config\comfyui.env run workflows\txt2img\flux.json
```

`config\comfyui.env` 里若已设置 `COMFYUI_OUTPUT_DIR`，按主题覆盖时仍用上面的 `$env:COMFYUI_OUTPUT_DIR`：进程环境变量优先，不会被配置文件覆盖。

Git commit 信息使用中文标题和中文正文。
