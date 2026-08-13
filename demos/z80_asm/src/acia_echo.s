; ==========================================================================
; ACIA Echo — reads a byte from the ACIA, writes it back
; ==========================================================================
; Uses the MC68B50 ACIA on I/O ports 0x80 (control/status) and 0x81 (data).
;
; Protocol:
;   1. Poll status register (port 0x80) for RDRF (bit 0)
;   2. When RDRF=1, read data register (port 0x81)
;   3. Poll status register for TDRE (bit 1)
;   4. When TDRE=1, write data register (port 0x81)
;
; Load into Z80 RAM at 0x2000 (linker -Ttext 0x2000 sets the base).
; The ROM at 0x0000 contains `jp 0x2000`.
; ==========================================================================

    ; Initialise ACIA:
    ;   CR[1:0] = 11 (divide by 1 clock mode)
    ;   CR[4:2] = 101 (8 bits, 1 stop bit)
    ;   CR[6:5] = 00 (RTS low, TX IRQ off)
    ;   CR[7]   = 0  (RX IRQ off)
    ; Value: 000 00 101 11 = 0x17
    ld a, 17h
    out (80h), a        ; write ACIA control register

echo_loop:
    ; Wait for RDRF (byte available from PS)
    in a, (80h)         ; read ACIA status register
    and 01h             ; mask RDRF bit
    jp z, echo_loop     ; if RDRF=0, keep polling

    ; Read the byte from ACIA data register
    in a, (81h)         ; read ACIA data register

    ; Wait for TDRE (TX ready to accept byte)
    push af             ; save the byte
wait_tdre:
    in a, (80h)         ; read ACIA status register
    and 02h             ; mask TDRE bit
    jp z, wait_tdre     ; if TDRE=0, keep polling

    ; Write the byte back
    pop af              ; restore the byte
    out (81h), a        ; write ACIA data register

    jp echo_loop        ; repeat forever

end:
    jp end
