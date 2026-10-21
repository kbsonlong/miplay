# 小米 AI 音箱 2（L15A）原厂固件本地 MiPlay Sender

> **状态：V14 原始脚本已在真实 L15A（固件 `1.92.16`）验证正常出声。**
> `src/` 模块化重构通过离线单元测试与 Dry Run；**尚未单独经过音箱实机回归**，请优先使用原版脚本验证环境。

无需刷机 / Root / 云端 TTS 播放接口，通过局域网 MiPlay Legacy + WFD/RTSP + AAC/MPEG-TS/RTP，向原厂 L15A 推送本地音频文件。本仓库以研究复现和可审核的协议文档为目的，不是官方小米 SDK，也不是生产级播放器。

## 目录

```text
l15a-miplay/
├── README.md
├── LICENSE
├── ACKNOWLEDGEMENTS.md
├── THIRD-PARTY-NOTICES.md
├── pyproject.toml
├── Makefile
├── .gitignore
├── miplay_client_v14.py              # 成功播放的 V14 原件：不做任何行为改动
├── miplay_client.py                  # 模块化入口（需实机回归）
├── docs/
│   ├── protocol.md                  # 控制、RTSP、TimerService、媒体交互
│   ├── debugging.md                 # 排障命令 / V7-V14 过程
│   ├── media-clock.md               # V14 时间轴根因及编码公式
│   ├── references.md                # 开源源码定位与贡献致谢
│   └── architecture.md              # 模块、边界、工程化路线
├── src/
│   ├── __init__.py
│   ├── cli.py
│   ├── common.py
│   ├── control/
│   │   ├── __init__.py
│   │   ├── wire.py                  # $ + 9 字节控制帧 / legacy HMAC
│   │   └── client.py                # 握手、Open、SetMediaInfo、状态
│   ├── rtsp/
│   │   ├── __init__.py
│   │   ├── wire.py                  # RTSP 文本协议编解码
│   │   └── session.py               # 反向 TCP、RTSP、会话服务
│   ├── timer/
│   │   ├── __init__.py
│   │   └── service.py               # 40 字节 UDP 时钟同步
│   └── media/
│       ├── __init__.py
│       ├── mpegts.py                # PAT/PMT/PCR/PES/TS
│       └── audio.py                 # ADTS 帧流 → RTP → MiPlay TCP（文件/流式同管线）
├── src/tts/                         # 流式 TTS 服务（Agent 可调用）
│   ├── __init__.py
│   ├── engine.py                    # edge-tts MP3 流 → FFmpeg → AAC ADTS 帧流
│   ├── speaker.py                   # 长驻会话/心跳/播报队列/自动重建
│   └── server.py                    # stdlib HTTP API（python3 -m src.tts.server）
├── tools/
│   └── inspect_pcap.py              # 标准库 PCAP 重组 / RTP、PCR、PTS 检查
├── examples/
│   ├── capture.sh
│   └── play.sh
└── tests/
    ├── test_protocol.py
    └── test_pcap.py
```

## 已实测环境

- 音箱：小米 AI 音箱 2 `xiaomi.wifispeaker.l15a` / 原厂固件 `1.92.16`
- Mac：Apple Silicon / macOS；测试源 IP `192.168.3.18`
- 音箱测试 IP：`192.168.3.4`
- Legacy TCP `8899`；Mac 反向 TCP 监听 `7274`、UDP Timer `36524`
- AAC-LC、48 kHz、双声道、256 kb/s 目标码率，90 kHz RTP/PES/PCR 时间域

**注意：上述 IP、端口是已验证环境，不是所有 LAN 的通用配置。** V14 为避免误操作，把目标设备严格固定为 `192.168.3.4:8899`，必须显式传 `--confirm-host 192.168.3.4` 才会建立真实 TCP 连接。切换音箱或网段前请审查代码中的目标检查与网卡绑定，而不是直接复制参数尝试操作其他设备。

## 快速开始（优先原始 V14）

macOS：

```bash
brew install python ffmpeg
cd l15a-miplay
python3 -m py_compile miplay_client_v14.py
python3 miplay_client_v14.py                    # 默认 dry run，不建立 TCP
nc -vz -G 3 192.168.3.4 8899                  # 检查设备是否可达

```

将下面命令中的输入文件替换为自己 Mac 上真实存在的音频文件：

```bash
afplay /tmp/l15a-v7-test.aiff
python3 miplay_client_v14.py \
  --connect \
  --confirm-host 192.168.3.4 \
  --trigger-open \
  --audio-file /tmp/l15a-v7-test.aiff \
  --hold 60
```

上面第一条 `afplay` 仅验证本机音频内容，第二条才会主动控制音箱。依赖：Mac 已安装 FFmpeg，局域网能互联，TCP 7274 与 UDP 36524 未被占用，监听地址 `--bind-ip` 确实存在于 Mac 网卡。

### 模块化入口

功能按原始 V14 拆分，供开发和测试使用：

```bash
python3 miplay_client.py                       # dry run
python3 -m src.cli --help
python3 -m unittest discover -s tests -v
```

模块化实机测试可改为 `python3 miplay_client.py --connect ...`，但它还没有取得原始 V14 那样的真机出声验证，因此应先保存原始 V14 的成功日志和 PCAP 用于比较。

## 流式 TTS 服务（Agent 调用）

在文件播放管线之上提供常驻 HTTP 服务：文字请求 → edge-tts 云端流式合成 MP3 → FFmpeg 转码 AAC-LC 48 kHz → 与 V14 相同的 PES/TS/RTP 管线推到音箱。实测 12.35 s 语音**首帧延迟约 1.9 s**、总出流耗时 2.56 s（合成未结束即开始播报）。

依赖：Python ≥ 3.9 + `edge-tts`（`pip install edge-tts`，需能访问 `speech.platform.bing.com`）+ FFmpeg。

```bash
# 启动（默认监听 0.0.0.0:9901，音箱 192.168.3.4，回连 IP 192.168.3.18）
python3 -m src.tts.server --listen 0.0.0.0:9901 --speaker 192.168.3.4

# Agent 提交播报（异步，202 + job id；blocking=true 则等待播完）
curl -s -X POST http://127.0.0.1:9901/tts -H 'Content-Type: application/json' \
  -d '{"text":"主人你好，水温已达标。","voice":"zh-CN-XiaoxiaoNeural","rate":"+0%"}'

curl -s http://127.0.0.1:9901/jobs/<id>     # 查询状态
curl -s http://127.0.0.1:9901/health        # 会话/队列快照
curl -s http://127.0.0.1:9901/voices        # 可用音色
curl -s -X POST http://127.0.0.1:9901/stop  # 抢占当前播报并清空队列
```

行为要点：

- **会话复用**：第一次请求触发完整 V14 bootstrap（含 Open）；后续播报复用同一反向媒体通道 #3，RTP seq/TS 计数连续，PTS 重同步到墙钟（与 PCR 同域）。
- **自动重建**：控制连接断开或心跳连续失败时，下次请求自动重新 bootstrap；单次失败只报错该 job，服务存活。
- **抢占**：`/stop` 立即打断当前播报并清空队列。
- **心跳**：默认每 10 s 发 0x001a（`--heartbeat 0` 关闭）。周期心跳与连续播报的 PTS 重同步**尚未实机验证**，首次上机请对照 V14 基线。
- **安全**：仅限局域网使用；可加 `--token <secret>` 要求写操作携带 `X-Token`。

## 常用抓包和离线验证

```bash
IFACE=$(route -n get 192.168.3.4 | awk '/interface:/{print $2; exit}')
sudo tcpdump -i "$IFACE" -nn -s 0 -U -w l15a-v14-media.pcap \
  'host 192.168.3.4 and (tcp port 8899 or tcp port 7274 or udp port 36524)'

python3 tools/inspect_pcap.py l15a-v14-media.pcap
python3 tools/inspect_pcap.py l15a-v14-media.pcap --extract-ts /tmp/l15a.ts
ffprobe -v error -show_streams /tmp/l15a.ts
```

工具支持经典 Ethernet IPv4 PCAP（`tcpdump -w`）并进行有限的 TCP 序号重组；不支持 PCAPNG、802.11 Monitor 模式或 IP 分片。使用 `--extract-ts` 会写出真实音频内容，注意隐私。**源码仓库/ZIP 不包含用户的音频、抓包、原始设备账号或认证信息。**

## 2026-10-21 V14 成功会话统计

| 观测量 | 已确认值 |
| --- | ---: |
| RTP 数据帧 | 224 |
| RTP Marker=1 | 216 |
| MPEG-TS 包 | 1,004 |
| Audio PES / AAC AU | 216 |
| 自定义 RTP TCP 媒体数据 | 192,336 bytes |
| 首个 RTP 与 PES PTS | 3,767,220,630 |
| 首个 PCR base（33-bit） | 8,062,202,057 |
| RTSP TIME_OFFSET | 566,798,454,289 µs |
| 接收端 `first-audiopcm=1`、`state=2` | 已出现 |
| 最终播放 | **用户确认音箱正常出声** |

该统计取自成功验证时的抓包及运行日志；因含网络会话信息与音频内容，原始 PCAP 不随 ZIP 分发。

## 文档导航

- [协议全链路](docs/protocol.md)
- [完整调试及 V7～V14 复盘](docs/debugging.md)
- [时间戳/时钟根因](docs/media-clock.md)
- [架构、边界与工程化建议](docs/architecture.md)
- [参考项目与原作者致谢](docs/references.md)
- [致谢声明](ACKNOWLEDGEMENTS.md)
- [第三方许可](THIRD-PARTY-NOTICES.md)

## 已知限制

- 原始 V14 是有限生命周期的文件播放 POC。Open 前验证心跳，但还不支持稳定的周期控制心跳；RTSP 循环设有约 35 秒硬期限。
- 单次发送固定文件，不支持连续 TTS 队列或抢占；**流式 TTS 由 `src/tts/` 服务承担**（心跳周期、连续播报 PTS 重同步尚未实机验证）。
- `VIDEO_LATENCY` 被记录但未响应或解析统计；离线抓包无法直接证明真实扬声器输出延迟。
- 仅验证 L15A 指定固件及网络；不保证兼容所有型号/固件。
- 原始 V14 的 docstring/CLI 描述保留早期版本文案，**它们是过期注释**；请以实际代码和已实测结果为准。

## 免责声明与开源致谢

项目与小米公司和引用的开源作者不存在官方合作或背书关系。MiPlay 包含非公开协议细节；只应在自己拥有或获得授权的设备上使用，不要把局域网设备控制端口暴露到公网。

本项目尤其感谢 [SUlTlUS/MiPlayForWindows](https://github.com/SUlTlUS/MiPlayForWindows) 的真实设备抓包、音频打包器与媒体时钟研究。本仓库没有打包任何上游仓库源码；协议实现依据公开研究与 V14 真机实验编写。更多来源和许可边界详见 [references](docs/references.md) 与 [THIRD-PARTY-NOTICES](THIRD-PARTY-NOTICES.md)。
