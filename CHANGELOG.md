# Changelog / 实验修复历程

当前 `v14` 是有声验证基准，下面是按真实调试记录整理的技术历程，并非正式语义化版本发布记录。

- V1-V3：Legacy TCP8899 / Challenge / HMAC / Basic device identity。
- V4-V6：WFD Open、反向 RTSP/TCP、UDP TimerService 探测。
- V7：AAC/ADTS → PES/MPEG-TS → RTP；发送成功但实际静音。
- V8：避免对 `VIDEO_LATENCY` 返回 501，仍静音。
- V9：PAT program_number 与 PES header 填充修正，仍静音。
- V10：PMT PCR PID 从错误值改为 0x1000，并纠正 PSI/RTP Marker，仍静音。
- V11：补 SetMediaInfo，能收到 `first-audiopcm=1` 和 `state=2`，仍静音。
- V12：研究播放就绪事件门控（没有独立实机成功验证）。
- V13：PCR 改为实时 Monotonic，启动 10 个 AU 后再等 PCM readiness，仍静音。
- **V14**：初始 PTS/RTP 由 RTSP TIME_OFFSET 的单调时间产生，PCR 保持实时同源采样，**实机成功出声**。

### 2026-10-21 project packaging

- 保留未经更改的 V14 原始文件。
- 拆分 `src/control`、`src/rtsp`、`src/timer`、`src/media` 并提供模块化入口。
- 新增 `tools/inspect_pcap.py`，在成功 PCAP 上统计 224 RTP 帧、216 AU、1004 TS、192336B。
- 新增 17 项离线测试，包括模块化与原版媒体输出 byte-for-byte parity。
- 增加中文完整协议、时钟、调试、架构和开源源码引用文档。

### 2026-10-21 streaming TTS service

- `src/media/audio.py` 重构：提取 `iter_adts(read)` 与 `send_adts_stream(frames, ...)`，发送循环接受任意 ADTS 帧迭代器；文件路径模式行为保持不变（原 17 项测试全绿）。
- 新增 `RtpStreamState`：跨播报复用 RTP seq / TS 连续性计数；首条播报 PTS 仍按 V14 由 TIME_OFFSET 推导，后续播报 PTS 重同步到墙钟（与 PCR 同为 90 kHz monotonic 域，避免空闲后 PTS 落后真实时间）。
- 新增 `src/tts/engine.py`：edge-tts 流式 MP3 → FFmpeg（低延迟参数）→ AAC-LC 48 kHz 立体声 ADTS 帧流；实测 12.35 s 语音首帧延迟 1.92 s、总耗时 2.56 s（流式比 0.21）。
- 新增 `src/tts/speaker.py`：长驻会话管理 —— V14 bootstrap 时序忠实转录、控制连接周期心跳（可配置/可关）、反向媒体通道 #3 复用、会话死亡自动重新 bootstrap、播报队列与抢占 stop。
- 新增 `src/tts/server.py`：stdlib HTTP API（`POST /tts`、`/stop`、`GET /health`、`/voices`、`/jobs/<id>`，可选 X-Token），供 Agent/curl 调用。
- 新增 7 项测试（含可选网络链路测试 `MIPLAY_TTS_LIVE=1`），全套 24 项通过。
- 已知未实机验证项：长驻心跳周期、连续多播报 PTS 重同步、HTTP 服务的自动重建路径——首次实机验证前请保留 V14 基线对照。
