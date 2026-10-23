/* Minimal AArch64 bare-metal stub for the QEMU virt machine.
 * Proves the host-side chain end to end: real QEMU TCG, real ARM64 code,
 * real PL011 UART output that UniPad's serial capture can read. */
    .section .text
    .global _start
_start:
    /* set up a stack in RAM */
    adrp  x0, stack_top
    add   x0, x0, :lo12:stack_top
    mov   sp, x0

    /* print the boot markers to the PL011 UART at 0x09000000 */
    movz  x1, #0x0900, lsl #16
    adrp  x2, msg
    add   x2, x2, :lo12:msg
1:  ldrb  w3, [x2], #1
    cbz   w3, 2f
    strb  w3, [x1]
    b     1b
2:  /* then spin, so the run must be stopped by QMP rather than exiting */
3:  wfe
    b     3b

    .section .rodata
msg:
    .ascii "UNIPAD-ARMSTUB: hello from AArch64\r\n"
    .ascii "UNIPAD-ARMSTUB: cpu0 up, uart alive\r\n"
    .ascii "UNIPAD-ARMSTUB: entering idle\r\n"
    .byte 0

    .section .bss
    .align 12
stack:
    .space 65536
stack_top:
