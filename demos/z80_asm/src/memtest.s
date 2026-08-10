; Z80 RAM memtest for EBAZ4205 (z80_soc)
;
; Tests the full 56K RAM space (0x2000 - 0xFFFF).
; Phase 1: Fill every byte with address-dependent pattern.
; Phase 2: Read back and verify each byte.
; Phase 3: Output results via OUT (port 0) to the FIFO bridge.
;
; Output protocol (3 bytes to OUT (0)):
;   byte 0 = error count high (MSB of 16-bit counter)
;   byte 1 = error count low  (LSB)
;   byte 2 = 0x00 = PASS, 0xFF = FAIL
;
; The test is assembled for ROM address 0x0100. The runner writes a boot
; vector JP 0x0100 at ROM address 0x0000, so the code does not overwrite
; itself while testing RAM.

.org 0

RAM_START   equ 0x2000       ; first Z80 address of RAM
; RAM goes from 0x2000 to 0xFFFF (56K)
; When HL wraps 0xFFFF -> 0x0000, we know we've covered all RAM addresses

; === Phase 1: Fill RAM with pattern ===
    ld   hl, RAM_START

fill_loop:
    ld   a, l
    xor  h                  ; pattern = low XOR high byte of address
    ld   (hl), a            ; write to RAM
    inc  hl
    ld   a, h
    or   l                  ; check if HL == 0x0000 (wrapped past 0xFFFF)
    jp   nz, fill_loop

; === Phase 2: Verify ===
    ld   hl, RAM_START
    ld   de, 0              ; DE = error counter (16-bit)

verify_loop:
    ld   a, l
    xor  h                  ; expected = low XOR high byte
    ld   c, a               ; save expected in C
    ld   a, (hl)            ; read back from RAM
    cp   c                  ; compare with expected
    jp   z, verify_ok
    inc  de                 ; count mismatch
verify_ok:
    inc  hl
    ld   a, h
    or   l
    jp   nz, verify_loop

; === Phase 3: Output results ===
; D = error count high byte, E = error count low byte
    ld   a, d
    out  (0), a             ; error count high byte
    ld   a, e
    out  (0), a             ; error count low byte

    ld   a, d
    or   e
    jp   nz, has_errors

    ld   a, 0x00            ; PASS marker
    out  (0), a
    jp   done

has_errors:
    ld   a, 0xFF            ; FAIL marker
    out  (0), a

done:
    jp   done
