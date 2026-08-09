; echo.s — Echo program: read byte from PS, send it back
;
; Load into Z80 RAM at 0x2000.  The PS writes a byte to the FIFO,
; the Z80 reads it via IN, immediately writes it back via OUT,
; and the PS reads it from the FIFO.
;
; This tests bidirectional I/O through the axis_byte_bridge.
;
; Assemble:
;   python3 assemble_z80.py echo.s --org 0x2000 -o echo.bin

    .org 0

echo_loop:
    in a, (0)          ; Read byte from PS via axis_byte_bridge
    out (0), a         ; Echo it back to PS
    jp echo_loop       ; Loop forever
