ENTRY(entry)
SECTIONS {
    sram_base = 0x01000000;
    sram_size = 0x001e0000;
    sram_end = sram_base + sram_size;
    fw_hdr_size = 0x2800;
    fw_hdr_used_size = 0x4a0;
    fw_hdr_pad_size = fw_hdr_size - fw_hdr_used_size;
    code_base = sram_end - fw_hdr_pad_size;
    stack_base = code_base - 0x2000;
    .code code_base : {
        *(.text.entry)
        *(.text*)
        *(.*data*)
        *(.bss*)
    }
}
ASSERT(code_base % 4 == 0, "code align");
ASSERT(. <= sram_end, "must fit in sram");
