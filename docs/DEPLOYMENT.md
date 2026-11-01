# 机器人本机部署

本仓库运行在机器人算力板上，没有电脑操作页。第一次安装、配置和查看日志可以通过 SSH；安装后的待机服务可由机器人自身运行。

当前设备仍处于硬件禁用待机。用户尚未恢复现场硬件测试，不要为了展示而直接改开关。

## 环境

- Jetson Linux / aarch64，Python 3.10+，并与媒体扩展的 ABI 匹配。
- 设备配套的媒体 SDK，含 `build/mediacontrol_py*.so`、`config/dds.xml` 和所需动态库。
- 媒体扩展需支持只读路由查询、麦克风订阅、`unsubscribe_internal_audio_capture` 和外部音频播放。
- 拍照使用系统 FFmpeg 或 GStreamer 的 `v4l2src`、`videoconvert`、`jpegenc`、`fdsink`。安装器不打开设备，也不会停止占用相机的原厂服务。
- 机器人需要能访问配置的火山方舟与豆包服务。

厂家 SDK 与二进制库不随仓库分发。标准 SDK 若没有麦克风回调清理接口，程序会拒绝采集，需要匹配经过检查的媒体扩展。

## 安装

在仓库根目录执行：

```sh
python3 deploy/install_robot.py --check-only
python3 deploy/install_robot.py --sdk-root /absolute/path/to/matching-media-sdk
.venv/bin/python configure_robot.py
.venv/bin/python robot_service.py --check
```

安装只写当前项目目录的独立虚拟环境与默认 `robot.json`，不使用 sudo，不改原厂运行库。缺少 pip 时会下载固定版本 wheel 并校验 SHA256；也可以传入 `--pip-wheel` 与 `--pip-sha256` 使用事先准备的 wheel。

配置工具隐藏输入视觉 API Key、语音 App ID 与 Access Key，并保存为私有 `.settings.json`、`.voice.json`。留空会保留已存值。不要把这两个文件提交到 GitHub。

`robot.json` 默认硬件禁用，`mic_channel=0`。相机候选为 `/dev/video2`，这不是已验收的彩色节点，需要恢复现场测试后确认。安装器保留现有配置，遇到已启用硬件的配置会停止，避免不知情地修改运行中的设备。

## 用户服务

先保留 `hardware_enabled=false`。生成服务文件：

```sh
python3 deploy/install_robot.py --install-service
systemctl --user daemon-reload
systemctl --user enable --now bufu-robot.service
systemctl --user status bufu-robot.service
```

安装器只生成用户服务文件，不自行执行 systemctl。服务入口是 `robot_service.py`，启动后硬件禁用时只等待，不打开麦克风、相机或扬声器。若希望在没有登录会话时保持用户服务，需要在该设备允许的方式下启用对应用户的 linger；是否随重启恢复应单独验证。

已有旧版 `bumi-outfit.service` 时，迁移前先停止旧版，避免以后两套程序同时监听：

```sh
systemctl --user stop bumi-outfit.service
systemctl --user disable bumi-outfit.service
```

这两个名字均属于本项目；不要停止或替换厂商的控制服务。

## 现场启用与停止

当前项目曾有未归因的意外移动，现场测试仍暂停。恢复测试前需落实厂家认可的身体动作限制，并由现场人员看护。

随后先核实原厂语音的输入和回复已隔离、外部扬声器路径已就绪，再分别验证一张彩色图与一句固定台词。程序不自动切换原厂开关、不唤醒或恢复原厂 agent。`gestures_enabled=false` 时不会加载动作SDK或发送本应用的动作指令。

新增动作保持单独关闭；不能只打开 `hardware_enabled` 就当作动作通过验收。思考不是厂家的预设指令，必须绑定已验证的示教编号；挥手也需要单独确认其幅度和返回模式。详见[动作与锐评v3](GESTURES.md)。

通过这些验证并明确恢复完整测试后，才设置 `robot.json` 中的 `hardware_enabled=true`，显式重启本项目服务。当前待机进程不会仅因配置被改动而静默开始采集。

启用后的链路为：等待「锐评一下」→ 暂停收音 → 拍照、调用视觉与合成 → 播声 → 再监听。监听期间音频发送到配置的豆包账号；照片发送到配置的方舟账号，应用不保存原图或原始收音。

停止本项目：

```sh
systemctl --user stop bufu-robot.service
```

停止发送不保证厂商播放缓冲瞬间清空，也不等于整机急停；现场紧急处置应使用厂家认可的方法。

## 查来源

`data/robot-status.json` 记录阶段；`data/api-audit.jsonl` 把视觉、语音和播放提交以同一个 run_id 关联。服务日志使用 `journalctl --user -u bufu-robot.service` 查看。

状态、模型请求 ID 和音频摘要能帮助判断是否经过本项目 API 链路。`frames_sent` 只代表已提交帧，`audibility_verified=false` 不代表实际听到了声音。
