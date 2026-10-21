# 项目架构与生产化演进

## 1. 为什么保留两个运行入口

- `miplay_client_v14.py`：逐字节保留用户真机播放验证通过的 V14 源文件，用作黄金基准及排查回归参考。文件顶部写着早期 v5 文案是历史残留，实际代码实现已经能发送音频；不要为清理注释而改动这份原件。
- `miplay_client.py`：调用模块化 `src.cli.main()`，将原 V14 的逻辑按职责组织。已通过 Dry Run、纯 Python 离线测试和源码语法检查；**暂未真实 L15A 回归验证**，因此不能等同于第二次成功实测。

## 2. 各模块说明

```text
cli.py ──> control/client.py (Legacy bootstrapping + metadata)
                         └──> rtsp/session.py (Services, reverse TCP,
                                              SETUP, PLAY, TIME_OFFSET)
                                    ├──> rtsp/wire.py
                                    ├──> timer/service.py
                                    └──> media/audio.py
                                             └──> media/mpegts.py
```

| 模块 | 核心导出 | 测试边界 |
|---|---|---|
| `src/control/wire.py` | `frame`, `recv_frame`, `legacy_hmac` | BE16/BE32、HMAC 输出 |
| `src/control/client.py` | `run`, `monitor_post_open_control` | Challenge/ACK、Open、状态通知 |
| `src/rtsp/wire.py` | `rtsp`, `parse_rtsp` | header / Content-Length / 增量解析 |
| `src/rtsp/session.py` | `Services` | listener、反向连接分派、RTSP 状态 |
| `src/timer/service.py` | `reply_timer`, `run_udp_loop` | 40B layout / LE64 时间值 |
| `src/media/mpegts.py` | `make_pat`, `make_pmt`, `pes_to_ts`, `make_pcr_packet` | CRC、PID、188B TS |
| `src/media/audio.py` | `adts_frames`, `send_adts_as_miplay_rtp` | FFmpeg ADTS、Marker、RTP/PCR/PTS |
| `tools/inspect_pcap.py` | `inspect` | 经典 PCAP TCP 重组、RTP/PTS/PCR 统计 |

## 3. 安全边界

发送端需要控制音箱的播放状态。默认 Dry Run；真实连接要求 `--connect` 与精确 `--confirm-host`；Open 需要再加 `--trigger-open`。不要将 8899 公开到外网，也不要在无权限的设备上试验。捕获到的设备账号、密钥、Wi-Fi 信息及音频均不打包。

## 4. 建议下一阶段（非当前交付功能）

```text
ESP32-S3 / Home Assistant / AI Agent
            |
         HTTP API
            v
  Local MiPlay Gateway
    ├── Device Registry
    ├── Control Session + Heartbeat
    ├── RTSP State Machine + TimerService
    ├── Shared Monotonic MediaClock
    ├── PCM/TTS → AAC Encoder
    ├── MPEG-TS / RTP sender
    └── Per-device Playback Queue / Cancellation
            |
           L15A
```

优先级建议：

1. 保留 V14 作为回归基准，新增一次真机测试来确认模块化入口与 V14 行为一致。
2. 心跳与状态监控：可重复连接、断线检测、RTSP Keepalive、TEARDOWN、取消音频发送。
3. 长音频边界：改造固定 35s RTSP 主循环及控制超时，长时间 PCR/PTS wrap、单调时钟漂移。
4. Session 统一 Clock：TIME_OFFSET / RTP / PTS / PCR 使用统一 source-side 时钟，避免流式 TTS 重置时间戳。
5. 播放队列与打断：严格串行分发每设备音频，支持一条会话内多段 TTS。
6. 对接 HTTP API 和 ESP32/Home Assistant 后补充认证、速率限制、资源隔离；避免让服务对公网开放。

这些能力在当前 ZIP 中 **未伪装为已实现**。
