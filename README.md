# Telegram Bot — Claude Code 远程控制

通过 Telegram 控制你的 Windows 电脑，使用 MiMo API 实现 AI 对话 + 工具调用。

## 功能

- 对话聊天（MiMo / Claude 兼容 API）
- 读取/写入/编辑文件
- 执行 Shell 命令
- 搜索文件（glob/grep）
- 截图并发送到手机
- 切换工作目录

## 安装

1. 安装 Python 3.10+
2. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```
3. 复制 `settings.example.json` 为 `settings.json`，填入你的密钥：
   ```json
   {
     "telegram_bot_token": "从 @BotFather 获取",
     "api_key": "你的 MiMo API Key"
   }
   ```

## 启动

双击 `启动Bot.bat`，或手动运行：

```bash
python telegram_bot.py
```

## Telegram 命令

| 命令 | 说明 |
|------|------|
| `/start` | 查看帮助 |
| `/clear` | 清除对话历史 |
| `/cd <路径>` | 切换工作目录 |
| `/status` | 查看当前状态 |

## 使用示例

- `打开记事本` — 通过 bash 启动程序
- `读取 C:\test.txt` — 读取文件内容
- `在桌面创建 hello.py` — 写入文件
- `截图` — 截取屏幕发送到手机
- `当前目录有哪些文件` — 列出目录

## 注意事项

- 需要代理访问 Telegram API（默认 `http://127.0.0.1:10090`）
- MiMo API 不支持图片分析，截图功能仅发送图片
- 不要让 AI 修改 `telegram_bot.py`，程序有保护机制
