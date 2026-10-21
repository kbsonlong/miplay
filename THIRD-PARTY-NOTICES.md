# Third-Party Notices

**Scope:** The shipped ZIP consists of this repository's Python implementation (including the unmodified experimentally validated V14 script), tests, documentation and shell examples. It does **not** bundle FFmpeg, Wireshark, tcpdump binaries, or any source files copied from the third-party repositories listed below. They are cited as references, not vendored dependencies.

| Project | URL | Public license/status (reviewed 2026-10-21) | Relationship |
|---|---|---|---|
| MiPlayForWindows | https://github.com/SUlTlUS/MiPlayForWindows | No repository-root LICENSE was found in the review. Do not assume a reusable license. | Important protocol and timestamp reference; no upstream `.cs` files copied |
| FusionPlay-Android | https://github.com/rosienosiesie/FusionPlay-Android | AGPL-3.0 (`LICENSE`) | Receiver-side technical reference, no code copied |
| miplay-mini-amp | https://github.com/MYHealer/miplay-mini-amp | AGPL-3.0 (`LICENSE`), plus stated usage cautions in README | ESP32-S3 receiver technical reference, no code copied |
| MiXPlay | https://github.com/juneix/MiXPlay | Proprietary / non-commercial limitations stated in `LICENSE` | Architecture-only reference, no code copied |
| FFmpeg | https://ffmpeg.org/ | LGPL / GPL, depending on build configuration | External optional executable required for actual encoding; **not bundled** |
| Wireshark | https://www.wireshark.org/ | GPL | Optional packet inspection tool; not bundled |
| tcpdump | https://www.tcpdump.org/ | BSD-style | Optional packet capture tool; not bundled |
| Python | https://www.python.org/ | Python Software Foundation License | Required runtime/standard library; not bundled |

The `LICENSE` in this ZIP applies only to material that its authors may lawfully license. Citing a public source, reverse-engineered behavior, or interface specification does not transfer ownership of original third-party code. If you later copy/modify third-party code, reevaluate the licensing obligations before publishing or distributing a combined work.

Audio recordings, packet captures, identifiers, login/account metadata, and device secrets are not provided in this ZIP.
