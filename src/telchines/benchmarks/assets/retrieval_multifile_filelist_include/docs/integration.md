# UART Integration Notes

The UART top wrapper is compiled through `filelists/uart.f`. The filelist adds
the `include` directory for `uart_defs.svh`, then compiles `uart_pkg.sv`,
`uart_core.sv`, and `top.sv` in package-before-consumer order.

The default baud divisor comes from `UART_DEFAULT_BAUD_DIVISOR`. Retrieval
should keep the filelist, include macro, package, core, and integration note
together when debugging hierarchy or build failures.
