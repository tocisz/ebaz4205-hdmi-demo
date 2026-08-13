; ==========================================================================
; ACIA Counter — outputs incrementing bytes through the ACIA
; ==========================================================================
; Uses the MC68B50 ACIA on I/O ports 0x80 (control/status) and 0x81 (data).
;
; Polls TDRE before each write — the byte is only sent when the ACIA TX
; buffer is empty (i.e. the previous byte was consumed by the bridge).
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

    ld b, 0             ; counter starts at 0

loop:
    ; Wait for TDRE (TX ready)
wait_tdre:
    in a, (80h)         ; read ACIA status register
    and 02h             ; mask TDRE bit
    jp z, wait_tdre     ; if TDRE=0, keep polling

    ; Send counter value
    ld a, b
    out (81h), a        ; write ACIA data register

    inc b               ; increment counter
    jp loop             ; repeat forever

end:
    jp end
