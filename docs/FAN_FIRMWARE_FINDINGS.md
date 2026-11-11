# TS-470 fan controller: official-firmware findings

Status: **offline investigation and bounded fan-runtime acceptance complete**.
[Issue #1](https://github.com/marzliak/qnap-ts470-linux/issues/1) has exact-model
controller evidence and the physical response/recovery results below. This
does not certify hazardous calibration, the rewritten LCD or unattended safety.

## Result

The inspected official QTS image selects **Fintek F71869A through Linux sysfs**
for the TS-470 and TS-470 Pro chassis fan. Its model profiles do not select the
IT8528 EC fan backend. The IT8528E suggestion in the original issue was a
hypothesis, not a detected component or established cause.

The firmware supplies TS-470 profiles `model_0.conf`, `model_2.conf` and
`model_6.conf`, and TS-470 Pro profiles `model_1.conf`, `model_3.conf` and
`model_7.conf`. All six specify `SIO_DEVICE=F71869A`, `FAN_UNIT=SIO` and
`FAN_1=I2`. The multi-model selector chooses among these profiles using CPU,
network and PCH information; every supplied selection has the same fan route.
This is exact-model static evidence, **not a new physical test of either model**.

The HAL parser reads the number after `I` without adding one. The SIO setter
formats that number directly into `pwm%d_enable` and `pwm%d`. Thus this image
maps its first logical chassis fan to **sysfs channel 2**. Earlier project
records also named channel 2; later reference observations named channel 3.
Do not overwrite passive discovery or force channel 2 based on a package:
reconcile the actual fan connection, RPM readings and installed driver first.

## What the vendor code does

The root fan dispatcher selects the SIO backend, then `sio_sys_set_fan_pwm()`:

1. Reads the configured SIO type.
2. For F71869A, writes manual mode (`1`) to `pwmN_enable`.
3. Writes the requested 0–255 value to `pwmN`.

The inspected helper uses `/sys/class/hwmon/hwmon0/device`. That historical
class index is **not** a portable path to copy into this project. No mailbox
probe, EC driver, ACPI resource override or raw port access is required by the
fan route observed in this image. Do not execute the extracted HAL utility.

## Linux ABI corrections

Primary source: [Linux v7.0 f71882fg driver](https://github.com/torvalds/linux/blob/v7.0/drivers/hwmon/f71882fg.c)
and [driver documentation](https://github.com/torvalds/linux/blob/v7.0/Documentation/hwmon/f71882fg.rst).
The relevant behavior was also inspected in Linux v6.8.

- **Automatic mode rejects duty writes.** The driver returns `EROFS` when
  `pwmN` is written in automatic mode. Recovery now leaves such a channel in
  automatic, reasserts that mode and checks its driver readback without first
  attempting an invalid duty write. A manual/unknown channel still receives a
  full-duty attempt before automatic handback. Failed writes remain failures.
- **Manual does not necessarily mean direct physical PWM.** The BIOS can select
  closed-loop RPM targeting. In that mode, the 0–255 value is a fraction of
  `fanN_full_speed`; integer divisions quantize the round trip. At 1500 RPM
  full scale, requests 100 and 120 read back as 99 and 119. The project accepts
  only the exact direct value or this deterministic conversion with an
  unchanged valid full-speed scale. It does not add a generic tolerance, infer
  the BIOS drive mode or claim that these two representations are distinguishable
  through this ABI. Missing/invalid scales and unexplained mismatches still fail.
- **Driver acknowledgement is not physical verification.** This driver caches
  writes and readings. An immediate readback can confirm its software state,
  not that the fan physically moved or that the chip was independently reread.
  Recovery logs explicitly state that physical effect is untested. Existing
  cleanup exit codes continue to represent driver acknowledgement only.
- **Unknown is not zero.** `status` preserves an unreadable RPM as JSON `null`
  and reports the full-speed scale. A zero from the driver also does not prove
  absence of rotation: its minimum-detectable tachometer value is mapped to zero.

## Provenance and reproducibility

- Downloaded from QNAP's HTTPS distribution endpoint on 2026-11-10:
  [TS-470_20240619-4.3.6.2805.zip](https://download.qnap.com/Storage/TS-470/TS-470_20240619-4.3.6.2805.zip).
- [Official release notes](https://www.qnap.com/en-us/release-notes/qts/4.3.6.2805/20240619)
  were indexed by search but direct HTML retrieval was WAF-challenged. Exact
  model applicability here is independently supported by the profiles inside
  the downloaded image. This is an analysis specimen, not an update recommendation.
- ZIP: 226902314 bytes; SHA-256
  `23cd28a65a17bbf90b12927695713ea0c82a2e251198886a4f920f9927a5d461`.
- IMG: 226852187 bytes; SHA-256
  `aebae67186c7ec271863f3e93d1558b427524b27df6d4f7fe0a6d77e701b7a1d`.
- HAL library: SHA-256
  `4d5b1a7102b91a609f2c767d7a32b0de1a5d4d2e0dc6b9e8d32e50de0ce62d91`.
- Complete ZIP CRC and decrypted gzip EOF/CRC passed. The encrypted prefix is
  1048576 bytes; the container footer identifies TS-470 / 4.3.6 / 20240619.
  `initrd.boot` is LZMA/newc; `rootfs2.bz` is bzip2/TAR in this build.
- Decoder reference: Unblob commit
  `1e73b5601c2a8c0c5d7db7bee1998b989c6c80e3`, QNAP Cryptor class.
- REA 6.8.0 with Ghidra 12.1.4 decompiled the actual SIO setter at
  `0x198d10`; retained evidence ID
  `ev_267375621a93c8e6c632973d9a28dadff687887f5d1ec0ff266ed3058d30750d`.
  The shared-object analysis bias is `0x100000`; its ELF virtual address is
  `0x98d10`. PLT and function-body names were disambiguated.
- Additional dispatch, profile-selection and file-helper inspection used GNU
  objdump and a separate offline Ghidra script, **not REA MCP output**.
  Dispatch at ELF `0x80c40`, channel parser `0x70110`, SIO file writer
  `0x95410`; function labels lacking ELF symbols were inferred from debug strings.
- Vendor binaries, complete firmware and raw decompiler outputs remain private;
  they are not redistributed in this source repository. Local hashes identify
  the received bytes, not independent publisher-signature authentication.

## Physical acceptance on 2026-11-10

The owner authorized deployment on the reference TS-470 Pro, Ubuntu 24.04.5
LTS / kernel 7.0.0-42-generic. The fan binary from PR #6 was installed
byte-for-byte (SHA-256
`1f3908ba484a9786a06ba6aff130141a6e8a0fabdc3ecd9a98ac0aa2fe40069f`).
Only fan control was replaced; the existing LCD process and files were preserved.

- Passive discovery found channel 3 spinning, channels 1/2 at zero and automatic.
  Bounded writes proved that **channel 3 controls the connected fan on this
  modified unit**. Why it differs from QTS profile channel 2 remains historical
  uncertainty, not an excuse to overwrite discovery.
- Commands 255 → 128 → 255 produced settled tachometer medians of
  **2564 → 1694 → 2564 RPM**. No stop command or stall calibration was used.
  CPU temperature stayed 30–32 °C during this test.
- Stopping the new service handed all channels back to automatic successfully;
  channel 3 remained spinning at 1116–1691 RPM during the eight-second
  observation. This is bounded actual recovery evidence, not merely a cached
  mode readback or proof of every future automatic thermal decision.
- The new service restarted successfully, stayed active with one writer and
  zero unexpected restarts, and produced 969–1144 RPM across four ten-second
  samples at 31–33 °C. The legacy service was disabled.
- Existing measured calibration was migrated, conservatively clamping legacy
  minimum/restart duties to 64. These are not newly measured stall thresholds.
- Boot enablement and runtime-directory tmpfiles provisioning were configured;
  **no reboot was performed**. The generic installer was not used for this
  fan-only deployment and is not covered by the acceptance.

Current fan runtime response/recovery is therefore accepted on this one unit.
Keep other models, hazardous calibration, rewritten LCD and unattended/disk
thermal protection outside this conclusion. The CPU-only curve is unchanged.
