; walk.s — Memory walking pattern
;
; Write an incrementing pattern to Z80 RAM and output each byte.
; Demonstrates memory store + load + I/O.
;
; Program runs at Z80 address 0x2000 (loaded via ctrl_gp1).
; Writes pattern starting at Z80 address 0x3000 (0x1000 into RAM).
; Outputs via OUT (port 0) to the axis_byte_bridge.
;
; NOTE: ld hl, 0x3000 is an absolute Z80 address, NOT relative to the
; program load address.  This is correct because the Z80 sees 0x3000
; in its address space as RAM (0x2000-0x3FFF region).
;
; Assemble:
;   python3 assemble_z80.py walk.s --org 0x2000 -o walk.bin

    .org 0

    ld hl, 0x3000      ; Data area at Z80 address 0x3000 (in RAM)
    ld b, 0             ; Pattern value (starts at 0)

fill_loop:
    ld (hl), b          ; Write pattern byte to memory
    inc hl              ; Next address
    inc b               ; Next pattern value (wraps at 0xFF)
    ld a, h
    cp 0x31             ; Stop at Z80 address 0x3100
    jr nz, fill_loop

    ld hl, 0x3000       ; Reset pointer to start of data area

output_loop:
    ld a, (hl)          ; Read back from memory
    out (0), a          ; Send to PS via axis_byte_bridge
    inc hl              ; Next address
    ld a, h
    cp 0x31
    jr nz, output_loop
    jp output_loop      ; Restart output loop forever
