# V14 真正出声的关键：TIME_OFFSET / RTP Timestamp / PTS / PCR 时间轴

## 1. 为什么 TCP / AAC 正确还是无声？

V7 到 V13 连续出现：Legacy 8899 握手成功、RTSP PLAY 成功、媒体字节完整发出、MPEG-TS 能离线解码；到 V11/V13 时还能收到 `first-audiopcm=1` 和 `state=2`，但实际扬声器仍不发声。

V13 首个 RTP Timestamp / PES PTS 从 `0` 开始，而 PCR 使用了 Mac `time.monotonic_ns()` 的实时数值；不同时间基准被混用，造成媒体计划呈现时间与 PCR 时钟严重脱节。仅纠正 PAT/PMT、PES 填充、RTP Marker 或播放元数据仍无法修复。

V14 改为先记录 RTSP `TIME_OFFSET` 的本机单调微秒值，再从**相同的时间域**初始化 PTS/RTP，PCR 继续按实时单调时钟运行。L15A 首次正常播放。这是当前最有力的根因证据；由于不是严密受控的单变量消融实验，最终复盘将其表述为「高置信度根因」，不是对所有接收端的数学证明。

## 2. 四个量的关系

- `TIME_OFFSET`：RTSP 中的时间偏移字段，单位微秒；V14 在 PLAY 后读取本机单调时钟，发送到 L15A。
- `RTP Timestamp`：无符号 32-bit，90 kHz 计数；AAC 每 AU 增加 `1920`。
- `PES PTS`：MPEG-TS 中音频媒体的呈现时间。编码器写入 33-bit PTS 结构，但 V14 实际源变量仅保存低 32-bit（保留的原样算法，不代表最优通用写法）。
- `PCR Base`：33-bit，90 kHz；PCR Extension 为 27 MHz 计数的 1/300 尾数，范围 0..299。

公式：

```python
T_offset_us = time.monotonic_ns() // 1000
pts0 = (T_offset_us * 90_000 // 1_000_000) & 0xffff_ffff
pts_i = (pts0 + i * 1920) & 0xffff_ffff

pcr27 = (time.monotonic_ns() // 1000) * 27
pcr_base = (pcr27 // 300) & ((1 << 33) - 1)
pcr_ext = pcr27 % 300
```

其中 `1024 / 48000 * 90000 = 1920`。注意 PCR 由独立的实际时间采样获得，不需要与 PTS 位级完全相等。初始 `TIME_OFFSET` 到第一帧还有 RTSP/控制消息与编码延迟，产生自然的时间偏移。

## 3. 实测对比：V13 无声 / V14 正常出声

| 观察 | V13 | V14 |
|---|---|---|
| Legacy + RTSP | 成功 | 成功 |
| SetMediaInfo / 2 项 readiness | 成功 | 成功 |
| 216 个 AAC AU | 已发送 | 已发送 |
| PCR 生成 | Mac Monotonic | Mac Monotonic |
| RTP/PES PTS 起点 | `0` | 取自 RTSP `TIME_OFFSET` |
| 结果 | 音箱无声 | **实机正常播放** |

V14 成功 PCAP 中记录：

```text
RTSP TIME_OFFSET  566798454289 us
First RTP TS      3767220630 (32-bit)
First PES PTS     3767220630
First PCR Base    8062202057 (33-bit)
PCR Base % 2^32   3767234761
Difference        14131 ticks = 157.01 ms
```

这个差值包含媒体包 PCR 与 RTSP TIME_OFFSET 采样的时间间隔，不能当成音箱设备的端到端播放时延。在 90 kHz 域下 RTP/PTS 与 PCR 至少已经能映射到同一个运行时钟。

## 4. 测试和保护措施

```bash
python3 -m unittest discover -s tests -v
python3 tools/inspect_pcap.py l15a-v14-media.pcap
```

在源码迁移至 Go、Rust、ESP32-S3 或实现流式 TTS 时，请保留以下不变量：

1. 在 TIME_OFFSET 发送时记录 **同一台发送端** 的单调时间源，并保存到 Session 上下文。
2. 所有 AU 的 RTP Timestamp 与 PES PTS 使用同一起点，按 1920 tick/AU 推进。
3. PCR 使用与 TIME_OFFSET 同源的单调时钟，按采样时刻更新，而不是简单硬编码为零或媒体 AU index。
4. 处理 RTP 32-bit / PES/PCR 33-bit 回绕，记录未回绕的逻辑时间用于长时间会话。
5. 发送节奏约每 21.333ms/AU，不能因批量 TTS 输出而无限速写入 TCP。
6. 新会话或重连要明确重置同步状态、PCR/PTS 原点和序列号；不能混用不同会话的时间域。

## 5. 源码与文献

- [MiPlayWfdMediaClock.cs](https://github.com/SUlTlUS/MiPlayForWindows/blob/main/src/DLNACast.Core/MiPlay/MiPlayWfdMediaClock.cs)：共享与单设备发送时钟计算。
- [MiPlayWfdAudioPacketizer.cs](https://github.com/SUlTlUS/MiPlayForWindows/blob/main/src/DLNACast.Core/MiPlay/MiPlayWfdAudioPacketizer.cs)：90 kHz AU 时间进位、PES/RTP 时间处理。
- 本仓库 `src/media/audio.py`：V14 时间域关联实现。

本节独立整理了本次实验观察与公开研究思路，不包含上游源码文件拷贝。
