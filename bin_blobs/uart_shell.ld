ENTRY(entry)
SECTIONS {
    .code 0x58000000 : {
        __start_text = .;
        *(.text.entry)
        *(.text*)
        __stop_text = .;
        __start_data = .;
        *(.*data*)
        __stop_data = .;
        *(.bss*)
    }
}
