# Acknowledgements / 致敬开源

本项目旨在研究小米 AI 音箱 2（L15A）原厂固件的本地音频播放能力。V14 首次完成无需刷机的 Mac → L15A MiPlay 音频推送实机验证，受益于社区长期的逆向工程、协议抓包、媒体封装与播放时钟研究。

特别感谢：

- **[SUlTlUS / MiPlayForWindows](https://github.com/SUlTlUS/MiPlayForWindows)**：提供真实设备的 Legacy 控制/RTSP/AAC/RTP 发送端研究与系统化实现，其中 `MiPlayWfdMediaClock` 对 V14 排除“传输成功但无声”的问题尤其重要。
- **[rosienosiesie / FusionPlay-Android](https://github.com/rosienosiesie/FusionPlay-Android)**：公开 MiPlay 多协议音频接收端的实现与研究。
- **[MYHealer / miplay-mini-amp](https://github.com/MYHealer/miplay-mini-amp)**：通过 ESP32-S3 / I2S 实现 MiPlay 接收端，为嵌入式研究提供参考。
- **[juneix / MiXPlay](https://github.com/juneix/MiXPlay)**：为局域网多协议音频网关和交互架构提供启发。MiXPlay 本体为专有软件，感谢其公开资料，但并不视作可自由复制的开源代码。
- **[FFmpeg](https://ffmpeg.org/)**、**[Wireshark](https://www.wireshark.org/)**、**[tcpdump](https://www.tcpdump.org/)** 与 **[Python](https://www.python.org/)** 项目社区：为编码、抓包、分析与协议复现提供工具基础。

本次实验总结与 V14 源文件被单独保留作为可核验的复现资产。我们尊重每一位作者的版权、许可证和研究成果；若未来引入上游源码，将按照具体许可记录来源、版权、改动和义务。本项目不代表任何小米官方授权或背书。

**Respect Open Source. Build on Knowledge. Give Credit.**

更多详细引用和许可证边界见 [docs/references.md](docs/references.md) 和 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。
