# Beeper Subsystem — Known Issues

## Hardware path

```
PS TTC0 (timer@f8001000) → ttc0_wave0_out (EMIO) → system_top.v → FPGA pin D18
→ transistor driver on expansion board → piezo buzzer
```

## Driver stack

```
beep -e /dev/input/event0
    ↓ writes struct input_event{E V_SND, SND_TONE, freq_hz}
input subsystem
    ↓ pwm_beeper_event()
pwm-beeper.c — sets period, schedules workqueue
    ↓ pwm_beeper_work()
    ↓ pwm_beeper_on() / pwm_beeper_off()
    ↓ pwm_apply_might_sleep()
pwm-cadence.c — ttc_pwm_apply()
    ↓ writes period/duty/prescaler to TTC registers
PS TTC0 hardware — generates square wave on ttc0_wave0_out
```

## Issue 1: Multi-tone unreliability (workqueue race)

The `pwm-beeper` driver handles EV_SND events **asynchronously** via `schedule_work()`:

```c
// pwm-beeper.c — pwm_beeper_event()
if (value == 0)
    beeper->period = 0;
else
    beeper->period = HZ_TO_NANOSECONDS(value);

if (!beeper->suspended)
    schedule_work(&beeper->work);  // deferred!
```

The work function reads `beeper->period` and applies the PWM state. For single isolated tones this works fine — the workqueue runs within microseconds on an idle system.

**What breaks**: When `beep` opens `/dev/input/event0`, plays a tone, and closes it in rapid succession, the `close()` callback fires `pwm_beeper_close()` → `pwm_beeper_stop()` → `cancel_work_sync()`. If the **previous work hasn't executed yet** (still queued), `cancel_work_sync()` cancels it and the tone is lost. The subsequent `pwm_beeper_off()` turns the PWM off (which was already off). The next `beep` invocation opens the device and tries to play a new tone, but the work from the first event was cancelled, so no sound.

The frequency sweep (which used `sleep 0.3` between each `beep` command) worked reliably because the 300 ms gap gave the workqueue plenty of time to run before the next open/close cycle.

**Workaround**: Use a single `fd` open (e.g. Python `os.open()`) and keep the device open for the duration of a tune, with generous sleep between tone-start and tone-stop events. Do **not** close the fd between notes.

## Issue 2: Severe frequency quantization

The TTC has a **16-bit counter** (max value 65,535) clocked at **~111 MHz** (`cpu_1x` from the Zynq clock controller, clkc index 6). At audio-range frequencies the counter needs a prescaler to keep the count within 16 bits:

```
effective_rate = 111,111,110 / 2^(PSV + 1)
frequency      = effective_rate / INTR_VAL
```

With only 16 bits of resolution and a 111 MHz base clock, the achievable output frequencies at audio range are **very coarsely quantized**.

**Register dump evidence** (captured via `devmem` during Python evdev tone sequence):

| Requested | CLK_CTRL | PSV | Divider  | INTR | Actual output | Error  |
|-----------|----------|-----|----------|------|---------------|--------|
| 1568 Hz   | 0x1D     | 14  | 32,768   | 2    | **1,695 Hz**  | +8.1%  |
| 988 Hz    | 0x1F     | 15  | 65,536   | 1    | **1,695 Hz**  | +71.6% |
| (return)  | 0x1D     | 14  | 32,768   | 2    | **1,695 Hz**  | —      |

Both 1568 Hz and 988 Hz requests produced **exactly the same output frequency** (~1695 Hz). The 988 Hz note sounded identical to the 1568 Hz note — the user could not distinguish them.

### Why this happens

The `pwm-cadence` driver's prescaler selection logic (`ttc_pwm_apply()`):

```c
period_cycles = mul_u64_u64_div_u64(state->period, rate, NSEC_PER_SEC);

if (period_cycles > priv->max) {            // > 65535
    div = mul_u64_u64_div_u64(state->period, rate, (NSEC_PER_SEC * priv->max));
    div = order_base_2(div);                // smallest n where 2^n >= div
    if (div) div -= 1;
    // ...
    rate = DIV_ROUND_CLOSEST(rate, BIT(div + 1));
    period_cycles = mul_u64_u64_div_u64(state->period, rate, NSEC_PER_SEC);
}
```

For requested frequencies in the ~500 Hz to ~2 kHz range, the raw `period_cycles` values are 2–4× above `max`, so `order_base_2` returns 1–2, and after subtracting 1, `div` becomes 0–1. With only 2‑3 possible prescaler values (divider 2, 4, or 8), the resulting output frequencies collapse to just a few distinct values.

Even with the prescaler at its minimum (divider=2, rate=55.6 MHz), a 16‑bit counter gives:
- **Maximum period**: 65,535 / 55,555,555 = 1.18 ms → **847 Hz minimum accurate frequency**
- For anything below ~850 Hz, the prescaler MUST increase, and frequency accuracy degrades further.

## Issue 3: CLK_CTRL register anomaly

The captured CLK_CTRL register values don't match what the driver's calculation should produce:

| Driver calc | Expected PSV | Observed PSV |
|-------------|-------------|--------------|
| div=0, divider=2  | **0** (bits[4:1]=0000) | **14** (bits[4:1]=1110) |
| div=0, divider=2  | **0** (bits[4:1]=0000) | **15** (bits[4:1]=1111) |

The driver uses a read-modify-write on bits [4:1]:
```c
clk_reg &= ~TTC_CLK_CNTRL_PSV;       // clear bits[4:1]
clk_reg |= (div << 1);               // set PSV = div
clk_reg |= TTC_CLK_CNTRL_PS_EN;      // set bit 0
```

For `div=0`, this should clear bits [4:1] to `0000`. But the register reads `1110` (PSV=14). The timer driver (`timer-cadence-ttc.c`) uses the same register layout (`PSV_MASK = 0x1e`, bits[4:1]) and works correctly as a clocksource, so the register definitions are correct for this silicon.

**Possible causes** (unresolved, needs JTAG/ILA trace):
1. The `writel_relaxed` doesn't take effect before the next `readl` in a subsequent apply call, creating a read-modify-write race.
2. The Zynq-7000's TTC CLK_CTRL has write-locked fields that require a specific unlock sequence not implemented in the driver.
3. The `CSRC` (clock source select, bit 5) interacts with the PSV field in an undocumented way on this silicon revision.

## Practical workaround for audio

Given the hardware limitations, the only reliable approach for playing tunes is:

1. **Use a single evdev fd** (Python `os.open("/dev/input/event0", os.O_WRONLY)`) — keep it open for the entire tune.
2. **Use generous timing**: ≥ 100 ms gap between tone-off and next tone-on.
3. **Stick to frequencies ≥ 1 kHz** where the 16‑bit counter gives better resolution.
4. **Expect frequency inaccuracy**: test individual frequencies first to find which ones produce distinct outputs.

Example (Python):
```python
import struct, time, os

EV_SND, SND_TONE = 0x0012, 0x0002
fd = os.open("/dev/input/event0", os.O_WRONLY)

def tone(freq_hz, dur_ms, gap_ms=120):
    ev = struct.pack("llHHi", 0, 0, EV_SND, SND_TONE, freq_hz)
    os.write(fd, ev)
    time.sleep(dur_ms / 1000.0)
    os.write(fd, struct.pack("llHHi", 0, 0, EV_SND, SND_TONE, 0))
    time.sleep(gap_ms / 1000.0)

tone(1568, 400)  # G5 — works
tone(2347, 400)  # D6 — works
tone(1319, 350)  # E5 — works (D#5/1244 unreliable)
os.close(fd)
```

## References

- `linux/drivers/pwm/pwm-cadence.c` — AMD TTC PWM driver (introduced 2023)
- `linux/drivers/input/misc/pwm-beeper.c` — PWM beeper input driver
- `linux/drivers/clocksource/timer-cadence-ttc.c` — reference TTC register definitions
- `hw/` — TTC0 at base 0xF8001000, clkc index 6 = `cpu_1x` (111,111,110 Hz)
- Device tree: `&ttc0 { #pwm-cells = <3>; }`, `beeper: beeper { compatible = "pwm-beeper"; pwms = <&ttc0 0 370370 0>; }`
- `system_top.v`: `.ttc0_wave0_out(buzzer)` — routes TTC0 wave output to FPGA pin D18