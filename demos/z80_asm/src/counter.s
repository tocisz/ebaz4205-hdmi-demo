; counter.s — Simple counter, outputs incrementing bytes via OUT
;
; Load into Z80 RAM at 0x2000.  The ROM at 0x0000 contains `jp 0x2000`.
; B starts at 0 after CPU reset.  Each loop sends B to the PS via
; the axis_byte_bridge (OUT), then increments B and repeats forever.
;
; Expected output (hex): 00 01 02 03 ... FF 00 01 ...
;
; Assemble:
;   python3 assemble_z80.py counter.s --org 0x2000 -o counter.bin

    .org 0

loop:
    ld a, b            ; B is our counter (starts at 0 after reset)
    out (0), a         ; Send byte to PS via axis_byte_bridge
    inc b              ; Increment counter
    jp loop            ; Loop forever
