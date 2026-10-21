# 从 V7 全程无声到 V14 出声：调试步骤与复盘

## 1. 调试环境与目标

实验为 L15A 固件 1.92.16；Mac 192.168.3.18；音箱 192.168.3.4 TCP8899；Mac TCP7274 与 UDP36524。本地输入 `/tmp/l15a-v7-test.aiff` 可通过 `afplay` 确认有声。当前复现不要先尝试刷机、Root 或 OTA 修改固件。

## 2. 安装、探测与播放

```bash
brew install python ffmpeg wireshark
python3 --version
ffmpeg -version
ffprobe -version
route -n get 192.168.3.4
nc -vz -G 3 192.168.3.4 8899
lsof -nP -iTCP:7274
lsof -nP -iUDP:36524

afplay /tmp/l15a-v7-test.aiff
ffprobe -v error -show_entries stream=codec_name,channels,sample_rate -show_entries format=duration -of json /tmp/l15a-v7-test.aiff

# 默认不发网络报文
python3 miplay_client_v14.py

# 只验证设备握手，无 Open
python3 miplay_client_v14.py --connect --confirm-host 192.168.3.4 --hold 5

# 主动播放本地音频：可能中断音箱当前播放，确保设备授权且闲置
python3 miplay_client_v14.py \
  --connect --confirm-host 192.168.3.4 --trigger-open \
  --audio-file /tmp/l15a-v7-test.aiff --hold 60 \
  2>&1 | tee l15a-v14.log
```

## 3. 标准抓包流程

终端 1：

```bash
IFACE=$(route -n get 192.168.3.4 | awk '/interface:/{print $2; exit}')
sudo tcpdump -i "$IFACE" -nn -s 0 -U -w l15a-v14-media.pcap \
  'host 192.168.3.4 and (tcp port 8899 or tcp port 7274 or udp port 36524)'
```

终端 2：运行 V14 播放。停止后分析：

```bash
python3 tools/inspect_pcap.py l15a-v14-media.pcap
python3 tools/inspect_pcap.py l15a-v14-media.pcap --extract-ts /tmp/l15a.ts
ffprobe -v error -show_streams -show_format /tmp/l15a.ts
ffmpeg -hide_banner -i /tmp/l15a.ts -af volumedetect -f null - 2>&1 | grep -E 'mean_volume|max_volume'
tshark -r l15a-v14-media.pcap -q -z conv,tcp
```

说明：`--extract-ts` 生成的流包含用户真实音频，请勿无意上传公开仓库；音箱信息响应可能含设备序列号、MAC、账号和房间标识，因此不要将未经脱敏的控制流发布到公共 Issue。

## 4. 版本差异与证据

| 版本 | 修改 | 真机结果 |
|---|---|---|
| V1-V3 | TCP8899、Challenge、legacy HMAC、控制协议 | 建立基础研究路径 |
| V4-V6 | Open、反向 TCP、RTSP 与 UDP 时钟 | 建立 WFD 控制链路 |
| V7 | 增加 AAC/ADTS → PES → TS → RTP | 发出约 192KB，仍然无声 |
| V8 | 对 VIDEO_LATENCY 不再回复 501 | 仍然无声 |
| V9 | 修复 PAT program_number、PES optional header | 仍然无声 |
| V10 | 修复 PMT PCR PID、PSI 形态与 RTP Marker | 仍然无声 |
| V11 | 增加 SetMediaInfo，采集 post-Open Notify | first-audiopcm/state=2，但无声 |
| V12 | 实验性状态门控 | 未保留为单独成功基准 |
| V13 | 实时 PCR 与启动 10 个 AU 后检查 readiness | 仍无声 |
| **V14** | **RTP/PES PTS 锚定到 RTSP TIME_OFFSET 的 Mac 单调时间域** | **音箱正常出声** |

V7-V13 的多次失败说明：**能够向 socket 发送字节、能够被 FFmpeg 解码、音箱报告 `state=2`，都不足以证明音频已经进入可听呈现流程。**

## 5. 诊断分层与判定

| 层级 | 成功标志 | 失败优先排查 |
|---|---|---|
| L0 输入 | Mac `afplay` 有声 | 音频文件、权限 |
| L1 控制 | 8899 Challenge、ACK、GetDeviceInfo | IP、防火墙、HMAC、长度/序号 |
| L2 WFD | `Open` 后三条反向 TCP | `wfd://` 地址、网卡监听、连接顺序 |
| L3 RTSP | `OPTIONS → GET_PARAMETER → SET_PARAMETER → SETUP → PLAY → TIME_OFFSET` | 能力匹配、Session、RTSP header |
| L4 时钟 | UDP 40 字节应答、TIME_OFFSET 成功确认 | `wfd_timer_server_port`、Little Endian、微秒单调时钟 |
| L5 元数据 | `SetMediaInfo 0x0012` 后 Notifies | JSON 字段、设备状态 |
| L6 封装 | 188 字节 TS、PT33、SSRC、RTP Marker | PAT CRC/PMT PCR PID/PES PTS/ADTS |
| L7 调度 | PTS/RTP 与 PCR 都能关联到 TIME_OFFSET | 首帧 PTS=0、PCR 使用另一时间基准 |
| L8 出声 | 音箱确实可听 | 音量/音箱状态/实机实际播放 |

## 6. 首选日志过滤

```bash
grep -E 'CONTROL connected|SetPlaySource|Open sent|REVERSE TCP|RTSP TX|TIME_OFFSET|SET_MEDIA_INFO|first-audiopcm|state=2|AUDIO sent|AUDIO sender failed|peer closed' l15a-v14.log
```

`VIDEO_LATENCY` 为高频诊断遥测，V14 忽略而不是应答。若希望评估接收端收到的 RTP 数量，应该在后续解析其 RTSP `rtppacketnum` 等头字段，不要仅凭其出现次数判断媒体成功。

## 7. V14 实际成功值

```text
RTSP TIME_OFFSET                 566798454289 us
RTP frames                       224
MPEG-TS 188 byte packets         1004
Audio PES starts / AAC AUs       216
RTP Marker set                   216
Media TCP bytes                  192336
Initial RTP Timestamp/PES PTS    3767220630
Initial PCR base                 8062202057
first-audiopcm=1                true
state=2                         true
Real audio from L15A            confirmed
```

## 8. 不要忽略的工程限制

V14 的音频会话仍受 RTSP 主循环 35 秒硬期限约束，并且没有周期控制心跳和主动 TEARDOWN。这些与 4.6 秒文件的成功播放不矛盾，但阻碍长文件和连续 TTS。生产化前先补充可恢复的状态机、正常停止行为和会话级监控。

此外，V14 `_maybe_start_audio()` 由不同线程触发，但依赖 `audio_started` 状态避免重复；模块化版本仍沿用实验实现。生产版本应明确锁住整个检查与启动临界区，并避免控制连接 monitor 与调用端并行读取同一 socket。
