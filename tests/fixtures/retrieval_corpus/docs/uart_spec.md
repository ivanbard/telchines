# UART Spec

The UART receiver waits for a start bit and may timeout if the line never drops.
The transmitter exposes `tx_fifo_level` to indicate buffered data.
