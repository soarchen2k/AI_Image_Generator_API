# 文生图

将从 ComfyUI 导出的 API Format JSON 放在此目录。运行前用 `inspect` 核对节点和输入，再通过 `run` 提交。

当前生图 HTTP API 使用 `qwen-image-2.1.json`。通过 `tools/comfyui_image_api.py` 启动后，请求 `POST /v1/images/generations`；API 会要求请求提供 `prompt`，其他参数省略时保留此工作流的默认值。
