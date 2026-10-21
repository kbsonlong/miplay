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
- 新增 11 项测试（含可选网络链路测试 `MIPLAY_TTS_LIVE=1`），全套 28 项通过。

### 2026-10-21 streaming TTS 实机验证（L15A, 固件 1.92.16）

真实音箱上逐项证实：

- 引导 + 播报：`first-audiopcm=1` 与 `state=2` 就绪门控按 V14 语义命中，首句 162 帧 `done`（5.4 s 含引导）。
- 背靠背会话复用：第二句免引导直接出流（4.1 s），RTP seq 连续、PTS 墙钟重同步正常出声。
- 周期心跳：每 10 s 发 0x001a，音箱逐条回 0x001b（观测 seq 16–21）。
- **设备拆通道行为**：播报结束进入 `state=3` 后，音箱会关闭反向媒体通道 #3（观测窗口：空闲 23 s 未拆、43 s 已拆）。服务检测到 `media_seen 且 #3 消失` 后自动重新 SetPlaySource+Open，第四句在 50 s 空闲后仍正常 `done`（日志含完整 `re-bootstrapping` 路径）。
- 抢占：`/stop` 在帧流动中约 0.1 s 内生效，job `stopped`（106 帧已播），worker 立即释放。
- 修复两处验证中发现的缺陷：① TTS 云端挂起会永久阻塞 worker → 加 select 停止唤醒 + 30 s 停滞看门狗（部分音频照常flush）；② 通道丢失直接报错 → 改为 0 帧时自动重建会话重试一次（已播过半则不重放，避免半句重复）。

### 2026-10-21 播报完整性修复（ffmpeg 低延迟参数吞开头，已实机确认）

- **现象**：短句开头丢字——"测试播报内容完整性"只听到"播报内容完整性"。
- **排查链**：TTS 源音频完整（silencedetect：0.187 s 起即语音）→ 加静音预铺（priming）+ 就绪双门控，仍丢 → 加提示音（chime）A/B 实测：**提示音完整播出但语音仍从"四"开始** → 证明丢弃不在接收端 → 离线复现：同一 MP3 经本服务的 ffmpeg 命令转码后开头少 0.68 s。
- **根因**：`-fflags nobuffer -flags low_delay -probesize 4096 -analyzeduration 0` 在管道输入下使 MP3 demuxer 不可逆丢弃探测期数据，静默吞掉开头 0.4–1.0 s。
- **修复**：删除全部输入低延迟参数。首帧延迟 1.92 s→2.77 s，由静音预铺吸收（对用户不可感）。离线验证：输出 2.67 s ≈ 源 2.64 s；实机复验用户确认完整播报。
- **顺带新增**：
  - `send_with_priming`：每条播报先按实时节奏铺静音，双门控（接收端就绪 + TTS 首帧在手）后再出正音，杜绝饿死与就绪前丢弃；
  - 注意提示音 chime（0.8 s 双音，服务级 `--chime/--no-chime`，请求级 `"chime": false`）；
  - 数数文本 + 用户听感 A/B 的排障方法（从第几个数字开始 = 精确丢弃窗口）。
- 测试 32 项全绿。
