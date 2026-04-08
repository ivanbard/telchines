# UART Failure Context

The UART receiver must see a start bit transition before timing out.
The UART transmitter depends on `tx_fifo_level` being declared and routed correctly.
