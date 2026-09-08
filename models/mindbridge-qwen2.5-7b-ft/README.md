# 本地模型目录

`Modelfile` 会随仓库版本化；GGUF、safetensors、PyTorch 权重和压缩包不会提交到 Git。

将微调后的 GGUF 文件放在本目录，并保证文件名与 `.env` 中的
`FINETUNED_MODEL_FILE` 一致，然后按项目根目录 `README.md` 的“接入和量化本地微调
GGUF 模型”章节创建 Ollama 模型。
