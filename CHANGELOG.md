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
