# 开源项目参考、源码定位与许可边界

> 本项目在真实 L15A 音箱上完成 V14 发送端验证，得益于社区公开的抓包研究与协议实现。这里区分「发送端技术参考」、「接收端交叉参考」和「架构考察」，尊重原作者贡献、许可证及未获授权的边界。

## A. 最重要的发送端技术参考：SUlTlUS / MiPlayForWindows

- 仓库：https://github.com/SUlTlUS/MiPlayForWindows
- README：https://github.com/SUlTlUS/MiPlayForWindows/blob/main/README.md
- 角色：Windows 系统音频 MiPlay Sender；公开材料记录 LX06 固件 1.94.13 的成功播放案例（不同于我们的 L15A 实验）。
- 技术价值：可查的 Legacy 8899 控制会话、三条反向 TCP、RTSP/WFD、AAC/PES/TS/RTP、单调时钟关联方案。

| 本项目模块/问题 | 上游参考具体文件 |
| --- | --- |
| Legacy Control + Open / TCP reverse 1/2/3 | [MiPlayLegacySystemAudioSessionRunner.cs](https://github.com/SUlTlUS/MiPlayForWindows/blob/main/src/DLNACast.Core/MiPlay/MiPlayLegacySystemAudioSessionRunner.cs) |
| AAC 音频 PES / PAT / PMT / PCR | [MiPlayMpegTsAudioMuxer.cs](https://github.com/SUlTlUS/MiPlayForWindows/blob/main/src/DLNACast.Core/MiPlay/MiPlayMpegTsAudioMuxer.cs) |
| AU 时间戳步长、PT 33、Marker / SSRC | [MiPlayWfdAudioPacketizer.cs](https://github.com/SUlTlUS/MiPlayForWindows/blob/main/src/DLNACast.Core/MiPlay/MiPlayWfdAudioPacketizer.cs) |
| `$ + BE24 len + RTP` 特殊帧 | [MiPlayWfdInterleavedFrameCodec.cs](https://github.com/SUlTlUS/MiPlayForWindows/blob/main/src/DLNACast.Core/MiPlay/MiPlayWfdInterleavedFrameCodec.cs) |
| V14 的单调时间域 / PCR / TIME_OFFSET | [MiPlayWfdMediaClock.cs](https://github.com/SUlTlUS/MiPlayForWindows/blob/main/src/DLNACast.Core/MiPlay/MiPlayWfdMediaClock.cs) |
| 真实 Legacy 会话与 Challenge 长度 | [miplay-real-legacy-source-fresh-session.md](https://github.com/SUlTlUS/MiPlayForWindows/blob/main/docs/miplay-real-legacy-source-fresh-session.md) |

**特别感谢** 项目维护者把设备上捕获的协议时序、MPEG-TS 结构、RTC/Monotonic 时间源及诊断经验整理成可参考的公开实现。我们定位 V14 问题时受到了该项目 `MiPlayWfdMediaClock` 的直接启发。项目源文件属于各自权利人，本仓库未打包该项目任何 `.cs` 源文件。

**许可提示**：截至本次资料核查，未在该仓库根目录找到 `LICENSE` 文件。源码可公开访问不等于获得可复制、修改或转授源码的许可。需要直接搬运代码时应先取得明确授权，不能默认其适用 MIT 或 Apache-2.0。

## B. 接收端参考：rosienosiesie / FusionPlay-Android

- 仓库：https://github.com/rosienosiesie/FusionPlay-Android
- 角色：Android/Android TV 上的多协议 **接收端**，包含 `FusionPlay.MiPlaySdk`。
- 用途：从接收端视角理解协议字段及音频接收行为；不能直接作为 Python/macOS 到原厂 L15A 的发送端。
- 仓库 `LICENSE`：GNU Affero General Public License v3.0 (AGPL-3.0)。

## C. 嵌入式接收端参考：MYHealer / miplay-mini-amp

- 仓库：https://github.com/MYHealer/miplay-mini-amp
- 角色：ESP32-S3 + I2S DAC 的 MiPlay **接收器**。
- 用途：嵌入式设备发现、MiPlay Legacy 控制、WFD/RTP 与音频播放参考。
- `LICENSE`：AGPL-3.0；README 还包含对学习和非商业用途的说明，使用与再分发时需进一步核实适用条款。
- 注意：ESP32 上实现 Receiver 并不等于已实现 ESP32 → 原厂 L15A Sender。

## D. 生态架构参考：juneix / MiXPlay

- 仓库：https://github.com/juneix/MiXPlay
- 角色：多协议音频中枢 / 生态参考，曾用于探索可行性与网关方向。
- 限制：仓库的 LICENSE 明确将 MiXPlay 本体标注为 **专有软件**，并限制某些源码的修改、重分发及商业用途。虽然包含/集成开源组件，不能将 MiXPlay 整体当作标准开源许可软件直接复用。

## E. 调试与音频工具

| 工具 | 官网 / 源码 | 本次用途 |
|---|---|---|
| FFmpeg | https://ffmpeg.org/ 和 https://github.com/FFmpeg/FFmpeg | AAC-LC 48 kHz stereo 编码，离线解码和媒体分析 |
| Wireshark / TShark | https://www.wireshark.org/ | RTSP/TCP/RTP 报文和设备响应分析 |
| tcpdump | https://www.tcpdump.org/ | macOS 现场抓包为经典 PCAP |
| Python | https://www.python.org/ | 发送端与标准库离线抓包工具实现 |

这些工具未随本项目 ZIP 打包二进制，也未将其源代码作为本仓库源码提供。FFmpeg 的实际许可证与编译选项有关；Wireshark 使用 GPL 系列许可；其他工具按其原始声明执行。

## F. 关于本项目的授权与署名

- `LICENSE` 仅对本项目作者有权许可的新增代码和文档起作用，不影响第三方权利人及其专有协议/商标。
- 本项目作为原始 V14 的重组和文档编排，不包含 MiPlayForWindows 的 `.cs` 文件、MiXPlay 专有模块、FusionPlay/MiPlay-mini-amp 的源文件拷贝。
- 将来如果直接引用任何上游受版权保护源码，应记录原始 commit/hash、文件、作者和适用许可证，并按条款处理（尤其是 AGPL 网络服务义务）。
- 抓包可以包含设备标识、用户账号、音频，默认不随 GitHub 仓库发布。
- 方案不是小米官方文档；实测范围是特定固件的 L15A，请独立验证其他型号。
