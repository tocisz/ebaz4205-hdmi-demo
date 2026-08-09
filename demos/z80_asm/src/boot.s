; boot.s — ROM bootstrap for Z80 SoC
;
; This is loaded into ROM at 0x0000 (via ctrl_gp2).  It simply jumps
; to 0x2000 where the user program is loaded (into RAM via ctrl_gp1).
;
; The PS writes this to ROM once, then the user program (counter,
; echo, etc.) is loaded to RAM each run.
;
; Assemble:
;   python3 assemble_z80.py boot.s --org 0x0000 -o boot.bin
;   # produces exactly 3 bytes: C3 00 20

    .org 0

    jp 0x2000          ; Jump to RAM where user program is loaded
