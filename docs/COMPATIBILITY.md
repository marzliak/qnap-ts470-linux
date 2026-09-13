# Compatibility

This project is validated on **one** machine. Everything else on this page is
an evidence-based estimate, and estimates are labelled as such.

> **Version 2.0.0 has not been executed on any TS-x70.** The rows below record
> what the *hardware* was observed to do, under the implementation that this
> release replaces. The current code has been validated offline only. A report
> confirming 2.0.0 itself on a TS-470 Pro is the single most useful thing
> anyone could contribute right now.

## How to read the status column

| Status | Meaning |
|---|---|
| **Tested** | The capability was exercised on the reference machine, with the distribution and kernel recorded below. This describes the hardware, not the 2.0.0 code — see the note above. |
| **Unverified** | No test report exists. The estimate is based on published specifications and on how the hardware is built, not on someone running it. |
| **Unlikely** | Published evidence actively points away from the feature being present. |

"Unverified" is not a soft "yes". Nobody has run it.

---

## 1. Feature matrix by model

| Model | LCD write | Buttons | Sensors | SMART | Fan control | Basis |
|---|---|---|---|---|---|---|
| **TS-470 Pro** | Tested | Tested | Tested | Tested | Single-machine evidence | Reference unit, Ubuntu 24.04 LTS, kernel 7.0 series, 2026-09-12 |
| TS-670 Pro | Unverified | Unverified | Unverified | Unverified | Unverified | Same tower family and same CPU generation as the reference unit |
| TS-870 Pro | Unverified | Unverified | Unverified | Unverified | Unverified | Same tower family and same CPU generation as the reference unit |
| TS-470 (non-Pro) | Unverified | Unverified | Unverified | Unverified | Unverified | Different CPU generation — see section 2 |
| TS-670 / TS-870 (non-Pro) | Unverified | Unverified | Unverified | Unverified | Unverified | Different CPU generation — see section 2 |
| TS-470U-RP | **Unlikely** | **Unlikely** | Unverified | Unverified | Unverified | Different chassis; QNAP's own product page does not list an LCD — see section 2 |

SMART support is a property of your disks and of `smartmontools`, not of the
NAS model, so it is the feature most likely to work anywhere.

---

## 2. Why "Pro" matters more than the model number

The `Pro` suffix is not cosmetic. QNAP's own specification places the Pro tower
models on **Ivy Bridge** (Intel Core i3-3220). The non-Pro models of the same
number are *reported* to ship an earlier **Sandy Bridge** Celeron — that
attribution comes from retailer listings and a review rather than from a QNAP
specification page retrieved for this document, so treat it as indicative
rather than established. If it holds, it is a different CPU generation, which
usually means a different board revision, and potentially a different Super I/O
chip and a different front-panel arrangement.

So `TS-470` and `TS-470 Pro` are less alike than the names suggest. Do not
assume that testing on one tells you anything definitive about the other.

**TS-470U-RP** deserves a stronger caution than the rest. It is a 1U
short-depth rackmount rather than a tower, ships a different CPU, and QNAP's
own product page for it does not mention an LCD panel at all — unlike the
TS-470 Pro specification, which explicitly lists one. Treat LCD support there
as unlikely until somebody demonstrates otherwise.

---

## 3. The tested reference machine

The reference system is described as "the reference TS-470 Pro" throughout this
repository. Its hostname, addresses and device serial numbers are deliberately
not published.

### As shipped by QNAP

| Component | Stock specification |
|---|---|
| CPU | Intel Core i3-3220, 3.30 GHz, 2 cores / 4 threads, 55 W |
| Memory | DDR3-1600 SO-DIMM, 4 slots |
| Network | 2 x Gigabit Ethernet |
| Front panel | Monochrome LCD with backlight, ENTER and SELECT buttons |
| Cooling | 1 x 9 cm 12 V DC chassis fan |

Sources: [QNAP TS-470 Pro hardware specification (archived)](https://web.archive.org/web/20230413145757/https://www.qnap.com/en/product/ts-470%20pro/specs/hardware)
and [Intel Core i3-3220 product specification](https://www.intel.com/content/www/us/en/products/sku/65693/intel-core-i33220-processor-3m-cache-3-30-ghz/specifications.html).

The archived QNAP page is linked rather than the live one because QNAP's site
does not reliably serve its specification pages to automated clients.

### As currently configured — this is a modified machine

The reference unit is **not** stock. Read this section as "what the maintainer
happens to run", not as a requirement.

| Component | Installed | Stock? |
|---|---|---|
| Chassis | QNAP TS-470 Pro | stock |
| Baseboard | Intel MAHOBAY; the chipset exposes Intel H61 LPC and 6-series/C200 components | stock |
| CPU | **Intel Core i7-3770T @ 2.50 GHz** - Ivy Bridge, 4 cores / 8 threads, 8 MiB L3, 1.6-3.7 GHz, VT-x | **upgraded, see below** |
| Memory | 16 GiB usable, 2 x 8 GB DDR3-1600 SO-DIMM (Crucial CT102464BF160B.C16 and CT102464BF160B.M16); two slots empty | upgraded capacity |
| Graphics | Intel Ivy Bridge GT2 / HD Graphics 4000 (integrated) | stock |
| Storage controllers | Intel 6-series/C200 6-port SATA AHCI, plus Marvell 88SE9235 PCIe 2.0 x2 4-port SATA 6 Gb/s | stock |
| Boot device | SanDisk SDSSDHII120G SATA SSD, 111.8 GiB reported; EFI system partition plus an ext4 root | user choice |
| Network | Intel 82579LM 1 GbE, Intel 82574L 1 GbE, **Realtek RTL8125 2.5 GbE** | the two Intel NICs are stock; the Realtek is an add-in card |
| USB 3 | Etron EJ188/EJ198 controller | stock |
| Front panel | ICP A125-compatible LCD, 2 x 16 characters, `/dev/ttyS1` at 1200 baud 8N1 | stock |
| OS | Ubuntu 24.04.4 LTS, x86_64, kernel 7.0.0-31-generic | - |

The boot layout above is what this machine happens to use. Nothing in this
project depends on it; any layout that boots Linux will do.

### About the CPU swap

The reference machine runs an **i7-3770T in place of the stock i3-3220**. Both
are Ivy Bridge parts on the same FCLGA1155 socket with the same official memory
support (DDR3-1333/1600, two channels), so the swap is mechanically and
electrically straightforward.

The `T` suffix matters: the i7-3770T is a 45 W part, **below** the stock
i3-3220's 55 W. That is the right direction for a chassis designed around a
single 9 cm fan. Sources:
[Intel Core i7-3770T specification](https://www.intel.com/content/www/us/en/products/sku/65525/intel-core-i73770t-processor-8m-cache-up-to-3-70-ghz/specifications.html),
[Intel Core i3-3220 specification](https://www.intel.com/content/www/us/en/products/sku/65693/intel-core-i33220-processor-3m-cache-3-30-ghz/specifications.html).

**This is not a recommendation.** If you copy it, check the TDP of whatever you
fit against your model's stock cooling. A higher-TDP Ivy Bridge part in a
single-fan chassis is a thermal problem, not an upgrade. Nothing in this
project requires a CPU swap, and none of its features depend on one.

### Sensors and fan channels observed

| Item | Observed on the reference unit |
|---|---|
| hwmon drivers present | `coretemp`, `acpitz`, and an `r8169`-provided NIC sensor |
| Kernel modules loaded | `f71882fg`, `coretemp` |
| Fan channels exposed | 3 |
| Fan channels actually turning | **channel 3 only**, about 1022 RPM at duty 68, in manual mode |
| Channels 1 and 2 | 0 RPM, automatic mode |

QNAP's specification lists exactly **one** chassis fan, but the Super I/O chip
exposes three channels regardless of how many are wired. Which channel carries
the fan depends on the header it is plugged into, and the reference unit has
had its fan replaced at least once.

Earlier revisions of this repository described channel 2 as "the" chassis fan.
That was true of one machine at one point in time. It is not a property of the
TS-x70 family, and nothing in this project hard-codes a channel any more:
channels are discovered by reading the tachometers.

The Super I/O chip is identified through the `f71882fg` kernel driver binding
on the reference unit. That driver serves a family of Fintek parts, so another
unit could expose a different but driver-compatible variant.

---

## 4. Distributions

| Distribution | Status |
|---|---|
| Ubuntu 24.04 LTS | Tested |
| Other systemd-based distributions | Unverified - the requirements are Python 3.9+, systemd, and optionally `smartmontools` and `util-linux` |
| Non-systemd init | Unverified - the binaries do not depend on systemd, but the units and installer do |
| QTS (QNAP's own firmware) | Out of scope |

---

## 5. Contributing a report

Reports are welcome for any model, including "it did not work". A negative
result on a TS-670 Pro is more useful than more confirmation of the TS-470 Pro.

Please include:

1. Your exact model, and whether it is a Pro.
2. Distribution and `uname -r`.
3. Output of `sudo qnap-tsx70-lcd --check`.
4. Whether text appeared on the display, and whether ENTER and SELECT worked.
5. For fan control: `qnap-tsx70-fancontrol status --json`.
6. A diagnostic bundle: `sudo ./scripts/diagnose.sh -o bundle.txt`.

`diagnose.sh` redacts hostnames, IP and MAC addresses, UUIDs and disk serial
numbers, but **read the file before you post it**. Redaction is a safety net,
not a guarantee.

What not to include: serial numbers, MAC addresses, IP addresses, hostnames, or
raw SMART output containing device identifiers.

A report that lets a feature move from Unverified to Tested is exactly what
this page needs.
