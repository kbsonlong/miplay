# L15A 本地 MiPlay 协议全链路（按成功 V14 还原）

> 实测目标：原厂小米 L15A 固件 1.92.16，测试 LAN 的 Mac 192.168.3.18、音箱 192.168.3.4。仅描述实验观察到的行为，不承诺所有 MiPlay 版本通用。

## 1. 角色与套接字

```text
Mac (MiPlay Source)                   L15A (Receiver)
Mac_ephemeral ----------------------> TCP 8899  [控制]
Mac:7274 <-------------------------- L15A ephemeral [RTSP TCP #1]
Mac:7274 <-------------------------- L15A ephemeral [Aux TCP #2]
Mac:7274 <-------------------------- L15A ephemeral [Media TCP #3]
Mac:36524 <------------------------- L15A ephemeral [Timer request UDP]
Mac:36524 -------------------------> L15A ephemeral [Timer reply UDP]
Mac:7274 ==========================> L15A ephemeral [RTP written on TCP #3]
```

三个反向 TCP 均由音箱主动拨号；音频字节方向与建连方向相反。第 2、3 条是通过 `accept()` 到达顺序识别的实验行为，生产实现可增加更严格的连接用途校验，防止某些设备出现到达乱序。

## 2. TCP 8899 Legacy 控制帧

固定九字节头 `0x24 '$' | command:BE16 | seq:BE16 | payload_len:BE32`，随后跟相应长度的 Payload。例如：

```python
b'$' + struct.pack('>HHI', cmd, seq, len(payload)) + payload
```

Challenge 为音箱先发送的 `0x0028`，payload 是 16 或 17 位 ASCII 数字；Legacy HMAC 使用 `MD5("0.0.0.0").hexdigest()` 的 ASCII 结果作为密钥，对完整 Challenge 进行 HMAC-SHA1，再使用小写十六进制 ASCII 发送 `0x0029`。这里只记录实际实验接口，不代表新的 SafetyAuth 机制。

| 交互 | 命令 | 示例序号 | 说明 |
| --- | --- | ---: | --- |
| Challenge | RX `0x0028` | 音箱生成 | 16/17 ASCII 数字 |
| SourceVersion | TX `0x0036` | 0 | `1.0.1123012\0` |
| Legacy HMAC | TX `0x0029` | 同 Challenge | HMAC-SHA1 hex |
| GetDeviceInfo | TX `0x001E` | 1,9 | 验证设备信息 |
| DeviceInfo | RX `0x001F` | 1,9 | 可能含个人/设备 ID，日志不要直接公开 |
| SetLocalDeviceInfo | TX `0x0058` | 2,3,8,10 | sourceName、account context |
| MirrorMode | TX `0x0034` / RX `0x0035` | 4,11 | 播放前状态检查 |
| Heartbeat | TX `0x001A` / RX `0x001B` | 12 | V14 播放前验证一次 |
| SetPlaySource | TX `0x0040` | 13 | 标记控制中心 / 单房间源 |
| Open | TX `0x0000` | 14 | `wfd://192.168.3.18:7274?mirrorMode=1\0` |
| SetMediaInfo | TX `0x0012` | 15 | JSON 元数据，打开媒体播放上下文 |
| Notifications | RX `0x0022` | 音箱生成 | `first-audiopcm=1`、`state=2` |

只有显式提供 `--trigger-open` 才调用 SetPlaySource / Open；默认 Dry Run 不进行控制连接。V14 将目标校验固定到测试音箱，避免误触其他设备。

## 3. 反向 RTSP 建链顺序

1. 监听 Mac TCP 7274 与 UDP 36524；再发送 Legacy `Open`。
2. 音箱向 Mac TCP 7274 建立 Reverse #1；Mac 在该连接上发送 `OPTIONS * RTSP/1.0`，包含 `wfd_timer_server_port`。
3. 音箱应答并发送自己的 `OPTIONS`；Mac 返回 `200`，随后发 `GET_PARAMETER` 查询音箱能力。
4. 收到包含 AAC 及 `RTP/AVP/TCP;interleaved mode=play` 的能力后，Mac 发 `SET_PARAMETER`：

   ```text
   wfd_audio_codecs: AAC 00000001 00
   wfd_client_rtp_ports: RTP/AVP/TCP;interleaved mode=play
   wfd_presentation_URL: rtsp://192.168.3.18/wfd1.0/streamid=0 none
   ```

5. 音箱向 Mac 建立第 2、3 条反向 TCP 连接，Mac 在第 3 条上准备写入 RTP 媒体。
6. Mac 发 `SET_PARAMETER`，body 为 `wfd_trigger_method: SETUP\r\n`。
7. 音箱发 `SETUP`，Mac 返回 Session/Transport；音箱发 `PLAY`，Mac 返回 `200 PLAY`。
8. Mac 取一次本机 `monotonic_us()`，以 RTSP `TIME_OFFSET` 方法发送到音箱，收到 `200 OK` 后宣布 RTSP ready。
9. Mac 在控制 TCP 8899 发送 `0x0012 SetMediaInfo`，随后开始发送 AAC 媒体，等音箱先收到少量音频后再检测 `first-audiopcm=1/state=2`。

接收端周期性发送 `VIDEO_LATENCY` 与 `GET_PARAMETER`；V14 对前者只记录不应答，对空 GET_PARAMETER 返回 `200`。RTSP 响应中的 Session id 是每次会话生成的值，不应硬编码抓包值。

## 4. UDP TimerService

音箱会请求发送端 UDP 36524；V14 返还 40 字节：前 16 字节及尾 8 字节沿用收到的数据，字节偏移 `[16:24]` 放入发送时的 `monotonic_us()`，偏移 `[24:32]` 放入接收时的 `monotonic_us()`，均为 **Little Endian UInt64**。参考 `src/timer/service.py`。

TimerService 同步与 RTSP TIME_OFFSET 相辅相成，不能仅通过 RTSP `PLAY 200` 判断音频时间轴已准备好。

## 5. AAC → PES → MPEG-TS → RTP

```text
输入音频 → FFmpeg AAC-LC 48k Stereo (ADTS)
      ↓ 每 AU 1024 samples，约 21.333 ms
PES (stream_id=0xC0，含 PTS) → TS (188 bytes)
      ↓ PAT=PID 0x0000，PMT=0x0100，PCR=0x1000，AAC=0x1100
RTP v2 PT=33，SSRC=0xDEADBEEF，Marker=每 AU 最后一个 RTP 分片
      ↓ 每帧最多 7×188 字节 TS；90k 时间戳每 AU +1920
MiPlay 4-byte framing: '$' + RTP_length_BE24 + RTP
      ↓
向反向 TCP #3 写入上述字节
```

### MPEG-TS 数据

- 每个 AAC Access Unit 构造一个 PES；头以 `00 00 01 C0` 开始。
- PES option header 使用 `84 80 07`、5 字节 PTS、2 字节 `FF FF` stuffing。
- PAT 以 Section CRC32 完整封装，映射 PMT PID `0x0100`。
- PMT 中 AAC stream_type `0x0f`，AAC PID `0x1100`，PCR PID `0x1000`。
- PCR 不放在 AAC PID，而是单独 PID `0x1000`，adaption_only packet。
- PAT/PMT/PCR 在第一个 AU 和 AU #13 起每五个 AU 重发；AAC PID continuity_counter 对每个 TS 音频包递增。
- 每个 TS 固定 188 字节。尾包使用 adaptation field 填充。

### RTP 自定义 TCP 帧

```text
24                      # '$'
xx xx xx                # RTP payload length: unsigned 24-bit BE
80 [marker|0x21]        # RTP v2, marker bit, PT=33
ss ss                   # sequence BE16
pp pp pp pp             # timestamp BE32
DE AD BE EF             # SSRC
47 ... 188 bytes ...    # MPEG-TS packet 1
47 ... 188 bytes ...    # MPEG-TS packet 2
...
```

**本实验的 TCP `$` 头是 24-bit 长度，不是传统 RTSP interleaved `$ + channel + BE16 length`。**

PTS 与 RTP 使用同一 90 kHz 起点，RTP sequence 每个 RTP frame +1。一个大型 AU 可分裂成两个 RTP 帧：二者时间戳相同，最后一个 marker=1。

## 6. 发送时间轴

见 [media-clock.md](media-clock.md)。要点：

```python
# RTSP PLAY 后立刻记录并发送 TIME_OFFSET
T0 = time.monotonic_ns() // 1000
# 音频 PTS/RTP：90 kHz；每个 AU +1920
pts0 = (T0 * 90000 // 1_000_000) & 0xffffffff
# PCR：每次生成 PAT/PMT/PCR 时实时采样 Monotonic clock
now_us = time.monotonic_ns() // 1000
pcr27 = now_us * 27
pcr_base = (pcr27 // 300) & ((1 << 33) - 1)
pcr_ext = pcr27 % 300
```

PCR base 33 位，RTP 32 位，应先处理取模再比较。V14 短音频成功不等于已验证长时间回绕、时钟漂移与多音箱同步。

## 7. 播放前置条件和结束行为

V14 先发送 10 个 AAC AU（约 213ms），然后检查音箱通知 `first-audiopcm=1` 和 `state=2`，等待最长 5s，收到后继续发送；不能在发送第一帧之前等待 first-audiopcm，否则容易形成逻辑死锁。

V14 发送完成后未主动 TEARDOWN，RTSP 主循环约 35s 后离开；需要生产化时增加状态机、主动关闭、周期心跳与断线恢复。

## 参考

技术协议实现最直接参考 [MiPlayForWindows](https://github.com/SUlTlUS/MiPlayForWindows) 的 `MiPlayLegacySystemAudioSessionRunner`、`MiPlayMpegTsAudioMuxer`、`MiPlayWfdAudioPacketizer` 与 `MiPlayWfdMediaClock`。具体源码链接及许可证声明见 [references.md](references.md)。
